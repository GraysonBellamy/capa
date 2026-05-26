---
description: capa Typer console script tour — sub-apps: run, gui, validate, finalize, catalog, plugins, devices, hardware, config, method, profile.
---

# CLI overview

**Audience:** anyone who runs capa from a terminal.
**Scope:** the `capa` console script — every top-level command, every sub-app, what they each do, and where to look in the source.

Installing capa via `uv pip install -e .` (or however your environment provisions it) puts a `capa` entry on `PATH`. It is a Typer dispatcher: `capa <thing> --help` lists what each thing accepts.

```
$ capa --help
Usage: capa [OPTIONS] COMMAND [ARGS]...

  Control and DAQ for cone-calorimeter-class lab instruments.

Commands:
  validate   Validate an experiment config without running it.
  run        Run an experiment.
  gui        Launch the GUI.
  finalize   Finalize an open or crashed bundle.
  version    Print the capa version + runtime revision.
  catalog    Manage the run catalog.
  plugins    Inspect plugin trust state.
  devices    Discover hardware visible on the local system.
  config     Validate and inspect experiment configs.
  hardware   Author and probe hardware profiles.
  method     Method-file utilities.
  profile    Domain-profile utilities.
```

---

## Top-level commands

These take their arguments directly — no sub-app indirection.

| Command | What it does | Source |
|---|---|---|
| [`capa run`](capa-run.md) | Run an experiment (default headless; `--gui` is a synonym for `capa gui`). | [run.py](../../src/capa/cli/run.py) |
| [`capa gui`](capa-gui.md) | Launch the PySide6 GUI. Optionally pre-load a config. | [run.py](../../src/capa/cli/run.py) |
| [`capa validate`](capa-validate.md) | Pydantic-validate an experiment config. `--strict` opens each adapter. | [validate.py](../../src/capa/cli/validate.py) |
| [`capa finalize`](capa-finalize.md) | Re-seal a bundle whose `capa run` did not exit cleanly. | [run.py](../../src/capa/cli/run.py) |
| `capa version` | Print the capa version and runtime revision. | [main.py](../../src/capa/cli/main.py) |

---

## Sub-apps

Each sub-app groups related commands. Calling the sub-app with no arguments prints its own help.

| Sub-app | Purpose | Commands today | Source |
|---|---|---|---|
| [`capa catalog`](capa-catalog.md) | Inspect the on-disk run catalog (`runs.sqlite`). | `list`, `verify`, `rebuild` | [catalog.py](../../src/capa/cli/catalog.py) |
| [`capa plugins`](capa-plugins.md) | Inspect and grant trust to procedure plugins. | `list`, `trust` | [plugins.py](../../src/capa/cli/plugins.py) |
| [`capa devices`](capa-devices.md) | Discover serial / DAQmx adapters (no cameras). | `discover` | [devices.py](../../src/capa/cli/devices.py) |
| [`capa hardware`](capa-hardware.md) | Author, validate, probe hardware TOMLs (cameras included). | `validate`, `check`, `discover`, `new` | [hardware.py](../../src/capa/cli/hardware.py) |
| [`capa config`](capa-config.md) | Layered validation of an experiment config. | `validate` | [config_cmd.py](../../src/capa/cli/config_cmd.py) |
| [`capa method`](capa-method.md) | Lint standalone `.method.toml` / `.method.yaml` files. | `validate` | [method_profile.py](../../src/capa/cli/method_profile.py) |
| [`capa profile`](capa-profile.md) | Validate a config's domain-profile metadata block. | `validate` | [method_profile.py](../../src/capa/cli/method_profile.py) |

---

## Headless vs GUI

Both real launchers — `capa run` and `capa gui` — accept the same `--runs-root` and `--plugins-lock`. The difference is what owns the event loop:

- **`capa run`** (default `--headless`) drives the conductor stack from anyio, prints a five-line result block, and exits. No Qt is imported. Ideal for CI, scripted sweeps, and remote SSH sessions.
- **`capa gui`** (or `capa run --gui`) starts the qasync bootstrap, loads PySide6, and hands control to the operator. The same conductor runs underneath; the GUI is the visualization and command surface on top of it.

See [headless runs](headless-runs.md) for the full headless lifecycle, signal handling, and exit-code table.

---

## Validation triangle — which `validate` do I want?

Three different commands exist for "tell me if this config is OK." Pick by what file you have in hand:

```
You have…                       Use…
─────────────────────────────────────────────────────────────
a full experiment YAML          capa config validate path.yaml
                                (or: capa validate path.yaml — same checks,
                                  shorter output; adds --strict for live probes)

a hardware-only TOML            capa hardware validate path.toml
                                (or: capa hardware check path.toml for live)

a standalone method file        capa method validate path.toml

a config and want to check
just the profile metadata       capa profile validate path.yaml
```

The per-command pages have decision trees that drill into the differences.

---

## Cross-cutting flags

A few flags repeat across commands; resolution order is consistent.

| Flag | Used by | Resolution order |
|---|---|---|
| `--runs-root` | `run`, `gui`, `finalize`, `catalog *` | flag > `$CAPA_RUNS_ROOT` > `./runs` |
| `--plugins-lock` | `run`, `gui`, `plugins *` | flag > `./plugins.lock` > `$XDG_CONFIG_HOME/capa/plugins.lock` |
| `--json` | `devices discover`, `hardware discover`, `catalog list` | flag → table by default, JSON when set |

---

## Where the bodies live

The dispatcher in [src/capa/cli/main.py](../../src/capa/cli/main.py) is intentionally small — it wires every sub-Typer app together and nothing else. To find the implementation of `capa <thing>`, look in the corresponding `src/capa/cli/<thing>.py` module. Shared helpers (runs-root resolution, lockfile auto-discovery, problem rendering, discovery-row emission) live in [src/capa/cli/_helpers.py](../../src/capa/cli/_helpers.py).

---

## See also

- [Headless runs](headless-runs.md) — the headless lifecycle and exit-code contract
- [Exit codes](../reference/exit-codes.md) — canonical exit-code reference
- [Environment variables](../reference/environment-variables.md) — `$CAPA_*` reference
- [Validation and problems](../configuration/validation-and-problems.md) — how the layered validator works
