---
description: capa channel binding reference — six `SourceBinding` variants pulling from Watlow, Alicat, Sartorius, NI-DAQ (polled and hardware-clocked), derived channels.
---

# Channel bindings

**Audience:** config authors wiring channels to device parameters.
**Scope:** every binding variant in `SourceBinding`, how records become
samples, and the calibration / decimation pipeline that sits between
them.

---

## What a binding does

A channel does not store data. It declares **where its value comes
from**: a specific field inside a specific device's emission. The
binding is the selector that pulls one number out of one library row.

The shape of that row depends on the library:

| Library | Emission shape | Selector you need |
|---|---|---|
| `watlowlib` | one long row per `(device, parameter, instance)` | parameter name + instance |
| `alicatlib` | one wide row per poll (many fields) | field name |
| `sartoriuslib` | one single-value row per balance reading | field name (defaults to `value`) |
| `nidaqlib` (polled) | one wide row per read (channel ↦ value) | task name + field name |
| `nidaqlib` (hardware-clocked) | rectangular `(channels, samples)` block | task name + channel name |
| n/a (derived) | computed from other channels | dependency list + expression id |

See [Devices overview — emission
shapes](../devices/overview.md#emission-shapes) for the picture of how
each library lays out its rows.

## The six binding kinds

Every binding is one of six discriminated variants. The `source` field
picks which model applies; Pydantic refuses any other value at load
time.

```toml
[channels.source]
source = "watlow_parameter"   # ← the discriminator
device = "heater"
parameter = "process_value"
instance = 1
```

### `watlow_parameter`

Selects one parameter on one loop of a Watlow EZ-Zone controller.
Watlow rows are long-format, so each `(device, parameter, instance)`
triple identifies exactly one value.

| Field | Type | Required | Notes |
|---|---|:-:|---|
| `device` | str | yes | The Watlow device name from `[[devices]]`. |
| `parameter` | str | yes | Canonical name from `watlowlib.registry.parameters` — `"process_value"`, `"setpoint"`, `"power"`, … |
| `instance` | int | no | 1-indexed loop number. Defaults to `1`. |

```toml
[channels.source]
source = "watlow_parameter"
device = "heater"
parameter = "setpoint"
instance = 1
```

### `alicat_frame_field`

Selects one field from an Alicat MFC's data-frame row. The library
exposes fields under canonical underscored names —
`Mass_Flow`, `Abs_Press`, `Mass_Flow_Setpt` — **not** the wire-format
names with spaces. Use the underscored form.

| Field | Type | Required | Notes |
|---|---|:-:|---|
| `device` | str | yes | The Alicat device name. |
| `field` | str | yes | Underscored canonical field name. |

```toml
[channels.source]
source = "alicat_frame_field"
device = "purge_mfc"
field = "Mass_Flow"
```

### `sartorius_reading`

Selects one field from a Sartorius balance reading. The library row
carries `value`, `unit`, `stable` / `overload` / `underload` flags,
`raw` bytes, `protocol`, plus error fields. Default is the numeric
value; pick a flag when you want it as its own channel.

| Field | Type | Required | Notes |
|---|---|:-:|---|
| `device` | str | yes | The Sartorius device name. |
| `field` | str | no | Reading field; defaults to `"value"`. Common choices: `"value"`, `"stable"`, `"overload"`. |

```toml
[channels.source]
source = "sartorius_reading"
device = "balance"
field = "value"
```

### `nidaq_reading_field`

Selects one field from a polled NI-DAQ `DaqReading`. Polled readings
are wide rows keyed by channel display name (the
`ChannelSpec.display_name` from the underlying `TaskSpec`).

| Field | Type | Required | Notes |
|---|---|:-:|---|
| `device` | str | yes | The NI-DAQ device name (the chassis-level adapter, not the physical channel). |
| `task` | str | yes | `TaskSpec.name` from the adapter's params. |
| `field` | str | yes | Channel display name. |

```toml
[channels.source]
source = "nidaq_reading_field"
device = "cdaq1"
task = "default_task"
field = "TC_top_1"
```

### `nidaq_block_channel`

Selects one channel within a hardware-clocked NI-DAQ `DaqBlock`. The
block is rectangular `(channels, samples_per_channel)`; the binding
picks one row.

| Field | Type | Required | Notes |
|---|---|:-:|---|
| `device` | str | yes | The NI-DAQ device name. |
| `task` | str | yes | `TaskSpec.name`. |
| `channel` | str | yes | Channel name within the block task. |

```toml
[channels.source]
source = "nidaq_block_channel"
device = "cdaq1"
task = "fast_task"
channel = "ai0_voltage"
```

Hardware-clocked blocks stay rectangular until either the adapter
derives channel samples at capa's normal 3–60 Hz class or hands the
byte path to TDMS for kHz-rate capture. The block path is
intentionally separate from the rest of the channel pipeline.

### `derived`

A channel computed from other channels rather than read from a
device. The binding records the dependency list so a future
derivation registry can topologically sort derived channels and
surface circular-dep errors at config-load.

| Field | Type | Required | Notes |
|---|---|:-:|---|
| `expression` | str | yes | Identifier of a registered derivation (e.g. `"oxygen_depletion"`). |
| `inputs` | array of str | yes | Channel names this derivation reads from. |

```toml
[channels.source]
source = "derived"
expression = "oxygen_depletion"
inputs = ["o2_in", "o2_out", "flow_in"]
```

The derivation evaluator is not wired yet; the schema ships ahead of
the runtime so existing configs stay forward-compatible.

## From records to samples

Every adapter emits a [`SourceRecord`](../devices/overview.md#what-an-adapter-emits)
per poll. The channel registry walks every declared
`ChannelSpec.source` and, for each match, derives one
`ChannelSample` from the record. The conductor's fan-out then writes:

- The `SourceRecord` (verbatim) to `device_records/<adapter>.parquet`.
- Each derived `ChannelSample` to `scalars.parquet`.

The two outputs are joined by `ChannelSample.source_record_id` — a
back-pointer to `SourceRecord.record_id` — so a downstream analyst
can recover the original library row even when the binding promoted
only a single field.

For wide rows (Alicat, NI polled), the same `SourceRecord` can produce
multiple `ChannelSample`s if multiple channels bind into it. For long
rows (Watlow), one row typically produces zero or one sample because
each row has a single parameter value. For blocks, the binding
produces one sample per (channel, sample-time) pair without going
through the normal `SourceRecord` path.

## The calibration layer

A calibration sits between the adapter's raw value and the channel's
final value. It transforms the raw float and, when declared, also
propagates an uncertainty:

```
adapter raw value  ──►  calibration.evaluate(raw)  ──►  ChannelSample.value
       │                                                       │
       └──► (preserved as ChannelSample.raw when keep_raw=True) │
                                                                ▼
                                          ChannelSample.unit = derived_unit
```

Five calibration kinds are defined in
[`capa.channels.calibration`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/channels/calibration.py):

| Kind | When to use |
|---|---|
| `identity` | No transformation. Default. |
| `linear` | Two-point linear fit. `value = slope * raw + intercept`. |
| `polynomial` | Higher-order polynomial fit. |
| `lookup` | Exact-match table (e.g. discrete gas-density curve). |
| `piecewise_linear` | Tabular calibration with linear interpolation between knots. |

Every variant validates dimensional compatibility: `input_unit` must
be compatible with the channel's `unit`, and `output_unit` with
`derived_unit`. Every variant also carries an `UncertaintySpec` (or
an explicit `None` declaring "unmeasured" — never silent zero).

The on-disk format and the `CalibrationSet` snapshot are documented
in [Calibrations on disk](calibrations.md).

## `keep_raw` mode

When a channel sets `keep_raw = true`, the pre-calibration value lands
in `ChannelSample.raw` alongside the calibrated value. Useful when a
researcher wants the raw counts for re-fitting later — the normal
output remains the calibrated value, the raw is just attached.

## Sampling rate hints and decimation

Two related knobs control how often samples flow:

- **`sample_rate_hz`** — producer rate hint. Optional; leave unset
  for event-driven channels (cameras) or channels whose cadence is
  set by the adapter rather than declared here.
- **`decimate_to_hz`** — **plot-only** decimation. Default 60 Hz,
  intentionally above the fastest producer (50 Hz Sartorius) so the
  ring buffer keeps every sample. Set lower only when a channel
  produces faster than the buffer can usefully store. **Disk capture
  is always at the native producer rate** — `decimate_to_hz` does
  not affect `scalars.parquet`.

## Channel bindings and required channel groups

Domain profiles declare `required_channel_groups` against
`ChannelSpec.metadata["capa_group"]`. The CAPA-pyrolysis profile
requires five groups:

| Group | Acceptable kinds | Min count |
|---|---|---|
| `heater_setpoint` | `setpoint`, `process_var` | 1 |
| `heater_pv` | `process_var` | 1 |
| `sample_temperature` | `tc`, `process_var` | 1 |
| `mass` | `mass` | 1 |
| `purge_gas_flow` | `mfc_flow`, `setpoint` | 1 |

Channels declare their group via `metadata`:

```toml
[channels.metadata]
capa_group = "heater_pv"
```

The profile preflight refuses to arm when a required group is missing
or undersized. See [CAPA profile fields](capa-profile.md) for the
full list and the per-group preflight checks.

## Binding-policy ordering

The Setup-tab combobox keeps every binding kind available, but
reorders them by likely match for the channel's `kind`. The table
lives in
[`capa.config.binding_policy`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/config/binding_policy.py):

| Channel kind | Preferred binding order |
|---|---|
| `tc` | `nidaq_reading_field`, `watlow_parameter`, `nidaq_block_channel` |
| `process_var` | `watlow_parameter`, `nidaq_reading_field` |
| `setpoint` | `watlow_parameter`, `alicat_frame_field` |
| `mass` | `sartorius_reading` |
| `mfc_flow` | `alicat_frame_field` |
| `analog_in` | `nidaq_reading_field`, `nidaq_block_channel` |
| `derived` | `derived` |

Variants outside a row's tuple still appear, just appended after the
preferred ones. The policy applies to UI ordering only — validation
itself doesn't restrict bindings by `kind`.

## Validation rules

- **Missing binding device.** Every binding's `device` must reference
  a `[[devices]]` entry. The derived variant is exempt.
- **Duplicate channel names.** Channel names are unique within a
  hardware profile.
- **Unit mismatch.** `calibration.input_unit` must be dimensionally
  compatible with the channel's `unit`, and `calibration.output_unit`
  with `derived_unit` (or `unit` when no `derived_unit` is declared).
- **Family mismatch.** When the adapter's
  `AdapterDescriptor.supported_binding_sources` is declared, only
  those binding kinds may target the adapter. Plugin adapters with no
  descriptor accept any binding.

Validation surfaces as [`Problem`](validation-and-problems.md)
records — non-blocking for warnings, blocking for hard errors.

## See also

- [Devices overview](../devices/overview.md) — emission shapes and
  per-family layouts.
- [Hardware TOML](hardware-toml.md) — the full channel entry shape.
- [Calibrations on disk](calibrations.md) — calibration kinds, fit
  metadata, uncertainty propagation.
- [Channel samples parquet](../bundles/parquet-channel-samples.md) —
  the on-disk schema bindings produce.
