---
description: Driving Watlow EZ-Zone PM heaters in capa via watlowlib — devices.params schema, Standard Bus and Modbus RTU, channel bindings, discovery, long-row emissions.
---

# Watlow heaters

**Audience:** operators and config authors using Watlow EZ-Zone controllers as the rig's heater control loop.
**Scope:** capa's [`watlowlib`](https://github.com/GraysonBellamy/watlowlib) adapter — `[devices.params]` fields, channel binding kind, command surface, discovery, and the rig-specific quirks the source docstrings already document.

---

## At a glance

| | |
|---|---|
| Adapter id | `capa.devices.watlow` |
| Sibling library | [`watlowlib`](https://github.com/GraysonBellamy/watlowlib) |
| Real adapter | [`capa.devices.watlow`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/watlow.py) |
| Sim adapter | [`capa.devices.sim.watlow_sim`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/watlow_sim.py) |
| Resource scheme | `serial:<port>` — one worker per COM port |
| Channel binding | [`watlow_parameter`](../configuration/channel-bindings.md) |
| Emission shape | `long_row` — one row per `(parameter, instance)` per tick |
| Default poll rate | 1 Hz (Std Bus) — see [`rate_hz`](#configuration) |

## Supported hardware

Watlow EZ-Zone PM-series controllers over Standard Bus or Modbus RTU,
addressed 1–16 (Std Bus) or 1–247 (Modbus). The full protocol scope —
which parameters are readable, which are writable, the registry of
parameter IDs — is owned by
[watlowlib](https://github.com/GraysonBellamy/watlowlib); capa's
adapter is a thin shim over it.

## Configuration

Every key under `[devices.params]` is parsed by
[`WatlowAdapterParams`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/watlow.py)
(Pydantic, `extra="forbid"` — typos are rejected, not silently dropped).

```toml title="configs/hardware/watlow_real.toml (excerpt)"
[[devices]]
name = "heater"
adapter = "capa.devices.watlow"

[devices.params]
port = "COM6"
address = 1
protocol = "stdbus"           # "stdbus" | "modbus_rtu" | "auto"
parameters = ["process_value", "setpoint"]
instances = [1]
rate_hz = 1.0
auto_reconnect = true
snapshot_period_s = 30.0
identify_on_open = true
wire_temperature_unit = "F"   # see Quirks
```

| Key | Default | Notes |
|---|---|---|
| `port` | required | `COM6`, `/dev/ttyUSB0`, or `fake://...` for tests against a `FakeTransport`. |
| `address` | `1` | Std Bus accepts 1–16; Modbus RTU accepts 1–247. |
| `protocol` | `"stdbus"` | `"auto"` runs watlowlib's conservative detector; pin one in production. |
| `baudrate` / `parity` / `stopbits` / `bytesize` | `38400 / "none" / 1 / 8` | EZ-Zone factory defaults. |
| `parameters` | `("process_value", "setpoint")` | Polled each tick. Names must resolve in `watlowlib.PARAMETERS`. |
| `instances` | `(1,)` | 1-indexed loop selectors. Single-loop devices stay at `(1,)`. |
| `rate_hz` | `1.0` (gt 0, le 100) | Production sits at 1–5 Hz. The 100 Hz cap exists to catch typos and to give unit tests a fast path; real Std Bus round-trips ~50 ms per parameter, so even Modbus tops out near 10 Hz for two-parameter polls. |
| `auto_reconnect` | `true` | Transient `WatlowConnectionError`s are logged and retried on the next tick instead of ending the stream. |
| `snapshot_period_s` | `30.0` | Cadence of `DeviceSnapshot` health pings. |
| `identify_on_open` | `true` | Run `Controller.identify` after `open` to populate the cached `DeviceInfo`. |
| `io_timeout_s` | `None` (inherits watlowlib `1.0 s`) | Per-call timeout for `set_setpoint` / `write_parameter` / `read_pv` / `identify`. Bump to 2.0–3.0 s for slow USB-RS485 bridges. Streaming reads ignore this knob. |
| `wire_temperature_unit` | `"F"` | Operator-asserted wire scale. **See Quirks — this is the most common bring-up footgun.** |

## Channels and emission shape

Watlow is the long-row family:
[`watlowlib.streaming.Sample`](https://github.com/GraysonBellamy/watlowlib)
emits one row per `(parameter, instance)` per tick. capa preserves that
shape verbatim into `device_records/watlow.parquet` via
[`watlowlib.sinks.sample_to_row`](https://github.com/GraysonBellamy/watlowlib).

The only supported binding source is `watlow_parameter`:

```toml
[[channels]]
name = "heater.pv"
kind = "process_var"
unit = "degF"
derived_unit = "degC"
plot_group = "temperatures"

[channels.source]
source = "watlow_parameter"
device = "heater"
parameter = "process_value"
instance = 1

[channels.calibration]
kind = "linear_two_point"
input_unit = "degF"
output_unit = "degC"
ref_low_raw = 32.0
ref_low_value = 0.0
ref_high_raw = 212.0
ref_high_value = 100.0
```

The channel's `unit` is the *wire* scale; `derived_unit` is what
operators see in plots and what the manual setpoint widget accepts.
Setpoint commands flow the calibration backwards — the operator types a
Celsius value, the adapter writes the Fahrenheit wire equivalent.

See [channel bindings](../configuration/channel-bindings.md) for the
selector contract and [devices overview § emission
shapes](overview.md#emission-shapes) for how `long_row` differs from the
other shapes.

## Capability flags

The Watlow descriptor declares:

| Flag | Used by |
|---|---|
| `HAS_SETPOINT` | Manual-control setpoint widget. |
| `HAS_RAMP` | Method-step ramp UI. |
| `READS_PROCESS_VAR` | Plot binding for PV channels. |
| `HAS_PARAMETER_CONFIG` | Diagnostic dock's parameter-read panel. |

It does *not* declare `SUPPORTS_AUTO_RECONNECT` even though
`auto_reconnect=True` is the default — the flag is reserved for adapters
where the operator can rely on reconnect *without* losing samples.
Watlow's reconnect silently drops the in-flight tick.

## Commands

The adapter exposes typed wrappers for IDE help and refactor safety;
the generic `command()` form exists so plugins that don't import
`WatlowAdapter` can still drive it.

| Typed call | `DeviceCommand.kind` | Payload |
|---|---|---|
| `set_setpoint(value, instance=1)` | `"set_setpoint"` | `{"value": float, "instance": int}` |
| `write_parameter(name, value, instance=1)` | `"write_parameter"` / `"set_parameter"` | `{"name": str, "value": float \| int, "instance": int}` |
| `set_display_units(unit)` | `"set_display_units"` | `{"unit": str}` |
| `read_state_snapshot()` | n/a | Used by the manual-control card; not gated. |

Every write goes through the [authorization
gate](../safety/authorization-gates.md) — `issued_by` is required, plus
either `authorization_id` (run-arm cover) or `confirmed_by` (manual
confirmation). Procedures should issue commands through
`ctx.dispatcher`, not directly on the adapter, so the per-resource
worker serializes them on shared serial buses.

`set_setpoint` inverts the bound channel's calibration before writing —
operators set "600 °C", the adapter writes "1112 °F" if the wire is
Fahrenheit. This is a no-op for identity calibrations and for raw
commands issued outside any bound channel.

## Discovery and handshake

Both hooks are read-only — they `open` + `identify` + `close` without
touching any control surface.

`discover(ports=..., addresses=..., baudrates=..., timeout_s=0.5)` is a
thin wrapper over
[`watlowlib.find_devices`](https://github.com/GraysonBellamy/watlowlib)
that iterates the cartesian product of
`ports × baudrates × protocols × addresses`. Defaults are
`addresses=(1,)` and `baudrates=(38400, 19200, 9600)` — CAPA rigs
universally leave Watlows at address 1, so the address sweep is
single-shot. Pass `addresses=range(1, 248)` for a slow exhaustive scan.

Results are deduped on `(port, address)`, first-hit-wins, so a bus that
answers at multiple bauds or protocols produces one row per physical
controller (the most-likely production config, since `find_devices`
iterates baud `38400 → 19200 → 9600` and protocol `STDBUS → MODBUS_RTU`
outermost-first).

`handshake(params)` is the per-device form `capa validate --strict`
runs. It returns a one-line identity summary or raises `AdapterError`
so the operator sees wiring problems before arming a run.

See [Discovery](discovery.md) for the cross-cutting UX.

## Quirks

### Wire-unit assertion (`wire_temperature_unit`)

This is the most common bring-up footgun and the only Watlow setting
that needs operator-level validation.

watlowlib 0.4+ requires the caller to assert the temperature scale on
the wire. The rig's PM3R1CA (fw=1) was empirically verified to emit
Fahrenheit on the wire regardless of panel-display configuration: with
the panel set to °C, the panel reads PV=18.7 and comms reports 65.65 —
65.65 °F = 18.69 °C. Parameter 3005 controls the panel display only;
parameter 17050 is label-only on at least one firmware revision. **The
wire scale appears to be hardware-pinned on this SKU.**

If you bring up a different PM3 (or a different SKU/firmware), verify
empirically with `watlow-diag probe-unit` (compare a panel reading to
the comms readback) before pinning. Set `wire_temperature_unit = "C"`
or `"F"` based on the result. Setting it to `None` suppresses the
assertion entirely; `Sample.unit` will be `None` for temperature
parameters and the per-channel drift check becomes a no-op.

### Poll-rate ceiling

`rate_hz` accepts up to 100 but production sits at 1–5 Hz. Std Bus
round-trips ~50 ms per parameter on the PM family, so even Modbus tops
out near 10 Hz for a two-parameter poll. The 100 Hz upper bound exists
to catch typos (`rate_hz=1000`) and to give the unit tests a fast path
against an in-process stub controller.

### Auto-reconnect drops the tick

`auto_reconnect=True` (the default) means transient
`WatlowConnectionError`s do not terminate the stream. The recorder
retries on the next tick — but the tick that hit the error is *gone*,
not buffered. For a typical 1 Hz heater this is invisible; for higher
rates or a flaky bus, watch the saturation pill and the `events.sqlite`
device-event entries.

### Sim equivalent

[`capa.devices.sim.watlow_sim`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/watlow_sim.py)
mirrors the same `long_row` shape and the same capability set
*minus* `HAS_PARAMETER_CONFIG`. Signal keys follow the
`"<parameter>/<instance>"` schema (`"process_value/1"`, `"setpoint/1"`);
bare `"process_value"` defaults to instance 1. The sim's
`parameter_units` knob coerces watlowlib aliases (`"C"`, `"degC"`,
`"%"`) into the right enum value; unknown strings pass through
verbatim as a cross-vendor escape hatch. See [Simulators](simulators.md)
for the full signal schema.

## See also

- [Devices overview](overview.md) — adapter contract, capability enum, resource grouping.
- [Hardware TOML](../configuration/hardware-toml.md) — how the `[[devices]]` block is parsed.
- [Channel bindings](../configuration/channel-bindings.md) — the `watlow_parameter` source schema.
- [Authorization gates](../safety/authorization-gates.md) — the contract for any device write.
- [Discovery](discovery.md) — cross-cutting Setup-tab and CLI behavior.
- [watlowlib docs](https://github.com/GraysonBellamy/watlowlib/tree/main/docs) — parameter registry, PID tuning, protocol-level reference.
