"""Shutdown result types and per-stage grace configuration.

These dataclasses give the in-process runtime a *truthful* shutdown
surface: every close path returns a structured description of what
actually happened instead of raising-and-suppressing or
logging-and-succeeding. The :class:`~capa.ui.shutdown.ShutdownCoordinator`
consumes these results to make real escalation decisions.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RunnerStopResult:
    """Outcome of one :meth:`WorkerRunner.stop` call.

    ``joined`` is the load-bearing field: it tells the caller whether the
    runner's thread actually exited within ``grace_s``. A non-joined
    ThreadedRunner is a degraded shutdown — the loop refused to stop or a
    submitted coroutine is wedged in a non-cancellable native call. The
    :class:`~capa.ui.shutdown.ShutdownCoordinator` escalates a non-joined
    runner to its hard wall-clock fuse; the runner itself does not
    pretend to kill the thread.

    ``thread_ident`` is non-``None`` for :class:`ThreadedRunner` and
    ``None`` for :class:`InlineRunner` (no thread to identify).
    """

    name: str
    joined: bool
    grace_s: float
    thread_ident: int | None


@dataclass(frozen=True, slots=True)
class WorkerCloseResult:
    """Outcome of one :meth:`Worker.close` call.

    Adapter errors are captured as strings rather than re-raised so the
    pool can aggregate every worker's outcome without losing visibility
    on the first failure. ``adapter_stop_errors`` is populated when the
    caller chose a disarm-then-close sequence (or when close itself
    drives an implicit stop); ``adapter_close_errors`` is per-adapter
    ``close()`` failures.

    ``disarm_result`` is the :class:`~capa.runtime.metrics.DisarmResult`
    value (``"ok"`` / ``"forced"`` / ``"leaked"``) when a disarm ran as
    part of the close, or ``None`` when close was called on an already-
    IDLE worker without a preceding disarm.
    """

    resource_id: str
    state_before: str
    adapter_stop_errors: tuple[str, ...]
    adapter_close_errors: tuple[str, ...]
    disarm_result: str | None
    runner_stop: RunnerStopResult


@dataclass(frozen=True, slots=True)
class PoolCloseResult:
    """Aggregate of :meth:`WorkerPool.close` over every worker.

    ``clean`` is ``True`` iff every per-worker result reports no adapter
    errors and ``runner_stop.joined`` is ``True``, *and* the pool itself
    encountered no aggregation-level errors (recorded in ``errors``).

    ``errors`` captures pool-layer problems — e.g. a worker that raised
    out of its close pipeline rather than returning a result.
    """

    clean: bool
    worker_results: tuple[WorkerCloseResult, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkerShutdownConfig:
    """Per-stage wall-clock deadlines for worker shutdown.

    Tests inject shorter values to keep the suite fast. The
    :class:`~capa.ui.shutdown.ShutdownCoordinator` budgets its own
    stage deadlines on top of these; the worker's deadlines bound the
    per-adapter work while the coordinator bounds the overall pool
    close.

    Two distinct stream-cancel deadlines:

    * ``stream_stop_grace_s`` — cooperative stop. How long to wait for
      stream tasks to exit cooperatively after ``adapter.stop()`` flipped
      their lifecycle.
    * ``stream_cancel_grace_s`` — forced cancel. After cancelling the
      stragglers, how long we'll wait for the cancellation to actually
      land. Anything past this is a stream task ignoring cancellation
      (vendor code wedged in a native blocking call); the worker can't
      help further and the application fuse takes over.

    **Budget Composition:** These inner timeouts compose into the pool-level
    deadline defined in :class:`~capa.ui.shutdown.ShutdownDeadlines`. The sum
    of (adapter_stop_grace_s × num_adapters + stream_stop_grace_s +
    stream_cancel_grace_s + adapter_close_grace_s × num_adapters +
    runner_stop_grace_s) MUST be less than ``ShutdownDeadlines.pool_close_s``
    to ensure worker close completes within the pool budget.
    """

    adapter_stop_grace_s: float = 2.0
    stream_stop_grace_s: float = 5.0
    stream_cancel_grace_s: float = 1.0
    adapter_close_grace_s: float = 3.0
    runner_stop_grace_s: float = 2.0


__all__ = [
    "PoolCloseResult",
    "RunnerStopResult",
    "WorkerCloseResult",
    "WorkerShutdownConfig",
]
