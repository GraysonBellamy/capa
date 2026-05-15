"""Per-resource worker runtime.

This package implements the per-resource-worker concurrency model. Each
hardware resource (one serial port, one DAQmx chassis, one camera handle)
gets its own thread hosting its own ``asyncio`` event loop; cross-thread
emission and command traffic flows over :class:`ThreadBridge` instances;
a per-run :class:`Conductor` coordinates workers via a config-lifetime
:class:`WorkerPool`.

Foundation types:

* :class:`~capa.runtime.bridge.ThreadBridge` — thread-safe bounded channel
  between two asyncio loops.
* :func:`~capa.runtime.heartbeat.heartbeat_task` — per-loop lag observer.
* :class:`WorkerState` / :class:`PoolState` and their legal-edge tables.
* :class:`RunContext` — per-run state installed into workers at arm.
* :class:`WorkerMetrics` / :class:`DisarmResult` — per-worker telemetry.
* :class:`ThreadedRunner` / :class:`InlineRunner` — the thread + loop
  pluggable host that lets the same :class:`Worker` code be exercised
  inline in unit tests and in a real thread in production.
"""

from __future__ import annotations

from typing import Final

from capa.runtime.bridge import (
    BridgePolicy,
    ThreadBridge,
    ThreadBridgeClosedError,
    ThreadBridgeMetrics,
)
from capa.runtime.build import build_workers
from capa.runtime.bundle_ref import BundleWriterRef
from capa.runtime.conductor import (
    Conductor,
    ConductorConfig,
    ConductorRunner,
    ConductorStateError,
    NoOpRunner,
    RunHandle,
    RunOutcome,
    RunResult,
    RunSession,
)
from capa.runtime.dispatch import (
    AdapterDispatcher,
    CommandDispatcher,
    ConductorDispatcher,
    DispatchError,
    PoolDispatcher,
)
from capa.runtime.dispatch import UnknownDeviceError as DispatchUnknownDeviceError
from capa.runtime.errors import (
    PoolStateError,
    ResourceConflict,
    RunnerStateError,
    UnknownDeviceError,
    WorkerStateError,
)
from capa.runtime.heartbeat import LoopLagMetric, heartbeat_task
from capa.runtime.lifecycle import (
    LEGAL_POOL_EDGES,
    LEGAL_WORKER_EDGES,
    PoolState,
    WorkerState,
    pool_edge_legal,
    worker_edge_legal,
)
from capa.runtime.metrics import DisarmResult, WorkerMetrics
from capa.runtime.pool import WorkerPool
from capa.runtime.procedure import ProcedureRunner
from capa.runtime.runcontext import BundleRef, RunContext, WriterRef
from capa.runtime.runner import InlineRunner, ThreadedRunner, WorkerRunner
from capa.runtime.saturation import (
    DEFAULT_POLL_PERIOD_S,
    DEFAULT_SATURATION_DEADLINE_S,
    SaturationEvent,
    SaturationMonitor,
    WriterSaturationSource,
)
from capa.runtime.session import RealRunSession, make_run_id
from capa.runtime.signals import install_sigint_handler
from capa.runtime.state import LEGAL_CONDUCTOR_EDGES, ConductorState, conductor_edge_legal
from capa.runtime.worker import Worker
from capa.runtime.writer_ref import DEFAULT_EVENT_SOURCE, WriterThreadRef

RUNTIME_VERSION: Final[str] = "0.2.0-p4a"
"""Runtime code revision marker.

Bumped when conductor / worker / pool semantics change in a way that
affects bundle interpretation. Newly-sealed bundles carry this in
``capa_block.engine_version`` for cross-version diagnostics."""

__all__ = [
    "DEFAULT_EVENT_SOURCE",
    "DEFAULT_POLL_PERIOD_S",
    "DEFAULT_SATURATION_DEADLINE_S",
    "LEGAL_CONDUCTOR_EDGES",
    "LEGAL_POOL_EDGES",
    "LEGAL_WORKER_EDGES",
    "RUNTIME_VERSION",
    "AdapterDispatcher",
    "BridgePolicy",
    "BundleRef",
    "BundleWriterRef",
    "CommandDispatcher",
    "Conductor",
    "ConductorConfig",
    "ConductorDispatcher",
    "ConductorRunner",
    "ConductorState",
    "ConductorStateError",
    "DisarmResult",
    "DispatchError",
    "DispatchUnknownDeviceError",
    "InlineRunner",
    "LoopLagMetric",
    "NoOpRunner",
    "PoolDispatcher",
    "PoolState",
    "PoolStateError",
    "ProcedureRunner",
    "RealRunSession",
    "ResourceConflict",
    "RunContext",
    "RunHandle",
    "RunOutcome",
    "RunResult",
    "RunSession",
    "RunnerStateError",
    "SaturationEvent",
    "SaturationMonitor",
    "ThreadBridge",
    "ThreadBridgeClosedError",
    "ThreadBridgeMetrics",
    "ThreadedRunner",
    "UnknownDeviceError",
    "Worker",
    "WorkerMetrics",
    "WorkerPool",
    "WorkerRunner",
    "WorkerState",
    "WorkerStateError",
    "WriterRef",
    "WriterSaturationSource",
    "WriterThreadRef",
    "build_workers",
    "conductor_edge_legal",
    "heartbeat_task",
    "install_sigint_handler",
    "make_run_id",
    "pool_edge_legal",
    "worker_edge_legal",
]
