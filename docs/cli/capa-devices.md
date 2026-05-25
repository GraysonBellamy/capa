# capa devices

**Audience:** config authors discovering attached hardware.
**Scope:** probe non-camera adapters (serial / DAQmx) visible on the local system. Output a table or JSON listing what was found.

```
$ capa devices --help
Usage: capa devices [OPTIONS] COMMAND [ARGS]...

  Discover hardware visible on the local system.

Commands:
  discover  Discover non-camera devices visible on the local system.
```

The sub-app currently exposes a single command: `discover`.

---

## `capa devices discover`

```
$ capa devices discover --help
Usage: capa devices discover [OPTIONS]

  Discover non-camera devices visible on the local system.

  Uses the same descriptor-driven path as ``capa hardware discover``.
  This older command stays scoped to non-camera adapters so scripts that
  expected serial/DAQ output do not suddenly see video devices.

Options:
  --adapter  TEXT  Only probe the named adapter
                   (watlow|alicat|sartorius|nidaq).
                   Default: probe every real adapter.
  --json           Emit machine-readable JSON instead of a table.
```

### What it does

Walks the [`AdapterDescriptor`](../../src/capa/devices/registry.py) registry, **excluding cameras**, and runs each adapter's `discover_descriptor()` async path. For serial adapters (Watlow, Alicat, Sartorius) that means a port scan + identify pass; for the NI-DAQ adapter that means an `nidaqmx.system.System()` enumeration.

Each row carries the adapter id and whatever identity fields the discovery path returns — typically port name, serial number, model id, and an `idn` string.

### Why two discovery commands?

`capa devices discover` is the **older, narrower** command: it predates camera adapters and is intentionally kept that way so scripts that parse its output never have a video device appear in the table. The broader command — including cameras and any plugin adapters — is [`capa hardware discover`](capa-hardware.md). The two share the same underlying discovery path; the only difference is the descriptor filter.

| You want… | Use |
|---|---|
| Serial / DAQ inventory only | `capa devices discover` |
| Everything, cameras included | [`capa hardware discover`](capa-hardware.md) |

### Synopsis

```bash
# Probe every non-camera adapter
uv run capa devices discover

# Probe a single adapter family
uv run capa devices discover --adapter watlow

# JSON output for scripting
uv run capa devices discover --json
```

### Output — table

```
$ uv run capa devices discover

[watlow]
  port='COM3', idn='Watlow F4T', serial='F4T-12345'
  port='COM5', idn='Watlow F4T', serial='F4T-99887'

[alicat]
  port='COM7', idn='Alicat MC-100', serial='M19200123'

[nidaq]
  device='Dev1', model='USB-6259', serial='1ABCDEF'

Notes:
  sartorius: no devices found
```

The `Notes:` block surfaces adapter-level outcomes (no devices found, or transient errors during the probe) without polluting the device table.

### Output — JSON

```
$ uv run capa devices discover --json
{
  "devices": [
    {"adapter": "watlow", "port": "COM3", "idn": "Watlow F4T", "serial": "F4T-12345"},
    {"adapter": "alicat", "port": "COM7", "idn": "Alicat MC-100", "serial": "M19200123"}
  ],
  "notes": [
    "sartorius: no devices found"
  ]
}
```

Use `--json` from CI or any script that wants stable parsing.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Probe completed. Empty result still exits 0 — "no devices found" is a valid answer. |
| 2 | `--adapter <unknown>` — adapter name does not match any non-camera descriptor. |

### Caveats

- The probe is read-only. No setpoints are written and no destructive commands are issued.
- The probe is **adapter-by-adapter**; if a single adapter family hangs, the command can take 10–30 s to complete. Run with `--adapter <one>` to scope down.
- Serial port enumeration follows the platform's native list (COM* on Windows, `/dev/tty*` on Linux). USB devices that have not enumerated yet (just plugged in) may need a re-run.

---

## See also

- [`capa hardware discover`](capa-hardware.md) — broader probe that includes cameras and plugin adapters
- [`capa hardware new`](capa-hardware.md) — write a blank hardware TOML once the device inventory is known
- [Devices overview](../devices/overview.md) — what each adapter expects in its `params` block
- [Discovery](../devices/discovery.md) — how the discovery layer interrogates each adapter
