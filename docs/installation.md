---
description: Install capa on Windows or Linux — Python 3.13, uv, sibling libs (alicatlib, watlowlib, sartoriuslib, nidaqlib), optional FLIR Atlas, duvc-ctl webcam wheel.
---

# Installation

**Audience:** new users on a fresh rig PC or dev machine.
**Scope:** Python, `uv`, sibling device libraries, optional extras (FLIR, simulator), the vendored Windows `duvc-ctl` wheel, install verification.

This is a working draft sufficient to get you to a running `capa gui`. Troubleshooting NI-DAQ driver install, FLIR Atlas SDK, and platform-specific gotchas are covered in [common issues](troubleshooting/common-issues.md).

## Supported platforms

- **Windows 10/11 (x64).** Primary target. The full feature set — USB camera control via `duvc-ctl`, FLIR Atlas SDK, NI-DAQ — works here.
- **Linux.** Aspirational; the runtime and most adapters work, but `duvc-ctl` and FLIR Atlas are Windows-only, and the project is not tested on Linux in CI today.
- **macOS.** Not targeted.

## Prerequisites

1. **Python 3.13.** Exactly 3.13.x — the `pyproject.toml` pins `requires-python = ">=3.13,<3.14"`. capa stays on 3.13 until `qasync` supports 3.14.
2. **[uv](https://github.com/astral-sh/uv).** Used for dependency resolution, environment management, and running every command. Bare `python -m pytest` style invocations are not supported in this repo — always go through `uv run`.
3. **Git.** Required for `setuptools-scm` to derive the version, and for cloning the sibling libraries.
4. **(Windows only) Visual C++ runtime.** Pulled in by most modern apps; capa needs it for `pyarrow` and `av`.
5. **(For real hardware)** NI-DAQmx drivers from NI's site, FLIR Atlas SDK if you're using IR cameras. Simulator runs need none of these.

## Repository layout

capa expects its sibling device libraries to live as editable path dependencies in the **parent directory**. A working layout:

```
~/git/
├── capa/              # this repo
├── alicatlib/         # required
├── watlowlib/         # required
├── sartoriuslib/      # required
├── nidaqlib/          # required
└── capa-flir/         # only if you need FLIR IR cameras (optional extra)
```

`pyproject.toml` declares `capa-flir` as an editable path source (`../capa-flir`); the four `*lib` packages resolve from their published versions but are commonly installed as editables for development. Clone all five (or six, with `capa-flir`) before running `uv sync`.

## Install

From the `capa/` directory:

```sh
# Baseline install — runtime, GUI, all four device libs, dev tooling
uv sync --group dev

# With FLIR IR camera support
uv sync --group dev --extra flir

# Docs tooling (zensical, mkdocstrings)
uv sync --group dev --group docs
```

`uv sync` creates `.venv/` in the project root and installs everything into it. The `dev` group pulls in pytest, pytest-qt, `anyio[trio]`, ruff, mypy, and pre-commit. CI installs the narrower `lint`, `type`, and `test` groups directly; see [pyproject.toml](https://github.com/GraysonBellamy/capa/blob/main/pyproject.toml)'s `[dependency-groups]` for the breakdown.

### The vendored `duvc-ctl` wheel (Windows only)

Webcam control on Windows uses [`duvc-ctl`](https://pypi.org/project/duvc-ctl/), which currently only publishes wheels for cp38–cp312 on PyPI. capa pins Python 3.13, so we built the cp313 wheel ourselves and committed it under [`vendor/`](https://github.com/GraysonBellamy/capa/tree/main/vendor):

```
vendor/duvc_ctl-2.1.0rc1-cp313-cp313-win_amd64.whl
```

`pyproject.toml`'s `[tool.uv.sources]` block points uv at the vendored file on Windows. Linux / macOS skip the dependency entirely (the `sys_platform == 'win32'` marker handles that). Once upstream publishes cp313 wheels we'll remove the vendored copy — see [`tools/build_duvc_wheel.ps1`](https://github.com/GraysonBellamy/capa/blob/main/tools/build_duvc_wheel.ps1) if you ever need to rebuild it.

## Verify the install

Three commands that touch no hardware:

```sh
# Version + runtime revision
uv run capa version

# Full command tree (validate, run, gui, finalize, devices, etc.)
uv run capa --help

# Pydantic-validate a stock simulator config
uv run capa validate configs/experiments/sim_freerun.yaml
```

All three should succeed without errors. If `capa version` reports `runtime 0.x.y-pNN` you're good — the `-pN` suffix is the runtime protocol revision and is unrelated to install correctness.

For a smoke test that exercises the simulator end-to-end, follow the [quick start](getting-started/quick-start.md) — it walks from launch to sealed bundle in about five minutes.

## Workspace layout

After install, two directories under the repo root matter:

- **`configs/`** — committed configs. Sub-folders for `experiments/`, `hardware/`, `methods/`, `calibrations/`. The simulator configs (`sim_*.yaml`) live alongside the real-rig ones; pick one with `uv run capa gui configs/experiments/sim_freerun.yaml`.
- **`runs/`** — bundle output. Each run produces `runs/<timestamp>/` containing `manifest.json`, `events.sqlite`, `scalars.parquet`, video sidecars. Writable; the rig PC's `runs/` is the canonical bundle archive.

Both directories are created on demand. Re-rooting them to a different drive is a Phase 1 documentation item (see [workspace layout](configuration/workspace-layout.md)).

## Next steps

- **[Quick start (simulator)](getting-started/quick-start.md)** — first sealed bundle in five minutes, no hardware.
- **[Your first real run](getting-started/first-real-run.md)** — same, but on a real rig.
- **[Dev setup](contributing/dev-setup.md)** — additional tooling for contributors (pre-commit, mypy targets, test layout).
