# FLIR IR cameras

**Audience:** operators with a FLIR thermal camera (validated against the E85; A-series and other Atlas-supported cameras work via the same adapter).
**Scope:** capa's FLIR adapter — distributed separately as `capa-flir` because it depends on the FLIR Atlas C SDK — its Atlas SDK prerequisites, radiometric `.csq` capture, IR colormap controls, and the sim peer.

---

## At a glance

| | |
|---|---|
| Adapter id | `capa_flir.flir_ir` (full module path) or `flir_ir` (after `capa-flir` is installed) |
| Adapter family | `camera_ir` |
| Real adapter | [`capa_flir.flir_ir.FlirIrAdapter`](https://github.com/GraysonBellamy/capa-flir/blob/main/src/capa_flir/flir_ir.py) |
| Sim adapter | [`capa.devices.sim.flir_ir_sim`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/flir_ir_sim.py) |
| Container | FLIR `.csq` (radiometric sequence) |
| Default circular buffer | 60 frames |
| Compression | off by default (uncompressed `.csq`) |

## Why FLIR is a separate package

The FLIR Atlas SDK is closed-source, license-controlled, and large. capa
core does not bundle it, mirror it, or depend on it. The
[`capa-flir`](https://github.com/GraysonBellamy/capa-flir/blob/main/README.md) package — distributed
separately — supplies:

- `FlirIrAdapter` (the camera Protocol implementation).
- A post-finalize `.csq` header-index extractor.
- The Atlas SDK loader (`os.add_dll_directory` on Windows,
  `LD_LIBRARY_PATH` on Linux).
- Entry-point registration: `capa-flir` advertises
  `flir_ir = "capa_flir:DESCRIPTOR"` in the `capa.cameras` entry-point
  group, so once installed it appears under the short id `flir_ir` in
  the Setup tab's adapter picker. For unattended headless runs, prefer
  the full module path `capa_flir.flir_ir`; the short id depends on the
  descriptor registry having been loaded by Setup/discovery.

If `capa-flir` is not installed, a config referencing `capa_flir.flir_ir`
fails fast at load with an import error — capa core does not silently
fall back.

## Supported hardware

Any IR camera supported by Atlas. Validated against the FLIR E85 over
USB. A-series and other Atlas-compatible cameras have been used in
development but the rig-side verified-against list is "E85 over USB."
The full Atlas-supported list is owned by FLIR.

## Atlas SDK prerequisites

Required version: **Atlas C SDK 2.19.0**. Obtain from FLIR's developer
portal (click-through licensing).

| Platform | Build |
|---|---|
| Linux dev / CI | `atlas-c-sdk-linux-gcc11-x64-2.19.0` |
| Windows deployment | `atlas-c-sdk-windows-vs16-x64-mt-2.19.0` |

Install steps and the `CAPA_FLIR_ATLAS_ROOT` environment variable are
documented in the [capa-flir README](https://github.com/GraysonBellamy/capa-flir/blob/main/README.md).

## Configuration

```toml title="configs/hardware/flir_e85_real.toml"
[[cameras]]
name = "ir_cam0"
adapter = "capa_flir.flir_ir"
kind = "ir"
estimated_bps = 4_500_000        # 30 Hz uncompressed .csq, conservative
on_failure = "warn"

[cameras.params]
circular_buffer_frames = 60
compression = false
discovery_timeout_s = 5.0
```

The Windows E85 USB driver enumerates the camera as model
`"FLIR USB Video"` (a generic vendor string), so `model_hint = "FLIR E85"`
does *not* match on Windows. Leave the hint blank for single-camera
rigs; switch to a serial-based selector when adding a second IR camera.

| Key | Default | Notes |
|---|---|---|
| `circular_buffer_frames` | `60` (must be > 0) | Atlas-side circular frame buffer. |
| `compression` | `false` | Uncompressed `.csq`. Set `true` for compressed at the cost of CPU during recording. |
| `discovery_timeout_s` | `5.0` | How long `discover()` waits for `onDiscoveryFinished`. Comfortable for USB + LAN scans. |
| `interfaces` | `COMM_USB` | Bitmask passed to Atlas discovery. |
| `connect_max_attempts` | `5` | The E85 has a racy connect handshake; see [Quirks](#quirks). |
| `connect_retry_delay_s` | (default in `_DEFAULT_CONNECT_RETRY_DELAY_S`) | Per-attempt delay. |

## Radiometric vs visual

The Atlas SDK exposes the camera in two distinct flavors:

- **Radiometric** — per-pixel temperature values. This is what makes
  IR useful: every pixel of every frame carries a temperature, not
  just an intensity. The `.csq` container preserves the radiometric
  payload so post-analysis can re-derive temperatures at any
  emissivity / atmospheric setting.
- **Visual / palette** — the false-color rendering the operator sees
  in the preview tile. Independent of the recorded radiometric data;
  changing the palette mid-run does not affect the `.csq`.

The `RADIOMETRIC` capability flag is always declared. `PALETTE`
controls the *preview-side* palette (what the dashboard renders).
`REMOTE_PALETTE` controls the camera's onboard display palette — a
separate property under Atlas's `ACS_Remote_Palette_*` API.

## Recording: native `.csq` capture

The adapter delegates recording to Atlas's
`ACS_ThermalSequenceRecorder` rather than rolling its own writer.
Output lands at the bundle path with extension `.csq`.

Size implications (rough order of magnitude):

- Uncompressed E85 at 30 Hz: ~4–5 MB/s (the `estimated_bps =
  4_500_000` default is conservative).
- Compression on: ~30–50% reduction depending on scene complexity, at
  noticeable CPU cost during recording.

For multi-minute pyrolysis runs the file size adds up — a 10-minute
uncompressed E85 capture is ~3 GB. Plan disk space accordingly; the
`estimated_bps` field drives the preflight check.

## Preview pipeline

IR cameras use a different preview path than the [webcam
adapter](cameras-webcam.md): Atlas frame callbacks arrive on the
Atlas worker thread, and `FramePump` bridges them onto the engine
loop's queue. The `_preview` module converts the radiometric payload
into a colored JPEG using the selected palette (`ACS_PALETTE_PRESET_IRON`
default) at the same 2 Hz cadence as the webcam preview.

`buffer_color_space(ACS_COLOR_SPACE_RGB)` keeps the preview output
sRGB so the dashboard renders consistently across cameras.

## Capabilities

`_BASE_CAPABILITIES` (always declared on a connected E85+Atlas):

| Flag | Notes |
|---|---|
| `RADIOMETRIC` | Per-pixel temperature available. |
| `PALETTE` | Preview-side palette is configurable. |
| `MEASUREMENT_SHAPES` | Spotmeters / boxes available (currently Atlas-side only). |
| `SUPPORTS_DISCOVERY` | The Atlas discovery API. |
| `MODEL_HINT` / `SERIAL_SELECT` | Standard camera selection. |
| `LIVE_PREVIEW` | Pumps preview JPEGs. |

Probed at `open()` time (some firmware variants omit certain
properties — the adapter ORs these in only when the property exists):

| Flag | Atlas property |
|---|---|
| `NUC_TRIGGER` | One-shot non-uniformity correction (`ACS_Remote_Calibration_nuc_executeSync`). |
| `RADIOMETRIC_PARAMS` | The bundled radiometric kit — emissivity, atmospheric temperature/transmission, reflected temperature, object distance, relative humidity. Atlas ships these together so capa exposes them as one flag. |
| `TEMPERATURE_RANGE_SELECT` | Range enumeration + selection (`ACS_Remote_TemperatureRange_*`). Switching ranges forces a multi-second recalibration; the adapter refuses while recording. |
| `AUTO_NUC_INTERVAL` | Auto-NUC scheduler interval (seconds; `0` = disabled). |
| `REMOTE_PALETTE` | Camera-side display palette selection. Distinct from `PALETTE` (preview-side). |

## Commands

The FLIR adapter has the richest typed command surface in the camera
family. Every write goes through the [authorization
gate](../safety/authorization-gates.md).

| Typed call | Purpose |
|---|---|
| `trigger_nuc()` | One-shot NUC; pauses imaging briefly. |
| `read_nuc_state()` | Current NUC state name. |
| `read_auto_nuc_interval_s()` / `set_auto_nuc_interval_s(seconds)` | Auto-NUC scheduler. `0` disables. |
| `read_temperature_ranges()` / `read_temperature_range_index()` / `set_temperature_range(index)` | Range enumeration + switch. Refused mid-recording. |
| `read_emissivity()` / `set_emissivity(value)` | 0..1. |
| `read_distance_m()` / `set_distance_m(value)` | Object distance. |
| `read_relative_humidity()` / `set_relative_humidity(fraction)` | 0..1. |
| `read_atmospheric_transmission()` / `set_atmospheric_transmission(value)` | 0..1. |
| `read_atmospheric_temp_c()` / `set_atmospheric_temp_c(celsius)` | |
| `read_reflected_temp_c()` / `set_reflected_temp_c(celsius)` | |
| `read_remote_palettes()` / `read_remote_palette()` / `set_remote_palette(name)` | Camera-side display palette. |
| `read_battery_percent()` | Where supported. |
| `read_camera_information()` | Identity / firmware dict. |

## Discovery

`FlirIrAdapter.discover()` uses Atlas's discovery API. The Setup tab
runs it through the cross-cutting
[`capa.devices.discovery`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/discovery.py)
dispatcher; the hook returns `CameraInfo` rows with `model`,
`serial`, and `transport="usb"` (or `"ethernet"` over LAN).

See [Discovery](discovery.md) for the cross-cutting UX.

## Quirks

### E85 connect handshake is racy

`ACS_Camera_connect` may return "device is not ready" for the first
several attempts even when the camera is in fact fine. The default
`connect_max_attempts = 5` matches the retry budget used by the
working `flir_streamer.exe` reference program; lower it only if you
have a specific reason. The `connect_retry_delay_s` knob controls the
inter-attempt delay.

This is the IR-camera analog of the [Sartorius cold-open
race](sartorius.md#cold-open-race) — pay the cost once at config
load, not once per run.

### Atlas callbacks fire on the SDK worker thread

Atlas owns its own thread; capa interacts with it through `FramePump`
(thread-safe queue) and held `PreviewCallbacks` (kept alive while
Atlas holds cdata pointers). This is invisible to operators but
matters when reasoning about post-finalize race conditions in the
extractor.

### Temperature-range switch is destructive to recording

Switching `set_temperature_range(index)` forces a multi-second
recalibration. The adapter refuses the operation while recording is
in progress; do this between runs.

### Sim equivalent — capa-private "fake-csq"

[`capa.devices.sim.flir_ir_sim`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/flir_ir_sim.py)
writes a small deterministic file with a capa-private magic
(`b"CAPA-IR-SIM\n"`) — *not* a real FLIR FFF/`.csq`. The magic is
distinct so a real Atlas extractor cannot mistake it for a vendor
file. The sim's job is twofold:

1. Prove the engine's camera-task wiring end-to-end without an Atlas
   install: files appear in the bundle, `manifest.json.cameras`
   populates, `manifest.sha256` covers the file, the frame-index
   parquet round-trips.
2. Give downstream tools a header-only parser target — capa core
   ships its own `extract_frame_index` for sim files; `capa-flir`
   ships the Atlas-backed extractor.

The two never overlap (different magic). See [Simulators](simulators.md).

## See also

- [Camera preview](../user-guide/camera-preview.md) — operator-facing UI page with the IR palette / span controls.
- [USB webcams](cameras-webcam.md) — the visible-spectrum peer.
- [Devices overview](overview.md) — adapter contract and the camera-vs-device distinction.
- [Authorization gates](../safety/authorization-gates.md) — the contract for any camera command.
- [Discovery](discovery.md) — cross-cutting Setup-tab and CLI behavior.
- [capa-flir README](https://github.com/GraysonBellamy/capa-flir/blob/main/README.md) — Atlas SDK install steps, environment variables, troubleshooting.
