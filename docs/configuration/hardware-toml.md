# Hardware TOML

**Audience:** config authors describing the physical rig.
**Scope:** every field of a hardware-profile TOML — top-level keys,
`[[devices]]`, `[[channels]]`, `[[cameras]]`, and the cross-validation
rules. Per-family `params` schemas are summarised here and linked out
to the [device pages](../devices/overview.md).

---

## Top-level shape

A hardware TOML declares one [`HardwareProfile`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/config.py):

```toml
name = "sim_capa"

[[devices]]
# ... one entry per physical instrument (or sim equivalent)

[[channels]]
# ... one entry per named scientific signal

[[cameras]]
# ... one entry per camera (visible or IR)
```

Only `name` is required at the top level. `devices`, `channels`, and
`cameras` are all optional (a hardware profile with zero devices is
legal — it makes sense for [Batch](../procedures/what-is-a-procedure.md#batch)
parent runs).

`extra="forbid"` is enforced on every model: an unrecognised key is a
validation error, not a warning. Typos surface immediately.

The shipped CAPA simulator profile is a good reference:

--8<-- "_snippets/sim_capa_hardware.toml.md"

## `[[devices]]`

One entry per physical instrument or sim equivalent. Each entry maps
1:1 onto a [`DeviceConfig`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/config.py):

| Field | Type | Required | Notes |
|---|---|:-:|---|
| `name` | str | yes | Adapter-assigned device name. Used as the `device:` key in channel bindings. |
| `adapter` | str | yes | Module path or registered id (see below). |
| `params` | table | no | Adapter-specific parameters; parsed by the adapter, not here. |
| `resource_id` | str | no | Override for the adapter's auto-derived [resource id](../devices/overview.md#resource-grouping-one-worker-per-resource). |
| `on_failure` | str | no | `"abort"` (default) or `"warn"`. Advisory until the conductor wires per-device failure policy. |

The `adapter` value is one of:

| Family | Real adapter id | Sim adapter id |
|---|---|---|
| Watlow | `capa.devices.watlow` | `capa.devices.sim.watlow_sim` |
| Alicat | `capa.devices.alicat` | `capa.devices.sim.alicat_sim` |
| Sartorius | `capa.devices.sartorius` | `capa.devices.sim.sartorius_sim` |
| NI-DAQ (polled) | `capa.devices.nidaq` | `capa.devices.sim.nidaq_polled_sim` |
| NI-DAQ (hardware-clocked) | `capa.devices.nidaq` | `capa.devices.sim.nidaq_block_sim` |
| Plugin adapter descriptors | resolved via `capa.adapters` / `capa.cameras` descriptor entry points (Setup/discovery) or dotted module-path adapter ids (runtime-safe path) | — |

### `params` per family

Each adapter constructs from its own Pydantic model — the outer
hardware profile only validates that `name` and `adapter` are
coherent. Field-by-field references live on the per-family pages:

- [Watlow](../devices/watlow.md) — serial port, baud rate, EZ-Zone
  address, loop count.
- [Alicat](../devices/alicat.md) — serial port, unit id, allowed
  gases, polling rate.
- [Sartorius](../devices/sartorius.md) — serial port, model,
  decimation, stability window.
- [NI-DAQ](../devices/nidaq.md) — chassis id, task definitions,
  channel list, sample mode (polled vs block).
- [Webcam](../devices/cameras-webcam.md) — USB index, codec,
  resolution.
- [FLIR](../devices/cameras-flir.md) — serial, palette, radiometric
  kit defaults.
- [Simulators](../devices/simulators.md) — declarative signal
  schemas (`kind = "constant" | "ramp" | "step" | "sine"`).

### Resource sharing

Two adapters that share a physical resource (an RS-485 multi-drop
bus, a DAQmx chassis) need the same `resource_id`. The default is
derived from `params` (e.g. `serial:COM6` from `params.port = "COM6"`),
so the explicit override is only needed when the auto-derivation
disagrees with reality. See [resource grouping](../devices/overview.md#resource-grouping-one-worker-per-resource)
for the full rule.

## `[[channels]]`

One entry per named scientific signal. Each entry maps onto a
[`ChannelSpec`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/channels/spec.py):

| Field | Type | Required | Notes |
|---|---|:-:|---|
| `name` | str | yes | Stable run-local identifier; UI / sinks / plots key off it. |
| `kind` | str | yes | `ChannelKind` value (see below). |
| `unit` | str | yes | Pre-calibration unit on the wire (e.g. `"V"`, `"K"`, `"kPa"`). |
| `derived_unit` | str | no | Post-calibration unit. `None` = same as `unit`. |
| `source` | table | yes | The [`SourceBinding`](channel-bindings.md) — selector into one device's emission shape. |
| `calibration` | table | no | Per-channel calibration; defaults to identity. |
| `keep_raw` | bool | no | When `True`, both pre- and post-calibration values land in `scalars.parquet`. |
| `sample_rate_hz` | float | no | Producer sampling rate hint. |
| `plot_group` | str | no | `"temperatures"`, `"flows"`, `"mass"` — UI grouping. |
| `alarms` | array of tables | no | Declarative alarm bands. |
| `sinks` | array of strings | no | Which named sinks receive the channel. Defaults to `["scalars"]`. |
| `decimate_to_hz` | float | no | Plot-only decimation (disk capture stays at native rate). Default 60. |
| `metadata` | table | no | Free-form string-keyed dict (CAPA-group tags live here). |

### `kind` values

The [`ChannelKind`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/channels/spec.py)
enum tags each channel's high-level role. The UI uses it to pick
widgets and axes; the binding-policy table uses it to reorder the
binding-kind combobox by likely match.

| Kind | Meaning |
|---|---|
| `analog_in` | Voltage / current / arbitrary scalar from a DAQ AI channel |
| `tc` | Thermocouple temperature |
| `ao` | Commanded analog voltage |
| `do` | Commanded digital line |
| `counter` | Edge / pulse counter |
| `process_var` | Controller measured process variable (Watlow PV) |
| `setpoint` | Controller commanded setpoint (Watlow SP, MFC flow setpoint) |
| `mass` | Balance reading |
| `mfc_flow` | Mass flow controller flow value |
| `video_visible` | Visible-camera frame stream (no scalar value column) |
| `video_ir` | IR-camera frame stream |
| `derived` | Computed from other channels (future derivation registry) |

### `source` — the binding

Every channel must declare exactly one binding under `[channels.source]`.
The `source` discriminator selects which variant applies:

```toml
[[channels]]
name = "heater.pv"
kind = "process_var"
unit = "degC"
[channels.source]
source = "watlow_parameter"
device = "heater"
parameter = "process_value"
instance = 1
```

The six binding kinds — `watlow_parameter`, `alicat_frame_field`,
`sartorius_reading`, `nidaq_reading_field`, `nidaq_block_channel`,
`derived` — are documented field-by-field on the [Channel
bindings](channel-bindings.md) page.

### `calibration` — per-channel transform

Calibrations live between the adapter's raw sample and the channel's
final value. The default is `Identity` — no transformation, units
just pass through. A two-point linear calibration looks like:

```toml
[channels.calibration]
kind = "linear"
input_unit = "V"
output_unit = "K"
slope = 100.0
intercept = 273.15
```

Calibration kinds: `identity`, `linear`, `polynomial`, `lookup`,
`piecewise_linear`. Schema reference: [Calibrations on
disk](calibrations.md). The model validator cross-checks
dimensional compatibility: `calibration.input_unit` must be
compatible with the channel's `unit`, and `calibration.output_unit`
with `derived_unit` (or `unit` when `derived_unit` is unset).

### `alarms`

One alarm band is one declarative threshold:

```toml
[[channels.alarms]]
id = "heater_overtemp"
op = ">"
threshold = 950.0
action = "safe_shutdown"
debounce_s = 0.5
message = "Heater PV above ceiling"
```

Actions: `warn`, `pause_method`, `abort_run`, `safe_shutdown`.
`debounce_s` is the continuous-true window before firing — catches
single-sample spikes. The current runtime parses and preserves this
schema; the dedicated safety-rule evaluator is planned but not active.

## `[[cameras]]`

Cameras are peers of devices but use their own
[`CameraSpec`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/camera/base.py)
model because they own their output container instead of emitting
`ChannelSample`s.

| Field | Type | Required | Notes |
|---|---|:-:|---|
| `name` | str | yes | Used as the manifest entry id and the frame-index parquet basename. |
| `adapter` | str | yes | Module path, or a `capa.cameras` descriptor id after the registry has been loaded by Setup/discovery (e.g. `flir_ir`). For unattended headless runs, prefer a dotted module path. |
| `kind` | str | yes | `"visible"` or `"ir"`. Picks file extension and UI grouping. |
| `model_hint` | str | no | Preferred model (`"FLIR E85"`, `"Logitech C920"`). |
| `serial` | str | no | Exact-match selector. |
| `output_root` | str | no | External path override (still records the in-bundle frame-index sidecar). |
| `on_failure` | str | no | `"warn"`, `"abort_run"`, `"safe_shutdown"`. |
| `estimated_bps` | int | no | Bytes-per-second hint for the disk-space preflight. Default 4 MB/s. |
| `params` | table | no | Adapter-specific (codec, palette, resolution). |

Camera and device names share one namespace (the cross-validator
refuses overlaps), so a webcam called `cam0` collides with a device
called `cam0`.

### Frame-index sidecar

Every camera writes `<name>.frames.parquet` alongside the container,
even when `output_root` points outside the bundle. Without it, a
downstream analyst cannot align frames to channel samples better than
container-level timestamp resolution. See [Video](../bundles/video.md).

## Cross-validation rules

Loading a hardware TOML triggers `_check_unique` and a few referential
checks. They fail loudly:

- Duplicate device names.
- Duplicate channel names.
- Duplicate camera names.
- A camera and a device sharing a name.
- A channel binding referencing a device not declared under
  `[[devices]]` (derived channels are exempt — they have no `device`).

For the broader validation pipeline (procedure preflight, domain
profile checks), see [Validation and
problems](validation-and-problems.md).

## See also

- [Channel bindings](channel-bindings.md) — every binding kind in
  field detail.
- [Calibrations on disk](calibrations.md) — calibration kinds and
  on-disk format.
- [Devices overview](../devices/overview.md) — adapter contract and
  resource grouping.
- The shipped real configs under
  [`configs/hardware/`](https://github.com/GraysonBellamy/capa/tree/main/configs/hardware) —
  copy-paste starting points.
