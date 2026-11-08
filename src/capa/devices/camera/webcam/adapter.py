"""``WebcamAdapter`` — PyAV-driven capture → MKV.

The adapter has a single long-lived input pump
(:meth:`WebcamAdapter._run_input_loop`) that opens the
:class:`av.container.InputContainer` once at :meth:`start_input_pump`
and closes it at :meth:`stop_input_pump`. The input is opened at the
configured frame size and rate, so the camera picks a capture format
that delivers both (MJPEG for a C930e at 1080p30). The pump
unconditionally emits 2 Hz preview JPEGs onto ``preview_stream`` so the
operator always has a live tile between runs, and while ``_recording``
is set it also records each frame + emits a :class:`FrameReceipt`:
re-encoded with ``codec``, or, with ``codec = "copy"``, the camera's own
packets written to the MKV unchanged.

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
``av.open`` happens exactly once per pool open, not once per recording.

MKV container metadata carries the run-start UTC anchor so an
external tool can re-correlate by absolute time without parsing capa's
manifest.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

import anyio
import av
import numpy as np
import structlog
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

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
    uvc_backend_available,
)
from capa.devices.camera.base import (
    CameraCapability,
    CameraEvent,
    CameraHealth,
    CameraInfo,
    CameraSpec,
    FrameReceipt,
    WebcamControlState,
    WebcamStateSnapshot,
    make_stream_pair,
)
from capa.devices.camera.metadata import WebcamMetadata
from capa.devices.camera.webcam.constants import (
    _BASE_CAPABILITIES,
    COPY_CODEC,
    DEFAULT_CODEC,
    DEFAULT_FPS,
    DEFAULT_PIX_FMT,
    OPEN_RETRY_DEADLINE_S,
    OPEN_RETRY_DELAYS_S,
    PREVIEW_DETAIL_INTERVAL_NS,
    PREVIEW_INTERVAL_NS,
    STREAM_START_TIMEOUT_S,
    _platform_default_format,
)
from capa.devices.camera.webcam.encoding import (
    _decode_packet,
    _drain_stream,
    _encode_preview_jpeg,
    _is_transient_open_error,
    _next_packet,
    _reformat_to_rgb24,
)
from capa.devices.camera.webcam.probe import _probe_dshow_format_info_sync, _probe_v4l2_info

_logger = structlog.get_logger("capa.devices.camera.webcam")


class WebcamAdapter:
    """Visible-camera adapter.

    Constructed via :meth:`from_params` so a hardware TOML can declare it::

        [[hardware.cameras]]
        name = "visible_cam0"
        adapter = "capa.devices.camera.webcam"
        kind = "visible"
        params = { fps = 30, width = 1280, height = 720, codec = "libx264" }
    """

    __slots__ = (
        "_capture_format",
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
        "_in_stream",
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
        "_preview_detail",
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
        "_uvc_unavailable",
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
        # Why ``_uvc`` is None after open(), for refusals and read-backs.
        self._uvc_unavailable: str | None = None
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
        # Full-size, faster previews for a pop-out window; see
        # :meth:`set_preview_detail`.
        self._preview_detail: bool = False
        self._started_t_mono_ns: int | None = None
        self._output_path: Path | None = None
        self._output_container: Any = None
        self._output_stream: Any = None
        # The open input's video stream and what the camera actually sends
        # (e.g. "mjpeg yuvj422p 1920x1080 @ 30 fps"); set by the pump.
        self._in_stream: Any = None
        self._capture_format: str | None = None

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
        """TOML-friendly constructor (``from_params`` convention)."""
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
        """The :class:`CameraSpec` (or device spec) this adapter was built from."""
        return self._spec

    @property
    def info(self) -> CameraInfo:
        """Library-side device-info struct populated after :meth:`open`."""
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
        """Per-resource worker key.

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

        Both fields populate from :attr:`_supported_resolutions` and
        :attr:`_resolution_fps_caps`, which are set at ``open()`` and never
        mutated. UVC control values change at runtime, so they come through
        :meth:`read_state_snapshot` instead.
        """
        return WebcamMetadata(
            supported_resolutions=tuple(self._supported_resolutions),
            resolution_hint=(self._width, self._height),
            resolution_fps_caps=MappingProxyType(dict(self._resolution_fps_caps)),
        )

    async def read_state_snapshot(self) -> WebcamStateSnapshot | None:
        """Read every UVC control the camera supports, live.

        Each control's value and auto mode come from the camera itself
        (about 100 ms apiece through duvc-ctl); its range is the one the
        camera declared at ``open()``. Feeds the manual card and an
        experiment's ``device_settings``. ``None`` before :meth:`open`; a
        snapshot whose ``unavailable`` says why when the camera's controls
        can't be reached (not Windows, no duvc-ctl match).
        """
        if not self._open:
            return None
        uvc = self._uvc
        if uvc is None:
            return WebcamStateSnapshot(unavailable=self._unavailable_reason())
        with_auto = set(AUTO_VERB_TO_PROPERTY.values())
        controls: dict[str, WebcamControlState] = {}
        for verb, prop in PROPERTY_BY_VERB.items():
            if not uvc.supports(prop):
                continue
            state = await uvc.get(prop)
            rng = uvc.get_cached_range(prop)
            controls[verb.removeprefix("set_")] = WebcamControlState(
                value=None if state is None else state.value,
                auto=state.auto if state is not None and prop in with_auto else None,
                minimum=None if rng is None else rng.minimum,
                maximum=None if rng is None else rng.maximum,
                step=None if rng is None else rng.step,
            )
        return WebcamStateSnapshot(controls=controls)

    def _unavailable_reason(self) -> str:
        return self._uvc_unavailable or "no duvc-ctl device match"

    def _no_match_reason(self) -> str:
        if self._spec.serial is not None:
            return f"duvc-ctl found no camera with serial {self._spec.serial!r}"
        if self._spec.model_hint is not None:
            return f"duvc-ctl found no camera matching model_hint {self._spec.model_hint!r}"
        return (
            "duvc-ctl found no camera, or several and no model_hint or serial "
            "in the hardware profile to pick one"
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
        """Open the underlying connection. See :class:`~capa.devices.adapter.DeviceAdapter`."""
        if self._open:
            return self._info
        if sys.platform == "linux" and self._input_format == "v4l2":
            probed = _probe_v4l2_info(self._input_url)
            if probed.card_name or probed.serial:
                # Replace stub identity with the device's own metadata so
                # ``manifest.json.cameras[*].identity`` reflects the actual
                # hardware. ``identity`` was previously ``None`` for a real
                # Logitech C930e because nothing populated it.
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
        # cannot match a device) the controller stays None, only the base
        # capability flags survive, and ``_uvc_unavailable`` says why.
        self._uvc_unavailable = None
        if sys.platform != "win32":
            self._uvc_unavailable = "camera controls are only available on Windows"
        elif not uvc_backend_available():
            self._uvc_unavailable = "duvc-ctl isn't installed, so camera controls are unavailable"
        else:
            controller = await UvcController.find(
                model_hint=self._spec.model_hint,
                serial=self._spec.serial,
            )
            if controller is None:
                self._uvc_unavailable = self._no_match_reason()
            else:
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
                    self._uvc_unavailable = (
                        f"{controller.device_name} reports no adjustable controls"
                    )
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
        """Close the underlying connection. Idempotent."""
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
        """Begin recording to ``output_path``. See :class:`~capa.devices.camera.base.Camera`."""
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
        """Stop the active recording. Idempotent."""
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
        """Return a health/status snapshot. See :class:`~capa.devices.adapter.DeviceAdapter`."""
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
        """Yield recorded frames while recording is active."""
        return _drain_stream(self._frame_recv)

    def preview_stream(self) -> AsyncIterator[bytes]:
        """Yield preview frames for the live UI tile (separate from the recorded stream)."""
        return _drain_stream(self._preview_recv)

    def event_stream(self) -> AsyncIterator[CameraEvent]:
        """Yield structured camera events (open / start / stop / error)."""
        return _drain_stream(self._event_recv)

    def set_preview_detail(self, enabled: bool) -> None:
        """Switch previews between thumbnails and full-size frames.

        A pop-out preview window turns this on so the operator can judge
        focus: previews keep the camera's full frame size and arrive at
        :data:`PREVIEW_DETAIL_INTERVAL_NS`. Ignored while recording —
        previews stay 2 Hz thumbnails, so a recording costs exactly what
        it did without the pop-out. UI-only, so this is not a command and
        is never logged.
        """
        self._preview_detail = enabled

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        """Dispatch a :class:`DeviceCommand` to the right typed verb.

        Verbs (gated on capability flags populated by the open() probe):

        * **STREAM_FORMAT** — ``set_resolution`` ``{"width", "height"}``,
          ``set_framerate`` ``{"fps"}``. Reopen a running input at the new
          format straight away (the camera captures at it, so it can't
          wait for the next recording); rejected during a recording.
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
                    "during recording; it reopens the camera input"
                )
            width = int(cmd.payload["width"])
            height = int(cmd.payload["height"])
            if width <= 0 or height <= 0:
                raise AdapterError(
                    f"webcam {self._spec.name!r}: set_resolution width/height must be > 0"
                )
            self._width = width
            self._height = height
            await self._restart_input_pump()
            return f"set_resolution width={width} height={height}"
        if kind == "set_framerate":
            if CameraCapability.STREAM_FORMAT not in self.capabilities:
                raise AdapterError(f"webcam {self._spec.name!r}: STREAM_FORMAT not advertised")
            if self._recording:
                raise AdapterError(
                    f"webcam {self._spec.name!r}: set_framerate refused "
                    "during recording; it reopens the camera input"
                )
            fps = float(cmd.payload["fps"])
            if fps <= 0:
                raise AdapterError(f"webcam {self._spec.name!r}: set_framerate fps must be > 0")
            self._fps = fps
            await self._restart_input_pump()
            return f"set_framerate fps={fps}"

        # ---- UVC numeric set ----
        if kind in PROPERTY_BY_VERB:
            if self._uvc is None:
                raise AdapterError(
                    f"webcam {self._spec.name!r}: UVC controls unavailable "
                    f"({self._unavailable_reason()})"
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
                    f"({self._unavailable_reason()})"
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
          recording (preview-only mode), when the encoder rejected the
          frame, or with ``codec = "copy"``, which records packets in
          :meth:`_mux_packet_sync` instead.
        * ``preview_bytes`` is ``None`` on throttled ticks (2 Hz cap, or
          10 Hz between runs while :meth:`set_preview_detail` is on).
        * ``drop_reason`` is non-``None`` only when the encoder rejected
          the frame mid-recording (libx264 EINVAL, format renegotiation,
          …). Dropping rather than raising keeps the pump alive across
          single-frame faults.

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
        if (
            recording
            and self._codec != COPY_CODEC
            and out_container is not None
            and out_stream is not None
        ):
            video_frame = av.VideoFrame.from_ndarray(frame, format="rgb24")
            video_frame.pts = self._frame_count
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
                receipt = self._count_frame(t_mono_ns, capture_latency_s)

        # Preview is unconditional: the live tile must stay current
        # whether or not we're recording. 2 Hz cap, JPEG-encoded thumbnail,
        # aspect preserved. Skipped encodes save the Pillow + libjpeg cost
        # on every frame the consumer cannot use (28 of 30 at webcam rate).
        # DROP_OLDEST is enforced by ``_preview_send`` having capacity 2;
        # ``send_nowait`` from the wrapper silently drops on backpressure.
        preview_bytes: bytes | None = None
        if self._preview_due(t_mono_ns, recording=recording):
            preview_bytes = _encode_preview_jpeg(
                frame, full_size=self._preview_detail and not recording
            )
            self._last_preview_t_mono_ns = t_mono_ns
        return receipt, preview_bytes, drop_reason

    def _preview_due(self, t_mono_ns: int, *, recording: bool) -> bool:
        """Whether the next preview is due at ``t_mono_ns``.

        2 Hz thumbnails, or 10 Hz full-size frames for a pop-out window.
        The pop-out rate is honoured only between runs: while recording,
        previews stay 2 Hz thumbnails whatever was asked, so the recording
        never pays for them.
        """
        detail = self._preview_detail and not recording
        interval = PREVIEW_DETAIL_INTERVAL_NS if detail else PREVIEW_INTERVAL_NS
        last = self._last_preview_t_mono_ns
        return last is None or t_mono_ns - last >= interval

    def _count_frame(self, t_mono_ns: int, capture_latency_s: float) -> FrameReceipt:
        """Advance the frame index past a recorded frame; return its receipt."""
        idx = self._frame_count
        self._frame_count = idx + 1
        self._last_frame_t_mono_ns = t_mono_ns
        return FrameReceipt(
            name=self._spec.name,
            frame_idx=idx,
            t_mono_ns=t_mono_ns,
            t_utc=self._clock.to_wall_ns(t_mono_ns),
            capture_latency_s=capture_latency_s,
        )

    def _mux_packet_sync(self, packet: av.Packet[Any]) -> tuple[FrameReceipt | None, str | None]:
        """Write one camera packet to the MKV unchanged (``codec = "copy"``).

        Timestamps are rewritten to the frame index at the configured rate,
        as the encoder path's are, so MKV frame N is receipt N. Returns
        ``(receipt, drop_reason)`` as :meth:`_push_frame_sync` does.
        """
        t_mono_ns = self._clock.t_mono_ns()
        out_container = self._output_container
        out_stream = self._output_stream
        if not self._recording or out_container is None or out_stream is None:
            return None, None
        packet.stream = out_stream
        packet.time_base = 1 / Fraction(self._fps).limit_denominator(1001)
        packet.pts = packet.dts = self._frame_count
        packet.duration = 1
        try:
            out_container.mux(packet)
        except av.error.FFmpegError as exc:
            self._dropped_frames += 1
            return None, f"{type(exc).__name__}: {exc} (codec=copy)"
        return self._count_frame(t_mono_ns, 0.0), None

    async def _record_packet(self, packet: av.Packet[Any]) -> None:
        """Record one camera packet (``codec = "copy"``) off the asyncio loop."""
        receipt, drop_reason = await anyio.to_thread.run_sync(self._mux_packet_sync, packet)
        await self._publish(receipt, drop_reason, None)

    async def _publish(
        self,
        receipt: FrameReceipt | None,
        drop_reason: str | None,
        preview_bytes: bytes | None,
    ) -> None:
        """Send a frame's receipt, drop warning and preview to their streams."""
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

    async def push_frame(
        self,
        frame: np.ndarray,
        *,
        capture_latency_s: float = 0.0,
    ) -> FrameReceipt | None:
        """Process ``frame`` (HxWx3 uint8, RGB24): encode to MKV + emit a
        :class:`FrameReceipt` if recording; always emit a 2 Hz preview JPEG.

        Returns the receipt when one was produced (recording succeeded);
        returns ``None`` when not recording (preview-only), when the
        encoder rejected the frame, or with ``codec = "copy"`` (the pump
        records the camera's packets instead). Encoder-reject paths land a
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
        await self._publish(receipt, drop_reason, preview_bytes)
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

        The input is opened at the configured frame size and rate. Without
        them DirectShow opens a C930e at its default 640×480, and every
        frame was upscaled to the configured size; with them it picks the
        capture format that delivers both (MJPEG at 1080p30), and a size or
        rate the camera can't deliver fails the open.
        """
        options = {"video_size": f"{self._width}x{self._height}", "framerate": f"{self._fps:g}"}
        deadline = time.monotonic() + OPEN_RETRY_DEADLINE_S
        attempt = 0
        last_exc: OSError | None = None
        for delay in OPEN_RETRY_DELAYS_S:
            try:
                return await anyio.to_thread.run_sync(
                    lambda: av.open(self._input_url, format=self._input_format, options=options)
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
                lambda: av.open(self._input_url, format=self._input_format, options=options)
            )
        except OSError as exc:
            if last_exc is not None and _is_transient_open_error(exc):
                raise AdapterError(
                    f"webcam {self._spec.name!r}: av.open kept returning an I/O "
                    f"error after {attempt + 1} retries. DirectShow returns it "
                    f"while another process holds the camera, and when the camera "
                    f"can't deliver {self._width}x{self._height} at {self._fps:g} fps. "
                    f"Last error: {exc}",
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

        Returns once the first frame arrives (or the pump gives up, or
        :data:`STREAM_START_TIMEOUT_S` passes). A camera applies some
        settings of its own when it starts streaming — the C930e resets
        its exposure — so camera settings must be sent after this returns
        or the stream start overwrites them; the experiment's
        ``device_settings`` are applied after the pool opens.

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
        streaming = asyncio.Event()
        loop = asyncio.get_running_loop()
        pump = loop.create_task(
            self._run_input_loop(streaming),
            name=f"webcam-input-{self._spec.name}",
        )
        self._pump_task = pump
        first_frame = loop.create_task(streaming.wait())
        try:
            await asyncio.wait(
                {pump, first_frame},
                timeout=STREAM_START_TIMEOUT_S,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            first_frame.cancel()
        if not streaming.is_set() and not pump.done():
            _logger.warning(
                "webcam.stream_start_slow",
                camera=self._spec.name,
                waited_s=STREAM_START_TIMEOUT_S,
            )

    async def _restart_input_pump(self) -> None:
        """Reopen a running input so the camera captures at a new format.

        The camera reapplies its own settings when the stream restarts, so
        the experiment's ``device_settings`` (re-planned before Start) send
        exposure again.
        """
        if self._pump_task is None or self._pump_task.done():
            return
        await self.stop_input_pump()
        await self.start_input_pump()

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

    async def _run_input_loop(self, streaming: asyncio.Event) -> None:
        """Long-lived input pump (pool lifetime).

        Opens the PyAV input container with retry, then loops over the
        camera's packets: ``decode → reformat → push_frame`` for each frame
        it needs, and with ``codec = "copy"`` the packet itself goes to the
        MKV while recording. ``push_frame`` emits a 2 Hz preview JPEG and,
        when re-encoding, encodes + emits a :class:`FrameReceipt` while
        :attr:`_recording` is set. Sets ``streaming`` at the first packet.
        Exits when :attr:`_pump_stop` is set, when the input reaches EOF,
        or on cancellation.

        A frame is decoded only when it is used: every frame while
        re-encoding a recording, otherwise only those a preview is due
        for, so copying a 1080p MJPEG stream decodes 2 frames a second,
        not 30. Inputs whose frames don't decode on their own (H.264 from
        a camera) decode every packet.

        ``av.open`` is wrapped in :meth:`_open_input_with_retry` so the
        Windows DirectShow filter-graph hold-time after a previous close
        is absorbed transparently. Reads, decodes, reformats and muxes run
        in a worker thread so the per-frame work doesn't block the asyncio
        loop.

        When ``CAPA_WEBCAM_FRAME_DIAG=1`` is set, the first 150 decoded
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
            await self._note_input(in_stream)
            copy = self._codec == COPY_CODEC
            intra_only = bool(in_stream.codec_context.codec.intra_only)
            demuxer = in_container.demux(in_stream)
            while self._open and not self._pump_stop.is_set():
                packet = await anyio.to_thread.run_sync(_next_packet, demuxer)
                if packet is None or self._pump_stop.is_set() or not self._open:
                    break
                streaming.set()
                recording = self._recording
                if (
                    not intra_only
                    or (recording and not copy)
                    or self._preview_due(self._clock.t_mono_ns(), recording=recording)
                ):
                    frames = await anyio.to_thread.run_sync(_decode_packet, in_stream, packet)
                    for frame in frames:
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
                if copy:
                    await self._record_packet(packet)
        finally:
            self._in_stream = None
            with contextlib.suppress(BaseException):
                await anyio.to_thread.run_sync(in_container.close)

    async def _note_input(self, in_stream: Any) -> None:
        """Record what the camera actually sends, and warn when its frame
        size isn't the configured one (the encoder would rescale it)."""
        self._in_stream = in_stream
        ctx = in_stream.codec_context
        rate = in_stream.average_rate
        fps = f"{float(rate):g} fps" if rate else "unknown fps"
        self._capture_format = f"{ctx.name} {ctx.pix_fmt} {ctx.width}x{ctx.height} @ {fps}"
        _logger.info("webcam.input_opened", camera=self._spec.name, format=self._capture_format)
        if (ctx.width, ctx.height) != (self._width, self._height):
            message = (
                f"camera sends {ctx.width}x{ctx.height}, not the configured "
                f"{self._width}x{self._height}"
            )
            _logger.warning("webcam.capture_mismatch", camera=self._spec.name, detail=message)
            # Not a pump_warning: the preview tile counts those as dropped frames.
            await self._emit_event(kind="capture_mismatch", severity="warning", message=message)

    # -------------------------------------------------------------- internals

    def _open_encoder(self) -> None:
        """Open the output container + stream.

        With ``codec = "copy"`` the stream copies the camera's (its codec,
        size and pixel format), so the camera must already be streaming.
        """
        assert self._output_path is not None
        copy = self._codec == COPY_CODEC
        if copy and self._in_stream is None:
            raise AdapterError(
                f"webcam {self._spec.name!r}: codec 'copy' records the camera's own "
                "frames, but the camera isn't streaming"
            )
        container = av.open(str(self._output_path), mode="w", format="matroska")
        # MKV container metadata anchor.
        anchor_utc = self._clock.to_wall_ns(self._clock.t_mono_ns())
        container.metadata["run_started_utc"] = anchor_utc.isoformat()
        container.metadata["camera_name"] = self._spec.name
        container.metadata["capa_codec"] = self._codec
        if self._capture_format is not None:
            container.metadata["capture_format"] = self._capture_format

        if copy:
            stream = container.add_stream_from_template(self._in_stream)
        else:
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
        # Flush remaining packets; a copied stream has no encoder to flush.
        if self._codec != COPY_CODEC:
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
