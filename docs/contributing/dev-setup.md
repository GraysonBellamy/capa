# Dev setup

**Audience:** first-time contributors getting capa running from source.
**Scope:** clone layout, dependency install with [uv](https://github.com/astral-sh/uv), running the GUI and CLI from source, the lint/type/test loop. Editor setup is intentionally not prescribed.

CAPA is a Python 3.13 project that depends on four sibling device libraries (`alicatlib`, `watlowlib`, `sartoriuslib`, `nidaqlib`) and one optional sibling (`capa-flir`). The dev loop is `uv sync` → edit → `uv run pytest` → `uv run ruff check` → `uv run mypy`. There is no `pip install -e .` path; everything goes through `uv`.

---

## Prerequisites

- **Python 3.13.** Pinned in [pyproject.toml](../../pyproject.toml#L10) via `requires-python = ">=3.13,<3.14"`. uv will install a matching interpreter if you don't have one — you don't need a separate pyenv / Conda install.
- **[uv](https://github.com/astral-sh/uv).** Single binary; install per the upstream instructions. Every command in capa's docs assumes `uv run <cmd>` rather than activating a virtualenv — bare `python -m pytest` is not the supported invocation.
- **git.**
- **Windows is the primary supported platform** (the vendored `duvc-ctl` wheel under [vendor/](../../vendor/) is Windows-only; UVC webcam control falls back to no-op on Linux/macOS). The rest of capa runs fine on Linux and macOS — you just can't drive a real Windows-attached camera there.

---

## Sibling-library layout (the non-obvious gotcha)

CAPA does not depend on PyPI builds of its sibling libraries. [pyproject.toml](../../pyproject.toml#L98-L104) declares:

```toml
[tool.uv.sources]
capa-flir = { path = "../capa-flir", editable = true }
duvc-ctl = { path = "vendor/duvc_ctl-2.1.0rc1-cp313-cp313-win_amd64.whl" }
```

The `../capa-flir` entry is a relative path. **`uv sync` will fail if `capa-flir` is not a sibling directory of `capa`** when you build with the `flir` extra. The other four device libraries (`alicatlib`, `watlowlib`, `sartoriuslib`, `nidaqlib`) are pulled from PyPI by default but most contributors check them out as siblings too, so edits to a device library are immediately picked up by an editable install (`uv pip install -e ../alicatlib`).

Recommended layout:

```text
parent-dir/
├── capa/             ← this repo
├── capa-flir/        ← optional; required only with --extra flir
├── alicatlib/        ← optional; sibling editable install for active development
├── watlowlib/        ← same
├── sartoriuslib/     ← same
└── nidaqlib/         ← same
```

Clone with:

```powershell
cd parent-dir
git clone https://github.com/GraysonBellamy/capa
git clone https://github.com/GraysonBellamy/capa-flir        # if you need IR cameras
git clone https://github.com/GraysonBellamy/alicatlib        # optional but common
git clone https://github.com/GraysonBellamy/watlowlib
git clone https://github.com/GraysonBellamy/sartoriuslib
git clone https://github.com/GraysonBellamy/nidaqlib
```

If you skip the optional clones, capa still installs and runs against the PyPI builds of the device libraries — you just can't edit those libraries and see the changes immediately.

---

## Install

From the `capa/` directory:

| Goal | Command |
|---|---|
| Standard dev install (ruff, mypy, pytest, pytest-qt, `anyio[trio]`, pre-commit) | `uv sync --group dev` |
| Add FLIR IR camera support (requires sibling `capa-flir/`) | `uv sync --group dev --extra flir` |
| Add docs tooling (zensical + mkdocstrings) | `uv sync --group dev --group docs` |
| Just the lint env (matches CI lint job) | `uv sync --group lint` |
| Just the type env (matches CI typecheck job) | `uv sync --group type --group test` |
| Just the test env (matches CI test job) | `uv sync --group test` |
| Everything at once | `uv sync --all-extras --all-groups` |

The dev-time groups (`lint`, `type`, `test`, `docs`, `dev`) are PEP 735 entries declared in [`[dependency-groups]`](../../pyproject.toml). The `dev` group is a union of `lint` + `type` + `test` plus `pre-commit`; CI installs the narrower groups directly so each job's env is minimal and identical to the local equivalent. The only `[project.optional-dependencies]` entry is `flir`, which is a real install-time opt-in for downstream users (it pulls in the sibling `capa-flir` package for IR cameras).

`uv sync` creates `.venv/` in the project root; capa's runtime conventions (output directory under `runs/`, plugin lockfile, etc.) are unaffected.

---

## Run capa from source

The `capa` console script is registered as the entry point (`capa = "capa.cli:main"`). All sub-commands go through it:

```powershell
uv run capa --help                              # top-level help
uv run capa gui                                 # launch the GUI, empty
uv run capa gui configs/experiments/sim_freerun.yaml   # GUI with a config pre-loaded
uv run capa run configs/experiments/sim_freerun.yaml   # headless run
uv run capa validate configs/experiments/sim_freerun.yaml
uv run capa devices discover
```

The `sim_freerun.yaml` experiment uses simulator adapters only — no hardware required, no rig PC. It's the right starting point for both manual GUI smoke-testing and as the fixture for many integration tests.

For the GUI's screenshot-driving HTTP probe (used to capture doc images and automate UI walks), see [UI probe](screenshot-probe.md).

---

## The dev loop

Four commands cover everything most contributors run before pushing:

```powershell
uv run ruff format         # format
uv run ruff check          # lint
uv run mypy                # type-check
uv run pytest              # full suite excluding hardware-marked tests
```

Run them in that order. `ruff format` and `ruff check` are cheap and obvious; `mypy` is slower; `pytest` is the longest. The full suite (no hardware) completes in well under a minute on a developer laptop.

If touching docs, also:

```powershell
uv run zensical build --clean
```

This is what CI ([.github/workflows/docs.yml](../../.github/workflows/docs.yml)) runs; if it fails on your branch it will fail on `main` too.

---

## Pre-commit hooks

The repo ships a [`.pre-commit-config.yaml`](../../.pre-commit-config.yaml) that mirrors what CI runs. Install once:

```powershell
uv run pre-commit install                  # commit-stage hooks
uv run pre-commit install --hook-type pre-push   # mypy at pre-push
```

What runs at commit time: `ruff check --fix`, `ruff format`, `uv lock` (drift check), `codespell`, and a handful of `pre-commit-hooks` basics (trailing whitespace, EOF newline, large-file guard sized for the vendored `duvc-ctl` wheel, YAML/TOML/JSON parse).

What runs at push time: `mypy`. It's deferred to pre-push deliberately — the project preference is "don't loop on mypy mid-task; pytest is the gate" (see [Typing and mypy](typing-and-mypy.md)), so per-commit iteration stays fast and mypy gates the push instead.

Hooks are advisory locally; CI ([`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)) re-runs the same checks on every PR and is the authoritative gate.

---

## Where to go next

- [Running tests](running-tests.md) — markers, common invocations, async patterns.
- [Hardware tests](hardware-tests.md) — the `CAPA_HARDWARE_TESTS=1` opt-in tier.
- [Code style](code-style.md) — ruff config, the per-file ignores and why.
- [Typing and mypy](typing-and-mypy.md) — strict baseline, override categories.
- [Commits and PRs](commit-and-pr.md) — subject style, when to bundle.
