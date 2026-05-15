"""Visible-camera adapter — PyAV-driven H.264 → MKV (plan §12.3).

The adapter has a single long-lived input pump
(:meth:`WebcamAdapter._run_input_loop`) that opens the
:class:`av.container.InputContainer` once at :meth:`start_input_pump`
and closes it at :meth:`stop_input_pump`. The pump unconditionally emits
2 Hz preview JPEGs onto ``preview_stream`` so the operator always has a
live tile between runs, and additionally encodes each frame to the
output container + emits a :class:`FrameReceipt` while ``_recording``
is set.

Two consumer paths:

* **Push mode.** Tests call :meth:`WebcamAdapter.push_frame` directly
  with a numpy ndarray to drive the encoder + preview path without
  going through the pump.
* **Pump mode.** Production: :meth:`start_input_pump` spawns the
  long-lived loop. The default input is a V4L2 device on Linux,
  AVFoundation on macOS, or DirectShow on Windows;
  ``params["input_url"]`` / ``params["input_format"]`` override.

Unifying the input pump across the recording / between-runs boundary
removes the DirectShow filter-graph hold-time that previously froze
the live preview tile for several seconds after every run-stop —
``av.open`` happens exactly once per pool open, not once per phase.

MKV container metadata carries the run-start UTC anchor (plan §12.5) so an
external tool can re-correlate by absolute time without parsing capa's
manifest.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import re
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

import anyio
import av
import numpy as np
import structlog
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from PIL import Image

from capa.core.clock import RunClock
from capa.core.errors import AdapterError
from capa.devices._helpers import (
    make_accepted_result,
    make_not_open_result,
    reject_unless_authorized,
)
from capa.devices.adapter import CommandResult, DeviceCommand
from capa.devices.camera._uvc import (
    AUTO_VERB_TO_PROPERTY,
    PROPERTY_BY_VERB,
    UvcController,
)
from capa.devices.camera.base import (
    CameraCapability,
    CameraEvent,
    CameraHealth,
    CameraInfo,
    CameraSpec,
    FrameReceipt,
    make_stream_pair,
)
from capa.devices.camera.metadata import UvcRangeMetadata, WebcamMetadata

_logger = structlog.get_logger("capa.devices.camera.webcam")

if TYPE_CHECKING:
    from capa.devices.registry import AdapterDescriptor


_BASE_CAPABILITIES: frozenset[CameraCapability] = frozenset(
    {
        CameraCapability.SUPPORTS_DISCOVERY,
        CameraCapability.SERIAL_SELECT,
        CameraCapability.MODEL_HINT,
        CameraCapability.LIVE_PREVIEW,
        # STREAM_FORMAT is always supported — PyAV reopens the input with
        # the new resolution/framerate on the next start_recording().
        # UVC control flags are added at open() time after duvc-ctl probes
        # the device; off Windows / on cameras with no controllable UVC
        # properties they stay absent.
        CameraCapability.STREAM_FORMAT,
    }
)

DEFAULT_CODEC = "libx264"
DEFAULT_PIX_FMT = "yuv420p"
DEFAULT_FPS = 30

OPEN_RETRY_DELAYS_S: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 2.0, 2.0)
"""Backoff schedule for transient ``av.open`` failures (Windows DirectShow
hold-time after ``cam.close()``). Cumulative ≈ 7.75 s, which covers the
worst-case observed C930e release latency on Windows 11. POSIX paths
normally open first try, so retries are dormant on Linux/macOS."""

OPEN_RETRY_DEADLINE_S: float = 8.0
"""Hard ceiling on retries; if we haven't opened by then the underlying
problem is not transient and we surface the original error."""

PREVIEW_INTERVAL_NS = 500_000_000
"""2 Hz preview cadence (plan §10.2; Camera Protocol docstring). At 30 fps the
adapter encodes one preview every 15 frames; the encode itself runs in the
worker thread already used for the H.264 pipeline."""

PREVIEW_MAX_WIDTH = 320
"""Preview thumbnails are width-capped, aspect preserved. 320 px keeps the
JPEG payload well under 30 kB at quality=70 even for high-detail frames."""

PREVIEW_JPEG_QUALITY = 70
"""Visually lossless enough for a thumbnail; cheap to encode at 2 Hz."""

_PLATFORM_DEFAULTS: dict[str, tuple[str, str]] = {
    "linux": ("v4l2", "/dev/video0"),
    "darwin": ("avfoundation", "default"),
    "win32": ("dshow", "video=0"),
}


def _platform_default_format() -> tuple[str, str]:
    """Return ``(input_format, default_input_url)`` for the current OS.

    Used when :attr:`CameraSpec.params` does not override. Only consulted by
    :meth:`WebcamAdapter.run_pump`; push-mode callers never hit this.
    """
    return _PLATFORM_DEFAULTS.get(sys.platform, ("v4l2", "/dev/video0"))


class WebcamAdapter:
    """Visible-camera adapter (plan §12.3).

    Constructed via :meth:`from_params` so a hardware TOML can declare it::

        [[hardware.cameras]]
        name = "visible_cam0"
        adapter = "capa.devices.camera.webcam"
        kind = "visible"
        params = { fps = 30, width = 1280, height = 720, codec = "libx264" }
    """

    __slots__ = (
        "_clock",
        "_codec",
        "_dropped_frames",
        "_event_recv",
        "_event_send",
        "_file_size",
        "_fps",
        "_frame_count",
        "_frame_recv",
        "_frame_send",
        "_height",
        "_info",
        "_input_format",
        "_input_url",
        "_last_frame_t_mono_ns",
        "_last_preview_t_mono_ns",
        "_open",
        "_output_container",
        "_output_path",
        "_output_stream",
        "_pix_fmt",
        "_preview_recv",
        "_preview_send",
        "_pump_stop",
        "_pump_task",
        "_recording",
        "_resolution_fps_caps",
        "_spec",
        "_started_t_mono_ns",
        "_supported_resolutions",
        "_uvc",
        "_width",
        "capabilities",
    )

    kind: Literal["visible"] = "visible"

    def __init__(
        self,
        spec: CameraSpec,
        *,
        clock: RunClock,
        fps: float = DEFAULT_FPS,
        width: int = 1280,
        height: int = 720,
        codec: str = DEFAULT_CODEC,
        pix_fmt: str = DEFAULT_PIX_FMT,
        input_url: str | None = None,
        input_format: str | None = None,
    ) -> None:
        if spec.kind != "visible":
            raise AdapterError(
                f"WebcamAdapter requires CameraSpec.kind == 'visible', got {spec.kind!r}"
            )
        if fps <= 0:
            raise AdapterError(f"WebcamAdapter fps must be > 0; got {fps}")
        self._spec = spec
        self._clock = clock
        self._fps = float(fps)
        self._width = int(width)
        self._height = int(height)
        self._codec = codec
        self._pix_fmt = pix_fmt
        platform_fmt, platform_url = _platform_default_format()
        self._input_url = input_url or platform_url
        self._input_format = input_format or platform_fmt
        self.capabilities = _BASE_CAPABILITIES
        self._info = CameraInfo(
            adapter="webcam",
            name=spec.name,
            model=spec.model_hint,
            serial=spec.serial,
            transport="usb",
            capabilities=tuple(c.name for c in self.capabilities if c.name is not None),
        )
        self._uvc: UvcController | None = None
        # Populated by :meth:`open` on Windows (dshow). Empty everywhere else;
        # the UI falls back to a static set when nothing was probed.
        self._supported_resolutions: list[tuple[int, int]] = []
        self._resolution_fps_caps: dict[tuple[int, int], float] = {}
        self._open: bool = False
        self._recording: bool = False
        self._pump_task: asyncio.Task[None] | None = None
        self._pump_stop: asyncio.Event | None = None
        self._frame_count = 0
        self._dropped_frames = 0
        self._file_size = 0
        self._last_frame_t_mono_ns: int | None = None
        self._last_preview_t_mono_ns: int | None = None
        self._started_t_mono_ns: int | None = None
        self._output_path: Path | None = None
        self._output_container: Any = None
        self._output_stream: Any = None

        self._frame_send: MemoryObjectSendStream[FrameReceipt]
        self._frame_recv: MemoryObjectReceiveStream[FrameReceipt]
        self._frame_send, self._frame_recv = make_stream_pair(64)
        self._preview_send: MemoryObjectSendStream[bytes]
        self._preview_recv: MemoryObjectReceiveStream[bytes]
        self._preview_send, self._preview_recv = make_stream_pair(2)
        self._event_send: MemoryObjectSendStream[CameraEvent]
        self._event_recv: MemoryObjectReceiveStream[CameraEvent]
        self._event_send, self._event_recv = make_stream_pair(16)

    # --------------------------------------------------------------- builders

    @classmethod
    def from_params(
        cls,
        *,
        spec: CameraSpec,
        clock: RunClock,
        fps: float = DEFAULT_FPS,
        width: int = 1280,
        height: int = 720,
        codec: str = DEFAULT_CODEC,
        pix_fmt: str = DEFAULT_PIX_FMT,
        input_url: str | None = None,
        input_format: str | None = None,
    ) -> WebcamAdapter:
        """TOML-friendly constructor (plan §16 ``from_params`` convention)."""
        return cls(
            spec=spec,
            clock=clock,
            fps=fps,
            width=width,
            height=height,
            codec=codec,
            pix_fmt=pix_fmt,
            input_url=input_url,
            input_format=input_format,
        )

    # -------------------------------------------------------------- properties

    @property
    def spec(self) -> CameraSpec:
        return self._spec

    @property
    def info(self) -> CameraInfo:
        return self._info

    @property
    def device_info(self) -> CameraInfo:
        """Duck-typed alias matching the device adapters' identity surface.

        ``_identity_from_device_info`` in the engine probes ``model`` and
        ``serial`` field names off ``adapter.device_info`` to populate
        ``equipment.toml`` and the manifest cameras section. Webcams
        present that data via :attr:`info` (a :class:`CameraInfo`); this
        alias makes the camera adapter shape symmetric with Watlow /
        Alicat / Sartorius for the engine's collector.
        """
        return self._info

    @property
    def resource_id(self) -> str:
        """Per-resource worker key (``docs/per-resource-worker-migration.md`` §4.10).

        Prefers the camera serial (stable across enumeration order); falls
        back to the configured camera ``name`` when no serial is declared
        (CI / dshow-by-name use). Two ``WebcamAdapter`` instances pointing
        at the same physical camera share this string and therefore would
        share a worker.
        """
        if self._spec.serial:
            return f"webcam:{self._spec.serial}"
        return f"webcam:{self._spec.name}"

    @property
    def supported_resolutions(self) -> list[tuple[int, int]]:
        """``(width, height)`` pairs the dshow input enumerated at open-time.

        Empty when probing was not run (non-Windows, non-dshow, or the camera
        was opened in a context that skipped open()). UI code uses this to
        rebuild a resolution selector from real device data; an empty list
        signals "fall back to a static set"."""
        return list(self._supported_resolutions)

    def max_fps_for_resolution(self, width: int, height: int) -> float | None:
        """Largest frame rate the device advertised for ``(width, height)``
        at open-time, or ``None`` if the probe never saw that resolution
        (or never saw an fps annotation alongside it). UI code uses this
        to cap the framerate spinbox so operators can't request 60 fps on a
        sensor that maxes out at 30."""
        return self._resolution_fps_caps.get((int(width), int(height)))

    @property
    def resolution_hint(self) -> tuple[int, int]:
        """``(width, height)`` currently configured for the next start_recording.

        Public read-only view of the configured size — used by the manual-
        control card to preselect the matching combo entry without poking
        :attr:`_width` / :attr:`_height` directly."""
        return (self._width, self._height)

    def snapshot_metadata(self) -> WebcamMetadata:
        """Build a :class:`WebcamMetadata` snapshot for cross-loop transfer.

        Called by :meth:`CameraDeviceAdapter.camera_metadata` on the worker
        loop. The result is consumed on the qasync loop by
        :class:`WebcamCard._apply_metadata`. Keeping the snapshot
        construction inside the adapter means the wrapper stays generic
        — it forwards via a ``getattr(camera, "snapshot_metadata", None)``
        capability-style probe.

        Safe to call before duvc-ctl has probed (``self._uvc is None``):
        the ``uvc_ranges`` mapping is empty and the card keeps its wide
        default bounds. The two resolution-related fields populate from
        :attr:`_supported_resolutions` and :attr:`_resolution_fps_caps`,
        which are themselves set at ``open()`` and never mutated.
        """
        ranges: dict[str, UvcRangeMetadata] = {}
        if self._uvc is not None:
            for verb, prop in PROPERTY_BY_VERB.items():
                rng = self._uvc.get_cached_range(prop)
                if rng is None:
                    continue
                ranges[verb] = UvcRangeMetadata(
                    minimum=rng.minimum,
                    maximum=rng.maximum,
                    step=rng.step,
                    default=rng.default,
                    current=self._uvc.get_cached_current(prop),
                )
        return WebcamMetadata(
            supported_resolutions=tuple(self._supported_resolutions),
            resolution_hint=(self._width, self._height),
            resolution_fps_caps=MappingProxyType(dict(self._resolution_fps_caps)),
            uvc_ranges=MappingProxyType(ranges),
        )

    # ----------------------------------------------------------- protocol API

    async def discover(self) -> tuple[CameraInfo, ...]:
        """No platform-portable enumeration with PyAV — return the configured
        camera as a single row so the engine's selection rules can run.

        Hardware tests can override by injecting a custom ``discover`` via
        a subclass; capa core only needs the single-camera path.
        """
        return (self._info,)

    async def open(self) -> CameraInfo:
        if self._open:
            return self._info
        if sys.platform == "linux" and self._input_format == "v4l2":
            probed = _probe_v4l2_info(self._input_url)
            if probed.card_name or probed.serial:
                # Replace stub identity with the device's own metadata so
                # ``manifest.json.cameras[*].identity`` reflects the actual
                # hardware. Hardware-day §5: ``identity`` was ``None`` for a
                # real Logitech C930e because nothing populated it.
                self._info = CameraInfo(
                    adapter=self._info.adapter,
                    name=self._info.name,
                    model=probed.card_name or self._info.model,
                    serial=probed.serial or self._info.serial,
                    transport=self._info.transport,
                    capabilities=self._info.capabilities,
                )
        # On Windows, probe duvc-ctl for UVC control surface. The controller
        # holds a separate DirectShow handle that does NOT compete with PyAV
        # for the capture pin — IAMCameraControl / IAMVideoProcAmp can be
        # queried while another graph renders. Off Windows (or when duvc-ctl
        # cannot match a device) the controller stays None and only the
        # base capability flags survive.
        if sys.platform == "win32":
            controller = await UvcController.find(
                model_hint=self._spec.model_hint,
                serial=self._spec.serial,
            )
            if controller is not None:
                probed_caps = await controller.probe_capabilities()
                if probed_caps:
                    self._uvc = controller
                    self.capabilities = self.capabilities | probed_caps
                    # Refresh CameraInfo so the manifest reflects the live
                    # capability set (was frozen with _BASE_CAPABILITIES).
                    self._info = CameraInfo(
                        adapter=self._info.adapter,
                        name=self._info.name,
                        model=self._info.model,
                        serial=self._info.serial,
                        transport=self._info.transport,
                        capabilities=tuple(c.name for c in self.capabilities if c.name is not None),
                    )
                else:
                    controller.close()
        # Enumerate supported (width,height) pairs via PyAV's dshow
        # list_options output. duvc-ctl does not expose IAMStreamConfig,
        # so this is the only path on Windows. The probe opens + closes
        # the dshow filter graph briefly; subsequent av.open calls absorb
        # the DirectShow release latency via :meth:`_open_input_with_retry`.
        if sys.platform == "win32" and self._input_format == "dshow":
            resolutions, fps_caps = await anyio.to_thread.run_sync(
                _probe_dshow_format_info_sync, self._input_url
            )
            if resolutions:
                self._supported_resolutions = resolutions
                self._resolution_fps_caps = fps_caps
        self._open = True
        return self._info

    async def close(self) -> None:
        if self._recording:
            await self.stop_recording()
        await self.stop_input_pump()
        await self._frame_send.aclose()
        await self._preview_send.aclose()
        await self._event_send.aclose()
        if self._uvc is not None:
            self._uvc.close()
            self._uvc = None
        self._open = False

    async def start_recording(self, output_path: Path) -> None:
        if not self._open:
            raise AdapterError("WebcamAdapter.start_recording requires open()")
        if self._recording:
            return
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._output_path = output_path
        self._open_encoder()
        self._frame_count = 0
        self._dropped_frames = 0
        self._file_size = 0
        self._last_frame_t_mono_ns = None
        # The 2 Hz preview throttle is measured against ``_clock``. The
        # CameraDeviceAdapter rebinds its clock proxy to the run's
        # RunClock immediately before start_recording, so any prior
        # value of ``_last_preview_t_mono_ns`` is in the *old* anchor's
        # units. Resetting here makes the first in-run frame fire the
        # preview encode immediately when recording begins.
        # Without this, ``t_mono_ns - _last_preview_t_mono_ns`` is a
        # large negative number until the new clock advances past the
        # stale value, so previews stay dark for the early part of the
        # run and the dock's stale detector trips.
        self._last_preview_t_mono_ns = None
        self._started_t_mono_ns = self._clock.t_mono_ns()
        self._recording = True
        await self._emit_event(
            kind="recording_started",
            message=f"path={output_path} codec={self._codec}",
            severity="info",
        )

    async def stop_recording(self) -> None:
        if not self._recording:
            return
        self._recording = False
        try:
            self._close_encoder()
        finally:
            self._output_container = None
            self._output_stream = None
            if self._output_path is not None and self._output_path.exists():
                self._file_size = self._output_path.stat().st_size
        await self._emit_event(
            kind="recording_stopped",
            message=f"frames={self._frame_count}",
            severity="info",
        )

    async def snapshot(self) -> CameraHealth:
        return CameraHealth(
            name=self._spec.name,
            t_mono_ns=self._clock.t_mono_ns(),
            t_utc=datetime.now(UTC),
            recording=self._recording,
            frame_count=self._frame_count,
            file_size_bytes=self._file_size,
            last_frame_t_mono_ns=self._last_frame_t_mono_ns,
            healthy=True,
            dropped_frames=self._dropped_frames,
        )

    def frame_stream(self) -> AsyncIterator[FrameReceipt]:
        return _drain_stream(self._frame_recv)

    def preview_stream(self) -> AsyncIterator[bytes]:
        return _drain_stream(self._preview_recv)

    def event_stream(self) -> AsyncIterator[CameraEvent]:
        return _drain_stream(self._event_recv)

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        """Dispatch a :class:`DeviceCommand` to the right typed verb.

        Verbs (gated on capability flags populated by the open() probe):

        * **STREAM_FORMAT** — ``set_resolution`` ``{"width", "height"}``,
          ``set_framerate`` ``{"fps"}``. Apply on the *next* start_recording;
          rejected during an active recording.
        * **EXPOSURE_CONTROL** — ``set_exposure`` ``{"value"}``,
          ``set_auto_exposure`` ``{"enable"}``.
        * **FOCUS_CONTROL** — ``set_focus`` ``{"value"}``,
          ``set_auto_focus`` ``{"enable"}``.
        * **ZOOM_CONTROL** — ``set_zoom`` ``{"value"}``,
          ``set_digital_zoom`` ``{"value"}``.
        * **WB_CONTROL** — ``set_white_balance`` ``{"value"}``,
          ``set_auto_white_balance`` ``{"enable"}``.
        * **PAN_TILT_CONTROL** — ``set_pan`` ``{"value"}``,
          ``set_tilt`` ``{"value"}``.
        * **IMAGE_ADJUST** — ``set_brightness`` / ``_contrast`` /
          ``_saturation`` / ``_sharpness`` / ``_gamma`` / ``_hue`` /
          ``_gain`` / ``_backlight_compensation`` (all ``{"value"}``).

        Authorization gate → open-state gate → recording-state gate →
        capability/verb-table dispatch. Rejections preserve ordering so
        the most informative reason surfaces first.
        """
        rejection = reject_unless_authorized(
            cmd, adapter_id="webcam", device_name=self._spec.name, clock=self._clock
        )
        if rejection is not None:
            return rejection
        if not self._open:
            return make_not_open_result(
                adapter_id="webcam", device_name=self._spec.name, clock=self._clock
            )

        try:
            detail = await self._dispatch_command(cmd)
        except AdapterError as exc:
            return CommandResult(
                accepted=False,
                detail=str(exc),
                t_mono_ns=self._clock.t_mono_ns(),
                t_utc=datetime.now(UTC),
            )
        if detail is None:
            return CommandResult(
                accepted=False,
                detail=f"webcam {self._spec.name!r}: unknown verb {cmd.kind!r}",
                t_mono_ns=self._clock.t_mono_ns(),
                t_utc=datetime.now(UTC),
            )
        return make_accepted_result(detail=detail, clock=self._clock)

    async def _dispatch_command(self, cmd: DeviceCommand) -> str | None:
        """Route ``cmd.kind`` onto typed implementations.

        Returns the success detail string, or ``None`` when the verb isn't
        recognized (handled at the call site as a not-accepted result).
        Raises :class:`AdapterError` for verb-level rejections (capability
        not advertised, recording in progress, UVC set failure) — caught
        by :meth:`command` and rendered as ``accepted=False``.
        """
        kind = cmd.kind

        # ---- STREAM_FORMAT ----
        if kind == "set_resolution":
            if CameraCapability.STREAM_FORMAT not in self.capabilities:
                raise AdapterError(f"webcam {self._spec.name!r}: STREAM_FORMAT not advertised")
            if self._recording:
                raise AdapterError(
                    f"webcam {self._spec.name!r}: set_resolution refused "
                    "during recording (applies to the next start_recording)"
                )
            width = int(cmd.payload["width"])
            height = int(cmd.payload["height"])
            if width <= 0 or height <= 0:
                raise AdapterError(
                    f"webcam {self._spec.name!r}: set_resolution width/height must be > 0"
                )
            self._width = width
            self._height = height
            return f"set_resolution width={width} height={height}"
        if kind == "set_framerate":
            if CameraCapability.STREAM_FORMAT not in self.capabilities:
                raise AdapterError(f"webcam {self._spec.name!r}: STREAM_FORMAT not advertised")
            if self._recording:
                raise AdapterError(
                    f"webcam {self._spec.name!r}: set_framerate refused "
                    "during recording (applies to the next start_recording)"
                )
            fps = float(cmd.payload["fps"])
            if fps <= 0:
                raise AdapterError(f"webcam {self._spec.name!r}: set_framerate fps must be > 0")
            self._fps = fps
            return f"set_framerate fps={fps}"

        # ---- UVC numeric set ----
        if kind in PROPERTY_BY_VERB:
            if self._uvc is None:
                raise AdapterError(
                    f"webcam {self._spec.name!r}: UVC controls unavailable "
                    "(no duvc-ctl device match)"
                )
            prop = PROPERTY_BY_VERB[kind]
            if not self._uvc.supports(prop):
                raise AdapterError(
                    f"webcam {self._spec.name!r}: device does not support "
                    f"{prop.name} ({prop.group.value} property)"
                )
            value = int(cmd.payload["value"])
            await self._uvc.set_value(prop, value)
            return f"{kind} value={value}"

        # ---- UVC auto-mode toggle ----
        if kind in AUTO_VERB_TO_PROPERTY:
            if self._uvc is None:
                raise AdapterError(
                    f"webcam {self._spec.name!r}: UVC controls unavailable "
                    "(no duvc-ctl device match)"
                )
            prop = AUTO_VERB_TO_PROPERTY[kind]
            if not self._uvc.supports(prop):
                raise AdapterError(
                    f"webcam {self._spec.name!r}: device does not support "
                    f"{prop.name} ({prop.group.value} property)"
                )
            enable = bool(cmd.payload["enable"])
            await self._uvc.set_auto(prop, enable)
            return f"{kind} enable={enable}"

        return None

    # ----------------------------------------------------------- frame ingest

    def _push_frame_sync(
        self,
        frame: np.ndarray,
        capture_latency_s: float = 0.0,
    ) -> tuple[FrameReceipt | None, bytes | None, str | None]:
        """Encode (if recording) + bookkeep + JPEG-thumbnail one frame.

        Returns ``(receipt, preview_bytes, drop_reason)``:

        * ``receipt`` is ``None`` when the adapter is not actively
          recording (preview-only mode) or when the encoder rejected the
          frame.
        * ``preview_bytes`` is ``None`` on throttled ticks (2 Hz cap).
        * ``drop_reason`` is non-``None`` only when the encoder rejected
          the frame mid-recording (libx264 EINVAL, format renegotiation,
          …). Dropping rather than raising keeps the pump alive across
          single-frame faults (hardware-day §6).

        The CPU-heavy work (libx264 encode, container mux, JPEG encode)
        lives here so :meth:`push_frame` can run it in a worker thread via
        :func:`anyio.to_thread.run_sync`, keeping the asyncio loop free for
        other adapters' I/O.
        """
        if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8:
            raise AdapterError(
                f"push_frame expects HxWx3 uint8 ndarray; got "
                f"shape={frame.shape} dtype={frame.dtype}"
            )

        t_mono_ns = self._clock.t_mono_ns()

        # Encode + receipt only when actively recording. Snapshot the
        # encoder handles so a concurrent stop_recording() flipping
        # _output_container to None doesn't race the check below.
        receipt: FrameReceipt | None = None
        drop_reason: str | None = None
        recording = self._recording
        out_container = self._output_container
        out_stream = self._output_stream
        if recording and out_container is not None and out_stream is not None:
            idx = self._frame_count
            video_frame = av.VideoFrame.from_ndarray(frame, format="rgb24")
            video_frame.pts = idx
            try:
                # time_base set by the stream; PyAV remuxes pts via that.
                for packet in out_stream.encode(video_frame):
                    out_container.mux(packet)
            except av.error.FFmpegError as exc:
                # Frame index is NOT advanced — the next push reuses
                # ``idx`` so surviving frames stay contiguous and the
                # encoder doesn't see a pts gap.
                self._dropped_frames += 1
                drop_reason = (
                    f"{type(exc).__name__}: {exc} (shape={frame.shape}, encoder={self._codec})"
                )
            else:
                self._frame_count = idx + 1
                self._last_frame_t_mono_ns = t_mono_ns
                receipt = FrameReceipt(
                    name=self._spec.name,
                    frame_idx=idx,
                    t_mono_ns=t_mono_ns,
                    t_utc=self._clock.to_wall_ns(t_mono_ns),
                    capture_latency_s=capture_latency_s,
                )

        # Preview is unconditional: the live tile must stay current
        # whether or not we're recording. 2 Hz cap, JPEG-encoded thumbnail,
        # aspect preserved. Skipped encodes save the Pillow + libjpeg cost
        # on every frame the consumer cannot use (28 of 30 at webcam rate).
        # DROP_OLDEST is enforced by ``_preview_send`` having capacity 2;
        # ``send_nowait`` from the wrapper silently drops on backpressure.
        preview_bytes: bytes | None = None
        if (
            self._last_preview_t_mono_ns is None
            or t_mono_ns - self._last_preview_t_mono_ns >= PREVIEW_INTERVAL_NS
        ):
            preview_bytes = _encode_preview_jpeg(frame)
            self._last_preview_t_mono_ns = t_mono_ns
        return receipt, preview_bytes, drop_reason

    async def push_frame(
        self,
        frame: np.ndarray,
        *,
        capture_latency_s: float = 0.0,
    ) -> FrameReceipt | None:
        """Process ``frame`` (HxWx3 uint8, RGB24): encode to MKV + emit a
        :class:`FrameReceipt` if recording; always emit a 2 Hz preview JPEG.

        Returns the receipt when one was produced (recording succeeded);
        returns ``None`` when not recording (preview-only) or when the
        encoder rejected the frame. Encoder-reject paths land a
        ``pump_warning`` on the event stream.

        Used by the long-lived input pump (:meth:`_run_input_loop`) and
        by tests that drive frames directly. The libx264 encode + JPEG
        encode run in a worker thread so the asyncio loop stays responsive
        at 30 fps.
        """
        if not self._open:
            raise AdapterError("WebcamAdapter.push_frame requires open()")
        receipt, preview_bytes, drop_reason = await anyio.to_thread.run_sync(
            self._push_frame_sync, frame, capture_latency_s
        )
        if drop_reason is not None:
            await self._emit_event(
                kind="pump_warning",
                message=f"frame dropped: {drop_reason}",
                severity="warning",
            )
        if receipt is not None:
            await self._frame_send.send(receipt)
        if preview_bytes is not None:
            with contextlib.suppress(anyio.WouldBlock):
                self._preview_send.send_nowait(preview_bytes)
        return receipt

    async def _open_input_with_retry(self) -> Any:
        """Open the PyAV input container, retrying on transient I/O errors.

        Windows DirectShow holds the camera filter graph for several seconds
        after ``cam.close()``; ``av.open(format='dshow', ...)`` returns
        ``OSError [Errno 5] I/O error`` until the graph drops. POSIX paths
        (v4l2, avfoundation) normally succeed on the first try, so the retry
        is a no-op there. The retry budget (:data:`OPEN_RETRY_DEADLINE_S`)
        is sized for a worst-case C930e release on Windows 11; longer holds
        indicate the camera is genuinely in use by another process and
        re-raising surfaces that.
        """
        deadline = time.monotonic() + OPEN_RETRY_DEADLINE_S
        attempt = 0
        last_exc: OSError | None = None
        for delay in OPEN_RETRY_DELAYS_S:
            try:
                return await anyio.to_thread.run_sync(
                    lambda: av.open(self._input_url, format=self._input_format)
                )
            except OSError as exc:
                if not _is_transient_open_error(exc) or time.monotonic() >= deadline:
                    raise
                attempt += 1
                last_exc = exc
                await self._emit_event(
                    kind="open_retry",
                    severity="info",
                    message=(
                        f"av.open transient error (attempt {attempt}); "
                        f"retrying after {delay:.2f}s: {exc}"
                    ),
                )
                await anyio.sleep(delay)
        # One more attempt after the last sleep — if it still fails, surface it.
        try:
            return await anyio.to_thread.run_sync(
                lambda: av.open(self._input_url, format=self._input_format)
            )
        except OSError as exc:
            if last_exc is not None and _is_transient_open_error(exc):
                raise AdapterError(
                    f"webcam {self._spec.name!r}: av.open kept returning a "
                    f"transient I/O error after {attempt + 1} retries; the "
                    f"camera is likely held by another process. Last error: {exc}",
                ) from exc
            raise

    async def start_input_pump(self) -> None:
        """Spawn the long-lived input pump task. Idempotent.

        The pump opens an :class:`av.container.InputContainer` once and
        drives the same decode → push_frame path for the entire
        adapter-open lifetime. It emits 2 Hz preview JPEGs unconditionally
        and additionally encodes + emits FrameReceipts while
        :attr:`_recording` is set — so the live tile stays current
        between runs and ``av.open`` happens exactly once per pool open.

        Idempotent: a second call while the pump is already running
        returns without action. Requires :meth:`open` to have completed.

        Called by :meth:`CameraDeviceAdapter.open` on the worker loop;
        the resulting task binds to that loop.
        """
        if not self._open:
            raise AdapterError("WebcamAdapter.start_input_pump requires open()")
        if self._pump_task is not None and not self._pump_task.done():
            return
        self._pump_stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        self._pump_task = loop.create_task(
            self._run_input_loop(),
            name=f"webcam-input-{self._spec.name}",
        )

    async def stop_input_pump(self) -> None:
        """Signal the input pump to exit and await it. Idempotent.

        Sets :attr:`_pump_stop` so the loop's next iteration breaks; the
        pump's ``finally`` block closes the input container. If the pump
        outlives a bounded grace window the task is hard-cancelled. Safe
        to call when no pump is running (e.g. test paths).
        """
        task = self._pump_task
        self._pump_task = None
        if task is None:
            self._pump_stop = None
            return
        if self._pump_stop is not None:
            self._pump_stop.set()
        if not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
            except BaseException:
                with contextlib.suppress(BaseException):
                    await task
        self._pump_stop = None

    async def _run_input_loop(self) -> None:
        """Long-lived input pump (pool lifetime).

        Opens the PyAV input container with retry, then loops:
        ``decode → reformat → push_frame``. ``push_frame`` always emits a
        2 Hz preview JPEG and additionally encodes + emits a
        :class:`FrameReceipt` while :attr:`_recording` is set. Exits when
        :attr:`_pump_stop` is set, when the decoder reaches EOF, or on
        cancellation.

        ``av.open`` is wrapped in :meth:`_open_input_with_retry` so the
        Windows DirectShow filter-graph hold-time after a previous close
        is absorbed transparently. Decode + reformat each run in a worker
        thread so the per-frame CPU work doesn't block the asyncio loop
        (hardware-day §5.B: 14 fps regression).

        When ``CAPA_WEBCAM_FRAME_DIAG=1`` is set, the first 150 input
        frames are logged at INFO with ``frame.format.name`` /
        ``width`` / ``height`` / ``pts``.
        """
        assert self._pump_stop is not None
        diag_enabled = os.environ.get("CAPA_WEBCAM_FRAME_DIAG") == "1"
        diag_remaining = 150 if diag_enabled else 0
        diag_log = structlog.get_logger("capa.webcam.frame_diag") if diag_enabled else None

        try:
            in_container = await self._open_input_with_retry()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            _logger.warning(
                "webcam.input_open_failed",
                camera=self._spec.name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return

        try:
            in_stream = next(s for s in in_container.streams if s.type == "video")
            decoder = in_container.decode(in_stream)
            while self._open and not self._pump_stop.is_set():
                frame = await anyio.to_thread.run_sync(_advance_decoder, decoder)
                if frame is None or self._pump_stop.is_set() or not self._open:
                    break
                if diag_remaining > 0 and diag_log is not None:
                    diag_log.info(
                        "webcam_frame_diag",
                        camera=self._spec.name,
                        frame_idx=self._frame_count,
                        format=frame.format.name,
                        width=frame.width,
                        height=frame.height,
                        pts=frame.pts,
                        time_base=str(frame.time_base) if frame.time_base else None,
                    )
                    diag_remaining -= 1
                rgb = await anyio.to_thread.run_sync(_reformat_to_rgb24, frame)
                if self._pump_stop.is_set() or not self._open:
                    break
                # ``push_frame`` handles both preview-only and recording
                # modes — _push_frame_sync checks _recording per-frame and
                # falls back to preview-only emission when not recording.
                # Encoder-reject paths land a pump_warning event and the
                # loop continues with the next frame.
                await self.push_frame(rgb)
        finally:
            with contextlib.suppress(BaseException):
                await anyio.to_thread.run_sync(in_container.close)

    # -------------------------------------------------------------- internals

    def _open_encoder(self) -> None:
        """Open the output container + stream."""
        assert self._output_path is not None
        container = av.open(str(self._output_path), mode="w", format="matroska")
        # MKV container metadata anchor (plan §12.5).
        anchor_utc = self._clock.to_wall_ns(self._clock.t_mono_ns())
        container.metadata["run_started_utc"] = anchor_utc.isoformat()
        container.metadata["camera_name"] = self._spec.name
        container.metadata["capa_codec"] = self._codec

        stream = container.add_stream(self._codec, rate=int(self._fps))
        assert isinstance(stream, av.VideoStream)
        stream.width = self._width
        stream.height = self._height
        stream.pix_fmt = self._pix_fmt
        # Sensible default tuning for libx264; harmless for other codecs.
        if self._codec in ("libx264", "h264"):
            stream.options = {"preset": "veryfast", "tune": "zerolatency"}

        self._output_container = container
        self._output_stream = stream

    def _close_encoder(self) -> None:
        """Flush the encoder, close the container."""
        if self._output_container is None or self._output_stream is None:
            return
        # Flush remaining packets.
        with contextlib.suppress(av.error.EOFError):
            for packet in self._output_stream.encode(None):
                self._output_container.mux(packet)
        self._output_container.close()

    async def _emit_event(self, *, kind: str, message: str, severity: str) -> None:
        with contextlib.suppress(anyio.BrokenResourceError):
            await self._event_send.send(
                CameraEvent(
                    name=self._spec.name,
                    t_mono_ns=self._clock.t_mono_ns(),
                    t_utc=datetime.now(UTC),
                    kind=kind,
                    message=message,
                    severity=severity,
                )
            )


@dataclass(frozen=True, slots=True)
class V4L2Probe:
    """Identity fields extracted from sysfs for a V4L2 device path.

    Each field is ``None`` when sysfs didn't expose it (non-Linux,
    non-V4L2 path, missing parent USB descriptor, …) — the adapter
    falls back to whatever was in :class:`CameraSpec` for those.
    """

    card_name: str | None
    serial: str | None
    bus_info: str | None


def _probe_v4l2_info(device_path: str) -> V4L2Probe:
    """Read sysfs metadata for ``/dev/videoN`` (Linux only).

    Returns a fully-``None`` :class:`V4L2Probe` on every error path so
    :meth:`WebcamAdapter.open` doesn't have to special-case missing files,
    non-Linux platforms, or non-USB cameras (built-in MIPI sensors don't
    expose a USB ``serial``). Layout queried:

    * ``/sys/class/video4linux/<node>/name`` — card name (e.g. card_type
      from the V4L2 driver, ``"Logitech Webcam C930e"``).
    * ``/sys/class/video4linux/<node>/device`` — symlink to the parent
      USB *interface*; one level up is the USB *device* whose ``serial``,
      ``idVendor``, ``idProduct`` files identify the unit.
    """
    empty = V4L2Probe(card_name=None, serial=None, bus_info=None)
    if not _is_linux_platform():
        return empty
    if not device_path.startswith("/dev/video"):
        return empty
    node = device_path.rsplit("/", 1)[-1]  # "video4"
    sysfs_root = Path("/sys/class/video4linux") / node
    if not sysfs_root.exists():
        return empty

    card_name: str | None = None
    name_file = sysfs_root / "name"
    try:
        if name_file.exists():
            card_name = name_file.read_text().strip() or None
    except OSError:
        pass

    serial: str | None = None
    bus_info: str | None = None
    device_link = sysfs_root / "device"
    try:
        if device_link.exists():
            interface_dir = device_link.resolve()
            usb_device_dir = interface_dir.parent
            serial_file = usb_device_dir / "serial"
            if serial_file.exists():
                serial = serial_file.read_text().strip() or None
            bus_info = usb_device_dir.name or None
    except OSError:
        pass

    return V4L2Probe(card_name=card_name, serial=serial, bus_info=bus_info)


_DSHOW_MAX_FORMAT_RE: re.Pattern[str] = re.compile(
    r"\bmax s=(\d+)x(\d+)\s+fps=([\d.]+)", re.IGNORECASE
)
_DSHOW_MIN_FORMAT_RE: re.Pattern[str] = re.compile(
    r"\bmin s=(\d+)x(\d+)\s+fps=([\d.]+)", re.IGNORECASE
)
_DSHOW_MAX_SIZE_RE: re.Pattern[str] = re.compile(r"\bmax s=(\d+)x(\d+)\b", re.IGNORECASE)
_DSHOW_MIN_SIZE_RE: re.Pattern[str] = re.compile(r"\bmin s=(\d+)x(\d+)\b", re.IGNORECASE)


def _is_linux_platform() -> bool:
    return sys.platform.startswith("linux")


def _probe_dshow_format_info_sync(
    input_url: str,
) -> tuple[list[tuple[int, int]], dict[tuple[int, int], float]]:
    """Enumerate ``(width, height)`` pairs and per-resolution max fps caps.

    FFmpeg's dshow demuxer prints the device's pin formats when opened with
    ``options={"list_options": "true"}`` — the call always fails with the
    expected ``Immediate exit requested``, but the format dump lands on the
    libav log channel first. We capture those lines via
    :func:`av.logging.Capture` and parse the ``max s=WxH fps=NN.NNN`` tail
    of each ``pixel_format=…`` line. Multiple pixel formats per resolution
    collapse to the highest reported fps for that size.

    Uses ``Capture(local=False)`` because this helper is invoked through
    :func:`anyio.to_thread.run_sync`; the libav log callback fires from the
    worker thread, and ``local=True`` would only route logs back to the
    constructing thread's id. Restores the prior log level on exit so the
    rest of capa's PyAV usage stays silent.

    Returns ``([], {})`` on any failure (PyAV missing, non-Windows path,
    parse mismatch). Callers fall back to a static resolution set and an
    uncapped fps spinbox when nothing was probed.
    """
    old_level = av.logging.get_level()
    av.logging.set_level(av.logging.VERBOSE)
    try:
        with av.logging.Capture(local=False) as logs, contextlib.suppress(Exception):
            container = av.open(input_url, format="dshow", options={"list_options": "true"})
            container.close()
    finally:
        av.logging.set_level(old_level)

    seen: set[tuple[int, int]] = set()
    resolutions: list[tuple[int, int]] = []
    fps_caps: dict[tuple[int, int], float] = {}
    for entry in logs:
        message = entry[2] if len(entry) >= 3 else ""
        size_w: int | None = None
        size_h: int | None = None
        fps_value: float | None = None
        fmt_match = _DSHOW_MAX_FORMAT_RE.search(message) or _DSHOW_MIN_FORMAT_RE.search(message)
        if fmt_match is not None:
            size_w = int(fmt_match.group(1))
            size_h = int(fmt_match.group(2))
            with contextlib.suppress(ValueError):
                fps_value = float(fmt_match.group(3))
        else:
            size_match = _DSHOW_MAX_SIZE_RE.search(message) or _DSHOW_MIN_SIZE_RE.search(message)
            if size_match is not None:
                size_w = int(size_match.group(1))
                size_h = int(size_match.group(2))
        if size_w is None or size_h is None:
            continue
        wh = (size_w, size_h)
        if wh not in seen:
            seen.add(wh)
            resolutions.append(wh)
        if fps_value is not None and fps_value > 0:
            existing = fps_caps.get(wh)
            if existing is None or fps_value > existing:
                fps_caps[wh] = fps_value
    resolutions.sort(key=lambda wh: (wh[0] * wh[1], wh[0]))
    return resolutions, fps_caps


def _is_transient_open_error(exc: BaseException) -> bool:
    """Return ``True`` for ``av.open`` errors that backoff is likely to clear.

    Windows DirectShow returns ``OSError [Errno 5] I/O error`` while the
    previous filter graph is still being torn down. PyAV surfaces this
    directly via :class:`OSError` (and its :class:`av.error.FFmpegError`
    subclass), so matching on ``errno == 5`` covers both code paths.
    Non-transient errors (missing device node, codec not found, permission
    denied) propagate immediately so we don't hide real wiring problems.
    """
    if not isinstance(exc, OSError):
        return False
    return getattr(exc, "errno", None) == 5


def _advance_decoder(decoder: Any) -> av.VideoFrame | None:
    """Pull the next decoded frame from a PyAV decoder; ``None`` at EOF.

    Wrapped in :func:`anyio.to_thread.run_sync` by :meth:`WebcamAdapter.run_pump`
    so the per-frame libav decode (~33 ms at 30 fps from a UVC source) does
    not block the asyncio loop.
    """
    frame = next(decoder, None)
    if frame is None:
        return None
    assert isinstance(frame, av.VideoFrame)
    return frame


def _reformat_to_rgb24(frame: av.VideoFrame) -> np.ndarray:
    """Convert a decoded frame to an HxWx3 uint8 RGB ndarray.

    Wrapped by :func:`anyio.to_thread.run_sync` for the same reason as
    :func:`_advance_decoder` — colour conversion is CPU-heavy.
    """
    return frame.reformat(format="rgb24").to_ndarray()


def _encode_preview_jpeg(frame: np.ndarray) -> bytes:
    """Width-cap to :data:`PREVIEW_MAX_WIDTH` (aspect preserved) and JPEG-encode.

    Runs inside ``_push_frame_sync``, which the async wrapper already executes
    via :func:`anyio.to_thread.run_sync`, so the libjpeg work stays off the
    asyncio loop.
    """
    img = Image.fromarray(frame)
    if img.width > PREVIEW_MAX_WIDTH:
        new_h = max(1, round(img.height * (PREVIEW_MAX_WIDTH / img.width)))
        img = img.resize((PREVIEW_MAX_WIDTH, new_h), Image.Resampling.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=PREVIEW_JPEG_QUALITY)
    return buf.getvalue()


async def _drain_stream[T](recv: MemoryObjectReceiveStream[T]) -> AsyncIterator[T]:
    """Yield items from a memory object stream until the send end is closed.

    Mirrors :func:`capa.devices.sim.flir_ir_sim._drain_stream`. Centralizing
    in :mod:`capa.devices.camera.base` would invite a circular import; keep
    the duplicate.
    """
    async for item in recv:
        yield item


__all__ = [
    "DEFAULT_CODEC",
    "DEFAULT_FPS",
    "DEFAULT_PIX_FMT",
    "DESCRIPTOR",
    "OPEN_RETRY_DEADLINE_S",
    "OPEN_RETRY_DELAYS_S",
    "PREVIEW_INTERVAL_NS",
    "PREVIEW_JPEG_QUALITY",
    "PREVIEW_MAX_WIDTH",
    "WebcamAdapter",
    "WebcamParams",
    "discover_cameras",
    "handshake",
]


# ---------------------------------------------------------------------------
# Module-level discovery + handshake (plan §7.2 item 1).
#
# Both the Setup editor's DiscoveryDialog and Layer 5 of the validation
# pipeline reach for these without ever constructing an adapter. They
# must be passive (no recording, no RunClock) and platform-tolerant —
# a missing OS API returns an empty list, not an exception.
# ---------------------------------------------------------------------------


async def discover_cameras() -> list[dict[str, Any]]:
    """Walk the local OS camera enumeration APIs and return a row per
    visible visible-light camera.

    Returns dicts shaped like the other adapters' ``discover()`` output
    so the CLI can render them uniformly::

        {
            "adapter": "capa.devices.camera.webcam",
            "selector": "/dev/video0" | "video=Logitech C920" | "0",
            "model":    "Logitech C920",
            "serial":   "ABC123" | None,
            "transport": "usb",
        }

    Platform paths:

    * **Linux** — walks ``/sys/class/video4linux/video*`` and reuses the
      existing :func:`_probe_v4l2_info` helper so card-name / USB serial
      come from sysfs without opening the device.
    * **Windows** — uses ``duvc_ctl.list_devices()`` when the wheel is
      installed. Returns one row per visible DirectShow camera.
    * **macOS / unsupported** — returns ``[]``. AVFoundation
      enumeration is a follow-up; for now operators add macOS cameras
      by hand.
    """
    platform = sys.platform
    if platform.startswith("linux"):
        return await anyio.to_thread.run_sync(_enumerate_v4l2_sync)
    if platform == "win32":
        return await _enumerate_directshow()
    return []


def _enumerate_v4l2_sync() -> list[dict[str, Any]]:
    """List visible-light V4L2 capture nodes via sysfs (Linux only)."""
    root = Path("/sys/class/video4linux")
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    seen_devices: set[str] = set()
    for node_dir in sorted(root.iterdir()):
        node = node_dir.name
        if not re.fullmatch(r"video\d+", node):
            continue
        device_path = f"/dev/{node}"
        probed = _probe_v4l2_info(device_path)
        # bus_info is the USB device id; collapse the multiple
        # /dev/videoN nodes one webcam exposes (capture + metadata) to
        # a single row keyed on the bus.
        bus = probed.bus_info or device_path
        if bus in seen_devices:
            continue
        seen_devices.add(bus)
        rows.append(
            {
                "adapter": "capa.devices.camera.webcam",
                "selector": device_path,
                "model": probed.card_name,
                "serial": probed.serial,
                "transport": "usb",
            }
        )
    return rows


async def _enumerate_directshow() -> list[dict[str, Any]]:
    """List DirectShow cameras via duvc-ctl (Windows only).

    Falls back to an empty list when the duvc-ctl wheel is missing —
    operators on a stripped-down Windows install simply see no camera
    rows rather than a crash.
    """
    try:
        from capa.devices.camera._uvc import _duvc  # noqa: PLC0415
    except ImportError:
        return []
    if _duvc is None:
        return []
    try:
        devices = await anyio.to_thread.run_sync(_duvc.list_devices)
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for dev in devices or ():
        name = getattr(dev, "name", None)
        path = getattr(dev, "path", None)
        rows.append(
            {
                "adapter": "capa.devices.camera.webcam",
                "selector": f"video={name}" if name else (path or ""),
                "model": name,
                "serial": path,  # duvc path is the DirectShow moniker
                "transport": "directshow",
            }
        )
    return rows


def _match_camera_row(
    rows: list[dict[str, Any]],
    *,
    model_hint: str | None,
    serial: str | None,
) -> dict[str, Any] | None:
    """Apply the plan §12.1 selector rules to a discover result list.

    Returns the chosen row or ``None`` when no unique match exists.
    """
    if serial is not None:
        for row in rows:
            row_serial = row.get("serial")
            if isinstance(row_serial, str) and serial.lower() in row_serial.lower():
                return row
        return None
    if model_hint is not None:
        matches = [
            row
            for row in rows
            if isinstance(row.get("model"), str) and model_hint.lower() in row["model"].lower()
        ]
        if not matches:
            return None
        return matches[0]
    if len(rows) == 1:
        return rows[0]
    return None


async def handshake(cam_spec: dict[str, Any]) -> str:
    """Layer-5 read-only verification for a configured visible camera.

    Unlike device handshakes (which open + identify + close a serial
    port), a real DirectShow / V4L2 open holds the capture pin for
    100s of ms and competes with whatever else might be watching the
    camera. We use the cheaper "the camera shows up in discovery"
    check instead — sufficient to catch the common wiring failure
    (cable yanked, device path renumbered) without paying the
    capture-pin cost. Plan §7.2 item 1.
    """
    rows = await discover_cameras()
    if not rows:
        raise AdapterError(
            "no visible cameras enumerated on this host (sysfs/duvc-ctl returned no devices)"
        )
    model_hint = cam_spec.get("model_hint")
    serial = cam_spec.get("serial")
    chosen = _match_camera_row(
        rows,
        model_hint=model_hint if isinstance(model_hint, str) else None,
        serial=serial if isinstance(serial, str) else None,
    )
    if chosen is None:
        wanted = (
            f"serial={serial!r}"
            if serial is not None
            else f"model_hint={model_hint!r}"
            if model_hint is not None
            else "no selector (and >1 camera present)"
        )
        raise AdapterError(f"no unique camera match for {wanted}; saw {len(rows)} devices")
    model = chosen.get("model") or "?"
    serial_seen = chosen.get("serial") or "?"
    selector = chosen.get("selector") or "?"
    return f"webcam model={model!r} serial={serial_seen!r} selector={selector!r}"


# ---------------------------------------------------------------------------
# Setup-editor descriptor (plan §5.7).
# ---------------------------------------------------------------------------


from pydantic import BaseModel, ConfigDict, Field  # noqa: E402


class WebcamParams(BaseModel):
    """View model for :class:`WebcamAdapter`'s ``params`` dict (plan §4.9.3).

    Mirrors :meth:`WebcamAdapter.__init__`'s keyword arguments. Used by
    the Setup editor's Cameras section to produce a curated auto-form
    over otherwise free-form scalar params; not consulted at runtime
    (the adapter validates kwargs the existing way)."""

    model_config = ConfigDict(extra="ignore")

    fps: float = Field(default=DEFAULT_FPS, gt=0)
    width: int = Field(default=1280, gt=0)
    height: int = Field(default=720, gt=0)
    codec: str = DEFAULT_CODEC
    pix_fmt: str = DEFAULT_PIX_FMT
    input_url: str | None = None
    input_format: str | None = None


def _build_descriptor() -> AdapterDescriptor:
    from capa.devices.registry import AdapterDescriptor  # noqa: PLC0415

    return AdapterDescriptor(
        id="capa.devices.camera.webcam",
        label="USB webcam (visible)",
        family="camera_visible",
        adapter_factory=WebcamAdapter,
        params_model=WebcamParams,
        supported_binding_sources=(),  # Cameras don't bind via SourceBinding
        default_params={
            "fps": DEFAULT_FPS,
            "width": 1280,
            "height": 720,
            "codec": DEFAULT_CODEC,
            "pix_fmt": DEFAULT_PIX_FMT,
        },
        channel_templates=(),
        discoverable=True,
        handshake_available=True,
    )


DESCRIPTOR = _build_descriptor()

from capa.devices.registry import register as _register  # noqa: E402

_register(DESCRIPTOR)
