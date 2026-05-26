---
description: Author and probe capa hardware profile TOMLs — discover, new, validate, check (live handshake) across Watlow, Alicat, Sartorius, NI-DAQ, camera adapters.
---

# capa hardware

**Audience:** config authors building hardware TOMLs.
**Scope:** author, validate, probe, and discover the hardware profile half of a capa config. Four commands, paired against each step of building a rig file from scratch.

```
$ capa hardware --help
Usage: capa hardware [OPTIONS] COMMAND [ARGS]...

  Author and probe hardware profiles.

Commands:
  validate  Validate a hardware TOML against Layers 1-2 + 4 (no live probes).
  check     Run Layer 5 (live handshake) against an experiment / hardware file.
  discover  Discover every adapter family visible on the local system.
  new       Write a minimal blank hardware TOML at ``path``.
```

The four commands map onto a typical hardware-authoring workflow:

```
discover  → see what's attached
new       → drop a blank profile to edit
validate  → lint the structure (no hardware needed)
check     → handshake every device (touches hardware)
```

---

## `capa hardware new`

```
$ capa hardware new --help
Usage: capa hardware new [OPTIONS] PATH

  Write a minimal blank hardware TOML at ``path``.

Arguments:
  PATH  [required]

Options:
  --name  TEXT  HardwareProfile.name field.  [default: new_profile]
```

Writes a valid empty `HardwareProfile` to `PATH`. The file has the right keys for `name`, `devices`, `channels`, and `cameras` (all empty) so it can be opened in the Setup tab immediately.

```bash
uv run capa hardware new configs/hardware/rig_2.toml --name rig_2
# wrote configs/hardware/rig_2.toml
```

Refuses to overwrite an existing file (exit 2). Delete the file first if that is what you want.

---

## `capa hardware validate`

```
$ capa hardware validate --help
Usage: capa hardware validate [OPTIONS] PATH

  Validate a hardware TOML against Layers 1-2 + 4 (no live probes).
```

Runs the layered validator's hardware-relevant subset (Layers 1, 2, 4) against the TOML. Stubs the experiment-side fields the layered pipeline normally expects (operator, sample, procedure) so the command can operate on a hardware-only file without complaining about missing run metadata.

What it catches:

- Structural errors (Layer 1): malformed TOML, wrong types, missing required fields.
- Schema errors (Layer 2): device entries that fail Pydantic validation.
- Wiring errors (Layer 4): channels referencing missing devices, duplicate device names, two devices declaring the same serial port.

What it does **not** do: open the hardware. Use `capa hardware check` for that.

```bash
$ uv run capa hardware validate configs/hardware/rig_sim.toml
OK: configs/hardware/rig_sim.toml  (0 error(s), 0 warning(s), 0 info)
```

On problems, each finding is printed with severity, section, JSON path, and message — same format as the Setup tab's Problems panel. Exits 2 on any error; warnings and info do not change exit code.

---

## `capa hardware check`

```
$ capa hardware check --help
Usage: capa hardware check [OPTIONS] PATH

  Run Layer 5 (live handshake) against an experiment / hardware file.

  Read-only: opens each adapter, identifies it, and closes — no
  setpoints written.
```

Layer 5 — the live handshake layer. Opens every declared device, identifies it, and closes. No setpoints, no destructive commands. This is the headless equivalent of the Setup tab's **Check Hardware** button.

`PATH` can be either an experiment YAML or a hardware-only TOML; the command tries to load it as an experiment first and falls back to hardware-only.

```bash
$ uv run capa hardware check configs/hardware/rig_2.toml
OK: configs/hardware/rig_2.toml  (0 error(s), 0 warning(s), 0 info)
```

Use this:

- Just after `capa hardware new` + manual editing, to confirm every port is reachable.
- After moving the rig to a new host.
- On a CI runner attached to real hardware as a pre-merge gate.

Exits 2 on any layer-5 problem (port not openable, identify mismatch, etc.).

### `capa hardware check` vs `capa validate --strict`

Both open + close each adapter. The differences:

| | `capa hardware check` | `capa validate --strict` |
|---|---|---|
| Input | Hardware TOML *or* experiment YAML | Experiment YAML only |
| Layers run | 1–5, on the hardware portion | Pydantic + adapter handshake |
| Output format | Layered problem report (severities, codes) | Per-device handshake summary |
| When to reach for it | Authoring a hardware profile, vetting a rig | Quick sanity check on a full experiment |

---

## `capa hardware discover`

```
$ capa hardware discover --help
Usage: capa hardware discover [OPTIONS]

  Discover every adapter family visible on the local system.

Options:
  --adapter  TEXT  Probe only the named adapter
                   (watlow|alicat|sartorius|nidaq|camera_visible|camera_ir).
  --json           Emit machine-readable JSON instead of a table.
```

The broad discovery command: routes through the [`AdapterDescriptor`](../../src/capa/devices/registry.py) registry, which means cameras and any plugin-registered adapters appear too. Compare to [`capa devices discover`](capa-devices.md), which is the older, narrower (serial + DAQ only) variant.

```bash
# Everything attached
uv run capa hardware discover

# Just the IR cameras
uv run capa hardware discover --adapter camera_ir

# Machine-readable
uv run capa hardware discover --json
```

Output format matches [`capa devices discover`](capa-devices.md). See that page for example tables and JSON shapes.

Exit codes: 0 on success (including empty result), 2 on `--adapter <unknown>`.

---

## Roadmap: subcommands that exist in the docs scaffold but not in code yet

The earlier stub for this page listed three commands that have not been implemented: `describe`, `materialize`, `probe`. The closest equivalents today:

- **`describe`** — pretty-print a hardware TOML's contents. Workaround: open the TOML; or run `capa config validate <yaml>` and read the layered output.
- **`materialize`** — resolve every device spec end-to-end. Workaround: `capa hardware check` returns the layered findings; full materialization happens inside the runtime today.
- **`probe`** — open every device, close, report. This is exactly what `capa hardware check` does; the historical `probe` name has been retired.

If you need one of these as a first-class command, open an issue with the use case.

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Command completed successfully (no errors for `validate` / `check`; probe completed for `discover`; file written for `new`). |
| 2 | Hard validation error, unknown adapter, file-already-exists for `new`, or `CapaError` raised by the layered pipeline. |

---

## See also

- [`capa devices discover`](capa-devices.md) — narrower discovery (no cameras)
- [`capa config validate`](capa-config.md) — same layered pipeline against a full experiment YAML
- [`capa validate`](capa-validate.md) — quick Pydantic + handshake check on an experiment
- [Hardware TOML](../configuration/hardware-toml.md) — every field in the file format
- [Devices overview](../devices/overview.md) — adapter contract, params blocks
- [Validation and problems](../configuration/validation-and-problems.md) — the layered validator concept
