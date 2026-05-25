# Camera preview

**Audience:** operators with one or more cameras on the rig (USB webcam, FLIR IR, or both).
**Scope:** what the live preview tile shows, how it relates to recording, what the cadence indicator and border colors mean, and the per-camera controls.

---

## The mental model

The preview tile and the recording stream are **separate consumers of
the same camera frames**. The preview is always live; recording only
happens between `Run → Start` and `Run → Stop`.

```
                     +----+
   Camera frame ---> | pump | --(every frame, while recording)--> encoder → .mkv / .csq
                     |      | --(every 500 ms, always)-----------> preview tile
                     +----+
```

What follows from this:

- **The tile shows a live image even when no run is active.** This is
  on purpose. The pump opens the camera once at config-load and keeps
  it open across runs — the Windows DirectShow filter graph hold-time
  used to freeze the tile for several seconds after every run-stop;
  the open-once design removed that.
- **A live tile does not mean recording is happening.** The recording
  badge on the Run tab is the only reliable signal. Trust the badge,
  not the moving image.
- **A frozen tile during a run means the pump stopped feeding *both*
  consumers.** If the tile is live and the badge is on, recording is
  happening.

## Cadence indicator

Each tile has a small cadence indicator that flips between three
states based on how recently a preview JPEG arrived:

| State | Meaning |
|---|---|
| `idle` | No preview has arrived yet (the dock was just created, or the camera has no `LIVE_PREVIEW` capability). |
| `live` | A preview arrived recently — within the last 2.5 s. |
| `stale` | No preview for ≥ 2.5 s. The preview cadence is nominally 2 Hz (500 ms); a single dropped tick will not trip `stale`. |

A `stale` indicator during an active run is the operator's first
signal that something is wrong with the camera path. Check the
diagnostics dock and the per-tile drop counter.

## Tile border colors

The border is sticky — once it changes, it does not auto-revert. A
new color is shown until the dock is rebuilt at the next config-load.
This is on purpose: a transient camera fault that auto-cleared would
hide a bug.

| Border | Trigger |
|---|---|
| Idle / default | No event yet. |
| **Yellow** | A `pump_warning` event arrived (typically a frame the encoder rejected — e.g. libx264 returned EINVAL on a malformed input). The drop counter on the tile increments and recording continues. |
| **Red** | A `pump_failed` event arrived. Recording for this camera ended in fault. The tile is labeled `failed`. |

The visible-webcam path used to trip `pump_warning` at t ≈ 23 s into
a recipe and silently lose the run; the adapter now drops the bad
frame, logs the event, and keeps recording — the yellow border
exists so the operator sees that something was lost.

## Cameras without `LIVE_PREVIEW`

If an adapter does not declare `CameraCapability.LIVE_PREVIEW`, the
tile shows a static placeholder and never receives frames. The runtime
short-circuits at
[`CameraDeviceAdapter.start_preview_channel`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/camera_adapter.py)
so the preview bridge stays empty rather than burning CPU pulling
frames nobody will draw.

This is rare in production — every shipping camera adapter (webcam,
FLIR, IR sim) declares `LIVE_PREVIEW`.

## Per-camera controls

The controls available on a tile depend on the camera's capability
flags. The tile only renders widgets for flags the adapter actually
declares after `open()` — a fixed-focus laptop webcam will not show a
focus slider.

### Webcam (visible)

UVC controls are Windows-only and require the `duvc-ctl` wheel. On
Linux/macOS, UVC controls are absent.

| Control | Capability | Notes |
|---|---|---|
| Resolution / framerate | `STREAM_FORMAT` | PyAV reopens the input on the next `start_recording`. Refused mid-recording. |
| Exposure | `EXPOSURE_CONTROL` | UVC exposure is logged as `2^value` seconds; capa passes the raw int. |
| Focus | `FOCUS_CONTROL` | Fixed-focus cameras don't advertise. |
| Zoom | `ZOOM_CONTROL` | Distinguishes optical vs digital — silent fallback would mislead. |
| White balance | `WB_CONTROL` | Manual K + auto on/off. |
| Pan / tilt | `PAN_TILT_CONTROL` | PTZ cameras only (Logitech BRIO, PTZ Pro 2). Fixed cameras reject. |
| Brightness / contrast / saturation / sharpness / gamma / hue / gain / backlight | `IMAGE_ADJUST` | Grouped under one flag — most UVC cameras support at least brightness + contrast; finer gating is by probing each property's `PropRange` at `open()`. |

The dshow `list_options` probe (Windows) enumerates the device's
supported `(width, height)` pairs and per-resolution max fps — those
populate the resolution spinbox so it can't request a mode the
camera doesn't have.

See [USB webcams](../devices/cameras-webcam.md) for the adapter
implementation.

### FLIR IR

The FLIR adapter has the richest camera-side command surface. The
preview tile exposes the operator-facing subset; the rest is
available from the manual-control card or via procedures.

| Control | Capability | Notes |
|---|---|---|
| Palette (preview) | `PALETTE` | Iron / Rainbow / Grayscale etc. Preview-side only — does not affect the `.csq`. |
| Span / range | `TEMPERATURE_RANGE_SELECT` | Forces a multi-second recalibration; refused mid-recording. |
| Radiometric vs visual toggle | `RADIOMETRIC` | What gets rendered in the tile; the radiometric data is always recorded regardless. |
| Trigger NUC | `NUC_TRIGGER` | One-shot non-uniformity correction. Pauses imaging briefly. |
| Auto-NUC interval | `AUTO_NUC_INTERVAL` | `0` disables. |
| Remote palette (camera display) | `REMOTE_PALETTE` | The camera's on-device LCD palette. Distinct from `PALETTE` (preview-side). |
| Emissivity / distance / atmospheric temp / atmospheric transmission / reflected temp / RH | `RADIOMETRIC_PARAMS` | The bundled radiometric kit. |

See [FLIR IR cameras](../devices/cameras-flir.md) for the full
command list and the Atlas SDK requirements.

## Preview throttling and CPU impact

Preview is capped at 2 Hz (`PREVIEW_INTERVAL_NS = 500_000_000`).
JPEG width is capped at 320 px (aspect preserved) at quality 70 —
well under 30 kB even for high-detail frames.

For the webcam adapter, encoding the preview JPEG runs on the same
worker thread as the H.264 encoder — the dominant cost is the
recording encoder, not the preview. For the FLIR adapter, the Atlas
callback fires on its own thread and the JPEG conversion happens
downstream of `FramePump`.

If the `sat` (saturation) pill goes yellow on a rig with cameras
active, the recording encoder is the suspect — not the preview. See
[saturation deadline](../safety/saturation-and-deadlines.md) and
[the saturation triage flow](status-bar-guide.md#sat-saturation).

## Confirming the camera is being recorded

The preview is never blank between runs — so "I see a live tile" is
not proof of recording. The Run tab's per-camera status badge is the
authoritative signal:

| Badge | Meaning |
|---|---|
| Off / grey | Not recording. The preview tile may still be live. |
| Green | Recording. Frames flowing through the encoder; file growing. |
| Yellow border on the preview tile | At least one frame was dropped. Recording continues. |
| Red border on the preview tile | The pump failed and this camera's recording ended. The bundle keeps what was recorded up to that point. |

For the bundle-side question — *"did the recording actually land in
the manifest?"* — see [reviewing a run](reviewing-a-run.md) and the
manifest's `cameras` block ([bundles → manifest and
schema](../bundles/manifest-and-schema.md)).

## See also

- [USB webcams](../devices/cameras-webcam.md) — visible-camera adapter.
- [FLIR IR cameras](../devices/cameras-flir.md) — IR adapter.
- [Status bar guide](status-bar-guide.md) — the `sat` pill and other live indicators.
- [Saturation deadline](../safety/saturation-and-deadlines.md) — what triggers the yellow/red state.
- [The Run tab](the-run-tab.md) — where the recording badge lives.
- [Reviewing a run](reviewing-a-run.md) — confirming the recording made it into the bundle.
