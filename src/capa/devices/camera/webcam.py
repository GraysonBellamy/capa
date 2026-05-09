"""Visible-camera adapter — PyAV-driven H.264 → MKV (plan §12.3).

The adapter runs in two complementary modes:

* **Push mode.** The host calls :meth:`WebcamAdapter.push_frame` with a numpy
  ndarray and a capture timestamp. Used by tests and by the engine when the
  frame source is a non-PyAV producer (e.g. a hardware-specific grabber that
  pushes via the loopback ingest).
* **Pump mode.** :meth:`WebcamAdapter.run_pump` opens an input
  :class:`av.container.InputContainer` and forwards each decoded frame
  through the same push path. The default input is a V4L2 device on Linux,
  AVFoundation on macOS, or DirectShow on Windows; ``params["input_url"]``
  / ``params["input_format"]`` override.

Both modes share the encoder + frame-index path, so live-capture and tests
exercise identical bookkeeping.

MKV container metadata carries the run-start UTC anchor (plan §12.5) so an
external tool can re-correlate by absolute time without parsing capa's
manifest.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import anyio
import av
import numpy as np
import structlog
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from PIL import Image

from capa.core.clock import RunClock
from capa.core.errors import AdapterError
from capa.devices.camera.base import (
    CameraCapability,
    CameraEvent,
    CameraHealth,
    CameraInfo,
    CameraSpec,
    FrameReceipt,
    make_stream_pair,
)

DEFAULT_CODEC = "libx264"
DEFAULT_PIX_FMT = "yuv420p"
DEFAULT_FPS = 30

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
        "_recording",
        "_spec",
        "_started_t_mono_ns",
        "_width",
    )

    capabilities: frozenset[CameraCapability] = frozenset(
        {
            CameraCapability.SUPPORTS_DISCOVERY,
            CameraCapability.SERIAL_SELECT,
            CameraCapability.LIVE_PREVIEW,
        }
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
        self._info = CameraInfo(
            adapter="webcam",
            name=spec.name,
            model=spec.model_hint,
            serial=spec.serial,
            transport="usb",
            capabilities=tuple(c.name for c in self.capabilities if c.name is not None),
        )
        self._open = False
        self._recording = False
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
        """TOML-friendly constructor (plan §16 P3 ``from_params`` convention)."""
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
        self._open = True
        return self._info

    async def close(self) -> None:
        if self._recording:
            await self.stop_recording()
        await self._frame_send.aclose()
        await self._preview_send.aclose()
        await self._event_send.aclose()
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

    # ----------------------------------------------------------- frame ingest

    def _push_frame_sync(
        self,
        frame: np.ndarray,
        capture_latency_s: float = 0.0,
    ) -> tuple[FrameReceipt | None, bytes | None, str | None]:
        """Encode + mux + bookkeep a single frame.

        Returns ``(receipt, preview_bytes, drop_reason)`` — ``drop_reason``
        is ``None`` on success and a short error string when the frame was
        rejected by the encoder (e.g. libx264 EINVAL for a malformed input;
        hardware-day §6 anomaly). Dropping rather than raising keeps the
        pump alive across single-frame faults.

        The CPU-heavy work (libx264 encode, container mux) lives here so
        :meth:`push_frame` can run it in a worker thread via
        :func:`anyio.to_thread.run_sync`, keeping the asyncio loop free for
        other adapters' I/O.
        """
        if not self._recording or self._output_container is None or self._output_stream is None:
            raise AdapterError("push_frame requires start_recording()")
        if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8:
            raise AdapterError(
                f"push_frame expects HxWx3 uint8 ndarray; got "
                f"shape={frame.shape} dtype={frame.dtype}"
            )

        idx = self._frame_count
        t_mono_ns = self._clock.t_mono_ns()

        video_frame = av.VideoFrame.from_ndarray(frame, format="rgb24")
        video_frame.pts = idx
        try:
            # time_base set by the stream; PyAV remuxes pts via that.
            for packet in self._output_stream.encode(video_frame):
                self._output_container.mux(packet)
        except av.error.FFmpegError as exc:
            # Frame index is NOT advanced — the next push reuses ``idx``,
            # so receipt frame_idx values stay contiguous over surviving
            # frames and the encoder doesn't see a pts gap.
            self._dropped_frames += 1
            reason = f"{type(exc).__name__}: {exc} (shape={frame.shape}, encoder={self._codec})"
            return None, None, reason

        self._frame_count = idx + 1
        self._last_frame_t_mono_ns = t_mono_ns
        receipt = FrameReceipt(
            name=self._spec.name,
            frame_idx=idx,
            t_mono_ns=t_mono_ns,
            t_utc=self._clock.to_wall_ns(t_mono_ns),
            capture_latency_s=capture_latency_s,
        )
        # Preview: 2 Hz cap, JPEG-encoded thumbnail, aspect preserved. Skipped
        # encodes save the Pillow + libjpeg cost on every frame the consumer
        # cannot use (28 of 30 at webcam rate). DROP_OLDEST is enforced by
        # ``_preview_send`` having capacity 2; ``send_nowait`` from the wrapper
        # silently drops on backpressure.
        preview_bytes: bytes | None = None
        if (
            self._last_preview_t_mono_ns is None
            or t_mono_ns - self._last_preview_t_mono_ns >= PREVIEW_INTERVAL_NS
        ):
            preview_bytes = _encode_preview_jpeg(frame)
            self._last_preview_t_mono_ns = t_mono_ns
        return receipt, preview_bytes, None

    async def push_frame(
        self,
        frame: np.ndarray,
        *,
        capture_latency_s: float = 0.0,
    ) -> FrameReceipt | None:
        """Encode ``frame`` (HxWx3 uint8, RGB24) into the MKV stream and emit
        a :class:`FrameReceipt`.

        Returns ``None`` when the encoder rejected the frame; the bad
        frame is counted in :attr:`CameraHealth.dropped_frames` and a
        ``pump_warning`` event lands on the event stream. Hardware-day §6
        regression: previously this raised :class:`av.error.FFmpegError`
        and killed the entire camera task.

        Used by tests and by adapters that source frames from outside PyAV
        (vendor SDKs, network grabbers). The engine's pump-mode loop also
        funnels through this path so the timestamp + frame-index bookkeeping
        is identical for both modes. The libx264 encode runs in a worker
        thread so the asyncio loop stays responsive at 30 fps.
        """
        receipt, preview_bytes, drop_reason = await anyio.to_thread.run_sync(
            self._push_frame_sync, frame, capture_latency_s
        )
        if drop_reason is not None:
            await self._emit_event(
                kind="pump_warning",
                message=f"frame dropped: {drop_reason}",
                severity="warning",
            )
            return None
        assert receipt is not None
        await self._frame_send.send(receipt)
        # ``preview_bytes`` is ``None`` on throttled ticks (2 Hz cap inside
        # ``_push_frame_sync``). DROP_OLDEST is preserved by ignoring
        # ``WouldBlock`` from the bounded preview stream.
        if preview_bytes is not None:
            with contextlib.suppress(anyio.WouldBlock):
                self._preview_send.send_nowait(preview_bytes)
        return receipt

    async def run_pump(self) -> None:
        """Open the configured PyAV input and push every decoded frame.

        Hardware-only path. Tests use :meth:`push_frame` directly. Cancellation
        propagates through the AnyIO task group.

        The decode and reformat steps each run in a worker thread; without
        this the per-frame CPU work (libav decode, RGB conversion) would
        block the asyncio loop and starve other adapters. Observed in
        hardware-day §5.B: webcam at ~14 fps instead of 30, recipe wall-
        clock 2.7× nominal.

        When ``CAPA_WEBCAM_FRAME_DIAG=1`` is set, the first 150 input frames
        are logged at INFO with ``frame.format.name`` / ``width`` / ``height``
        / ``pts``. Used to investigate the libx264 EINVAL observed at t≈23 s
        in hardware-day §5.A — correlate any UVC frame-format renegotiation
        with the first ``pump_warning`` event.
        """
        if not self._recording:
            raise AdapterError("run_pump requires start_recording()")
        diag_enabled = os.environ.get("CAPA_WEBCAM_FRAME_DIAG") == "1"
        diag_remaining = 150 if diag_enabled else 0
        diag_log = structlog.get_logger("capa.webcam.frame_diag") if diag_enabled else None
        in_container = await anyio.to_thread.run_sync(
            lambda: av.open(self._input_url, format=self._input_format)
        )
        try:
            in_stream = next(s for s in in_container.streams if s.type == "video")
            decoder = in_container.decode(in_stream)
            while self._recording:
                frame = await anyio.to_thread.run_sync(_advance_decoder, decoder)
                # close() may flip _recording to False while the decode worker
                # was running; bail before push_frame's precondition guard
                # would raise AdapterError("push_frame requires
                # start_recording()") and surface as a misleading
                # engine.camera.pump_failed warning on every clean stop.
                if frame is None or not self._recording:
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
                # Same race window applies after reformat (which is itself a
                # threaded yield). Match the ``_push_frame_sync`` precondition
                # shape to defeat mypy narrowing of ``_recording`` from the
                # ``while`` condition above and to match the guard the push
                # path itself enforces.
                if not self._recording or self._output_container is None:
                    break
                # push_frame returns None on encoder-rejected frames; the
                # adapter has already emitted a pump_warning event and
                # bumped dropped_frames. Pump continues with the next
                # frame rather than dying.
                await self.push_frame(rgb)
        finally:
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
    if sys.platform != "linux":
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
    "PREVIEW_INTERVAL_NS",
    "PREVIEW_JPEG_QUALITY",
    "PREVIEW_MAX_WIDTH",
    "WebcamAdapter",
]
