"""Control-surface flags on :class:`CameraCapability`.

capa-flir Phase 4 references these by name on the FLIR adapter; missing
names break the adapter at import time. The check here is intentionally
narrow — pure-Python enum semantics, no adapter wiring.
"""

from __future__ import annotations

from capa.devices.camera.base import CameraCapability


def test_control_surface_flags_present() -> None:
    """capa-flir Phase 4 references these by name; missing names break the
    adapter at import time."""
    for name in (
        "NUC_TRIGGER",
        "RADIOMETRIC_PARAMS",
        "TEMPERATURE_RANGE_SELECT",
        "AUTO_NUC_INTERVAL",
        "REMOTE_PALETTE",
    ):
        assert hasattr(CameraCapability, name), name


def test_control_surface_flags_compose() -> None:
    """Flag-of-flags semantics — adapters OR these together to advertise
    the live capability set."""
    bundle = (
        CameraCapability.NUC_TRIGGER
        | CameraCapability.RADIOMETRIC_PARAMS
        | CameraCapability.LIVE_PREVIEW
    )
    assert CameraCapability.NUC_TRIGGER in bundle
    assert CameraCapability.MEASUREMENT_SHAPES not in bundle
