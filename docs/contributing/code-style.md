# Code style

**Audience:** contributors writing or modifying Python in `src/capa/` or `tests/`.
**Scope:** the ruff configuration and what each per-file ignore actually means, naming exceptions for Qt overrides and scientific unit suffixes, the docstring style mkdocstrings expects, and the project's stance on comments.

The whole style configuration lives in [pyproject.toml](../../pyproject.toml#L106-L151) (`[tool.ruff]` and friends). This page exists to explain *why* each rule is enabled or ignored, so a contributor can tell whether a new ignore is a legitimate domain exception or a hack that needs a real fix.

---

## Formatter

```powershell
uv run ruff format          # write
uv run ruff format --check  # check, don't write
```

- Line length: **100**.
- Quote style: **double**.
- Target version: **Python 3.13**.

ruff is the only formatter — there is no `black`, no `isort` separate step. ruff's `I` lint family does the import sort; the formatter is invoked separately.

---

## Linter

```powershell
uv run ruff check          # report
uv run ruff check --fix    # autofix what's safe
```

Selected rule families:

| Family | What it catches |
|---|---|
| `E`, `F`, `W` | pyflakes + pycodestyle baseline |
| `I` | isort import ordering |
| `B` | bugbear (mutable default args, etc.) |
| `UP` | pyupgrade — modern Python syntax |
| `SIM` | simplify (`if x: return True; else: return False` → `return x`) |
| `RUF` | ruff's own rules |
| `N` | pep8-naming |
| `PLC`, `PLE`, `PLW` | pylint convention / error / warning subsets |

Repo-wide ignores (these are intentional, not laziness):

| Rule | Why |
|---|---|
| `E501` (line too long) | The formatter owns line length. The linter shouldn't double-report. |
| `RUF001` / `RUF002` / `RUF003` (ambiguous unicode) | CAPA's scientific text uses `×`, `±`, `°`, `µ` in docstrings, comments, and string literals. These are the correct characters, not typos. |

---

## Per-file ignores — and the domain reason for each

Per-file ignores aren't free passes; each is documented in `pyproject.toml`. If you touch one of these files, the listed rule was relaxed for a specific reason — don't accidentally re-enable it.

### `tests/**/*.py`

```toml
"tests/**/*.py" = [
    "PLC0415",  # inline imports are common in test setup
    "B017",     # pytest.raises(Exception) is fine for "this should fail somehow"
    "F841",     # unused-but-assigned in tests is often deliberate
    "PLR2004",  # magic numbers are fine in tests
]
```

Tests are not production code. Inline imports inside a parametrised case, broad `pytest.raises(Exception)` when the precise exception type is part of what's being pinned, deliberately-unused assignments to keep a reference alive, and magic-number assertions (`assert frame_count == 47`) are all idiomatic in tests.

### `src/capa/experiment/profiles/cone_calorimeter.py` — `N815` waived

```toml
"src/capa/experiment/profiles/cone_calorimeter.py" = ["N815"]
```

Scientific unit suffixes look like `target_external_flux_kW_m2`. PEP-8 (rule `N815`) would call the `kW_m2` mixed-case in a field name a violation. The wider scientific convention says SI unit symbols keep their case (`kW`, not `kw`); the suffix is intentional and matches how this field appears in ISO 5660 / ASTM E1354 paperwork. Don't rename them to `target_external_flux_kw_m2` — that would diverge from the standard.

The same convention applies anywhere you encode a unit in an identifier: keep the SI symbol case (`mA`, `Pa`, `s`, `°C`, `kW_m2`). Only the [CAPA profile](../configuration/capa-profile.md) currently does this enough to need a file-level ignore; if you add a second file with the same pattern, add the same ignore there.

### Qt override files — `N802` waived

```toml
"src/capa/ui/docks/log.py" = ["N802"]
"src/capa/ui/tabs/setup_problems.py" = ["N802", "B008"]
"src/capa/ui/tabs/setup_sections/*.py" = ["N802", "B008"]
"src/capa/ui/tabs/setup_wizard.py" = ["N802"]
"src/capa/ui/welcome.py" = ["N802"]
```

PySide6's virtual methods are camelCase by Qt convention (`mousePressEvent`, `paintEvent`, `sizeHint`). Overriding them requires preserving the camelCase exactly — `mouse_press_event` would not override; it would create a new method that Qt never calls. Rule `N802` (function name should be lowercase) is suppressed in the files that override Qt virtuals.

`B008` (function call in default argument) is also waived on the setup-tab modules because Qt models use `QModelIndex()` as a sentinel default, and replacing it with `None` plus an `or` branch would be noisier than the lint it avoids.

### `scripts/*.py` — `PLC0415` waived

Top-level scripts often need conditional imports near the call site (e.g. importing a heavy dep only when a specific command path runs). The inline-imports lint is waived for `scripts/` for the same reason it's waived in `tests/`.

---

## Naming

PEP-8 defaults, plus these exceptions:

1. **Qt overrides keep camelCase.** Required for the override to bind to the Qt virtual.
2. **Scientific identifiers keep SI unit case.** `kW_m2`, `mA`, `Pa`, `°C` in field names and variable names.
3. **Private helpers prefix with `_`** as usual; double-underscore is reserved for class-namespace mangling and used very rarely.

For new modules, prefer a long descriptive name (`saturation.py`) over a short cryptic one (`sat.py`). The project's `src/capa/runtime/` directory is the reference for naming density.

---

## Docstrings — Google style

[zensical.toml](../../zensical.toml#L191-L202) configures `mkdocstrings-python` with:

```toml
[project.plugins.mkdocstrings.handlers.python.options]
docstring_style = "google"
```

Public classes and functions in `src/capa/` should have Google-style docstrings:

```python
def run_headless(
    config: ExperimentConfig,
    *,
    runs_root: Path,
) -> RunResult:
    """Execute one experiment headlessly and return its bundle outcome.

    Args:
        config: Fully validated experiment configuration.
        runs_root: Directory the new bundle is created under.

    Returns:
        A RunResult with bundle_path, run_status, bundle_status,
        integrity_status, and exit_reason populated.

    Raises:
        ConfigError: If `config` references a calibration set that does
            not exist in the runtime catalog.
    """
```

The API reference pages under `docs/api/*.md` are pure `::: capa.foo` directives — the content is rendered from these docstrings. Improving a public docstring directly improves the rendered docs site. See Phase 13 in [`DOCS_AUTHORING_PLAN.md`](https://github.com/GraysonBellamy/capa/blob/main/DOCS_AUTHORING_PLAN.md) for the docstring-coverage sweep.

Private helpers (leading underscore) don't need a docstring unless their behavior is non-obvious; one-line module docstrings at the top of a file are encouraged.

---

## Comments — sparing

Default to writing **no** comments. Add one when the *why* is non-obvious:

- A hidden constraint or invariant (e.g. why a write must be serialized through one thread).
- A workaround for a specific upstream bug — link to the issue.
- Behaviour that would surprise a reader who didn't write the code.

Do **not** write comments that:

- Restate what the code does (`# increment counter`).
- Reference the current task or fix (`# fix for bug 123`) — that belongs in the commit message.
- Reference callers (`# used by the run controller`) — caller information rots; grep is reliable.

This is project policy, not a personal preference; it appears in the repo-level guidance and is consistently enforced. If a function needs a paragraph of explanation, that paragraph belongs in the docstring, not in a block comment.

---

## Where to go next

- [Typing and mypy](typing-and-mypy.md) — the strict mode baseline and the four override categories.
- [Commits and PRs](commit-and-pr.md) — subject style and the local CI contract.
