---
description: Configuring Alicat mass-flow and pressure controllers in capa via alicatlib — setpoint, gas-select, tare, multi-drop RS-485 buses, and wide-row frame fields.
---

# Alicat mass-flow

**Audience:** operators and config authors with Alicat mass-flow / pressure controllers or meters on the rig.
**Scope:** capa's [`alicatlib`](https://github.com/GraysonBellamy/alicatlib) adapter — `[devices.params]` fields, wide-row frame fields, the command surface (setpoint, gas-select, tare), discovery, and the gas-change safety implication.

---

## At a glance

| | |
|---|---|
| Adapter id | `capa.devices.alicat` |
| Sibling library | [`alicatlib`](https://github.com/GraysonBellamy/alicatlib) |
| Real adapter | [`capa.devices.alicat`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/alicat.py) |
| Sim adapter | [`capa.devices.sim.alicat_sim`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/alicat_sim.py) |
| Resource scheme | `serial:<port>` — one worker per COM port (multi-drop buses share) |
| Channel binding | [`alicat_frame_field`](../configuration/channel-bindings.md) |
| Emission shape | `wide_row` — one row per poll, many fields |
| Default poll rate | 2 Hz |

## Supported hardware

The Alicat MFC, MFM, pressure-controller, and pressure-meter families
across gas and liquid mediums, plus the CODA Coriolis line. The
library's model-prefix matrix —
[alicatlib `docs/devices.md`](https://github.com/GraysonBellamy/alicatlib/blob/main/docs/devices.md) —
is the authoritative list of supported prefixes and the Kind × Medium
matrix. capa's adapter is a thin shim and inherits the library's full
scope; it does not maintain its own model list.

The library's identify probe determines `DeviceKind` (`FLOW_METER`,
`FLOW_CONTROLLER`, `PRESSURE_METER`, `PRESSURE_CONTROLLER`,
`UNKNOWN`) at `open` time; capa's adapter refines its declared
capabilities from that — `HAS_SETPOINT` and `HAS_VALVE_HOLD` are only
added when a controller is detected.

## Configuration

```toml title="configs/hardware/alicat_real.toml (excerpt)"
[[devices]]
name = "purge_mfc"
adapter = "capa.devices.alicat"

[devices.params]
port = "COM7"
unit_id = "A"
baudrate = 115200
rate_hz = 2.0
auto_reconnect = true
snapshot_period_s = 30.0
```

[`AlicatAdapterParams`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/alicat.py)
is `extra="forbid"` — typos are rejected at parse time.

| Key | Default | Notes |
|---|---|---|
| `port` | required | `COM7`, `/dev/ttyUSB0`. |
| `unit_id` | `"A"` | Bus-level single-letter id. Multi-drop RS-485 buses use distinct letters per device — keep this in sync with the device's stored unit id. |
| `baudrate` | `19200` | Alicat factory default. Some labs reconfigure to 38400 or 115200; set explicitly. |
| `timeout_s` | `0.5` | Per-call command timeout. |
| `rate_hz` | `2.0` (gt 0, le 50) | Production sits at 1–10 Hz. The 50 Hz cap is a typo guard; serial round-trips dominate. |
| `auto_reconnect` | `true` | Transient `AlicatConnectionError`s are logged and retried; the tick is dropped, not buffered. Toggles the `SUPPORTS_AUTO_RECONNECT` capability flag. |
| `snapshot_period_s` | `30.0` | Health-ping cadence. |
| `overflow` | `"block"` | `"block"` or `"drop_newest"`. Block matches the rest of capa — producers wait on a slow sink rather than silently drop. |

## Channels and emission shape

Alicat is the wide-row family: one
[`alicatlib.streaming.Sample`](https://github.com/GraysonBellamy/alicatlib)
per poll carries every measurement field the firmware exposes
(`Mass_Flow`, `Volumetric_Flow`, `Abs_Press`, `Gauge_Press`,
`Temperature`, `Mass_Flow_Setpt`, `Mix_Gas`, totalizer counts, etc.).
The native frame is preserved verbatim into
`device_records/alicat.parquet` via
[`alicatlib.sample_to_row`](https://github.com/GraysonBellamy/alicatlib).

The only supported binding source is `alicat_frame_field`. Each
channel selects one column of the wide row:

```toml
[[channels]]
name = "purge.flow"
kind = "mfc_flow"
unit = "slpm"
plot_group = "flows"

[channels.source]
source = "alicat_frame_field"
device = "purge_mfc"
field = "Mass_Flow"

[channels.calibration]
kind = "identity"
input_unit = "slpm"
output_unit = "slpm"
```

The `field` name is the underscored key
[`alicatlib.Reading.as_dict`](https://github.com/GraysonBellamy/alicatlib)
exposes. Which fields are populated depends on the device's firmware
and configured "data frame" — a meter with totalizer enabled produces
different keys than the same meter with totalizer disabled.

See [channel bindings](../configuration/channel-bindings.md) for the
selector contract and [devices overview § emission
shapes](overview.md#emission-shapes) for how `wide_row` compares to
`long_row`.

## Capability flags

The Alicat descriptor declares a fixed baseline:

| Flag | Notes |
|---|---|
| `HAS_TARE` | Tare commands always available. |
| `HAS_GAS_SELECT` | Gas-select widget shown in manual controls. |
| `READS_PROCESS_VAR` | Plot binding for PV channels. |
| `HAS_PARAMETER_CONFIG` | Diagnostic dock exposes parameter writes. |
| `HAS_DISPLAY_CONTROL` | Lock / unlock / blink display commands. |
| `HAS_TOTALIZER` | Totalizer reset / save commands. |
| `SUPPORTS_AUTO_RECONNECT` | Added when `auto_reconnect=True` (the default). |

After `open` + identify, the adapter adds:

| Flag | Added when |
|---|---|
| `HAS_SETPOINT` | The library identifies the device as a `FlowController` or `PressureController`. |
| `HAS_VALVE_HOLD` | Same — the valve-hold mixin only exists on controllers. |

A meter (`FlowMeter` / `PressureMeter`) will refuse setpoint commands at
the library boundary even if a procedure forgets to gate on the flag —
but the UI relies on the flag to decide whether to render the setpoint
widget at all.

## Commands

| Typed call | `DeviceCommand.kind` | Notes |
|---|---|---|
| `set_setpoint(value, units=None)` | `"set_setpoint"` | Controllers only. |
| `set_gas(gas)` | `"set_gas"` | **Safety-relevant — see Quirks.** |
| `tare_flow()` | `"tare_flow"` | Meters and controllers. |
| `tare_absolute_pressure()` | `"tare_absolute_pressure"` | Pressure surface only. |
| `tare_gauge_pressure()` | `"tare_gauge_pressure"` | Pressure surface only. |
| `set_units(...)`, `set_zero_band(...)`, `set_stp_pressure(...)`, `set_stp_temperature(...)` | matching kinds | Configuration writes; gated. |
| `hold_valves()`, `hold_valves_closed()`, `cancel_valve_hold()` | matching kinds | Controllers only; latches valve state. |
| `totalizer_reset()`, `totalizer_reset_peak()`, `totalizer_save()` | matching kinds | Totalizer-equipped devices. |
| `lock_display()`, `unlock_display()`, `blink_display()` | matching kinds | Cosmetic; still gated. |
| `read_gas_list()` | n/a | Read-only — not gated. |

Every write goes through the [authorization
gate](../safety/authorization-gates.md). The full command kind list is
in [`_dispatch_command`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/alicat.py)
(line 451+).

## Discovery and handshake

`discover(ports=None, unit_ids=("A",), baudrates=None)` wraps
[`alicatlib.find_devices`](https://github.com/GraysonBellamy/alicatlib).
`baudrates=None` uses `alicatlib.DEFAULT_DISCOVERY_BAUDRATES`. The
returned rows carry `port`, `unit_id`, `baudrate`, `model`, `serial`,
`firmware`, and `kind` — sufficient for the Setup tab to render
"`MC-50SCCM-D` (FlowController) on `COM7` `A` @ 115200" and write a
matching `[[devices]]` block back to the hardware TOML.

`handshake(params)` is the per-device form `capa validate --strict`
runs: read-only `open` + `identify` + `close` returning a one-line
summary.

See [Discovery](discovery.md) for the cross-cutting UX.

## Quirks

### Gas changes are safety-relevant writes

A gas-select command (`set_gas`) reconfigures the controller's
calibration table. A unit configured for "Air" will mis-read mass flow
for "Hydrogen" (and vice versa) by the gas-correction factor — silently.
The wire write is fast and the controller does not announce it on the
status bus.

The authorization gate enforces `issued_by` + (`authorization_id` |
`confirmed_by`) like any other write, but the per-rig policy question
— *"who should be allowed to change the purge gas mid-method?"* — is
not something the adapter answers. Procedures that change gas mid-run
should issue them under an explicit `authorization_id` recorded in the
method, not under operator `confirmed_by` mid-flight.

See [authorization gates](../safety/authorization-gates.md) for the
contract and [destructive
operations](../safety/destructive-operations.md) for the operator-
confirmation rules.

### Shared serial buses share a worker

Multi-MFC blends typically sit on one RS-485 multi-drop bus with
distinct `unit_id`s per device. They will produce the same
`resource_id` (`serial:COM7`) and capa's `build_workers` groups them
into one
[`Worker`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/worker.py) —
the worker serializes I/O so two devices' commands cannot collide on
the wire. The `expected_emission_rate_hz` you see on a shared bus is
the *sum* across devices.

### Wide-row sparsity

A meter does not populate `Mass_Flow_Setpt` (it has no setpoint); a
controller without a totalizer does not populate the totalizer
columns. Bindings to columns the firmware never emits will resolve to
`None` on every tick — by design, since the wide row is the union of
what the library can extract.

### Sim equivalent

[`capa.devices.sim.alicat_sim`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/alicat_sim.py)
takes signals keyed by underscored frame-field names
(`{"Mass_Flow": {...}, "Abs_Press": {...}}`) plus a `static_fields`
dict for fields that don't vary per tick (`{"Mix_Gas": "Air"}`). It
declares `HAS_SETPOINT | HAS_TARE` unconditionally — sim does not
distinguish meters from controllers. See [Simulators](simulators.md)
for the signal schema.

## See also

- [Devices overview](overview.md) — adapter contract, capability enum, resource grouping.
- [Hardware TOML](../configuration/hardware-toml.md) — how `[[devices]]` blocks are parsed.
- [Channel bindings](../configuration/channel-bindings.md) — the `alicat_frame_field` source schema.
- [Authorization gates](../safety/authorization-gates.md) — the contract for any device write.
- [Discovery](discovery.md) — cross-cutting Setup-tab and CLI behavior.
- [alicatlib docs](https://github.com/GraysonBellamy/alicatlib/tree/main/docs) — model-prefix matrix, command surface, protocol reference.
