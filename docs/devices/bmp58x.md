---
description: Logging ambient barometric pressure in capa with a Bosch BMP580/581/585 on the same MCP2221 USB-I2C bridge as the SHT4x — params, forced-mode polling, the oversampling command, sharing the bridge.
---

# BMP58x barometer

**Audience:** operators logging the lab's barometric pressure alongside a CAPA run.
**Scope:** capa's `capa.devices.bmp58x` adapter — `[devices.params]` fields, the wide-row emission, the `bmp58x_reading` binding, the oversampling command, and how it shares an MCP2221 bridge with an [SHT4x](sht4x.md).

---

## At a glance

| | |
|---|---|
| Adapter id | `capa.devices.bmp58x` |
| Transport | [`hidapi`](https://pypi.org/project/hidapi/) via the in-tree MCP2221 driver, [`capa.devices._mcp2221`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/_mcp2221.py) |
| Real adapter | [`capa.devices.bmp58x`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/bmp58x.py) |
| Sim adapter | [`capa.devices.sim.bmp58x_sim`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/bmp58x_sim.py) |
| Resource scheme | `hid:mcp2221:<bridge serial or any>` — the same as an SHT4x on that bridge |
| Channel binding | [`bmp58x_reading`](../configuration/channel-bindings.md#bmp58x_reading) |
| Emission shape | `wide_row` — pressure and temperature together, one row per poll |
| Default poll rate | 1 Hz; capped at 10 Hz |

## Supported hardware

A Bosch BMP580, BMP581 or BMP585 on the I2C bus of a Microchip MCP2221
or MCP2221A, usually daisy-chained after the SHT4x. The three parts
share one register map; the chip id register tells a BMP585 (`0x51`)
from a BMP580 or BMP581 (`0x50`, which cannot be told apart). The
sensor compensates on-chip, so pressure and temperature arrive as 24-bit
words already in pascals and degrees Celsius, with no trimming
coefficients to read. As with the SHT4x there is no sibling library: the
adapter needs nine registers.

## Configuration

```toml title="configs/hardware/sht4x_real.toml (excerpt)"
[[devices]]
name = "baro"
adapter = "capa.devices.bmp58x"
on_failure = "warn"

[devices.params]
i2c_address = 0x47
pressure_oversampling = 64
temperature_oversampling = 4
rate_hz = 1.0
i2c_speed_hz = 100000
auto_reconnect = true
snapshot_period_s = 30.0
```

[`Bmp58xAdapterParams`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/bmp58x.py)
is `extra="forbid"`.

| Key | Default | Notes |
|---|---|---|
| `serial` | unset | USB serial of the MCP2221. Leave unset with one bridge, and set it the same as the SHT4x's when it is set there. |
| `i2c_address` | `0x47` | `0x47` with SDO high (most breakouts), `0x46` with SDO low. |
| `pressure_oversampling` | `64` | Samples averaged per conversion: 1, 2, 4 … 128. See the table below. `set_oversampling` changes it live; `device_settings` can declare it. |
| `temperature_oversampling` | `4` | The same, for temperature. Adds little conversion time. |
| `rate_hz` | `1.0` | One forced conversion per poll. Capped at 10 Hz. |
| `i2c_speed_hz` | `100000` | 47 kHz to 400 kHz. Must match every other sensor on the bridge. |
| `auto_reconnect` | `true` | A failed poll keeps its row, counts as a recoverable error, and the sensor is reset and reconfigured before the next poll; a bridge I/O failure also reopens the bridge after a one-second back-off. Toggles `SUPPORTS_AUTO_RECONNECT`. |
| `snapshot_period_s` | `30.0` | Health-ping cadence. |

Oversampling trades noise for conversion time. Measured on the rig's
BMP581 over the bridge, at 1 Hz:

| Pressure / temperature | Conversion | Pressure noise (1 σ) |
|---|---|---|
| 1× / 1× | ~1 ms | 0.4 Pa |
| 16× / 2× | ~12 ms | 0.2 Pa |
| 64× / 4× (default) | ~45 ms | 0.1 Pa |
| 128× / 8× | ~86 ms | 0.1 Pa |

A poll adds about 20 ms of bridge traffic to the conversion, so the
default poll takes 65–70 ms. 0.1 Pa is about a centimetre of altitude;
anything past the default only buys conversion time.

Set `on_failure = "warn"` on the device: ambient logging should degrade a
run, not abort it.

## Channels and emission shape

Each poll is one forced conversion. The adapter writes forced mode to
the sensor, waits the conversion time the oversampling implies, polls
the data-ready flag, and reads the six data bytes. One row per poll goes
into `device_records/bmp58x.parquet` with `pressure_pa`,
`temperature_c`, the raw 24-bit words `pressure_raw` and
`temperature_raw`, both oversampling ratios, `latency_ms` from trigger
to data, and `error_type` / `error_message` (both null on a good poll).
The record's timestamp is the midpoint of the poll.

The only supported binding source is `bmp58x_reading`, with `field` set
to `"pressure"` (Pa) or `"temperature"` (°C):

```toml
[[channels]]
name = "ambient.pressure"
kind = "pressure"
unit = "Pa"
derived_unit = "Pa"
plot_group = "ambient_pressure"

[channels.source]
source = "bmp58x_reading"
device = "baro"
field = "pressure"

[channels.calibration]
kind = "identity"
input_unit = "Pa"
output_unit = "Pa"

[channels.metadata]
capa_group = "ambient_pressure"
```

Keep pressure on its own `plot_group`: around 101 000 Pa on the same
axes as °C and %RH flattens both. The rig profiles log pressure only.
The barometer's temperature is the chip's own, used for its
compensation, and reads a few tenths of a degree below the SHT4x. It is
always in the device record, and `configs/hardware/sht4x_real.toml` also
binds it as `ambient.baro_temperature` (`kind = "temperature"`, `capa_group =
"ambient_temperature"`). `ambient_pressure` is an optional group under
the CAPA profile.

A pressure outside the sensor's 30–125 kPa operating range is a fault,
not weather: the poll fails with the error fields set and no sample. The
data registers' reset pattern decodes to 130.6 kPa, so a conversion that
never happened cannot pass as a reading.

## Capability flags

| Flag | Notes |
|---|---|
| `HAS_PARAMETER_CONFIG` | The oversampling setting. |
| `SUPPORTS_DISCOVERY` | `capa hardware discover` probes every bridge. |
| `SUPPORTS_AUTO_RECONNECT` | Added when `auto_reconnect=True`. |

There is no manual-control card: nothing about the sensor needs an
operator's hand mid-run.

## Commands

| Typed call | `DeviceCommand.kind` | Notes |
|---|---|---|
| `set_oversampling(...)` | `"set_oversampling"` | Payload `{"pressure": n, "temperature": n}`, either key optional, each 1, 2, 4 … 128. Written to the sensor between conversions and read back; a write that does not stick leaves the old setting. |
| `soft_reset()` | `"soft_reset"` | The sensor's reset command, then the configuration is written again. |
| `read_once()` | n/a | Read-only — not gated. One conversion outside the stream. |
| `read_state_snapshot()` | n/a | Read-only — not gated. Oversampling, chip, revision, bridge revision and the last reading. |

Both writes go through the [authorization
gate](../safety/authorization-gates.md). An experiment can declare
either ratio under [`device_settings:`](../configuration/device-settings.md).

## Discovery and handshake

`discover()` enumerates every MCP2221 and reads the chip id register at
`0x46` and `0x47`, returning one row per BMP58x found: the bridge's
`serial` and HID `path`, the `i2c_address` and the `chip`. It does not
reset the sensor.

`handshake(params)` is the per-device form `capa validate --strict`
runs: open (reset, chip and NVM checks, configuration), one conversion,
close. On the rig:

```text
strict: ambient -> sht4x serial=0x1643e746 addr=0x44 bridge=MCP2221 rev=A6 / 1.2 T=19.81C RH=62.6%
strict: baro -> bmp58x chip=BMP580/BMP581 rev=0x32 addr=0x47 bridge=MCP2221 rev=A6 / 1.2 P=100819.6Pa T=19.46C
```

## Sharing the bridge

The SHT4x and the BMP58x declare the same `hid:mcp2221:…` resource, so
capa runs both on one worker, and both adapters open the bridge through
one process-wide pool: the first to open claims the HID interface, the
second gets the same bus, and the last to close releases it. A second
HID handle on the chip is not an option, because Windows hands every
open handle a copy of every response, so two handles would read each
other's answers.

Each sensor transaction holds a bus lock for its command and for its
read, but not across the conversion wait, so a 1 s SHT4x heater pulse
or a long barometer conversion does not stall the other sensor. A
per-sensor lock keeps a command (an oversampling change, a heater
pulse) out of that sensor's own conversions. A bridge reopen after an
I/O failure is a reopen of the shared bus, so it heals both sensors.

## Quirks

### Pressure is off after a reset

The sensor powers up and resets with pressure measurement disabled and
deep standby allowed. The adapter's configuration enables pressure,
disables deep standby and turns the data-ready flag on (the interrupt
pin stays off), then reads the registers back. If the sensor ever
reports a power-on reset mid-run, as after a brown-out on the cable,
that poll fails and the sensor is reset and reconfigured before the
next one.

### Placement

Pressure does not care where in the room the sensor sits, but the
reading is absolute: a sensor inside a purged or exhausted enclosure
reads that enclosure, not the lab. Keep it in lab air next to the SHT4x.

## Sim equivalent

`capa.devices.sim.bmp58x_sim` takes `pressure_signal` (Pa) and
`temperature_signal` (°C) specs, both optional (the defaults are a
steady 101 325 Pa and 22 °C), plus `tick_period_s` and the two
oversampling ratios. It keeps the oversampling the real adapter keeps and
answers `read_state_snapshot()` the same way.
