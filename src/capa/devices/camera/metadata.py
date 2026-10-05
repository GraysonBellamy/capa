""":class:`WebcamMetadata` — cross-loop snapshot of webcam probe data.

The :class:`~capa.ui.manual.cards.webcam.WebcamCard`
needs the supported resolutions and per-resolution fps caps to rebuild
its stream-format widgets when the pool publishes. Those values live on the
:class:`~capa.devices.camera.webcam.WebcamAdapter` instance, which is
owned by the worker loop.
Reading them from the qasync loop is a cross-loop access.

The de facto read used to be safe — the attributes are populated at
``open()`` and never mutated — but the invariant is brittle and the
comment in the card invited future contributors to copy the pattern.
This module replaces that read with a typed snapshot taken on the worker
loop and shipped back through the existing :class:`WorkerRunner` future
machinery.

Only webcams expose this surface today. IR cameras (FLIR Atlas, the IR
sim) don't probe stream formats; the dispatcher returns ``None`` for them
and the card falls back to its static widget set. A webcam's UVC
controls (ranges, current values, auto modes) change at runtime, so they
come through the live read-back instead
(:class:`~capa.devices.camera.base.WebcamStateSnapshot`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class WebcamMetadata:
    """Probe snapshot for one :class:`WebcamAdapter`.

    Captured on the worker loop by
    :meth:`CameraDeviceAdapter.camera_metadata` and consumed on the
    qasync loop by :class:`WebcamCard`. Carries everything the card
    needs to rebuild its resolution combo and fps cap without ever
    touching the live adapter from the UI loop.

    The fps-cap mapping is a :class:`MappingProxyType` so the consumer
    can't mutate the snapshot — same shape as the dataclass-immutability
    of the rest of the fields.
    """

    supported_resolutions: tuple[tuple[int, int], ...]
    """``(width, height)`` pairs the device advertised at open(). Empty
    when the probe never ran (non-Windows, dshow probe came up empty)
    — card falls back to its static list."""

    resolution_hint: tuple[int, int]
    """The ``(width, height)`` currently configured for the next
    ``start_recording``. Card uses this to preselect the matching combo
    entry on rebuild."""

    resolution_fps_caps: Mapping[tuple[int, int], float] = field(
        default_factory=lambda: MappingProxyType({}),
    )
    """Per-resolution maximum fps the device advertised. Empty mapping
    when the probe didn't capture fps annotations alongside the
    resolution list."""


__all__ = ["WebcamMetadata"]
