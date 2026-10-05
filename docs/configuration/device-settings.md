---
description: Declare device settings (Alicat gas, balance stability, gas analyzer response times and ranges, IR camera range and radiometric parameters, webcam zoom, pan, tilt, focus and exposure) in an experiment and have capa apply them when the config loads.
---

# Device settings

**Audience:** config authors and operators who want an experiment to set
its instruments up, instead of re-selecting the same settings on the
manual cards every time a config loads.
**Scope:** the `device_settings:` block of an experiment, what each
adapter accepts, and what happens on load, before Start, and in headless
runs.

---

## What it does

An experiment can declare the settings its devices should be in:

```yaml
device_settings:
  purge_mfc:                  # Alicat
    gas: N2
  balance:                    # Sartorius
    filter_mode: very stable
    stability_range: accurate
  gas_analyzer:               # Fuji ZP gas analyzer
    o2_response_time_s: 10
    o2_range_method: manual
    o2_range: {full_scale: 25, unit: vol%}
  ir_cam0:                    # FLIR IR camera
    temperature_range: {min_c: 0.0, max_c: 650.0}
    emissivity: 0.95
    distance_m: 0.5
  visible_cam0:               # USB webcam
    zoom: 265
    tilt: 3600
    auto_exposure: false
    exposure: -6
```

Keys are device or camera names from the hardware profile. Every field
is optional: a field you leave out is left as the device has it.

When the config loads, capa reads each named device, compares it with
the declaration, and — only if something differs — shows a dialog of
current and declared values. Apply sends the checked changes, then reads
each device again to confirm they took. If everything already matches,
nothing is shown beyond a status-bar note.

Writes are **session-only**: nothing is saved to device EEPROM. A device
that is power-cycled reverts to its saved settings, which is why the
settings are checked again before Start. The gas analyzer is the
exception: it keeps every setting it takes, through a power cycle.

## Settings per adapter

### Alicat (`capa.devices.alicat`, `capa.devices.sim.alicat_sim`)

| Field | Values | Notes |
|---|---|---|
| `gas` | A gas name: `N2`, `Air`, `Ar`, `CO2`, … | Any alicatlib name or alias (`nitrogen` → `N2`); a typo is a validation error with suggestions. Custom mixtures are not supported. |

### Sartorius balance (`capa.devices.sartorius`, `capa.devices.sim.sartorius_sim`)

| Field | Values | Menu |
|---|---|---|
| `filter_mode` | `very stable`, `stable`, `unstable`, `very unstable` | p01 (ambient conditions) |
| `app_filter` | `final reading`, `filling`, `reduced`, `off` | p02 |
| `stability_range` | `max accuracy`, `very accurate`, `accurate`, `fast`, `very fast`, `max fast` | p03 |
| `stability_delay` | `none`, `short`, `average`, `long` | p04 |
| `auto_zero` | `on`, `off` | p06 |
| `tare_behavior` | `without stability`, `with stability`, `at stability` | p05 |

Written to the balance's runtime menu; never saved with `save_menu`.

### Fuji gas analyzer (`capa.devices.fuji`, `capa.devices.sim.fuji_sim`)

Settings are declared per gas, by the gas the device's `channel_map`
asserts (`co2_…`, not `CH1`). `<gas>` is one of `co2`, `co`, `o2`,
`ch4`, `so2`, `no`, `nox`.

| Field | Values | Notes |
|---|---|---|
| `output_hold` | `true` / `false` | Hold the outputs, and the recorded values, during a calibration. |
| `hold_mode` | `last reading`, `preset value` | What the outputs hold. |
| `<gas>_response_time_s` | 0 – 60 s | The gas's response-time filter; `0` switches it off. |
| `<gas>_range_method` | `manual`, `auto` | `auto` switches up at 90 % of the low range and back below 80 %. |
| `<gas>_range` | `{full_scale, unit}` | The range by its span, e.g. `{full_scale: 25, unit: vol%}`; `unit` is `vol%`, `ppm`, `mg/m3` or `g/m3`. Matched against the analyzer's ranges for that gas. |

- **The analyzer keeps them.** Unlike the other adapters' settings,
  these survive a power cycle.
- **Range and method.** A range is selected only while the method is
  `manual`, so a gas's method is applied before its range. Declaring
  `auto` together with a range is a validation error
  (`device_settings.value_error`). A range declared without a method,
  on a gas the analyzer has on `auto`, is refused when applied.
- **Unit.** A range in another unit stops a capa channel declared in
  the old unit for the rest of a run (`unit_mismatch`); change the
  channel's `unit` with it.
- **Checked when read.** A gas the channel map doesn't assert, or a span
  the analyzer doesn't offer for it, is listed as a setting that can't
  be applied, with the ranges it does offer.
- **Not declarable.** The calibration gases: the next calibration is
  computed from them, so they are set deliberately on the manual card.

### IR cameras (`capa_flir.flir_ir`, `capa.devices.sim.flir_ir_sim`)

| Field | Values | Notes |
|---|---|---|
| `temperature_range` | `{min_c, max_c}` | Matched against the camera's own ranges within 1 °C. Switching makes the camera recalibrate for a few seconds, and is refused while recording. |
| `emissivity` | 0.001 – 1 | |
| `atmospheric_temp_c` | °C | |
| `reflected_temp_c` | °C | |
| `distance_m` | > 0 m | |
| `relative_humidity` | 0 – 1 | A fraction, not percent. |
| `atmospheric_transmission` | 0 – 1 | |
| `auto_nuc_interval_s` | ≥ 0 s | `0` turns automatic NUC off. |

The range is applied first, then the radiometric parameters, then the
auto-NUC interval.

### USB webcams (`capa.devices.camera.webcam`)

Windows only: the camera's controls are reached through duvc-ctl. Every
value is an integer in the camera's own units.

| Field | Values | Notes |
|---|---|---|
| `zoom` | camera range | Shown as "Optical zoom". Applied before pan and tilt. |
| `digital_zoom` | camera range | |
| `pan`, `tilt` | camera range | Arc-seconds on most cameras (3600 = 1°); 0 is centered. |
| `auto_focus` | `true` / `false` | |
| `focus` | camera range | Turns auto focus off. |
| `auto_exposure` | `true` / `false` | |
| `exposure` | camera range | UVC log2 seconds (−6 ≈ 1/64 s). Turns auto exposure off. |
| `auto_white_balance` | `true` / `false` | |
| `white_balance` | K | Turns auto white balance off. |
| `brightness`, `contrast`, `saturation`, `sharpness`, `gamma`, `hue`, `gain`, `backlight_compensation` | camera range | |

- **Range and step.** A value outside the range the camera reports, or
  off its step (pan and tilt often move in steps of 3600), is listed as
  a setting that can't be applied, with the nearest values it takes.
  The manual card shows each control's range.
- **Auto modes.** A control the camera is driving itself shows as
  `auto` in the dialog. Declaring `auto_exposure: true` together with
  `exposure` is a validation error (`device_settings.value_error`); the
  same goes for focus and white balance.
- **Order.** Zoom is applied before pan and tilt, because digital-PTZ
  cameras such as the C930e limit pan and tilt to the zoomed view. Each
  auto toggle is applied before its value.
- **Other platforms.** Off Windows, or when duvc-ctl can't find the
  camera (check `model_hint` or `serial` in the hardware profile), every
  declared webcam setting is listed as unappliable with the reason. A
  headless run is aborted.

Other adapters (Watlow, NI-DAQ) have no declarable settings; naming one
under `device_settings:` is a validation error.

## On load, in the GUI

1. Devices open as usual. The hardware dialog then shows **Reading
   device settings** while each named device is read.
2. If every device matches, the status bar says so and you're done.
3. Otherwise the **Device settings** dialog lists one row per setting
   that differs: device, setting, current value, declared value, and a
   note (for instance the IR camera's recalibration). Rows are checked
   by default. A setting that can't be applied as written — a range the
   camera doesn't offer, a device that didn't answer — is listed but
   can't be checked, with the reason.
4. **Apply selected** sends the checked changes. Each row then shows
   whether the device now reports the declared value (✓), accepted it
   but doesn't report it back (⚠), still reports something else (⚠), or
   refused it (✗). The manual cards refresh to the new values.
5. **Skip** leaves the devices as they are.

**File → Apply Device Settings…** runs the same check on demand — after
power-cycling an instrument, for instance.

Clicking the dialog's Apply is the operator's confirmation: each change
goes out as a [manual override](../safety/authorization-gates.md#manual-overrides-from-the-ui)
issued and confirmed by the current operator, exactly as if it had been
set from the manual card.

## Before Start

Start reads the devices once more. If a declared setting has drifted
(a manual-card change, a power-cycled MFC), the same dialog opens with
**Apply selected**, **Start anyway** and **Cancel**. What each device
reports at that moment is recorded in the bundle.

## Headless runs

`capa run` applies the declared settings right after the devices open,
without asking — launching the run with this config is the
confirmation, and the commands are issued and confirmed as the config's
`operator.id`. If any setting can't be applied or doesn't take, or a
setting that already matched was moved by another (a webcam's zoom
shifting its tilt, say), the run is aborted before a bundle is created,
with an exit reason such as:

```text
device_settings: purge_mfc gas: the device doesn't offer SF6
```

## Validation

| Code | Severity | Meaning |
|---|---|---|
| `device_settings.unknown_device` | error | The key isn't a device or camera in the hardware profile. |
| `device_settings.unsupported_adapter` | error | That device's adapter has no declarable settings. |
| `device_settings.<pydantic type>` | error | A field is unknown or its value is invalid, e.g. `device_settings.extra_forbidden`, `device_settings.literal_error`, `device_settings.int_type` (a webcam value that isn't a whole number), `device_settings.value_error` (a webcam auto toggle declared on together with its value). |
| `capa_profile.purge_gas_mismatch` | warning | Under the CAPA profile, the purge MFC's declared gas differs from `atmosphere.purge.species`. Skipped when the species isn't a gas name the MFC knows (a mixture such as `5% O2/N2`). |

Values that depend on the device itself — whether the MFC offers the
gas, whether the camera has the range — can only be checked when the
device is read, and show up in the dialog instead.

## What the bundle records

- `config.toml` holds the declared `device_settings`, like the rest of
  the resolved config.
- `equipment.toml` holds each device's settings as read when the run
  started, under its `settings` table.

## For adapter authors

An adapter opts in through its descriptor's `settings` field: a
[`DeviceSettingsSpec`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/settings.py)
naming a Pydantic model (every field `X | None = None`) and one
`SettingField` per setting. Each field says how to read the current
value from the adapter's `read_state_snapshot()` result, which command
sets it, and how to compare and display values. The shared IR-camera
spec in
[`ir_settings.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/camera/ir_settings.py)
is a worked example.

## See also

- [Experiment YAML](experiment-yaml.md) — the rest of the experiment file.
- [Manual controls](../user-guide/manual-controls.md) — changing settings by hand.
- [Authorization gates](../safety/authorization-gates.md) — how the commands are authorized.
