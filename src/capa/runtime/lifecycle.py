"""State enums and legal-edge tables for :class:`Worker` and :class:`WorkerPool`.

The per-resource-worker migration (``docs/per-resource-worker-migration.md``)
specifies three nested lifetimes (§3.2) and exact state machines for each
component. This module centralizes the enums and the *only* legal-edge tables
so every state-mutating call site checks against the same source of truth.

Why a single edge table instead of pairwise ``if state == X`` branches in
:class:`Worker`: the migration doc explicitly lists "topology invariant #9 —
worker state transitions are atomic per worker and verified" (§3.11 line 524).
A single table makes the test that enumerates every legal/illegal edge a
one-liner; without it, the same correctness has to be reasserted at every
caller of :meth:`Worker._transition`.

Phase 1 scope: enums + edge tables only. :class:`Worker` (this same phase)
performs the transitions; :class:`Conductor` (Phase 2) walks the pool through
its own enum.
"""

from __future__ import annotations

from enum import Enum
from typing import Final


class WorkerState(Enum):
    """State of one :class:`~capa.runtime.worker.Worker`.

    Migration doc §3.3 lines 218-258. Edges live in
    :data:`LEGAL_WORKER_EDGES`.
    """

    CLOSED = "closed"
    """Pre-start or post-close. The worker's thread is not running. Only
    :meth:`Worker.start` is permitted; every other call raises."""

    IDLE = "idle"
    """Adapters open, no streams running, no per-run state installed.
    :meth:`Worker.dispatch` is permitted (manual commands between runs).
    :meth:`Worker.arm` and :meth:`Worker.close` are the only transitions."""

    ARMED = "armed"
    """Per-run :class:`~capa.runtime.runcontext.RunContext` installed (clock,
    writer ref, run_id). Streams are NOT yet running. :meth:`Worker.dispatch`
    is permitted; commands record into the bundle via the run context's writer.
    Next transition: :meth:`Worker.begin_sampling` → SAMPLING or
    :meth:`Worker.disarm` → DRAINING (no streams to stop)."""

    SAMPLING = "sampling"
    """``adapter.stream()`` running; emissions flowing to the outbound bridge.
    :meth:`Worker.dispatch` is permitted. Next transition is
    :meth:`Worker.disarm` → DRAINING."""

    DRAINING = "draining"
    """``adapter.stop()`` in flight; outbound bridge drains and closes.
    :meth:`Worker.dispatch` is REFUSED. Drain is bounded by the disarm
    ``grace_s``; on grace expiry the worker is hard-stopped and the run is
    marked degraded (migration doc §3.8 Phase B)."""


class PoolState(Enum):
    """State of one :class:`~capa.runtime.pool.WorkerPool`.

    Migration doc §4.3 line 821. The pool's state is independent of any
    individual worker's state — pool transitions cover only construction and
    teardown of the worker set as a whole.
    """

    CLOSED = "closed"
    """Pre-open or post-close. No workers exist."""

    OPENING = "opening"
    """:meth:`WorkerPool.open` is in flight: workers are starting in parallel
    and adapters are opening. The pool is not usable for dispatch or run-arm
    until OPEN is reached. On any single-worker start failure, the pool
    transitions through CLOSING back to CLOSED."""

    OPEN = "open"
    """Every worker is in :attr:`WorkerState.IDLE` (or has been temporarily
    moved through ARMED/SAMPLING/DRAINING by an active :class:`Conductor` run
    — pool state does not change during runs). The pool accepts dispatch
    calls and is eligible for :meth:`WorkerPool.arm_all`."""

    CLOSING = "closing"
    """:meth:`WorkerPool.close` is in flight: workers are being stopped in
    parallel."""


LEGAL_WORKER_EDGES: Final[frozenset[tuple[WorkerState, WorkerState]]] = frozenset(
    {
        (WorkerState.CLOSED, WorkerState.IDLE),
        (WorkerState.IDLE, WorkerState.ARMED),
        (WorkerState.ARMED, WorkerState.SAMPLING),
        (WorkerState.SAMPLING, WorkerState.DRAINING),
        (WorkerState.ARMED, WorkerState.DRAINING),
        (WorkerState.DRAINING, WorkerState.IDLE),
        (WorkerState.IDLE, WorkerState.CLOSED),
    }
)
"""The seven edges the worker state machine permits.

Edge intent (one per row, in migration doc §3.3 order):

* ``CLOSED → IDLE`` — :meth:`Worker.start`. Thread spawned, adapters opened.
* ``IDLE → ARMED`` — :meth:`Worker.arm`. Per-run context installed.
* ``ARMED → SAMPLING`` — :meth:`Worker.begin_sampling`. ``adapter.start()``.
* ``SAMPLING → DRAINING`` — :meth:`Worker.disarm`. ``adapter.stop()`` issued.
* ``ARMED → DRAINING`` — :meth:`Worker.disarm` before sampling began (no
  streams to drain, but the bridge close still flows through DRAINING for
  a uniform shutdown path).
* ``DRAINING → IDLE`` — drain complete; per-run context cleared.
* ``IDLE → CLOSED`` — :meth:`Worker.close`. Adapters closed; thread joined.

Any other ``(from, to)`` pair is illegal and raises
:class:`~capa.runtime.errors.WorkerStateError` at the transition site.
"""


LEGAL_POOL_EDGES: Final[frozenset[tuple[PoolState, PoolState]]] = frozenset(
    {
        (PoolState.CLOSED, PoolState.OPENING),
        (PoolState.OPENING, PoolState.OPEN),
        (PoolState.OPENING, PoolState.CLOSING),
        (PoolState.OPEN, PoolState.CLOSING),
        (PoolState.CLOSING, PoolState.CLOSED),
    }
)
"""The five edges the pool state machine permits.

* ``CLOSED → OPENING`` — :meth:`WorkerPool.open` entry.
* ``OPENING → OPEN`` — all workers reached IDLE.
* ``OPENING → CLOSING`` — partial open failure; rollback in progress.
* ``OPEN → CLOSING`` — :meth:`WorkerPool.close` entry.
* ``CLOSING → CLOSED`` — all workers stopped; pool reusable for nothing.
"""


def worker_edge_legal(from_state: WorkerState, to_state: WorkerState) -> bool:
    """Return ``True`` if ``from_state → to_state`` is a permitted worker edge.

    The :class:`Worker` calls this exactly once per transition, inside its
    ``_transition`` mutator. External callers use this only for property-test
    state-machine strategies — production code should rely on the transition
    method instead.
    """
    return (from_state, to_state) in LEGAL_WORKER_EDGES


def pool_edge_legal(from_state: PoolState, to_state: PoolState) -> bool:
    """Return ``True`` if ``from_state → to_state`` is a permitted pool edge."""
    return (from_state, to_state) in LEGAL_POOL_EDGES


__all__ = [
    "LEGAL_POOL_EDGES",
    "LEGAL_WORKER_EDGES",
    "PoolState",
    "WorkerState",
    "pool_edge_legal",
    "worker_edge_legal",
]
