"""PyAV decode / RGB reformat / preview-JPEG helpers.

Each function runs once per frame in the input pump and is wrapped by
:func:`anyio.to_thread.run_sync` at the call site so the libav and
libjpeg CPU cost stays off the asyncio loop.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from typing import Any

import av
import numpy as np
from anyio.streams.memory import MemoryObjectReceiveStream
from PIL import Image

from capa.devices.camera.webcam.constants import PREVIEW_JPEG_QUALITY, PREVIEW_MAX_WIDTH


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

    Wrapped in :func:`anyio.to_thread.run_sync` by :meth:`WebcamAdapter._run_input_loop`
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
