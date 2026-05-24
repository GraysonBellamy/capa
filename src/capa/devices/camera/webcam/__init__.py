"""Visible-camera adapter package — PyAV-driven H.264 → MKV.

Importing this package registers :data:`DESCRIPTOR` into the global
adapter registry, so plugin discovery + the Setup editor see the
adapter without any explicit module load.
"""

from __future__ import annotations

from capa.devices.camera.webcam.adapter import WebcamAdapter
from capa.devices.camera.webcam.constants import (
    DEFAULT_CODEC,
    DEFAULT_FPS,
    DEFAULT_PIX_FMT,
    OPEN_RETRY_DEADLINE_S,
    OPEN_RETRY_DELAYS_S,
    PREVIEW_INTERVAL_NS,
    PREVIEW_JPEG_QUALITY,
    PREVIEW_MAX_WIDTH,
)
from capa.devices.camera.webcam.descriptor import DESCRIPTOR, WebcamParams
from capa.devices.camera.webcam.probe import discover_cameras, handshake
from capa.devices.registry import register as _register

_register(DESCRIPTOR)


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
