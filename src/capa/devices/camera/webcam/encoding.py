"""PyAV demux / decode / RGB reformat / preview-JPEG helpers.

Each function runs per packet or per frame in the input pump and is
wrapped by :func:`anyio.to_thread.run_sync` at the call site so the
blocking read and the libav and libjpeg CPU cost stay off the asyncio loop.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator, Iterator
from typing import Any

import av
import numpy as np
from anyio.streams.memory import MemoryObjectReceiveStream
from PIL import Image

from capa.devices.camera.webcam.constants import (
    PREVIEW_DETAIL_JPEG_QUALITY,
    PREVIEW_JPEG_QUALITY,
    PREVIEW_MAX_WIDTH,
)


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


def _next_packet(demuxer: Iterator[av.Packet[Any]]) -> av.Packet[Any] | None:
    """Pull the next non-empty packet from a PyAV demuxer; ``None`` at EOF.

    The demuxer ends with an empty flush packet, which is skipped. Wrapped in
    :func:`anyio.to_thread.run_sync` by :meth:`WebcamAdapter._run_input_loop`
    because the read blocks until the camera delivers the next frame.
    """
    for packet in demuxer:
        if packet.size:
            return packet
    return None


def _decode_packet(stream: Any, packet: av.Packet[Any]) -> list[av.VideoFrame]:
    """Decode one packet with the input stream's decoder.

    Capture packets are whole frames (MJPEG, raw), so one packet gives one
    frame. Wrapped by :func:`anyio.to_thread.run_sync`: a 1080p MJPEG decode
    takes a few milliseconds.
    """
    return [frame for frame in stream.decode(packet) if isinstance(frame, av.VideoFrame)]


def _reformat_to_rgb24(frame: av.VideoFrame) -> np.ndarray:
    """Convert a decoded frame to an HxWx3 uint8 RGB ndarray.

    Wrapped by :func:`anyio.to_thread.run_sync` for the same reason as
    :func:`_advance_decoder` — colour conversion is CPU-heavy.
    """
    return frame.reformat(format="rgb24").to_ndarray()


def _encode_preview_jpeg(frame: np.ndarray, *, full_size: bool = False) -> bytes:
    """Width-cap to :data:`PREVIEW_MAX_WIDTH` (aspect preserved) and JPEG-encode.

    ``full_size`` keeps the whole frame at :data:`PREVIEW_DETAIL_JPEG_QUALITY`
    for a pop-out preview window instead of a thumbnail.

    Runs inside ``_push_frame_sync``, which the async wrapper already executes
    via :func:`anyio.to_thread.run_sync`, so the libjpeg work stays off the
    asyncio loop.
    """
    img = Image.fromarray(frame)
    if full_size:
        quality = PREVIEW_DETAIL_JPEG_QUALITY
    else:
        quality = PREVIEW_JPEG_QUALITY
        if img.width > PREVIEW_MAX_WIDTH:
            new_h = max(1, round(img.height * (PREVIEW_MAX_WIDTH / img.width)))
            img = img.resize((PREVIEW_MAX_WIDTH, new_h), Image.Resampling.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


async def _drain_stream[T](recv: MemoryObjectReceiveStream[T]) -> AsyncIterator[T]:
    """Yield items from a memory object stream until the send end is closed.

    Mirrors :func:`capa.devices.sim.flir_ir_sim._drain_stream`. Centralizing
    in :mod:`capa.devices.camera.base` would invite a circular import; keep
    the duplicate.
    """
    async for item in recv:
        yield item
