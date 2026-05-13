""":class:`PreviewFrame` wire type and :func:`run_preview_drain` worker task.

Phase 4 follow-up — fills in the cross-thread plumbing the migration left
deferred. Cameras live on the worker loop after Phase 4; the UI loop cannot
iterate :meth:`Camera.preview_stream` directly (migration doc §3.11
invariant 2). This module gives the worker side a tiny coroutine that drains
the camera's preview iterator onto a :class:`ThreadBridge` whose consumer is
the qasync loop.

The bridge is **per-camera** and **pool-resident**: cameras stay open across
runs (migration doc §3.2), so preview must too — operators get a live tile
between runs as well as during one. The bridge therefore cannot live on the
Conductor (per-run lifetime); it lives on :class:`WorkerPool`.

The wire type carries the camera name explicitly so the UI side can route a
frame to the correct tile without consulting which bridge it arrived on
(the dock and the manual webcam card both want this lookup).

Policy: :attr:`BridgePolicy.DROP_OLDEST`. Preview is a best-effort viewport;
falling-behind UI must never block the worker pump. This mirrors the
``_preview_send`` capacity-2 buffer already inside :class:`WebcamAdapter`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import anyio
import structlog

if TYPE_CHECKING:
    from capa.devices.camera.base import Camera
    from capa.runtime.bridge import ThreadBridge

_logger = structlog.get_logger("capa.runtime.preview")


@dataclass(frozen=True, slots=True)
class PreviewFrame:
    """One JPEG-thumbnailed preview frame from a camera, headed for the UI.

    Wire type for the preview :class:`ThreadBridge`. Distinct from
    :class:`~capa.devices.camera.base.FrameReceipt` (durable, indexed) and
    :class:`~capa.devices.camera.base.CameraEvent` (logged): preview is
    best-effort, UI-only, DROP_OLDEST. Carries the camera name so the UI side
    can route a frame to the right tile without looking up the bridge it
    arrived on (the dock and the manual cards both want this lookup).
    """

    name: str
    """Camera name (matches :attr:`CameraSpec.name`)."""
    t_mono_ns: int
    """Monotonic timestamp at the point the drainer received the JPEG. The
    drainer reads it once on receipt rather than threading the producer's
    own timestamp through, because preview is best-effort and a few-ms skew
    is invisible at the 2 Hz cadence."""
    jpeg: bytes
    """The encoded preview payload. JPEG is the convention the existing
    :class:`~capa.ui.docks.camera_preview._PreviewTile` decodes via
    :meth:`QImage.fromData`."""


async def run_preview_drain(
    *,
    camera: Camera,
    bridge: ThreadBridge[PreviewFrame],
) -> None:
    """Drain ``camera.preview_stream()`` onto ``bridge`` for the camera's
    entire open lifetime. Runs on the worker loop.

    Exits when:

    * The camera's preview iterator terminates (``preview_send.aclose``
      inside the adapter on :meth:`Camera.close` causes the async-for to
      end naturally), or
    * The task is cancelled (worker close path).

    Per-frame errors (e.g. a transient encode failure on the producer side)
    are logged and swallowed — a single hiccup must not kill the long-lived
    pump task. A bridge-close error is treated as terminal: the consumer
    went away, so we exit cleanly.
    """
    name = camera.spec.name
    try:
        async for jpeg in camera.preview_stream():
            try:
                await bridge.put(
                    PreviewFrame(
                        name=name,
                        t_mono_ns=time.monotonic_ns(),
                        jpeg=jpeg,
                    )
                )
            except anyio.get_cancelled_exc_class():
                raise
            except BaseException as exc:
                # ThreadBridgeClosedError lands here too; if the consumer
                # is gone there's no work left. Log and exit.
                _logger.debug(
                    "preview.bridge_put_failed",
                    camera=name,
                    error=str(exc),
                )
                return
    except anyio.get_cancelled_exc_class():
        raise
    except BaseException as exc:
        _logger.warning(
            "preview.drain_failed",
            camera=name,
            error=str(exc),
        )


__all__ = [
    "PreviewFrame",
    "run_preview_drain",
]
