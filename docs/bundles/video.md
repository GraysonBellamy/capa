---
description: capa bundle video/ layout — webcam .mkv via PyAV, FLIR IR .csq via Atlas SDK, frames.parquet sidecars mapping frame index to monotonic time, meta sidecars.
---

# Video

**Audience:** analysts handling recorded video (visible and IR); operators triaging a partial recording; plugin authors writing a new camera adapter.
**Scope:** the `video/` directory layout — visible-camera `.mkv` files, FLIR IR `.csq` files, the `<name>.frames.parquet` sidecar that maps frame number to monotonic time, and the external `output_root` case.

Every camera in a run produces up to three artifacts: a **container file** (visible cameras write `.mkv`, IR cameras write `.csq`), a **`<name>.frames.parquet` sidecar** that maps every frame index to the run's monotonic clock, and — for IR cameras only — an adapter-specific **`<name>.csq.meta.json`** metadata sidecar. The container is owned by the camera adapter; the frame-index sidecar is owned by a separate per-camera sink in capa's storage layer. The split matters: if an adapter crashes mid-recording and leaves a truncated container, the sidecar is still complete and the analyst can recover frame timestamps for every frame the adapter actually pushed.

---

## The `video/` directory

A two-camera run (one visible webcam, one FLIR IR camera) lands the following files inside the bundle's `video/` subdirectory:

```
runs/<run_id>/video/
├── webcam0.mkv                  # visible container (PyAV / libx264)
├── webcam0.frames.parquet       # frame-index sidecar (visible)
├── flir_ir.csq                  # IR container (FLIR SDK / Atlas)
├── flir_ir.frames.parquet       # frame-index sidecar (IR)
└── flir_ir.csq.meta.json        # IR-only metadata sidecar
```

The file stems (`webcam0`, `flir_ir`) are the camera names declared in `CameraSpec.name`. The same stem is reused for every artifact a camera produces, so a single `glob("video/<name>.*")` round-trips every file the bundle owns for that camera.

When a camera is excluded from the resolved recording plan — for example a `recording_policy` rule that suppresses the IR camera for diagnostics-only runs — **no `video/<name>.*` files exist at all** for that camera, and the manifest records `recorded=False` with a `suppressed_reason`. See [`CameraEntry` fields you'll care about](#cameraentry-manifest-fields-youll-care-about) below.

---

## Visible recording — `.mkv` via PyAV

Visible cameras (USB webcams, capture cards) record through capa's [`WebcamAdapter`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/camera/webcam/adapter.py), which drives PyAV. The container is **Matroska** (`.mkv`), not MP4.

**Why MKV and not MP4?** MKV tolerates a truncated tail. An MP4 stores its `moov` atom (the per-frame index that makes the file seekable) at the end of the file by default; if a run crashes before the encoder flushes, an MP4 is at best painful to recover and at worst unplayable. MKV is built around a streaming-friendly EBML structure that any conformant player can read up to the point of truncation. Operator-recoverable bundles win over a marginally smaller container.

The defaults live in [`constants.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/camera/webcam/constants.py):

| Knob | Default | Override |
|---|---|---|
| Container | `matroska` (`.mkv`) | not user-overridable |
| Codec | `libx264` | `CameraSpec.params["codec"]` |
| Pixel format | `yuv420p` | `CameraSpec.params["pix_fmt"]` |
| Frame rate | `30 fps` | `CameraSpec.params["fps"]` |
| libx264 tuning | `preset=veryfast, tune=zerolatency` | not user-overridable |

The keyframe interval is **not pinned** by the adapter — libx264 chooses its own GOP based on the scene-change detector and the `zerolatency` tune. Operators who need a guaranteed keyframe cadence (for example, frame-accurate extraction at fixed intervals without decoding the whole stream) must transcode the `.mkv` after the run with their preferred GOP setting.

The encoder is opened in [`_open_encoder`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/camera/webcam/adapter.py) and the container metadata carries `run_started_utc`, `camera_name`, and `capa_codec` so an external tool can re-correlate by absolute time without parsing the bundle manifest.

**The container is owned by the adapter, not by the storage sink.** [`video_sink.py`'s module docstring](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/video_sink.py) is explicit about the division: the sink writes only the frame-index parquet. If the adapter forgets to close its container handle on shutdown, the result is a truncated `.mkv` — and the sidecar is still complete.

---

## IR recording — FLIR `.csq`

IR cameras record to **native FLIR `.csq`** (a FFF-based proprietary container). The real adapter ships in the `capa-flir` plugin and uses FLIR's Atlas SDK; the in-tree [`flir_ir_sim`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/flir_ir_sim.py) emits a capa-private fake-`.csq` (distinct magic, header-only parser) so the storage path can be tested end-to-end without the SDK.

**Why not transcode to a portable codec?** Radiometric calibration. A `.csq` frame carries per-pixel temperature data — emissivity, atmospheric attenuation, calibration tables, the camera's optical chain, all baked into the stream's pixel values. Any transcode to H.264, ProRes, or any standard codec collapses the radiometric channel into 8-bit luminance and loses the temperatures **irreversibly**. The `.csq` is the canonical product. capa does not transcode it.

**The `<name>.csq.meta.json` sidecar** carries the adapter-side metadata the SDK exposes per frame — written when recording starts and rewritten with the final frame count when recording stops. From [`_write_meta_sidecar`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sim/flir_ir_sim.py):

- `name`, `adapter`, `model`, `serial` — camera identity
- `fps`, `width`, `height` — stream format
- `started_mono_ns_offset` — `RunClock.t_mono_ns()` captured at `start_recording`, the anchor that lets frame indices map to run time
- `started_utc` — wall-clock equivalent of the anchor
- `output_path` — the container filename next to the sidecar
- `frame_count`, `file_size_bytes`, `final` — final counts after `stop_recording`
- (real adapter) emissivity, atmospheric temperature / transmission, reflected temperature, distance, relative humidity, NUC state, palette — every radiometric and control-surface parameter the SDK reports

**Reading the `.csq` itself is not a one-line job.** capa does not bundle a CSQ reader. Use FLIR's Researcher IR tools, FLIR Atlas SDK, or one of the third-party parsers (e.g. `exiftool` for header inspection, `flirpy` for radiometric extraction). The frame-index sidecar and the meta JSON give you everything you need to drive the SDK to a specific frame — pixels themselves require the vendor toolchain.

---

## The `frames.parquet` sidecar

This is the central artifact this page exists to document. Every camera writes a `<name>.frames.parquet` next to its container, with the schema locked at v1 in [`video_sink._arrow_schema()`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/video_sink.py):

| Column | Type | Notes |
|---|---|---|
| `frame_idx` | `int64` (non-null) | Camera-assigned monotonic id, 0-based at recording start. |
| `t_mono_ns` | `int64` (non-null) | `RunClock`-derived monotonic ns. Anchor in `manifest.started_mono_ns_anchor`. |
| `t_utc` | `timestamp[us, tz=UTC]` (non-null) | Wall-clock anchor for human inspection. |
| `capture_latency_s` | `float64` (non-null) | SDK→Python hand-off latency. Tail latencies here indicate a slow camera or contended CPU. |
| `camera` | `dict<string>` (non-null) | Stable camera name (matches the file stem). |

**Why this sidecar exists at all.** Container-level PTS timestamps are nowhere near precise enough to align video frames with channel samples at the millisecond granularity capa expects. Visible `.mkv` PTS is wall-clock-ish and drifts; `.csq` per-frame timestamps come from the camera's own clock, not capa's `RunClock`. The sidecar collapses the problem: an analyst can ask **"what was the heater setpoint at the moment frame 4521 was captured?"**, look up `t_mono_ns` in `frames.parquet`, and join against [`scalars.parquet`](parquet-channel-samples.md) with a clean integer-nanosecond key. No PTS arithmetic, no clock-domain conversion, no surprises.

The `capture_latency_s` column is the signal to watch when a recording looks slow or jittery. For the webcam path it's the delta between PyAV's `frame.time` (the SDK's view of when the frame was captured) and capa's view of when the frame was handed off — sustained tail latencies here mean the camera is slow, the worker thread is contended, or the CPU is saturated.

---

## Anchoring to the manifest

`CameraEntry.started_mono_ns_offset` is the value of `RunClock.t_mono_ns()` captured at `start_recording()` for that camera. It's how an analyst converts `frame_idx` → run-relative time without trusting the container's internal clock.

The manifest's `started_mono_ns_anchor` (a top-level field) is the corresponding run-start anchor. Subtract one from the other and you get the camera-start offset relative to the run:

```
camera_start_relative_s =
    (CameraEntry.started_mono_ns_offset - manifest.started_mono_ns_anchor) / 1e9
```

For the common case (camera starts recording at run start), this is ~0. For cameras driven by a `record` step that fires mid-run, it's the offset into the run where recording began.

---

## External `output_root`

When `CameraSpec.output_root` is set, the container file lives **outside** the bundle directory — typically a faster SSD or a dedicated capture drive. The pattern is `<output_root>/<run_id>/video/<name>.<ext>`. In that case:

- The **sidecar still lives inside the bundle.** `<name>.frames.parquet` is tiny (a few hundred kB for a 30 Hz run); putting it next to the manifest keeps the bundle self-describing for the index-only case.
- `CameraEntry.output_path_external` records the external container path.
- `CameraEntry.output_path` records the **bundle-relative POSIX reference** even when external — the field is non-optional, and the manifest module preserves it so analysis tools have a stable name to query alongside the absolute external pointer.

**Archival implication:** if you move the bundle to another machine, you must also move the external container file (or update the analysis tool to find it). The bundle's `manifest.sha256` covers the in-bundle sidecar, not the externally-rooted container; the bundle is internally consistent on its own, but the pixels live elsewhere.

---

## `CameraEntry` manifest fields you'll care about

Every camera in the run gets one [`CameraEntry`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/manifest.py) in `manifest.cameras`. Key fields:

| Field | Purpose |
|---|---|
| `name`, `adapter`, `kind`, `model`, `serial` | Identity. `kind` is `"visible"` or `"ir"`. |
| `output_path` | Bundle-relative POSIX path to the container (always populated). |
| `output_path_external` | External container path when `CameraSpec.output_root` was in effect, else `None`. |
| `frames_path` | Bundle-relative path to `<name>.frames.parquet`. `None` until finalize rewrites the in-flight file. |
| `meta_path` | Path to `<name>.csq.meta.json` for IR cameras; `None` for visible. |
| `frame_count` | Final frame count after finalize. |
| `started_mono_ns_offset` | The anchor for `frame_idx → t_mono_ns`. |
| `healthy`, `error` | Final health verdict; `error` is a short string for failed recordings. |
| `recorded`, `suppressed_reason` | `recorded=False` when the recording plan excluded the camera (e.g. `recording_policy`); `suppressed_reason` explains why. |

Every field is non-`None` for a normal happy-path recording; `recorded=False` is the only case where the bundle has a `CameraEntry` with no on-disk artifacts.

---

## Recipes

**Extract a frame at a given event timestamp** (pseudocode — the runnable version lives in [Reading a bundle](reading-bundles.md)):

```python
# 1. Look up the event in events.sqlite to get its t_mono_ns.
# 2. Load the per-camera frame index:
#    frames = polars.read_parquet("video/webcam0.frames.parquet")
# 3. Find the frame_idx whose t_mono_ns is closest to the event time:
#    target_idx = frames.filter(
#        (frames["t_mono_ns"] - event_t_mono_ns).abs().arg_min()
#    )["frame_idx"]
# 4. Decode that frame out of the container with ffmpeg / opencv / pyav.
```

**Play the visible `.mkv` with `ffplay`:**

```
ffplay runs/<run_id>/video/webcam0.mkv
```

**Inspect the IR meta sidecar:**

```
python -m json.tool runs/<run_id>/video/flir_ir.csq.meta.json
```

---

## What's NOT in scope

For symmetry with what *is* in the bundle:

- **We do not transcode IR to a portable codec.** The `.csq` is the canonical product; any conversion loses radiometric data.
- **The frame-index sidecar does NOT carry frame-level temperature data.** That lives in the `.csq`. The sidecar is timestamps only.
- **The sidecar does NOT carry exposure, focus, or radiometric-parameter changes.** Those are adapter-emitted events that land in [`events.sqlite`](events-sqlite.md) (e.g. `nuc_triggered`, `set_emissivity`).
- **Frame extraction is left to the analyst's choice of tool.** The bundle gives you the offsets; pixels require ffmpeg / pyav / FLIR Atlas / Researcher IR per container type.
- **No per-camera audio.** Microphones are not part of capa's recording model.

---

## Implementation notes for contributors

A few details about the sink that aren't obvious from the public API:

- **The frame-index schema is locked at v1** (see [`_arrow_schema()`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/video_sink.py)). Adding a column requires a bundle schema bump — see [bundle versioning](bundle-versioning.md).
- **Two-stage finalize.** During the run, the sink writes `<name>.frames.in-flight.arrows` as an Arrow IPC stream (durable, append-only, recoverable from a truncated tail). The finalize stage rewrites it to `<name>.frames.parquet` sorted by `t_mono_ns` with large row groups and zstd compression — same treatment `scalars.parquet` gets.
- **The flush threshold is 256 rows** (`DEFAULT_FLUSH_ROWS_FRAMES`, roughly 8 s of 30 Hz video). That's the worst-case data loss window if the process dies between flushes — small enough that even a crash mid-recording leaves the in-flight file usefully complete.
- **One sink instance per camera.** Multiple cameras = multiple sinks = multiple `frames.parquet` files. Sinks don't share state and don't coordinate; routing is by `FrameReceipt.name`.
- **The sink does NOT own the container handle.** If an adapter forgets to close its `.mkv` or `.csq` cleanly on shutdown, the container is truncated — but the sidecar is unaffected. This is the asymmetry that makes recovery possible.
- **`FrameReceipt` is the protocol contract** between adapter and sink ([`base.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/camera/base.py)). New camera adapters only need to emit `FrameReceipt` instances with correct `name`, `frame_idx`, `t_mono_ns`, `t_utc`, and `capture_latency_s` — the sink does the rest.

---

*See also:* [What's in a bundle](what-is-a-bundle.md) · [Reading a bundle](reading-bundles.md) · [Manifest and schema § Cameras](manifest-and-schema.md#cameras) · [Cameras: webcam](../devices/cameras-webcam.md) · [Cameras: FLIR](../devices/cameras-flir.md) · [Camera preview](../user-guide/camera-preview.md)
