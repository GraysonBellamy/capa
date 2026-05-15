"""Per-resource worker runtime — small production facade.

This package implements the per-resource-worker concurrency model. Each
hardware resource (one serial port, one DAQmx chassis, one camera handle)
gets its own thread hosting its own ``asyncio`` event loop; cross-thread
emission and command traffic flows over :class:`ThreadBridge` instances;
a per-run :class:`Conductor` coordinates workers via a config-lifetime
:class:`WorkerPool`.

The names re-exported here are the intentional public surface. Test
seams, state-machine internals (edge tables, lifecycle enums), bridge
plumbing, heartbeat/saturation helpers, and dispatcher impls live in
their concrete submodules (``capa.runtime.runner``, ``capa.runtime.bridge``,
``capa.runtime.lifecycle``, ``capa.runtime.dispatch`` etc.) and should be
imported from there. Importing from those modules directly is fine —
this package facade is deliberately small.
"""

from __future__ import annotations

from typing import Final

from capa.runtime.conductor import (
    Conductor,
    ConductorConfig,
    RunOutcome,
    RunResult,
    RunSession,
)
from capa.runtime.dispatch import ManualClient
from capa.runtime.errors import (
    ConductorStateError,
    PoolStateError,
    ResourceConflict,
    RunnerStateError,
    UnknownDeviceError,
    WorkerStateError,
)
from capa.runtime.headless import HeadlessResult, run_headless
from capa.runtime.pool import WorkerPool
from capa.runtime.session import RealRunSession
from capa.runtime.signals import install_sigint_handler

RUNTIME_VERSION: Final[str] = "0.2.0-p4a"
"""Runtime code revision marker.

Bumped when conductor / worker / pool semantics change in a way that
affects bundle interpretation. Newly-sealed bundles carry this in
``capa_block.engine_version`` for cross-version diagnostics."""

__all__ = [
    "RUNTIME_VERSION",
    "Conductor",
    "ConductorConfig",
    "ConductorStateError",
    "HeadlessResult",
    "ManualClient",
    "PoolStateError",
    "RealRunSession",
    "ResourceConflict",
    "RunOutcome",
    "RunResult",
    "RunSession",
    "RunnerStateError",
    "UnknownDeviceError",
    "WorkerPool",
    "WorkerStateError",
    "install_sigint_handler",
    "run_headless",
]
