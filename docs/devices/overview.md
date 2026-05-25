# Devices overview

**Audience:** config authors, plugin authors, contributors touching
[`src/capa/devices/`](https://github.com/GraysonBellamy/capa/tree/main/src/capa/devices).
**Scope:** the families capa supports, the
[`DeviceAdapter`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/adapter.py)
contract every adapter implements, resource grouping, and the emission
shapes that downstream sinks key off.

---

## The four sibling libraries

capa never talks to instruments directly. Every device family is wrapped
by a dedicated *device library* — a separately maintained Python
package, async-first, with its own test suite, that owns the on-wire
protocol. capa hosts a thin **adapter** that maps that library's native
emissions onto capa's universal `DeviceAdapter` contract.

| Family | Library | Adapter (real) | Adapter (sim) |
|---|---|---|---|
| Watlow EZ-Zone controllers | [`watlowlib`](https://github.com/GraysonBellamy/watlowlib) | [`capa.devices.watlow`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/watlow.py) | `capa.devices.sim.watlow_sim` |
| Alicat mass-flow controllers | [`alicatlib`](https://github.com/GraysonBellamy/alicatlib) | [`capa.devices.alicat`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/alicat.py) | `capa.devices.sim.alicat_sim` |
| Sartorius lab balances | [`sartoriuslib`](https://github.com/GraysonBellamy/sartoriuslib) | [`capa.devices.sartorius`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sartorius.py) | `capa.devices.sim.sartorius_sim` |
| NI cDAQ / DAQmx | [`nidaqlib`](https://github.com/GraysonBellamy/nidaqlib) | [`capa.devices.nidaq`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/nidaq.py) | `capa.devices.sim.nidaq_polled_sim`, `nidaq_block_sim` |

Cameras are peers of devices but not subtypes — they live under
[`capa.devices.camera`](https://github.com/GraysonBellamy/capa/tree/main/src/capa/devices/camera).
The per-family pages ([Watlow](watlow.md), [Alicat](alicat.md),
[Sartorius](sartorius.md), [NI-DAQ](nidaq.md), [Webcam](cameras-webcam.md),
[FLIR](cameras-flir.md), [Simulators](simulators.md)) document the
adapter-specific `params` schema and operational quirks.

## The adapter contract

Every adapter — real or sim — implements the same Protocol:

```python
class DeviceAdapter(Protocol):
    name: str
    capabilities: frozenset[Capability]
    resource_id: str

    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def start(self, ctx: AdapterStartContext) -> None: ...
    async def stop(self) -> None: ...
    async def snapshot(self) -> DeviceSnapshot: ...
    def stream(self) -> AsyncIterable[DeviceEmission]: ...
    async def command(self, cmd: DeviceCommand) -> CommandResult: ...

    @property
    def expected_emission_rate_hz(self) -> float | None: ...
```

Two operations are separated deliberately:

- `open` / `close` — connection layer (open the serial port, negotiate
  protocol, query identity). Costs are paid once per **config**
  lifetime; the Sartorius "cold open" race lives here.
- `start` / `stop` — sampling layer. Arms hardware-clocked tasks,
  begins streaming. A transient I/O hiccup can be recovered with
  `stop` + `start` without renegotiating the device.

Both `open` and `close` are idempotent — calling them on an
already-open or already-closed adapter is a no-op, not an error. This
matters because the worker state machine relies on it during recovery.

### Capabilities

Adapters advertise feature flags via the
[`Capability`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/adapter.py)
`Flag` enum. The UI uses them to gate widgets ("show the ramp control
only if `HAS_RAMP`"); procedures use them via
`required_capabilities` to refuse runs that need flags an adapter
does not declare.

| Group | Flags |
|---|---|
| Control surface | `HAS_SETPOINT`, `HAS_RAMP`, `HAS_GAS_SELECT`, `HAS_VALVE_HOLD`, `WRITES_DIGITAL` |
| Acquisition | `READS_PROCESS_VAR`, `HARDWARE_CLOCKED`, `EMITS_BLOCKS`, `EMITS_STABILITY_FLAG` |
| Balance / mass | `HAS_TARE`, `HAS_ZERO`, `HAS_INTERNAL_CAL`, `HAS_TOTALIZER` |
| Discovery / lifecycle | `SUPPORTS_DISCOVERY`, `SUPPORTS_AUTO_RECONNECT`, `HAS_PARAMETER_CONFIG`, `HAS_DISPLAY_CONTROL` |

See the per-family pages for which flags each adapter actually
declares.

## Resource grouping (one worker per resource)

`resource_id` is the most consequential field on the protocol. Two
adapters that share a physical resource — an RS-485 multi-drop serial
bus, a DAQmx chassis, a single USB camera handle — **must** expose the
same `resource_id`. Two adapters that do not share a resource **must**
expose different ones.

The format is `<scheme>:<body>`:

| Scheme | Body | Example |
|---|---|---|
| `serial` | port name | `serial:COM4` |
| `daqmx` | chassis/device | `daqmx:cDAQ1` |
| `webcam` | OS index | `webcam:0` |
| `sim` | per-adapter-instance | `sim:watlow_sim-heater` |

[`build_workers`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/pool.py)
groups adapters by `resource_id` and constructs one
[`Worker`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/worker.py)
thread per group. Sharing a worker is the only safe way to share a
serial bus: the worker serializes I/O so two Alicat MFCs on the same
COM port never collide.

The `resource_id` is computed from constructor inputs without I/O —
the runtime must be able to read it *before* `open()` runs.

## What an adapter emits

`stream()` yields a tagged union called
[`DeviceEmission`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/records.py):

```python
DeviceEmission = SourceRecord | ChannelSample | DeviceEvent | DeviceSnapshot
```

Most adapters emit one `SourceRecord` per poll, followed by zero or
more `ChannelSample`s derived from that record. The split exists so
the bundle can keep both representations:

- **`SourceRecord`** — the library's native row, preserved without
  reshaping. Lands in `device_records/<adapter>.parquet`. This is the
  re-analysis path: if a future researcher needs a field the channel
  binding did not surface, the data is still there.
- **`ChannelSample`** — the normalized scientific stream. Lands in
  `scalars.parquet`. This is what plots, alarms, procedures, and
  cross-device analyses key off.

`DeviceEvent` (alarms, comm errors) goes to `events.sqlite`;
`DeviceSnapshot` (periodic health pings) goes to `status.sqlite`.
Neither flows through the main fan-out queue.

### Emission shapes

`SourceRecord.shape` is the layout tag that decides how the record
maps onto a `device_records/<adapter>.parquet` schema. The four
shapes match the four library row layouts:

| Shape | One row is… | Used by |
|---|---|---|
| `wide_row` | one poll, many fields | `alicatlib` `Sample`, `nidaqlib` polled `DaqReading` |
| `long_row` | one `(device, parameter, instance)` | `watlowlib` `Sample` |
| `single_value_row` | one balance reading | `sartoriuslib` `Sample` |
| `block` | a rectangular `(channels, samples_per_channel)` chunk | `nidaqlib` hardware-clocked `DaqBlock` |

Block records do not flow through the channel-samples sink at all —
`row` is empty and `block_ref` points at a sidecar (TDMS or in-bundle
Parquet block file). The kHz-rate hardware-clocked path is intentionally
separate from capa's normal 3–60 Hz channel pipeline.

Each shape maps onto one or more [channel binding
kinds](../configuration/channel-bindings.md):

| Shape | Compatible binding kinds |
|---|---|
| `wide_row` (Alicat) | `alicat_frame_field` |
| `wide_row` (NI polled) | `nidaq_reading_field` |
| `long_row` (Watlow) | `watlow_parameter` |
| `single_value_row` (Sartorius) | `sartorius_reading` |
| `block` (NI hardware-clocked) | `nidaq_block_channel` |

## Commands

`DeviceAdapter.command` is the generic write path. Every command
carries `issued_by`, `authorization_id`, and `confirmed_by` so the
audit trail can pin every device write to either a run-arm
authorization or an explicit operator confirmation. Adapters refuse
commands that lack one of those. See [Authorization
gates](../safety/authorization-gates.md) for the contract.

Concrete adapters also expose typed wrappers (e.g.
`WatlowAdapter.set_setpoint`) for IDE help and refactor safety; the
generic `command()` form exists so plugins that do not know the
concrete adapter type can still drive a device. Procedures should
issue commands through `ctx.dispatcher`, not directly on the adapter —
the dispatcher routes through the per-resource worker so commands
serialize correctly on shared buses.

## Lifecycle and the worker state machine

The worker that hosts an adapter walks through:

```
CLOSED → IDLE → ARMED → SAMPLING → DRAINING → IDLE
```

The conductor drives transitions across the thread seam through a
[bridge](../glossary.md#bridge). An adapter's `open` runs during
`CLOSED → IDLE`; `start` runs during `ARMED → SAMPLING`; `stop` runs
during `SAMPLING → DRAINING`; `close` runs only when the
[`WorkerPool`](../glossary.md#worker) tears down. Per-run actions
(arm / disarm) never re-`open` the device.

The full state-machine reference is in [Runtime
topology](../architecture/runtime-architecture.md) §4.1; this page
just establishes that the device contract is split for it.

## Per-channel `expected_emission_rate_hz`

The `expected_emission_rate_hz` property is a hint to the engine
queue sizer. Adapters compute it after `configure_channels` runs —
typically `(poll rate Hz) × (1 + bound_channel_count)` for poll-based
adapters, or `(block size / block period)` for hardware-clocked
adapters. Returning `None` is acceptable; the engine falls back to a
conservative default and logs the fallback so the operator knows the
producer queue was sized blind.

## See also

- [Hardware TOML](../configuration/hardware-toml.md) — how devices and
  channels are declared.
- [Channel bindings](../configuration/channel-bindings.md) — the
  selector-into-emission contract that bridges adapter and channel
  layers.
- [Runtime topology](../architecture/runtime-architecture.md) — where
  Workers fit in the thread layout.
- [Writing a device adapter](../extending/writing-a-device-adapter.md) —
  tutorial for adding a new family.
