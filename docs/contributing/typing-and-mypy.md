---
description: capa's `mypy --strict` baseline — the pydantic plugin, strictness flags, the four categories of override, and why pytest (not mypy) is the in-loop CI gate.
---

# Typing and mypy

**Audience:** contributors writing typed Python in `src/capa/` or working under `--strict`.
**Scope:** the strict-mode baseline, the pydantic plugin, the four categories of override that exist and when to add a new one in each, and the workflow rule that pytest — not mypy — is the in-loop gate.

CAPA is `mypy --strict` clean. The whole policy lives in [pyproject.toml](../../pyproject.toml#L153-L231) under `[tool.mypy]`. New code goes in fully typed; broken types break CI exactly as broken tests do (locally, since there is no Python CI workflow yet — see [Commits and PRs](commit-and-pr.md)).

---

## Strict mode is the baseline

```toml
[tool.mypy]
python_version = "3.13"
strict = true
mypy_path = "src"
packages = ["capa"]
plugins = ["pydantic.mypy"]
warn_unused_ignores = true
warn_redundant_casts = true
warn_unreachable = true
namespace_packages = true
explicit_package_bases = true
```

`strict = true` enables every individual strictness flag — most importantly:

- `disallow_untyped_defs` (every function signature must be typed)
- `disallow_any_generics` (no `list` — must be `list[str]` or `list[Any]` explicitly)
- `no_implicit_optional` (no implicit `None`-default-makes-it-Optional)
- `disallow_untyped_calls` (calling an untyped function is an error)

On top of the strict baseline:

- `warn_unused_ignores` — a `# type: ignore` that no longer suppresses anything is an error. This means dead ignores get cleaned up rather than accumulating.
- `warn_redundant_casts` and `warn_unreachable` — same principle, for casts and dead branches.

Run with:

```powershell
uv run mypy
```

(The packages-and-mypy_path config tells mypy to scan `src/capa/` automatically; you don't need to pass it a path.)

The mypy cache lives at `.mypy_cache/regular/`. The `regular` subdir suffix is deliberate — if a future configuration adds a second mypy invocation (e.g. with a different override set), it gets its own cache and the two don't poison each other.

---

## Pydantic plugin

```toml
plugins = ["pydantic.mypy"]
```

CAPA's config layer is pydantic v2. The plugin teaches mypy how `BaseModel.__init__` is generated from field declarations, how validators alter field types, and how `model_validate(...)` narrows. Without it, every `ExperimentConfig(**dict)` call would be untyped.

Pydantic-specific patterns that come up:

- **Field validators that change the type** (e.g. `str → Path`) may still produce a mypy complaint inside the validator body. A narrowly scoped `# type: ignore[misc]` is acceptable when the validator is doing what it's supposed to.
- **`model_config = ConfigDict(...)`** is the right way to set model-level config in v2; the plugin recognizes it.
- **Don't reach for `Any` to dodge a validator type.** A `# type: ignore[misc]` with a short reason in a comment is preferable to silently widening to `Any`.

---

## The four override categories

CAPA accumulates mypy overrides only when there's a structural reason. Every override in `pyproject.toml` falls into one of four buckets. Knowing the bucket a new exception lives in is the right way to decide whether it's a legitimate addition.

### 1. Upstream package has no `py.typed` marker

```toml
[[tool.mypy.overrides]]
module = ["pint", "pint.*"]
ignore_missing_imports = true
```

`pint`, `ruamel`, `pyarrow`, `pyqtgraph`, `qasync`, `psutil`, and `nidaqmx` all ship without `py.typed`. Mypy refuses to read their actual type hints (if any exist) and would otherwise raise `import-untyped` everywhere. `ignore_missing_imports = true` silences that — at the cost that calls into those packages get `Any` back, which strict mode then catches via `disallow_untyped_calls` (handled in category 3 below for the cases where it bites).

**Adding a new package in this category is fine.** Add a new `[[tool.mypy.overrides]]` block with `module = ["newpkg", "newpkg.*"]` and `ignore_missing_imports = true`. Document the upstream gap inline (one line: "no py.typed").

### 2. Platform-conditional native import

```toml
[[tool.mypy.overrides]]
module = ["duvc_ctl", "duvc_ctl.*"]
ignore_missing_imports = true
```

`duvc-ctl` ships `py.typed` but only as a Windows wheel. On Linux/macOS dev machines mypy can't import the module. The runtime adapter ([src/capa/devices/camera/](../../src/capa/devices/camera/)) handles real absence with a soft-import; the mypy override just silences the missing-import noise on non-Windows runs so strict mode stays clean.

**When to add a new entry here:** a dependency is platform-conditional in `pyproject.toml` (via `; sys_platform == 'win32'` or similar) and the dependency lacks cross-platform stubs.

### 3. Untyped pyarrow helpers at use sites

```toml
[[tool.mypy.overrides]]
module = [
    "capa.storage._ipc",
    "capa.storage.channel_samples_sink",
    "capa.storage.device_records_sink",
    "capa.storage.finalize",
    "capa.storage.video_sink",
]
disallow_untyped_calls = false
```

pyarrow's `ParquetWriter.write_table`, `pyarrow.parquet.read_table`, `pyarrow.ipc.open_stream`, etc., are untyped. Downstream code that calls them shouldn't be punished under strict's `disallow_untyped_calls`. The right fix would be type stubs upstream; the workaround is to disable that one check on the specific modules that touch the helpers.

**Keep this list narrow.** A new storage module that wraps pyarrow can be added here. An unrelated runtime module that suddenly fails strict because it touched `pyarrow` should probably not be added here — find a way to push the pyarrow boundary into one of the listed modules instead.

### 4. Tests that read parquet for assertions

```toml
[[tool.mypy.overrides]]
module = [
    "tests.hardware.test_alicat_smoke",
    "tests.hardware.test_nidaq_smoke",
    ...
]
disallow_untyped_calls = false
```

Same root cause as category 3: tests that call `pq.read_table(...)` would otherwise trip `disallow_untyped_calls`. Listing individual test modules (rather than `tests/**`) keeps the relaxation as narrow as possible — unrelated test regressions in untyped-call land still surface.

**When you add a new test that reads parquet bundles for assertions, add its module here too** — not a broader pattern.

---

## When to add an override vs. fix the type

Prefer fixing. The decision tree:

1. **Is the missing type information in our own code?** Fix it. Add the annotation; remove the `Any`.
2. **Is it in an upstream package that has stubs available** (e.g. `types-foo` on PyPI)? Add the stubs to the `dev` extra rather than overriding.
3. **Is it in an upstream package with no stubs and no `py.typed`?** Override under category 1.
4. **Is it platform-conditional?** Override under category 2.
5. **Is it pyarrow's untyped helpers specifically?** Override under category 3 or 4, narrowly per module.

If a new override doesn't cleanly fit any of these, that's a smell — flag it in the PR rather than just adding the override.

---

## Workflow: mypy is not the in-loop gate

CAPA's project convention: **pytest is the gate; mypy gets cleaned up in a dedicated pass at the end of a change.**

Reasoning: chasing strict-mode complaints mid-feature work pulls focus from behavior into types and tends to produce premature `# type: ignore` markers that survive the feature. The order is:

1. Make the behavior change.
2. Get `uv run pytest` clean.
3. *Then* run `uv run mypy` and fix the type fallout in one focused pass.

This is the order assumed by the project. It does not mean "mypy doesn't have to pass" — mypy must be clean before push — it means the cleanup pass is its own step.

---

## Where to go next

- [Code style](code-style.md) — ruff config, per-file ignores, naming.
- [Running tests](running-tests.md) — the pytest invocation that gates everything else.
- [Commits and PRs](commit-and-pr.md) — what to run before pushing.
