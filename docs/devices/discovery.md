---
description: How capa probes a rig for connected hardware — per-adapter discover() hooks, Setup tab Discover dialog, non-destructive scans, results into hardware TOML.
---

# Discovery

**Audience:** operators connecting a fresh rig, replacing a device, or troubleshooting "the Setup tab can't see my hardware."
**Scope:** how capa scans the local system for devices and cameras, what each adapter family probes, conflict resolution, and how a scan result gets written back into the hardware TOML.

---

## What discovery is

Every adapter ships a `discover()` (or `discover_cameras()`) hook that
probes the local system, opens nothing, writes nothing, and returns a
list of dicts. The cross-cutting dispatcher lives at
[`capa.devices.discovery`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/discovery.py):

- `discoverable_descriptors(adapter=..., include_cameras=...)` — returns the
  set of `AdapterDescriptor`s to probe.
- `discover_descriptor(descriptor)` — imports the descriptor's adapter
  module, picks `discover_cameras()` if present else `discover()`,
  calls it, and normalizes the result to a `DiscoveryResult` (`rows` or
  `error`).

Discovery is **non-destructive**: hooks must be read-only. They do not
open a worker pool, do not write to disk, and never issue device
commands. The Setup tab disables Discover while a run is active —
serial buses can only be probed by one process at a time, and a probe
mid-run would collide with the running worker.

## Where it surfaces

Three entry points dispatch the same underlying hooks:

### Setup tab — Discover button

Opens the
[`DiscoveryDialog`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/tabs/setup_discovery.py).
Aggregates the rows from every adapter into one table, offers an
`[Add]` button per row, and merges the chosen rows into the in-memory
hardware payload (the operator still needs to save). The dialog
routes serial/USB adapters into `[[devices]]` and cameras into
`[[cameras]]`.

### `capa hardware discover`

The full-fan-out CLI: probes every discoverable adapter including
cameras and plugins. Pass `--adapter <family>` to scope. `--json`
emits machine-readable output for scripts.

```bash
uv run capa hardware discover                    # everything
uv run capa hardware discover --adapter watlow   # just Watlows
uv run capa hardware discover --json             # script-friendly
```

### `capa devices discover`

The non-camera variant — kept distinct so older scripts that expected
serial / DAQ output don't suddenly see video devices in their parse
loop. Same dispatcher, `include_cameras=False`.

## Per-family discovery

Each adapter's `discover()` takes adapter-specific knobs and returns
rows with adapter-specific keys. The cross-cutting dispatcher does not
flatten the schema — the Setup tab and the CLI render per-family
columns.

| Family | Hook | Probes | Knobs | Returned row keys |
|---|---|---|---|---|
| [Watlow](watlow.md#discovery-and-handshake) | `discover` | serial `ports × bauds × protocols × addresses` via `watlowlib.find_devices` | `ports`, `addresses`, `baudrates`, `timeout_s` | `port`, `address`, `baudrate`, `protocol`, `model`, `firmware`, `hardware`, `family` |
| [Alicat](alicat.md#discovery-and-handshake) | `discover` | serial scan + identify via `alicatlib.find_devices` | `ports`, `unit_ids`, `baudrates` | `port`, `unit_id`, `baudrate`, `model`, `serial`, `firmware`, `kind` |
| [Sartorius](sartorius.md#discovery-and-handshake) | `discover` | serial scan via `sartoriuslib.find_devices` + `summarize_discovery` | `ports`, `baudrates`, `timeout_s` | `port`, `protocol`, `baudrate`, `autoprint_active` |
| [NI-DAQ](nidaq.md#discovery-and-handshake) | `discover` | DAQmx enumeration via `nidaqlib.find_devices` | none (chassis-driven) | `device`, `product_type`, `serial`, `ai_channels`, `ao_channels`, `di_lines`, `do_lines`, `ci_channels`, `co_channels` |
| [USB webcams](cameras-webcam.md#discovery) | `discover_cameras` | per-OS API: V4L2 sysfs / DirectShow `duvc_ctl.list_devices` / `[]` on macOS | none | `selector`, `model`, `serial`, `transport` |
| [FLIR IR](cameras-flir.md#discovery) | `discover_cameras` | Atlas SDK discovery | `discovery_timeout_s`, `interfaces` | `model`, `serial`, `transport` |

Adapters with no `discover` hook (or that set `discoverable=False` on
the descriptor) are skipped by the dispatcher and do not appear in the
table.

## Persisting a scan back into the hardware TOML

The Setup tab does the mapping. Each row's per-family payload
extraction is in
[`build_device_payload_from_row`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/tabs/setup_discovery.py)
and [`build_hw_entry_from_row`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/tabs/setup_discovery.py).
The rules are simple per-family lifts:

| Family | What gets lifted | Defaults to |
|---|---|---|
| Watlow | `port`, `address` | `descriptor.default_params` (e.g. `protocol="stdbus"`, `rate_hz=1.0`) |
| Alicat | `port`, `unit_id`, `baudrate` | `rate_hz=2.0` |
| Sartorius | `port`, `baudrate`, `protocol` (if surfaced) | descriptor defaults |
| NI-DAQ | `device` → `task_name = "<device>_ai"` | operator still fills the channel list |
| Cameras (visible/IR) | full camera payload via `build_hw_entry_from_row` | `kind` from family |

Device `name` is auto-picked by `_pick_unique_name(family,
existing_names)` so re-running discovery on the same rig produces
predictable names (`watlow1`, `alicat1`, …) and avoids collisions
when the operator has already named devices manually.

The merge is *additive*: existing entries are preserved, and the
operator must explicitly save to persist. See [Hardware
TOML](../configuration/hardware-toml.md) for the on-disk schema.

## Conflict resolution

Per family:

- **Watlow** — dedup is on `(port, address)`, first-hit-wins. The
  inner library iterates `baud × protocol × address` outermost-first
  (`38400 → 19200 → 9600`, `STDBUS → MODBUS_RTU`), so the first hit is
  the most-likely production config.
- **Alicat** — per `(port, unit_id, baudrate)` row. A multi-drop bus
  with several Alicats produces one row per device; the operator picks
  which to add.
- **Sartorius** — one row per port that responded; `summarize_discovery`
  collapses per-baudrate probes for the same port.
- **NI-DAQ** — one row per physical NI device. No conflict possible —
  device names are globally unique.
- **Cameras** — `serial` exact-match wins; `model_hint` with multiple
  candidates logs a warning and picks first deterministically; unique
  camera with no selectors is accepted; multiple cameras with no
  selector is an error. See [`CameraInfo`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/camera/base.py) for the rules.

## Why serial scans run sequentially

Three families probe serial ports: Alicat, Watlow, Sartorius. The UI
dispatcher runs these **sequentially**, not in parallel. The reason
lives in
[`setup_discovery.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/tabs/setup_discovery.py)
(`_SERIAL_PORT_FAMILIES`):

> Running these scans in parallel makes them race for the same COM
> port handle — on Windows the loser gets a connection error on its
> very first probe and upstream `find_devices` puts the port in a
> `dead_ports` set, so the rest of the sweep (other bauds / protocols)
> is silently skipped.

Symptom of the bug: "Watlow not found on initial scan / rescan,
intermittently." Fix: run them sequentially per family. Non-serial
scans (NI-DAQ, cameras) keep their parallel fan-out — they don't
collide.

## Re-running discovery after a hardware swap

Discovery is idempotent — running it twice on the same rig produces
the same rows (modulo wall-clock state like `autoprint_active`).
After swapping a device:

1. Open the Setup tab.
2. Click Discover.
3. Compare the new table against the existing `[[devices]]` /
   `[[cameras]]` entries.
4. Add the new device; remove the old `[[devices]]` entry by hand
   from the TOML if the swap was a replacement.
5. Save.

The Setup tab does *not* auto-remove `[[devices]]` entries that
no longer appear in discovery. Stale entries fail loudly at config
load (the adapter cannot open) rather than being silently dropped —
which is the right failure mode for an acquisition system, but means
post-swap cleanup is a manual edit.

## When discovery returns nothing

Common causes:

- **Wrong port / wrong driver.** Serial scans need the OS to enumerate
  the port. Check Device Manager (Windows) or `ls /dev/tty*` (Linux).
- **A bus already in use.** Discover is disabled while a run is
  active, but a separate process (the device's vendor utility, a
  competing capa instance) holds the bus.
- **NI-DAQmx runtime not installed.** `find_devices()` returns no rows
  if the NI-DAQmx driver is absent — capa surfaces this as `ok=False`
  internally and filters it out. Install the NI-DAQmx runtime from
  National Instruments.
- **`capa-flir` not installed.** The FLIR descriptor is missing from
  the registry; the dispatcher silently skips it. `uv pip install -e
  ../capa-flir` adds it.
- **`duvc-ctl` wheel missing (Windows).** Webcam discovery on Windows
  returns `[]` without the wheel. Install it via the project's optional
  dependencies.

The CLI's `notes` block (the second return of `collect_discovery_rows`)
surfaces per-adapter error strings so the operator can see *why* a
family contributed zero rows.

## See also

- [Devices overview](overview.md) — adapter contract and the descriptor registry.
- [Hardware TOML](../configuration/hardware-toml.md) — the on-disk schema that discovery writes into.
- Per-family discovery details:
  [Watlow](watlow.md#discovery-and-handshake) ·
  [Alicat](alicat.md#discovery-and-handshake) ·
  [Sartorius](sartorius.md#discovery-and-handshake) ·
  [NI-DAQ](nidaq.md#discovery-and-handshake) ·
  [USB webcams](cameras-webcam.md#discovery) ·
  [FLIR IR](cameras-flir.md#discovery).
- [The Setup tab](../user-guide/the-setup-tab.md) — operator UI for the Discover dialog.
