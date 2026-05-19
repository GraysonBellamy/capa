""":data:`WorkerEmission` — the wire type for the per-worker outbound bridge.

Device adapters yield
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
* :class:`ProcedureTick` → :meth:`Conductor._publish_ui` only — UI-only
  mirror, never lands on disk or the data bus
* anything else → :meth:`WriterThread.submit` + :meth:`DataBus.publish`

Cameras do not publish to the procedure-side :class:`~capa.core.databus.DataBus` —
matching today's engine behavior, where frame receipts never landed on the bus.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from capa.devices.camera.base import CameraEvent, FrameReceipt
from capa.devices.records import DeviceEmission


@dataclass(frozen=True, slots=True)
class ProcedureTick:
    """One UI-only telemetry tick from a long-running procedure.

    Procedures with mid-run state worth surfacing (heat-flux tune's
    rolling windows, predicate dwell, iteration #, etc.) emit ticks
    from inside their poll loop via the
    :class:`~capa.experiment.procedures.base.ProcedureUiSink` handed
    in on :class:`~capa.experiment.procedures.base.ProcedureContext`.

    The runtime treats the payload as opaque — it crosses the worker→UI
    seam through the same :class:`~capa.runtime.bridge.ThreadBridge`
    that carries device emissions, but takes a UI-only short-circuit in
    :meth:`~capa.runtime.conductor.Conductor._dispatch_emission`:
    no writer hit, no data-bus publish. The consuming dock parses
    ``payload`` against its own schema (e.g. the heat-flux dock validates
    a small Pydantic model) — there is no runtime-level validation.

    Frequency is procedure-driven; the heat-flux tune emits at roughly
    ``1 / poll_interval_s`` (~2 Hz) which the ``DROP_OLDEST`` UI bridge
    handles fine. Backpressure is impossible by construction — the
    bridge will evict old ticks before signalling the producer.
    """

    procedure_id: str
    """Stable plugin id (``ctx.procedure.id``) so the consuming dock can
    filter — multiple long-running procedures could in principle ship
    ticks concurrently in the future."""

    t_mono_ns: int
    """Monotonic timestamp at emit time, from
    :attr:`ProcedureContext.clock`. Lets a dock detect staleness without
    a wall-clock comparison."""

    payload: Mapping[str, object] = field(default_factory=dict)
    """Procedure-defined payload. Opaque to the runtime; the dock owns
    the schema."""


@runtime_checkable
class ProcedureUiSink(Protocol):
    """Synchronous, non-blocking UI-only sink for procedure ticks.

    The conductor wires an instance into the
    :class:`~capa.experiment.procedures.base.ProcedureContext` so the
    procedure can publish without learning about the bridge or the
    conductor itself. Implementations must be safe to call from the
    conductor loop (the procedure's loop); they are explicitly **not**
    required to be thread-safe — the procedure already runs on the
    conductor loop.

    A closed bridge is treated as a silent drop (the UI may have
    disconnected mid-run); the sink must not raise on closed-bridge.
    """

    def publish(self, tick: ProcedureTick) -> None:
        """Hand ``tick`` to the UI bridge. Returns immediately."""
        ...


WorkerEmission = DeviceEmission | FrameReceipt | CameraEvent | ProcedureTick
"""Anything a :class:`~capa.runtime.worker.Worker`'s outbound bridge carries
**or** that a procedure can publish straight onto the UI bridge.

Wider than :data:`~capa.devices.records.DeviceEmission` by three types:
:class:`FrameReceipt` and :class:`CameraEvent` come from the
:class:`~capa.runtime.camera_adapter.CameraDeviceAdapter` wrapper;
:class:`ProcedureTick` originates on the conductor loop inside a procedure
and skips the worker entirely. Device adapters never produce any of the
three.
"""

__all__ = ["ProcedureTick", "ProcedureUiSink", "WorkerEmission"]
