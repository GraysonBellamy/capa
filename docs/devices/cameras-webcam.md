# USB webcams

**Audience:** operators with one or more USB cameras on the rig (typically a visible-light camera mounted above the sample).
**Scope:** capa's webcam adapter — the PyAV → MKV pipeline, the always-on preview pump, encoder choice and its saturation implications, UVC controls, and recovery from disconnect mid-run.

---

## At a glance

| | |
|---|---|
| Adapter id | `capa.devices.camera.webcam` |
| Adapter family | `camera_visible` |
| Real adapter | [`capa.devices.camera.webcam`](https://github.com/GraysonBellamy/capa/tree/main/src/capa/devices/camera/webcam) |
| Sim adapter | none — visible cameras have no sim counterpart today |
| Resource scheme | `webcam:<selector>` |
| Container | Matroska (`.mkv`) |
| Default codec | `libx264` |
| Default frame size / rate | `1280×720 @ 30 fps` |
| Preview cadence | 2 Hz (`PREVIEW_INTERVAL_NS = 500_000_000`) |

Cameras are *peers* of devices, not subtypes — they implement
[`Camera`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/camera/base.py),
not `DeviceAdapter`. Emissions are `FrameReceipt` + `CameraHealth`
records (one per frame, posted from the worker thread), not
`ChannelSample`s. See [Devices overview § "The four sibling
libraries"](overview.md#the-four-sibling-libraries) for the placement.

## Supported hardware

Any camera the OS exposes through its native capture API and that PyAV
can open via the per-OS demuxer:

| OS | Demuxer | URL form |
|---|---|---|
| Linux | `v4l2` | `/dev/video0` |
| macOS | `avfoundation` | `default` or `0` |
| Windows | `dshow` | `video=<friendly-name>` |

`from_params` picks the default for the current OS; `input_format` and
`input_url` override.

## Configuration

```toml title="configs/hardware/webcam_real.toml"
[[cameras]]
name = "visible_cam0"
adapter = "capa.devices.camera.webcam"
kind = "visible"
estimated_bps = 1_500_000        # 30 fps × 720p H.264 ≈ 2 Mbps; gives headroom
on_failure = "warn"

[cameras.params]
fps = 30
width = 1280
height = 720
codec = "libx264"
pix_fmt = "yuv420p"
input_format = "dshow"
input_url = "video=Logitech Webcam C930e"
```

Note: cameras live under `[[cameras]]`, not `[[devices]]`.

[`WebcamParams`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/camera/webcam/descriptor.py)
is the Setup-editor view; the adapter itself validates kwargs the
existing way.

| Key | Default | Notes |
|---|---|---|
| `fps` | `30` (`DEFAULT_FPS`) | Encoder target. Must be > 0. |
| `width` / `height` | `1280` / `720` | Frame size. The dshow `list_options` probe (Windows) enumerates the device's supported sizes — see [Quirks](#quirks). |
| `codec` | `"libx264"` (`DEFAULT_CODEC`) | See [Encoder choice](#encoder-choice). |
| `pix_fmt` | `"yuv420p"` (`DEFAULT_PIX_FMT`) | H.264-compatible default. |
| `input_url` | platform default | OS-specific demuxer URL. |
| `input_format` | platform default | PyAV format name. |

## Encoder choice

The encoder is the dominant CPU cost of recording and the most common
cause of the `sat` (saturation) pill turning yellow. Choices that ship:

| `codec` | Hardware | When to use | Saturation risk |
|---|---|---|---|
| `libx264` | CPU only | Default. Available everywhere. | Highest CPU — watch the `sat` pill on dense rigs. |
| `h264_qsv` | Intel Quick Sync | Intel iGPU available. | Low CPU; lowest sustained risk. |
| `h264_nvenc` | NVIDIA NVENC | Discrete NVIDIA GPU available. | Very low CPU. |
| `mjpeg` | CPU | Diagnostic / max-fidelity at small frame sizes. | Very large output files; disk I/O becomes the limit before CPU does. |

If the `sat` pill goes yellow on a rig that's idle elsewhere, switch
from `libx264` to one of the hardware encoders before trying to lower
fps or resolution. See [saturation
deadline](../safety/saturation-and-deadlines.md) for the escalation
contract.

## How the pipeline is wired

One long-lived input pump opens the camera once at
`start_input_pump` and closes it once at `stop_input_pump` — *not*
per-recording. The pump unconditionally emits 2 Hz preview JPEGs onto
`preview_stream` and, while `_recording=True`, additionally encodes
each frame to the output container and emits a `FrameReceipt`.

This is the design point that fixed the multi-second freeze of the
live preview tile after every run-stop on Windows. The DirectShow
filter graph hold-time used to be paid every time the camera was
opened; opening once per `WorkerPool` instead of once per recording
moves the cost to config load.

MKV container metadata carries the run-start UTC anchor so an external
tool can re-correlate by absolute time without parsing capa's
manifest.

## Recording vs preview — the mental model

```
                     +----+
   Camera frame ---> | pump | --(every frame, while recording)--> encoder --> .mkv
                     |      | --(every 500 ms, always)-----------> preview JPEG
                     +----+
```

The live tile reads `preview_stream` — it is *always* fed, between
runs and during runs. The encoder reads the same frames but only
activates when `start_recording(path)` is called. The two streams
share one PyAV input but diverge after that.

What this means in practice:

- **Preview never goes blank** because the camera was idle between
  runs.
- **Recording badge on the Run tab** is the only reliable signal that
  the encoder is *also* consuming the stream. Trust the badge, not the
  preview, to confirm capture is happening. See [Camera
  preview](../user-guide/camera-preview.md).
- **Disconnect mid-run** — the pump emits a `CameraEvent`; the
  encoder closes the output container; the preview also stops. The
  bundle keeps whatever was recorded; the operator sees the camera
  badge go red.

## UVC controls (Windows only)

When the `duvc-ctl` wheel is installed, the adapter probes the camera
for UVC properties (brightness, contrast, exposure, focus, zoom, pan,
tilt) and adds the corresponding `CameraCapability` flags after
`open()`. Operators can set these from the manual-control card.

Off Windows, UVC controls are not exposed. The flags stay absent and
the UI does not render the widgets.

## Capabilities

Always declared (`_BASE_CAPABILITIES`):

| Flag | Notes |
|---|---|
| `SUPPORTS_DISCOVERY` | The per-OS `discover_cameras()` hook. |
| `SERIAL_SELECT` | Honors `CameraSpec.serial` for exact-match selection. |
| `MODEL_HINT` | Honors `CameraSpec.model_hint` for friendly-name matching. |
| `LIVE_PREVIEW` | Pumps frames onto `preview_stream`. |
| `STREAM_FORMAT` | PyAV reopens the input with new resolution/framerate on the next `start_recording`. |

Added after `open()` (Windows, depending on what `duvc-ctl` probes):
the UVC-control flags listed in
[`base.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/camera/base.py).

## Discovery

`discover_cameras()` walks the per-OS enumeration API:

| OS | Path |
|---|---|
| Linux | `/sys/class/video4linux/video*` + sysfs metadata via `_probe_v4l2_info`. Dedups multiple `/dev/videoN` nodes per physical camera by `bus_info`. |
| Windows | `duvc_ctl.list_devices()` (requires the wheel). |
| macOS | Returns `[]` — AVFoundation enumeration is future work; add macOS cameras by hand. |

Each row carries `selector`, `model`, `serial`, and `transport`.

See [Discovery](discovery.md) for the cross-cutting UX.

## Quirks

### dshow `list_options` resolution probe

On Windows, the adapter calls `av.open(input_url, format="dshow",
options={"list_options": "true"})` to enumerate the device's pin
formats — `(width, height)` pairs and per-resolution max fps caps. The
call always fails with "Immediate exit requested" (intentional), but
the format dump lands on the libav log channel first; the adapter
captures it via `av.logging.Capture` and parses the
`max s=WxH fps=NN.NNN` lines.

The result populates `_supported_resolutions` and
`_resolution_fps_caps` so the Setup tab's resolution spinbox can be
capped at the device's actual capability. On parse failure (or non-
Windows paths) the lists stay empty and the spinbox falls back to a
static set.

### Open-retry backoff

`OPEN_RETRY_DELAYS_S = (0.25, 0.5, 1.0, 2.0, 2.0, 2.0)` covers the
DirectShow hold-time after a previous `cam.close()` — cumulative
≈ 7.75 s, which matches the worst observed Logitech C930e release
latency on Windows 11. POSIX paths typically open first try and the
retries are dormant.

`OPEN_RETRY_DEADLINE_S = 8.0` is the hard ceiling; past that, the
underlying problem isn't transient and the original error is surfaced.

### `pix_fmt` mismatches

PyAV will silently re-format frames if the requested `pix_fmt` doesn't
match the device's native format. The `_reformat_to_rgb24` helper
handles the preview path; the encoder takes the requested `pix_fmt`
verbatim. If the device cannot deliver the requested format the open
will fail loudly — but if it can deliver a *similar* one, you may not
notice the reformat cost until the saturation pill goes yellow.

### Preview throttling and CPU impact

The 2 Hz preview encodes one JPEG every 15 frames (at 30 fps).
`PREVIEW_MAX_WIDTH = 320` keeps the payload well under 30 kB at
`PREVIEW_JPEG_QUALITY = 70`. The encode itself runs in the worker
thread already used for H.264, so the dominant cost is the recording
encoder, not the preview.

### No sim equivalent

Visible cameras do not have a sim adapter. UI iteration on the camera
tile uses [`flir_ir_sim`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/flir_ir_sim.py)
(IR) or a real webcam.

## See also

- [Camera preview](../user-guide/camera-preview.md) — operator-facing UI page for the live tile and per-camera controls.
- [FLIR IR cameras](cameras-flir.md) — the IR peer.
- [Devices overview](overview.md) — adapter contract and the camera-vs-device distinction.
- [Saturation deadline](../safety/saturation-and-deadlines.md) — what happens when the encoder can't keep up.
- [Hardware TOML](../configuration/hardware-toml.md) — `[[cameras]]` block schema.
- [Discovery](discovery.md) — cross-cutting Setup-tab and CLI behavior.
