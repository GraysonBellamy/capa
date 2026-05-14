"""Progress events for config-lifetime hardware initialization.

The runtime owns the truth about adapter open/rollback progress, but the UI
owns presentation. These small, Qt-free records are the bridge between the
two: workers emit one row at a time, and the UI/controller aggregates them
into an operator-facing "Preparing hardware" view.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

Primitive = str | int | float | bool


class DeviceInitStatus(StrEnum):
    """Per-adapter status while a config's worker pool opens."""

    PENDING = "pending"
    OPENING = "opening"
    READY = "ready"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True, slots=True)
class DeviceInitProgress:
    """One progress row for a device or camera adapter."""

    name: str
    adapter: str
    resource_id: str
    status: DeviceInitStatus
    detail: str = ""
    error_type: str | None = None
    identity: Mapping[str, Primitive] | None = None


OpenProgressCallback = Callable[[DeviceInitProgress], None]


def identity_from_device_info(info: Any) -> dict[str, Primitive] | None:
    """Best-effort identity extraction from an adapter's ``device_info``.

    Non-primitive values, such as vendor firmware-version objects, are
    coerced through ``str()`` so callers can safely display or serialize the
    resulting mapping.
    """
    if info is None:
        return None
    out: dict[str, Primitive] = {}
    for attr in (
        "vendor",
        "model",
        "model_number",
        "serial",
        "serial_number",
        "firmware",
        "firmware_version",
        "device_id",
        "v4l2_id",
        "bus",
        "uri",
    ):
        value = getattr(info, attr, None)
        if value is None:
            continue
        if not isinstance(value, str | int | float | bool):
            value = str(value)
        out[attr] = value
    if not out:
        return None
    return out


def identity_summary(identity: Mapping[str, Primitive] | None) -> str:
    """Compact human-facing identity string for progress rows."""
    if not identity:
        return "connected"

    parts: list[str] = []
    for key in ("model", "model_number", "device_id"):
        value = identity.get(key)
        if value not in (None, ""):
            parts.append(str(value))
            break
    for key in ("serial", "serial_number", "v4l2_id"):
        value = identity.get(key)
        if value not in (None, ""):
            parts.append(f"serial {value}")
            break
    for key in ("firmware", "firmware_version"):
        value = identity.get(key)
        if value not in (None, ""):
            parts.append(f"fw {value}")
            break
    return ", ".join(parts) if parts else "connected"


__all__ = [
    "DeviceInitProgress",
    "DeviceInitStatus",
    "OpenProgressCallback",
    "Primitive",
    "identity_from_device_info",
    "identity_summary",
]
