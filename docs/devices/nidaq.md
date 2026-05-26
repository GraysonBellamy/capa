---
description: NI-DAQmx acquisition in capa via nidaqlib — polled software-timed vs hardware-clocked block mode, thermocouple and voltage channels, blocks in the bundle.
---

# NI-DAQ

**Audience:** config authors using an NI-DAQmx chassis (analog in, digital in, counters) — typically thermocouple and voltage acquisition for sample temperatures and auxiliary sensors.
**Scope:** capa's [`nidaqlib`](https://github.com/GraysonBellamy/nidaqlib) adapter — the two operating modes (**polled** software-timed and **hardware-clocked block**), `[devices.params]` fields, channel kinds, and how block-mode samples land in the bundle.

---

## At a glance

| | Polled mode | Block mode |
|---|---|---|
| Adapter id (in `SourceRecord.adapter`) | `nidaq_polled` | `nidaq_block` |
| When | `timing is None` or `timing.mode = "on_demand"` | `timing.mode in {finite, continuous}` |
| Rate | 3–60 Hz software-timed | NI onboard sample clock (kHz feasible) |
| Channel binding | [`nidaq_reading_field`](../configuration/channel-bindings.md) | [`nidaq_block_channel`](../configuration/channel-bindings.md) |
| Emission shape | `wide_row` — one `DaqReading` per poll | `block` — one `DaqBlock` per emission |
| Goes to `scalars.parquet` | yes (one row per binding per tick) | yes (unrolled per `(channel, sample)`), bounded by `max_samples_per_block_unroll` |

Both modes share one adapter (`capa.devices.nidaq`), one params model
(`NIDAQAdapterParams`), and one descriptor. The mode is decided by the
presence of a `[devices.params.timing]` block. Both produce the same
`resource_id` (`daqmx:<chassis>`) so multiple tasks against the same
chassis share a worker.

Sibling library: [`nidaqlib`](https://github.com/GraysonBellamy/nidaqlib).
Real adapter: [`capa.devices.nidaq`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/nidaq.py).
Sim adapters: [`capa.devices.sim.nidaq_polled_sim`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/nidaq_polled_sim.py),
[`capa.devices.sim.nidaq_block_sim`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/nidaq_block_sim.py).

## Supported hardware

NI-DAQmx chassis and modules supported by
[`nidaqlib`](https://github.com/GraysonBellamy/nidaqlib) and the NI-DAQmx
driver runtime. Acquisition surface (AI, AO, DI, DO, CI, CO),
thermocouple types, voltage ranges, and timing-engine capabilities are
all per-module — see the NI documentation for the module's spec sheet.

## Configuration

```toml title="configs/hardware/nidaq_real.toml (excerpt)"
[[devices]]
name = "cdaq1"
adapter = "capa.devices.nidaq"

[devices.params]
task_name = "default_task"
rate_hz = 5.0
snapshot_period_s = 30.0
auto_reconnect = false
overflow = "block"

# K-type thermocouple, NI 9214 in cDAQ1Mod1, ai0
[[devices.params.channels]]
kind = "thermocouple"
physical_channel = "cDAQ1Mod1/ai0"
name = "TC_top_1"
thermocouple_type = "K"
min_val = 0.0
max_val = 1000.0
cjc_source = "BUILT_IN"
units = "DEG_C"
adc_timing_mode = "HIGH_RESOLUTION"
auto_zero_mode = "ONCE"
```

| Key | Default | Notes |
|---|---|---|
| `task_name` | required | Used as `TaskSpec.name`, the `task` field on emitted `DaqReading`s, and the join key in `NIDAQReadingField.task` / `NIDAQBlockChannel.task`. |
| `channels` | required (non-empty) | Per-channel typed configs — see [Channel kinds](#channel-kinds). |
| `timing` | `None` (polled) | Block-mode `[devices.params.timing]` block — see [Block mode](#hardware-clocked-block-mode). |
| `rate_hz` | `10.0` (gt 0, le 1000) | Polled-mode polling cadence. Ignored in block mode. Capped at 1 kHz — anything higher belongs in block mode. |
| `snapshot_period_s` | `30.0` | Health-ping cadence. |
| `auto_reconnect` | `false` | Polled mode only. Default off because most NI errors are *not* transient (driver fault, module unplug) and a failed run should fail visibly. When true, transients surface via the per-tick `error` field. |
| `overflow` | `"block"` | `"block"` or `"drop_newest"`. |
| `max_samples_per_block_unroll` | `10_000` | Block-mode guardrail. See [Block mode](#hardware-clocked-block-mode). |

## Channel kinds

`[[devices.params.channels]]` entries are typed by the `kind`
discriminator. Each kind maps to a Pydantic model in
[`capa.devices.nidaq_channels`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/nidaq_channels.py).
NI enum-typed fields take the **canonical UPPER_SNAKE name only** —
typos fail at config-load time with the list of valid names.

| `kind` | Typed validator | Notable fields |
|---|---|---|
| `thermocouple` | `NIDAQThermocoupleConfig` | `thermocouple_type` (`"K"`, `"J"`, …), `cjc_source` (`"BUILT_IN"` / `"CONSTANT_VAL"` / …), `units` (`"DEG_C"` / `"DEG_F"` / `"K"` / `"DEG_R"`), `adc_timing_mode`, `auto_zero_mode`. |
| `ai_voltage` | `NIDAQVoltageConfig` | `min_val`, `max_val`, `terminal_config`, `units`. |
| `digital_input`, `digital_output`, `ao_voltage`, counter family | `NIDAQRawChannelConfig` | Pass-through dict forwarded to `nidaqlib.channels.ChannelSpec.from_dict` unchanged. Add a typed model when a config starts using one. |

Names are case-sensitive on purpose: `"k"` is Kelvin (temperature unit)
and `"K"` is K-type (thermocouple alloy) — a forgiving validator would
let a typo parse as something the operator didn't intend.

## Polled mode

Default for the cone rig. Software-timed polling at `rate_hz` produces
one [`DaqReading`](https://github.com/GraysonBellamy/nidaqlib) per
tick, which capa turns into one wide-row `SourceRecord` plus one
`ChannelSample` per matching `NIDAQReadingField` binding.

Channels bind by **task name + display name**:

```toml
[[channels]]
name = "TC_sample_top"
kind = "tc"
unit = "degC"
plot_group = "temperatures"

[channels.source]
source = "nidaq_reading_field"
device = "cdaq1"
task = "default_task"
field = "TC_top_1"
```

The `field` matches the channel's `name` in `[[devices.params.channels]]`.
A `DaqReading` carries one column per channel plus a parallel
`<channel>_unit` column for unit metadata.

The bundle path is the normal one: native `DaqReading` rows land in
`device_records/nidaq.parquet`; derived `ChannelSample`s land in
`scalars.parquet` keyed by channel name.

## Hardware-clocked block mode

`timing.mode in {"finite", "continuous"}` engages the NI onboard
sample clock. The adapter drives
[`nidaqlib.streaming.block.record`](https://github.com/GraysonBellamy/nidaqlib)
and emits, per [`DaqBlock`](https://github.com/GraysonBellamy/nidaqlib),
one `SourceRecord` with `shape="block"` plus one `ChannelSample` per
`(channel, sample)` for every `NIDAQBlockChannel` binding.

```toml
[devices.params]
task_name = "fast_ai"
overflow = "block"

[devices.params.timing]
rate_hz = 1000.0
mode = "continuous"        # "finite" | "continuous" | "on_demand"
samples_per_channel = 1000
active_edge = "rising"

[[devices.params.channels]]
kind = "ai_voltage"
physical_channel = "cDAQ1Mod2/ai0"
name = "load_cell"
min_val = -10.0
max_val = 10.0
terminal_config = "DIFFERENTIAL"
```

Per-sample timestamps are computed at unroll time:

```
sample_time = task_started_at + (first_sample_index + k) / sample_rate_hz
```

— so the wall-clock time of every sample is deterministic from the
block header without requiring a per-sample timestamp on the wire.

### The `max_samples_per_block_unroll` guardrail

Block mode unrolls every block into per-sample `ChannelSample`s so
they reach the normal channel-pipeline sinks. That's fine at low rates
(≤ a few hundred Hz × a few channels) but **explodes at kHz** — a
1 kHz × 16-channel × 1-second block produces 16 000 channel samples.

The default cap of `10_000` covers the practical low-rate envelope
(500 Hz × 20 ch × 1-second blocks) and trips at `NIDAQAdapter.open()`
when the resolved chunk size exceeds it, pointing the operator at the
rectangular-block sidecar / TDMS path — currently future work; see
[`nidaq.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/nidaq.py)
module docstring.

Until that lands: keep block-mode acquisitions in the low-rate
envelope, or set `timing.mode = "on_demand"` to fall back to polled.

## Capability flags

Always declared:

| Flag | Notes |
|---|---|
| `READS_PROCESS_VAR` | Plot binding for PV channels. |
| `SUPPORTS_DISCOVERY` | `find_devices` enumerates the local NI system. |

Added when `is_block_mode()`:

| Flag | Notes |
|---|---|
| `HARDWARE_CLOCKED` | Drives the engine queue sizer. |
| `EMITS_BLOCKS` | UI gating for block-shaped subscribers. |

Added when `auto_reconnect=True`:

| Flag | Notes |
|---|---|
| `SUPPORTS_AUTO_RECONNECT` | Polled-mode only. |

## Commands

The NI adapter is acquisition-only — there is no command surface to
gate. AO writes (`ao_voltage` channels) flow through the same NI task
on the streaming path; capa does not currently expose a generic
`DeviceCommand.kind` for analog writes.

## Discovery and handshake

`discover()` takes no arguments — it enumerates the local NI system
via [`nidaqlib.find_devices`](https://github.com/GraysonBellamy/nidaqlib)
and returns one row per physical NI device with full inventories
(`ai_channels`, `ao_channels`, `di_lines`, `do_lines`, `ci_channels`,
`co_channels`). Driver / runtime failures surface as `ok=False` rows
from the library and are filtered out, so the hook is safe to call
from an idle CLI.

`handshake(params)` does *not* try to start the task — it parses the
config, calls `find_devices` to confirm the physical channels exist on
the local system, and returns a one-line summary listing the channel
count and the NI device families involved. The full task only arms
when a run starts.

See [Discovery](discovery.md) for the cross-cutting UX.

## Quirks

### `cDAQ<n>` chassis vs single-board cards

Modular cDAQ chassis show up in the device inventory with a chassis
name (`cDAQ1`); modules carry channel prefixes like
`cDAQ1Mod1/ai0`. Single-board PCIe and USB-DAQ cards report `chassis =
None` in `NIDAQDeviceInfo` and use bare channel names. The
`resource_id` derivation uses the chassis when present; on single-
board cards the worker grouping happens by device name.

### CJC is a per-module concern

`cjc_source = "BUILT_IN"` works on modules with built-in CJC (e.g. NI
9214, NI 9213). Modules without built-in CJC need an external CJC
source declared (`CONSTANT_VAL` plus a value, or a dedicated CJC
channel). Wrong CJC config silently biases every thermocouple reading
— the symptom looks like a calibration drift.

### Validation is strict — typos fail at load

Enum-typed fields take the canonical UPPER_SNAKE name only. A typo
(`auto_zero_mode = "Once"`) fails with the full list of valid names at
config-parse time, before any device is opened. This is on purpose —
silent acceptance of mis-spelled enum names is the worst possible
failure mode for an acquisition system.

### Sim equivalents

[`nidaq_polled_sim`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/nidaq_polled_sim.py)
takes signals keyed by channel display name (`{"TC_top_1": {...},
"AI0": {...}}`) plus a `units` dict, and emits one `DaqReading` per
tick at `tick_period_s`. Capabilities: `HARDWARE_CLOCKED |
SUPPORTS_DISCOVERY` (the sim is honest about producing block-shape-
adjacent output).

[`nidaq_block_sim`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/nidaq_block_sim.py)
takes `sample_rate_hz`, `block_size`, and signals evaluated at every
block sample; it emits `SourceRecord(shape="block")` so the bundle's
block plumbing exercises. See the [block sim docstring](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/nidaq_block_sim.py) for the rectangular-payload caveat.

## See also

- [Devices overview](overview.md) — adapter contract, capability enum, resource grouping.
- [Hardware TOML](../configuration/hardware-toml.md) — how `[[devices]]` and `[[devices.params.channels]]` are parsed.
- [Channel bindings](../configuration/channel-bindings.md) — `nidaq_reading_field` and `nidaq_block_channel`.
- [Discovery](discovery.md) — cross-cutting Setup-tab and CLI behavior.
- [nidaqlib docs](https://github.com/GraysonBellamy/nidaqlib/tree/main/docs) — task spec, streaming, block API.
- The NI-DAQmx Python driver reference for canonical enum names.
