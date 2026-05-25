# Environment variables

**Audience:** operators, CI authors, contributors driving capa from a script or a doc-tooling harness.
**Scope:** every environment variable capa reads, grouped by who sets it and why.

The runtime list is grepped from `src/capa/`; contributor-only test variables come from `tests/hardware/`. If a variable is named with the `CAPA_` prefix but does not appear here or on [Hardware tests](../contributing/hardware-tests.md), capa does not read it — silently ignoring an unfamiliar env var is intentional, and setting a misspelled name has no effect.

For configuration that lives *in files* (experiment YAML, hardware TOML, method TOML) see [Configuration overview](../configuration/overview.md). For runtime flags that are CLI options rather than env vars, see the per-command [CLI pages](../cli/overview.md).

---

## Path overrides

| Variable | Effect | Default | Source |
|---|---|---|---|
| `CAPA_RUNS_ROOT` | Directory that holds every sealed bundle and the `runs.sqlite` catalog. Honoured by every CLI command that needs a bundle parent (`run`, `gui`, `finalize`, `catalog list`, `catalog verify`, `catalog rebuild`). An explicit `--runs-root` flag overrides this. | `./runs` (resolved against the CWD) | [`cli/_helpers.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/cli/_helpers.py) |
| `XDG_CONFIG_HOME` | Used as the parent of the user-global `plugins.lock` fallback (`$XDG_CONFIG_HOME/capa/plugins.lock`). Standard XDG variable; not capa-specific. | OS-default; on Linux/macOS falls back to `$HOME/.config` | [`cli/_helpers.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/cli/_helpers.py) |
| `HOME` | Used to derive `$HOME/.config/capa/plugins.lock` and `~/.capa/logs/` (the pre-run log directory). Standard POSIX variable; not capa-specific. On Windows the same lookup uses `pathlib.Path.home()` via the `USERPROFILE`/`HOMEPATH` machinery. | OS-default | [`cli/_helpers.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/cli/_helpers.py), [`core/logging.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/logging.py) |

Resolution order for `--runs-root`-style paths is **explicit flag > env var > built-in default**. The same rule applies everywhere a path can come from more than one source.

---

## Plugin trust

| Variable | Accepted values | Effect | Default | Source |
|---|---|---|---|---|
| `CAPA_PLUGIN_MODE` | `dev` / `production` (case-insensitive; anything else is treated as `dev`) | `dev` loads every procedure plugin that passes the contract check, ignoring `plugins.lock`. `production` refuses any procedure plugin missing from `plugins.lock` or whose distribution hash drifts. `capa run` / `capa gui` read this variable; `capa plugins list --plugin-mode` can override it for inspection. | `dev` | [`core/plugins_runtime.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/plugins_runtime.py) |

A `production` run that finds no usable `plugins.lock` fails fast — pass `--plugins-lock` explicitly or place one at `./plugins.lock` or `$XDG_CONFIG_HOME/capa/plugins.lock`. See [Plugin lockfile](../extending/plugin-lockfile.md) for the lockfile schema.

---

## Contributor testing

| Variable | Effect | Default | Source |
|---|---|---|---|
| `CAPA_HARDWARE_TESTS` | Set to `1` to opt into the `hardware`-marked pytest tier. Default behaviour is to skip every test marked `@pytest.mark.hardware`, which is every test that opens a real device. Per-vendor connection parameters are read by the individual modules under `tests/hardware/`. | unset (skip) | [`pyproject.toml`](https://github.com/GraysonBellamy/capa/blob/main/pyproject.toml), [`tests/hardware/`](https://github.com/GraysonBellamy/capa/tree/main/tests/hardware) |

See [Hardware tests](../contributing/hardware-tests.md) for the full opt-in workflow.

---

## FLIR IR camera SDK

| Variable | Effect | Default | Source |
|---|---|---|---|
| `CAPA_FLIR_ATLAS_ROOT` | Filesystem root of the FLIR Atlas C SDK install (Atlas 2.19.0). The capa-flir plugin's `_loader.py` `dlopen`s `libatlas_c_sdk.so` (Linux) or `atlas_c_sdk.dll` (Windows) from `$CAPA_FLIR_ATLAS_ROOT`. Without this variable set, the plugin's adapters refuse to open and `capa hardware discover --adapter camera_ir` returns no devices. | unset | capa-flir plugin (see [capa-plan.md → FLIR Atlas SDK install](../architecture/capa-plan.md)) |

On Linux you also need `$CAPA_FLIR_ATLAS_ROOT/lib` prepended to `LD_LIBRARY_PATH`. On Windows you need `$CAPA_FLIR_ATLAS_ROOT/bin` on `PATH` so dependent DLLs resolve. See [FLIR IR cameras](../devices/cameras-flir.md) for the per-platform install steps.

---

## Diagnostic logging

| Variable | Effect | Default | Source |
|---|---|---|---|
| `CAPA_WEBCAM_FRAME_DIAG` | Set to `1` to log the first 150 webcam input frames at `INFO` with their format name, width, height, and `pts`. Useful when chasing format-negotiation regressions; verbose, so leave unset in normal operation. | unset | [`devices/camera/webcam/adapter.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/camera/webcam/adapter.py) |

For general log-reading guidance see [Reading event logs](../troubleshooting/reading-event-logs.md).

---

## Doc-tooling and screenshot harness

!!! warning
    **Do not set these on a real rig.** They exist so the screenshot-harness and the `make-docs` tooling can hold transient runtime states (CONNECTING, DRAINING) long enough to capture a screenshot, and to force sim adapters into failure modes that don't occur naturally. None of them produce useful behaviour during real experiments.

| Variable | Accepted values | Effect | Default | Source |
|---|---|---|---|---|
| `CAPA_SCREENSHOT_PROBE` | `1` (or any non-empty string) | Enables a read-only HTTP probe on `127.0.0.1:9876` when the GUI launches. Exposes `GET /widgets`, `GET /actions`, `GET /property`. The probe runs in a daemon thread; cost is zero when the variable is unset (module not imported). | unset | [`ui/app.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/app.py), [`ui/_screenshot_probe.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/_screenshot_probe.py) |
| `CAPA_SCREENSHOT_PROBE_INTERACTIVE` | `1` (or any non-empty string) | **Requires `CAPA_SCREENSHOT_PROBE` set as well.** Promotes the probe to write-capable: `POST /screenshot`, `POST /click`, `POST /type`, `POST /key`, `POST /trigger`, `POST /set_tab`, `POST /set_property`, `POST /dismiss`, `POST /wait_for`, `POST /resize`, `POST /hover`. These endpoints can fire real UI actions — never enable this on a rig connected to live hardware. | unset | [`ui/_screenshot_probe.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/_screenshot_probe.py) |
| `CAPA_DRAINING_DELAY_S` | float seconds | Holds the conductor in the `DRAINING` state for the configured number of seconds before disarming workers. Lets doc tooling screenshot the status badge in DRAINING; under a clean sim shutdown the state lasts milliseconds. | `0` (no delay) | [`runtime/conductor.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/conductor.py) |
| `CAPA_SIM_OPEN_DELAY_MS` | int milliseconds | Stretches the simulated Watlow adapter's `open()` so the connection-strip badge sits in `CONNECTING` long enough to screenshot. | `0` (no delay) | [`devices/sim/watlow_sim.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/watlow_sim.py) |
| `CAPA_SIM_OPEN_FAIL` | any non-empty string | Forces the simulated Watlow adapter's `open()` to raise `AdapterError`. Drives the connection strip into `FAILED` so the error-state UI can be captured. | unset | [`devices/sim/watlow_sim.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/watlow_sim.py) |

---

## Not currently env-configurable

For completeness, things that look like they should be env vars but aren't:

- **Configs root.** There is no `CAPA_CONFIGS` lookup today. Hardware / experiment / method files are passed by explicit path argument to every CLI command; the GUI reads its last-opened paths from its own settings store. Setting `CAPA_CONFIGS` has no effect.
- **Log level / format.** Log behaviour is controlled by [`configure_logging()`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/logging.py) arguments, not env vars. `CAPA_LOG_LEVEL` and `CAPA_LOG_JSON` are not read anywhere. Pre-run logs go to `~/.capa/logs/capa-YYYYMMDD.log` (human-friendly console renderer); in-bundle logs go to `run.log` (JSON lines). To change levels for a debugging session, edit the call site in [`cli/run.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/cli/run.py) or run the engine from a Python harness that passes the levels you want.

If you want one of these, file an issue — these are deliberate gaps, not oversights, but the absence is worth surfacing.

---

## See also

- [Exit codes](exit-codes.md) — how outcomes map to process exit codes.
- [File formats](file-formats.md) — every on-disk format capa reads or writes.
- [Configuration overview](../configuration/overview.md) — how the file-based configuration layers compose.
- [Hardware tests](../contributing/hardware-tests.md) — full `CAPA_HARDWARE_TESTS=1` workflow.
- [FLIR IR cameras](../devices/cameras-flir.md) — Atlas SDK install and `CAPA_FLIR_ATLAS_ROOT` walkthrough.
