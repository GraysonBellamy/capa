---
description: Logging ambient temperature and humidity in capa with a Sensirion SHT4x on an MCP2221 USB-I2C bridge — params, wide-row emission, precision and heater commands, the NACK quirk.
---

# SHT4x ambient sensor

**Audience:** operators logging the lab's air temperature and relative humidity alongside a CAPA run.
**Scope:** capa's `capa.devices.sht4x` adapter — `[devices.params]` fields, the wide-row emission, the `sht4x_reading` binding, the precision and heater command surface, and the bridge quirk the driver hides.

---

## At a glance

| | |
|---|---|
| Adapter id | `capa.devices.sht4x` |
| Transport | [`hidapi`](https://pypi.org/project/hidapi/) via the in-tree MCP2221 driver, [`capa.devices._mcp2221`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/_mcp2221.py) |
| Real adapter | [`capa.devices.sht4x`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sht4x.py) |
| Sim adapter | [`capa.devices.sim.sht4x_sim`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/sht4x_sim.py) |
| Resource scheme | `hid:mcp2221:<bridge serial or any>` |
| Channel binding | [`sht4x_reading`](../configuration/channel-bindings.md#sht4x_reading) |
| Emission shape | `wide_row` — temperature and humidity together, one row per poll |
| Default poll rate | 1 Hz; capped at 10 Hz |

## Supported hardware

A Sensirion SHT40, SHT41 or SHT45 (they share one protocol; the chip
cannot say which it is) wired to the I2C pins of a Microchip MCP2221 or
MCP2221A USB bridge. The bridge enumerates as a serial port plus a
vendor HID interface; capa talks I2C over the HID interface and ignores
the port. Unlike the serial families there is no sibling library: the
whole sensor is six commands and a checksum, so the protocol lives in
the adapter module and the bridge's four HID commands in a private
module next to it.

## Configuration

```toml title="configs/hardware/sht4x_real.toml (excerpt)"
[[devices]]
name = "ambient"
adapter = "capa.devices.sht4x"
on_failure = "warn"

[devices.params]
i2c_address = 0x44
precision = "high"      # "high" | "medium" | "low"
rate_hz = 1.0
i2c_speed_hz = 100000
auto_reconnect = true
snapshot_period_s = 30.0
```

[`Sht4xAdapterParams`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sht4x.py)
is `extra="forbid"`.

| Key | Default | Notes |
|---|---|---|
| `serial` | unset | USB serial of the MCP2221 to use. The chip ships with serial enumeration off, so with one bridge leave this unset and the first bridge found is used. |
| `i2c_address` | `0x44` | `0x44` for SHT40-A, SHT41-A and SHT45; `0x45` for the -B variants; `0x46` for SHT40-C. |
| `precision` | `"high"` | Repeatability setting the stream starts with: an 8.3, 4.5 or 1.7 ms conversion. `set_precision` changes it live; `device_settings` can declare it. |
| `rate_hz` | `1.0` | Capped at 10 Hz: the datasheet keeps the measurement duty cycle under ten percent to limit self-heating. |
| `i2c_speed_hz` | `100000` | 47 kHz to 400 kHz, the bridge's range. |
| `auto_reconnect` | `true` | A failed poll keeps its row, counts as a recoverable error and does not end the stream; a bridge I/O failure reopens the bridge after a one-second back-off. Toggles `SUPPORTS_AUTO_RECONNECT`. |
| `snapshot_period_s` | `30.0` | Health-ping cadence. |

Set `on_failure = "warn"` on the device: ambient logging should degrade a
run, not abort it.

## Channels and emission shape

Every poll measures temperature and humidity together, so the adapter is
a wide-row family: one row per poll into `device_records/sht4x.parquet`
with `temperature_c`, `humidity_pct`, the raw 16-bit words
`temperature_raw` and `humidity_raw`, the `precision` used, `latency_ms`
from command to data, and `error_type` / `error_message` (both null on a
good poll). The record's timestamp is the midpoint of the poll.

The only supported binding source is `sht4x_reading`, with `field` set
to `"temperature"` or `"humidity"`:

```toml
[[channels]]
name = "ambient.temperature"
kind = "temperature"
unit = "degC"
plot_group = "ambient"

[channels.source]
source = "sht4x_reading"
device = "ambient"
field = "temperature"

[channels.calibration]
kind = "identity"
input_unit = "degC"
output_unit = "degC"

[channels.metadata]
capa_group = "ambient_temperature"
```

The humidity channel is the same with `field = "humidity"`, `kind =
"humidity"`, `unit = "percent"` and `capa_group = "ambient_humidity"`.
Both groups are optional under the CAPA profile. Humidity is clipped to
0–100 %RH as the datasheet asks; the raw word keeps any overshoot.

A failed poll (checksum mismatch, a sensor that did not answer, a bridge
I/O error) preserves the native row with the error fields populated but
emits no `ChannelSample` — the sample's `status` does not get to lie
about a value that didn't arrive.

See [channel bindings](../configuration/channel-bindings.md) for the
selector contract.

## Capability flags

| Flag | Notes |
|---|---|
| `HAS_PARAMETER_CONFIG` | Precision setting; the manual card. |
| `HAS_SENSOR_HEATER` | The heater pulse command. |
| `SUPPORTS_DISCOVERY` | `capa hardware discover` probes every bridge. |
| `SUPPORTS_AUTO_RECONNECT` | Added when `auto_reconnect=True`. |

## Commands

| Typed call | `DeviceCommand.kind` | Notes |
|---|---|---|
| `set_precision(...)` | `"set_precision"` | Payload `{"precision": "high" \| "medium" \| "low"}`. Changes which measure command the stream sends; nothing is written to the sensor. |
| `heater_pulse(...)` | `"heater_pulse"` | Payload `{"power_mw": 200 \| 110 \| 20, "duration_s": 1.0 \| 0.1}`. Runs the sensor's heater once and returns the high-precision reading taken at the end of the pulse. |
| `soft_reset()` | `"soft_reset"` | The sensor's reset command; the serial is re-read. |
| `read_once()` | n/a | Read-only — not gated. One measurement outside the stream. |
| `read_state_snapshot()` | n/a | Read-only — not gated. Precision, sensor serial, bridge revision, last reading and heater history, for the manual card. |

All writes go through the [authorization
gate](../safety/authorization-gates.md) — `issued_by` plus either
`authorization_id` or `confirmed_by`.

### The heater

The SHT4x has a small heater meant for driving condensation off the
element after a humid excursion, or for a plausibility check of the
humidity response. It is not needed in normal CAPA use, and a pulse
skews the reading warm and dry for a while after (several °C right
after a short pulse, decaying over tens of seconds), so the card asks
for confirmation and the stream's next records are not ambient. Two
guards are enforced in the adapter, both answered with
`accepted=False` rather than an error:

- **Ambient limit.** A pulse is refused while the last reading is above
  65 °C, the datasheet's limit for heater use.
- **Duty cycle.** Heater-on time is held to ten percent: after a 1 s
  pulse the next is allowed 9 s later, after a 0.1 s pulse 0.9 s later.

Each pulse is logged as a `heater_pulse` event with the end-of-pulse
reading in its metadata, so a skewed stretch of the record is
explained in `events.sqlite`.

An experiment can declare the precision under
[`device_settings:`](../configuration/device-settings.md) to have it
applied when the config loads; the heater is never declarative.

## Discovery and handshake

`discover()` enumerates every MCP2221 on the machine and probes each
for a sensor at `0x44`, `0x45` and `0x46`, returning one row per sensor
found: the bridge's `serial` (null unless enumeration is enabled in its
flash) and HID `path`, the `i2c_address` and the `sensor_serial`.
Bridges another process holds are skipped.

`handshake(params)` is the per-device form `capa validate --strict`
runs: open, soft reset, read the serial and one measurement, close.

See [Discovery](discovery.md) for the cross-cutting UX.

## Quirks

### The bridge sticks after a NACK

The sensor NACKs its address while a conversion is in progress, and the
MCP2221 answers that by parking its I2C engine in an error state in
which every later transfer is refused until a cancel command. The driver
cancels on every NACK and on open, so a read issued a little early, a
process killed mid-transfer or a sensor unplugged and replugged all
recover without operator action. The adapter also waits the datasheet's
maximum conversion time before reading and keeps retrying a NACKed read
for 20 ms after that, so a healthy sensor never surfaces this at all.

### One sensor per bridge

The adapter opens the bridge itself, so a second SHT4x on the same bus
(the `0x44` and `0x45` variants can share one) is not supported yet;
declare one device per bridge. Two bridges are told apart by `serial`
once serial enumeration is enabled in their flash.

### Placement

The sensor measures the air around it, including whatever the cone
heater and the PC radiate. Mount it away from the heater's plume and
the exhaust, and expect it to read a degree or so above the room if it
sits on a warm enclosure.

## Sim equivalent

`capa.devices.sim.sht4x_sim` takes `temperature_signal` and
`humidity_signal` specs (both optional; the defaults are a steady 22 °C
and 50 %RH), `tick_period_s` and `precision`. It keeps the precision
and heater state the real adapter keeps, answers `read_state_snapshot()`
the same way, and a heater pulse adds a warm offset that decays over ten
seconds so the perturbation shows up in a sim run.
