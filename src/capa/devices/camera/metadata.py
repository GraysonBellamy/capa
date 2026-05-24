""":class:`WebcamMetadata` — cross-loop snapshot of webcam probe data.

The :class:`~capa.ui.manual.cards.webcam.WebcamCard`
needs UVC ranges, supported resolutions, and per-resolution fps caps to
rebuild its widgets when the pool publishes. Those values live on the
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
sim) don't probe stream formats or UVC ranges; the dispatcher returns
``None`` for them and the card falls back to its static widget set.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class UvcRangeMetadata:
    """One UVC property's range + current value, captured at probe time.

    Mirrors :class:`capa.devices.camera._uvc.UvcPropertyRange` plus the
    device's last-reported current value (so the card can seed spinboxes
    with the value the camera actually holds, not the factory default).
    Frozen so the snapshot can be passed across loops without aliasing.
    """

    minimum: int
    maximum: int
    step: int
    default: int
    current: int | None


@dataclass(frozen=True, slots=True)
class WebcamMetadata:
    """Probe snapshot for one :class:`WebcamAdapter`.

    Captured on the worker loop by
    :meth:`CameraDeviceAdapter.camera_metadata` and consumed on the
    qasync loop by :class:`WebcamCard`. Carries everything the card
    needs to rebuild its resolution combo, fps cap, and UVC spinbox
    bounds without ever touching the live adapter from the UI loop.

    The two mappings use :class:`MappingProxyType` so the consumer
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

    uvc_ranges: Mapping[str, UvcRangeMetadata] = field(
        default_factory=lambda: MappingProxyType({}),
    )
    """UVC property ranges keyed by the verb name (e.g. ``"set_exposure"``)
    used by :data:`capa.devices.camera._uvc.PROPERTY_BY_VERB`. Empty when
    duvc-ctl is unavailable or the device exposed no controllable
    properties — card leaves the wide default spinbox bounds."""


__all__ = ["UvcRangeMetadata", "WebcamMetadata"]
