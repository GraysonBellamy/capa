---
description: Declare device settings (Alicat gas, balance stability, IR camera range and radiometric parameters) in an experiment and have capa apply them when the config loads.
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
  ir_cam0:                    # FLIR IR camera
    temperature_range: {min_c: 0.0, max_c: 650.0}
    emissivity: 0.95
    distance_m: 0.5
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
settings are checked again before Start.

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

Other adapters (Watlow, Fuji, NI-DAQ, webcams) have no declarable
settings; naming one under `device_settings:` is a validation error.

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
`operator.id`. If any setting can't be applied or doesn't take, the run
is aborted before a bundle is created, with an exit reason such as:

```text
device_settings: purge_mfc gas: the device doesn't offer SF6
```

## Validation

| Code | Severity | Meaning |
|---|---|---|
| `device_settings.unknown_device` | error | The key isn't a device or camera in the hardware profile. |
| `device_settings.unsupported_adapter` | error | That device's adapter has no declarable settings. |
| `device_settings.<pydantic type>` | error | A field is unknown or its value is invalid, e.g. `device_settings.extra_forbidden`, `device_settings.literal_error`. |
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
