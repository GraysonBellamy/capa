""":data:`WorkerEmission` — the wire type for the per-worker outbound bridge.

Migration doc §6 (camera unification). Device adapters yield
:data:`~capa.devices.records.DeviceEmission`; the new
:class:`~capa.runtime.camera_adapter.CameraDeviceAdapter` wrapper yields
:class:`~capa.devices.camera.base.FrameReceipt` and
:class:`~capa.devices.camera.base.CameraEvent` in addition. Both flow through
the same per-worker :class:`~capa.runtime.bridge.ThreadBridge`, so the bridge
type parameter is the union of both.

The union lives here rather than in :mod:`capa.devices.records` to avoid a
``records → camera.base → adapter → records`` import cycle (``adapter`` already
imports ``records.DeviceEmission``, and ``camera.base`` already imports
``adapter`` for :class:`~capa.devices.adapter.DeviceCommand`). Keeping the
camera-aware union in the runtime layer also reflects the architecture: a
"worker emission" is a runtime concept, not a device-layer concept.

Dispatch is by runtime type in :meth:`~capa.runtime.conductor.Conductor._drain_worker`:

* :class:`FrameReceipt` → :meth:`WriterThread.record_frame`
* :class:`CameraEvent` → :meth:`WriterThread.write_event` (kind prefixed
  ``camera.``, source ``camera:<name>``)
* anything else → :meth:`WriterThread.submit` + :meth:`DataBus.publish`

Cameras do not publish to the procedure-side :class:`~capa.core.databus.DataBus` —
matching today's engine behavior, where frame receipts never landed on the bus.
"""

from __future__ import annotations

from capa.devices.camera.base import CameraEvent, FrameReceipt
from capa.devices.records import DeviceEmission

WorkerEmission = DeviceEmission | FrameReceipt | CameraEvent
"""Anything a :class:`~capa.runtime.worker.Worker`'s outbound bridge carries.

Wider than :data:`~capa.devices.records.DeviceEmission` by exactly two types:
:class:`FrameReceipt` and :class:`CameraEvent`. The
:class:`~capa.runtime.camera_adapter.CameraDeviceAdapter` wrapper produces
these from a :class:`~capa.devices.camera.base.Camera`'s native streams;
device adapters never produce them.
"""

__all__ = ["WorkerEmission"]
