"""Camera subsystem ().

A :class:`Camera` is a peer of :class:`~capa.devices.adapter.DeviceAdapter` —
not a subtype. The shape is different:

* Cameras own their own output container (MKV for visible, ``.csq`` for IR).
  They do not emit :class:`~capa.devices.records.ChannelSample`\\ s; the bundle
  records frame-index parquet plus a meta-json sidecar instead.
* Sampling is event-driven (frame arrival), not poll-driven.
* The camera task lives in the run task group alongside device producers but
  drains a separate set of streams (preview, health, frame-receipt records).

capa core ships the Protocol, the visible :class:`~capa.devices.camera.webcam.WebcamAdapter`,
and an in-process IR sim fixture under ``capa.devices.sim.flir_ir_sim``. The
real FLIR IR adapter ships in the separate ``capa-flir`` package, registered
through the ``capa.cameras`` entry-point group — capa core remains FLIR-free
by design.
"""

from __future__ import annotations

from capa.devices.camera.base import (
    Camera,
    CameraCapability,
    CameraEvent,
    CameraHealth,
    CameraInfo,
    CameraSpec,
    FrameReceipt,
)

__all__ = [
    "Camera",
    "CameraCapability",
    "CameraEvent",
    "CameraHealth",
    "CameraInfo",
    "CameraSpec",
    "FrameReceipt",
]
