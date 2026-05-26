---
description: Run capa full layered experiment-config validation pipeline (Layers 1-5) from CLI — Problems-panel parity with severity, section, JSON path per finding.
---

# capa config

**Audience:** config authors and anyone diagnosing a Setup-tab Problems-panel finding from the command line.
**Scope:** sub-app for the full layered validation pipeline. Today the sub-app exposes a single command: `validate`. See [Roadmap](#roadmap-subcommands-not-yet-implemented) for what is *not* here yet.

```
$ capa config --help
Usage: capa config [OPTIONS] COMMAND [ARGS]...

  Validate and inspect experiment configs.

Commands:
  validate  Run the layered validation pipeline against an experiment config.
```

---

## `capa config validate`

```
$ capa config validate --help
Usage: capa config validate [OPTIONS] PATH

  Run the layered validation pipeline against an experiment config.

  Headless equivalent of the Setup tab's Problems panel: prints each
  finding with section + path + severity. Exits non-zero if any error
  is found; warnings and info do not change the exit code.

Arguments:
  PATH  [required]

Options:
  --live  Also run Layer 5 (live discovery + handshake) — touches hardware.
```

### What it does

Loads the experiment config via [`ConfigDocument.load`](../../src/capa/config/__init__.py) and runs the layered validator. Each finding is printed as a colored row with severity, section, JSON path, and a `code` identifier. The exit code reflects errors only — warnings and info entries are informational.

With `--live`, the command also runs Layer 5: opens every declared adapter, identifies it, closes it. Same read-only handshake [`capa validate --strict`](capa-validate.md) uses; same handshake [`capa hardware check`](capa-hardware.md) uses. The three commands differ in input shape and output formatting, not in what they actually do to the hardware.

### Output

Clean config:

```
$ uv run capa config validate configs/experiments/sim_freerun.yaml
OK: configs/experiments/sim_freerun.yaml  (0 error(s), 0 warning(s), 0 info)
```

Config with problems:

```
$ uv run capa config validate configs/experiments/broken.yaml
[error] hardware.devices.1.params.port :: device_resource_conflict
    Devices 'watlow_a' and 'watlow_b' both claim port COM3.
    source: configs/hardware/rig_broken.toml
[warning] experiment.method.steps.3.target.name :: step_target_unknown
    Step references target 'tc_alpha' which is not declared as a channel.
[info] experiment.runtime.saturation_deadline_s :: tuning_override
    saturation_deadline_s=30.0 overrides the 10 s default.
2 error(s), 1 warning(s), 1 info
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | No errors. Warnings and info are reported but do not change the exit code. |
| 2 | At least one `[error]`-severity finding. |

---

## When to reach for it

| Situation | Command |
|---|---|
| "I want the full Problems-panel output from the command line." | `capa config validate <yaml>` |
| "Touch the hardware too." | `capa config validate <yaml> --live` |
| "I just want a yes/no on Pydantic + plugin resolution." | [`capa validate <yaml>`](capa-validate.md) |
| "I only have a hardware TOML, not a full experiment." | [`capa hardware validate`](capa-hardware.md) |

`capa validate` is shorter output. `capa config validate` is the full layered report.

---

## Roadmap: subcommands not yet implemented

The earlier stub for this page listed three commands that were planned but never built:

- **`canonicalize`** — emit a canonical-form rewrite of a config file.
- **`diff`** — compare two configs and surface meaningful drift.
- **`migrate`** — apply a schema migration on a future schema bump.

If you want any of these, open an issue describing the use case. None are blocking the current workflow:

- For *canonicalize*-style cleanup, the Setup tab's save path rewrites in canonical form.
- For *diff*, `diff -u <a> <b>` is usually fine.
- For *migrate*, no schema bumps have shipped yet that need an explicit migration command.

---

## See also

- [`capa validate`](capa-validate.md) — narrower, quicker check on an experiment YAML
- [`capa hardware validate`](capa-hardware.md) — same layered pipeline on a hardware-only TOML
- [`capa hardware check`](capa-hardware.md) — Layer 5 live handshake against a hardware TOML
- [Validation and problems](../configuration/validation-and-problems.md) — what each layer does
- [Configuration overview](../configuration/overview.md) — the four config kinds and how they compose
