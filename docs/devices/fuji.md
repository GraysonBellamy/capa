---
description: Recording CO2, CO and O2 from a Fuji ZP-series gas analyzer (also sold as the CAI ZPA) in capa via fujilib — channel map, validity states, settings writes, guarded zero and span.
---

# Fuji gas analyzer

**Audience:** operators recording gas concentrations from a Fuji ZP-series NDIR analyzer, and whoever calibrates it.
**Scope:** capa's [`fujilib`](https://github.com/GraysonBellamy/fujilib) adapter — `[devices.params]` fields, wide-row emission, the validity state on every reading, the settings and calibration command surface, and what the readings can and cannot be used for.

---

## At a glance

| | |
|---|---|
| Adapter id | `capa.devices.fuji` |
| Sibling library | [`fujilib`](https://github.com/GraysonBellamy/fujilib) |
| Real adapter | [`capa.devices.fuji`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/fuji.py) |
| Sim adapter | [`capa.devices.sim.fuji_sim`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/fuji_sim.py) |
| Resource scheme | `serial:<port>` — one analyzer per port |
| Channel binding | [`fuji_channel`](../configuration/channel-bindings.md#fuji_channel) |
| Emission shape | `wide_row` — one row per poll, every channel in it |
| Default poll rate | 1 Hz |
| Declarable settings | Per gas: response time, range method, range; output hold and hold mode — see [Device settings](../configuration/device-settings.md) |

## Supported hardware

Fuji Electric ZP-series NDIR gas analyzers over Modbus RTU (RS-485, fixed
38400 8-N-1). The ZPA is the model capa has been run against; it is also sold
as the CAI (ENVEA) ZPA. Which models share the register map, and what has
been checked on hardware, is owned by
[fujilib](https://fujilib.graysonbellamy.dev/).

Two limits belong on this page rather than in a footnote:

!!! warning "Not validated for oxygen-consumption calorimetry"

    The O2 value over Modbus is the analyzer's display value. It arrives in
    steps of 0.01 vol% and has passed the analyzer's response-time filter.
    It has not been compared against the analyzer's analog output, and it is
    **not validated for heat-release-rate calculation**. The cone profile's
    oxygen group still takes an analog input only; a `fuji_channel` O2
    channel is recorded beside it, not in place of it.

!!! warning "The analyzer does not say when it is warming up"

    After power-on the readings are far off for about a minute — CO2 has
    been seen at more than twice its range — and no status flag says so.
    Follow the warm-up time in the analyzer's manual before trusting a run.
    capa marks readings `settling` only where it can know something
    happened: see [Warm-up and `settling`](#warm-up-and-settling).

## Configuration

```toml title="configs/hardware/fuji_real.toml (excerpt)"
[[devices]]
name = "analyzer"
adapter = "capa.devices.fuji"

[devices.params]
port = "COM8"
address = 1
rate_hz = 1.0
auto_reconnect = true
snapshot_period_s = 30.0

[devices.params.channel_map]
CH1 = "co2"
CH2 = "co"
CH3 = "o2"
```

[`FujiAdapterParams`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/fuji.py)
is `extra="forbid"`.

| Key | Default | Notes |
|---|---|---|
| `port` | required | `COM8`, `/dev/ttyUSB0`. The link is fixed at 38400 8-N-1. |
| `address` | `1` | Station number, 1–31, as set on the analyzer's front panel. |
| `channel_map` | required | The gas on each analyzer channel, `CH1`–`CH12`. Asserted by you: see [The channel map](#the-channel-map-is-asserted). Only channels named here can be bound. |
| `rate_hz` | `1.0` | Up to 5. One poll is two Modbus transactions and takes about 0.12 s; the analyzer's own filter makes more than a few polls a second pointless. |
| `timeout_s` | `0.5` | Per-reply timeout. |
| `snapshot_period_s` | `30.0` | Health-ping cadence. The snapshot carries the analyzer's settings, the range method of each measured channel among them (`ch3_range_method`). |
| `auto_reconnect` | `true` | A connection failure does not end the stream: every tick of the outage is an error row and the port is reopened on a back-off. Toggles `SUPPORTS_AUTO_RECONNECT`. |
| `overflow` | `"block"` | `"block"` or `"drop_newest"`. |
| `options` | `[]` | Analyzer options you assert are fitted, e.g. `["auto_calibration", "auto_zero"]` for a unit whose calibration gases are plumbed through its valve contacts. Without them the automatic-calibration commands are refused. |

In the Setup tab's device form `channel_map` is a JSON line,
`{"CH1": "co2", "CH2": "co", "CH3": "o2"}`. Discovery pre-fills it with
what the analyzer's type code suggests.

## Channels and emission shape

Every poll reads all of the analyzer's channels and its status, and becomes
one [`fujilib` `Sample`](https://fujilib.graysonbellamy.dev/). The native
row is preserved into `device_records/fuji.parquet` via
`fujilib.sample_to_row`: per channel the value, unit, gas, validity state
and hold / calibration / error flags; then the analyzer-level errors and
alarms; then `error_type` and `error_message`.

The only supported binding source is `fuji_channel`:

```toml
[[channels]]
name = "gas.o2"
kind = "gas_concentration"
unit = "percent"
derived_unit = "percent"
plot_group = "gases"

[channels.source]
source = "fuji_channel"
device = "analyzer"
channel = "CH3"

[channels.calibration]
kind = "identity"
input_unit = "percent"
output_unit = "percent"
```

`field` picks what the channel carries:

| `field` | Value |
|---|---|
| `"value"` (default) | The concentration, in the analyzer's unit for the channel's current range. |
| `"valid"` | `1.0` while the reading is live, `0.0` while it is not. No sample while the analyzer does not report validity. |

### The validity state

Each reading has one state, and it travels on the `ChannelSample` as
`status`. Only `ok` is a live measurement:

| `status` | Meaning |
|---|---|
| `ok` | A live reading. |
| `hold` | The outputs are held; the value is frozen. |
| `calibrating` | A manual zero or span is under way on the channel. |
| `auto_calibration` | The analyzer's automatic calibration is running. |
| `analyzer_error` | The analyzer reports an instrument error (errors 1, 2, 3, 10). |
| `channel_error` | The channel reports an error (errors 4–9). |
| `settling` | Read within 90 s of a reconnect. See [Warm-up and `settling`](#warm-up-and-settling). |
| `source_invalid` | A derived channel whose source channel or O2 channel is not valid. |
| `unknown` | The status was not read, or the value did not decode. |

When several apply, the first in this order wins: analyzer error, channel
error, calibrating, auto calibration, hold, source invalid, settling.

The raw value is kept in every state: a held or calibrating reading is
recorded as read, with its state beside it. capa stores `status` in
`scalars.parquet`, but **plots, alarms and procedures do not read it**. To
act on validity, bind a second channel with `field = "valid"` and watch
that. `plot = false` keeps it off the Run tab's plots, where a flag that
sits at 1 would only draw a flat line; it is still recorded and shown in
the Numerics dock. Add one where an alarm or a procedure needs it. The
shipped `fuji_real.toml` declares none.

```toml
[[channels]]
name = "gas.o2_valid"
kind = "gas_concentration"
unit = "dimensionless"
derived_unit = "dimensionless"
plot = false

[channels.source]
source = "fuji_channel"
device = "analyzer"
channel = "CH3"
field = "valid"

[channels.calibration]
kind = "identity"
input_unit = "dimensionless"
output_unit = "dimensionless"
```

### Failed polls

A poll that fails still produces a row: the same columns, the readings
empty, `error_type` and `error_message` filled. No `ChannelSample` is
derived from it, so a gap in `scalars.parquet` is a gap in the data, and
`device_records/fuji.parquet` says why. With `auto_reconnect = false`
any failed poll ends the stream.

### The unit check

A range change at the analyzer can change a channel's unit (vol% on one
range, ppm on the other). The adapter compares every reading's unit with
the unit its capa channel declares. On a mismatch it **quarantines** the
channel for the rest of the run — no further samples — and emits one
`unit_mismatch` event, rather than record values wrong by a factor of
10,000. Fix the channel's `unit` or the analyzer's range; the next run
starts clean.

See [channel bindings](../configuration/channel-bindings.md) for the
selector contract.

## Events

Each change is one `DeviceEvent` in the run's `events.sqlite`:

| `kind` | When |
|---|---|
| `comm_lost` / `comm_restored` | The first failed poll of an outage, and the first good one after it, with the outage's length and when `settling` ends. |
| `instrument_error` | The analyzer's instrument errors change; the codes are in the event. |
| `hold` | Output hold comes on or goes off. |
| `calibration` | A zero or span ends — made at the front panel or from capa — with its `fujilib-calibration/1` record in the event's metadata. |
| `unit_mismatch` | A channel is quarantined (above). |
| `label_disagreement` | At the start of a run: the type code suggests another gas than `channel_map` asserts. A warning only. |
| `setting_changed` | A setting written through capa during a run: the setting, its previous value and the new one. |
| `write_uncertain` / `calibration_uncertain` | A write or calibration whose outcome could not be established. |
| `metadata_stale` | The analyzer's settings could not be read; the snapshots show them as last read. |

Events exist only during a run. A command from the manual control panel
between runs is logged by the panel itself.

## Capability flags

| Flag | Notes |
|---|---|
| `READS_PROCESS_VAR` | The concentrations. |
| `HAS_PARAMETER_CONFIG` | Settings writes: response times, ranges, hold, calibration gases. |
| `HAS_GAS_CALIBRATION` | A zero or span driven from capa against a gas the operator supplies. |
| `HAS_INTERNAL_CAL` | Added only when `options` asserts `auto_calibration` or `auto_zero`. |
| `SUPPORTS_AUTO_RECONNECT` | Added when `auto_reconnect = true`. |

## Commands

All writes go through the [authorization
gate](../safety/authorization-gates.md) — `issued_by` plus either
`authorization_id` or `confirmed_by`. On top of it the adapter applies a
rule by fujilib's safety tier: **everything `DANGEROUS` needs
`confirmed_by`**, a person at the interface, even inside an authorized
run. A method step cannot change what the analyzer calibrates against.

| Typed call | `DeviceCommand.kind` | Tier | Needs |
|---|---|---|---|
| `set_response_time(target, seconds)` | `"set_response_time"` | persistent | either authorization |
| `set_output_hold(enabled)` | `"set_output_hold"` | persistent | either |
| `set_hold_mode(mode)` | `"set_hold_mode"` | persistent | either |
| `set_hold_value(channel, percent_fs)` | `"set_hold_value"` | persistent | either |
| `set_range(channel, range_number)` | `"set_range"` | persistent | either |
| `set_range_method(channel, method)` | `"set_range_method"` | persistent | either |
| — | `"write_parameter"` | the register's own | by tier |
| — | `"apply_settings"` | the highest in the document | by tier |
| `set_calibration_gas(...)` | `"set_calibration_gas"` | dangerous | `confirmed_by` |
| `return_to_measurement()` | `"return_to_measurement"` | stateful | either |
| — | `"calibration_plan"` | reads only | either |
| `begin_calibration(...)` | `"calibration_begin"` | stateful | `confirmed_by` |
| `commit_calibration()` | `"calibration_commit"` | dangerous | `confirmed_by` |
| `cancel_calibration()` | `"calibration_cancel"` | stateful | either |
| — | `"start_auto_calibration"`, `"start_auto_zero_calibration"` | dangerous | `confirmed_by`, and the option in `options` |

Blowback is not exposed. Key lock, alarms and the calibration schedule
are not writable.

A setting is written once and read back. `apply_settings` takes a
`fujilib-settings/1` document, compares all of it with the analyzer first,
and writes nothing if any part is refused or is above the tier the
command's authorization allows.

What comes back:

- **Accepted** — the write read back as written (or the command did what
  it says). The detail names the change by gas and range span, as the
  `setting_changed` event does: `O2 response time: 15 s -> 10 s`,
  `O2 range: 0–21 vol% -> 0–25 vol%`, `CO2 0–10 vol% span gas: 0.2 vol% -> 0.25 vol%`.
  The event's metadata keeps fujilib's register name (`response_time.o2`).
- **Refused** (`accepted=False`) — capa's gate, the tier rule, a bad
  payload, or fujilib or the analyzer declining: a value that does not
  fit, the analyzer calibrating or in a menu, an option not fitted, an
  exception reply. Nothing changed.
- **Uncertain** (`accepted=False`, and an `error` event during a run) —
  the write did not read back as written, or its outcome is unknown. The
  analyzer may hold something other than what was asked for: read the
  setting back before going on.
- **Failed** — the connection. The command raises.

## Calibrating from capa

A zero or span is driven from the analyzer's card in the [Manual Control
dock](../user-guide/manual-controls.md). capa presses the analyzer's own
calibration keys over Modbus; the operator switches the gas.

1. **Put the gas at the inlet.** capa does not switch valves.
2. **Plan** (optional) reads what the calibration would reach: every
   gas and range, and the calibration gas of each, e.g. `span of O2: O2
   0–21 vol% against 20.95 vol%`. A zero can cover more than one gas
   when the analyzer is set to zero them together.
3. **Begin…** — pick the gas, zero or span, and name the gas at the
   inlet: its value and a label (cylinder, lot) for the record. The unit
   is that of the range the gas measures on, and the analyzer's
   calibration-gas setting is shown beside the value: the gas named must
   equal it; change the setting first if it does not. After a
   confirmation, the panel is taken to its wait step. Nothing is
   calibrated yet.
4. **Wait for steady.** The card shows whether the reading is steady on
   the named gas: it must move no more than 0.5 % of full scale over at
   least 30 s (longer for a long response time) and lie within 10 % of
   full scale of the gas.
5. **Hold to calibrate** is enabled only while the reading is steady.
   Holding it sends the key that calibrates. fujilib checks everything
   again in the same breath — the panel, the gas, key lock, output hold,
   instrument errors — and refuses if anything has moved. A refused
   calibration leaves the run on the wait step.
6. **Cancel** leaves the wait step without calibrating.

There is no way to calibrate on an unsteady reading from capa. If the
reading will not settle within the rule, calibrate at the front panel:
capa records that too (below).

What ends a run that nobody ended:

- **15 minutes on the wait step** — it cancels itself.
- **A run starting** — it is cancelled, and that is the run's first
  event. The manual panel cannot reach a calibration during a run.
- **The hardware closing** (config reload, exit) — the panel is returned
  to measurement first.
- **ESC at the front panel** — the run ends without calibrating.

Every run that sent a key leaves a record: a `fujilib-calibration/1`
JSON document saved under `configs/calibrations/analyzer/`, named by
serial number, channel, kind and UTC time, and never written over. It
holds the plan, the named gas, the readings on the wait step, every key
sent and how the panel was left.

**A zero or span made at the front panel during a run** is noticed by the
adapter from the polls themselves: the rows are `calibrating`, and when it
ends a `calibration` event carries its record. It costs no extra traffic.

!!! danger "Automatic calibration on a unit without valves"

    `start_auto_calibration` and `start_auto_zero_calibration` make the
    analyzer switch its own calibration-gas valves. On an analyzer whose
    gases are not plumbed through those contacts it would calibrate on
    whatever is at the inlet. The adapter refuses both unless `options`
    asserts the option, and the card does not offer them.

## Discovery and handshake

`discover(ports=None, addresses=(1,), timeout_s=0.3)` wraps
[`fujilib.find_devices`](https://fujilib.graysonbellamy.dev/): a Modbus RTU
read to each station on each port, and an identification of whatever
answers. Reads only, but every port scanned receives the probe frames, so
the Setup tab scans serial families one at a time. One row per analyzer,
keyed by `port`, `address`, `model`, `serial`, `type_code`, and the
suggested `channel_map`.

`handshake(params)` is the per-device form `capa validate --strict`
runs: read-only `open` + `identify` + `close`.

See [Discovery](discovery.md) for the cross-cutting UX.

## Quirks

### The channel map is asserted

Which gas a channel carries is not something the analyzer reports. Its
type code suggests it, and the suggestion can be wrong or out of date: one
analyzer capa was developed against carries O2 on a channel its type code
says is not fitted. So `channel_map` is yours to assert, and it wins. A
disagreement with the type code is a `label_disagreement` warning at the
start of each run, nothing more.

[Layer 2 validation](../configuration/validation-and-problems.md) flags a
`fuji_channel` binding to a channel the map does not name
(`channels.fuji_channel_unmapped`) and two analyzers on one port
(`devices.fuji.shared_port`).

### Warm-up and `settling`

When a connection failure is followed by a reconnect, every reading in
the next 90 s that would be `ok` is `settling` instead — and `valid` is
`0`. A pulled USB cable may have taken the analyzer's power with it, and
90 s of good data marked is the cheaper mistake.

Two cases are **not** marked:

- **The first open.** capa cannot know how long the analyzer has been on.
  Respect the manual's warm-up time.
- **A power cut that leaves the USB adapter powered.** The port never
  fails, so nothing reconnects: the polls during the cut are error rows
  with a timeout, and the readings after it are `ok` while the analyzer
  warms up. A `comm_lost` / `comm_restored` pair whose error is a timeout
  rather than a connection failure is the sign; treat the minute or two
  after it as suspect.

### One analyzer per port

fujilib addresses one station per open port. A second Fuji device on the
same port fails validation rather than at open.

### Unplugged between runs

The port is reopened by the stream, so an outage *during* a run heals
itself. An adapter unplugged while no run is active stays disconnected
until the next run starts or the config is reloaded; manual commands in
between fail with a connection error.

### A run that starts during an outage

`device_records/fuji.parquet` takes each column's type from its first
1,024 rows (or all of a shorter run). If every one of them is an error
row, the reading columns are stored as text for that run. Nothing is
lost, but a reader has to convert them.

### Sim equivalent

[`capa.devices.sim.fuji_sim`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/fuji_sim.py)
builds real fujilib frames, so its rows have exactly the real adapter's
columns. It takes a `channel_map` (default CO2 / CO / O2 on `CH1`–`CH3`),
a signal per channel (ambient air without one), `tick_period_s`,
`hold_from_s` to switch output hold on part-way, and `settle_s`, how long
a simulated calibration takes to read steady. The settings verbs change
its own settings and a zero or span runs through the same three commands,
so the card and declared device settings can be exercised offline. Its
ranges are all in vol%: one per gas, two for O2 (0–25 and 0–10 vol%); a
range change does not change the readings. It models no outages and no
`settling`. See [Simulators](simulators.md).

## See also

- [Devices overview](overview.md) — adapter contract, capability enum, resource grouping.
- [Hardware TOML](../configuration/hardware-toml.md) — how `[[devices]]` blocks are parsed.
- [Channel bindings](../configuration/channel-bindings.md#fuji_channel) — the `fuji_channel` source schema.
- [Manual controls](../user-guide/manual-controls.md) — the analyzer's card.
- [Device settings](../configuration/device-settings.md) — declaring the analyzer's settings in an experiment.
- [Destructive operations](../safety/destructive-operations.md) — which analyzer writes ask for confirmation.
- [Authorization gates](../safety/authorization-gates.md) — the contract for any device write.
- [Discovery](discovery.md) — cross-cutting Setup-tab and CLI behavior.
- [fujilib docs](https://fujilib.graysonbellamy.dev/).
