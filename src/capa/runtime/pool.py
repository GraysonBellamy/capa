""":class:`WorkerPool` — config-lifetime container for :class:`Worker`\\ s.

Migration doc §4.3 lines 721-820 and the topology diagram in §3.1. The pool
owns every worker the loaded config produces, opens hardware once per
config, lives across runs, and exposes manual dispatch when no run is
armed (subsuming today's :class:`~capa.devices.registry.DeviceRegistry`).

State machine: ``CLOSED → OPENING → OPEN → CLOSING → CLOSED``
(edges enumerated in :data:`~capa.runtime.lifecycle.LEGAL_POOL_EDGES`).
Runs may arm/sample/disarm any number of times while the pool stays in
:attr:`PoolState.OPEN`; the pool's state itself does not change during a
run.

Phase 1 scope:

* ``open`` / ``close`` lifecycle with reverse-order rollback on partial
  open failure.
* ``arm_all`` / ``begin_sampling_all`` / ``disarm_all`` — parallel
  per-worker driving; the conductor (Phase 2) calls these.
* ``dispatch`` / ``snapshot`` — synchronous facade for the manual-control
  surface (PoolClient lands in Phase 2 as the async wrapper).
* ``worker_for`` — device-name → worker lookup.

What's deliberately deferred:

* :class:`~capa.runtime.dispatch.PoolClient` (Phase 2). The pool exposes
  sync ``dispatch`` for the conductor; the PoolClient is the same call
  wrapped for the UI/CLI async surface.
* Saturation monitor wiring (Phase 2). The pool reports per-worker
  metrics; the conductor reads them.
* Per-worker watchdog (Phase 2). The pool surfaces ``fatal_error``
  per worker; the conductor decides what to do per ``on_failure``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from types import MappingProxyType

import structlog

from capa.devices.adapter import CommandResult, DeviceCommand
from capa.devices.camera.metadata import WebcamMetadata
from capa.devices.records import DeviceEmission
from capa.experiment.config import ExperimentConfig
from capa.runtime.bridge import BridgePolicy, ThreadBridge
from capa.runtime.build import build_workers
from capa.runtime.emissions import WorkerEmission
from capa.runtime.errors import PoolStateError, UnknownDeviceError
from capa.runtime.lifecycle import (
    LEGAL_POOL_EDGES,
    PoolState,
    WorkerState,
)
from capa.runtime.metrics import DisarmResult
from capa.runtime.preview import PreviewFrame
from capa.runtime.runcontext import RunContext
from capa.runtime.runner import WorkerRunner
from capa.runtime.worker import Worker

_logger = structlog.get_logger("capa.runtime.pool")


class WorkerPool:
    """All :class:`Worker`\\ s for one loaded config.

    The pool is constructed against a prebuilt ``workers`` map (so tests
    can inject fakes without going through TOML); the
    :classmethod:`from_config` factory builds the workers via
    :func:`build_workers`.

    Lifetime: pool lives as long as the loaded config. Reloading the
    config tears down the old pool (every worker closes) and a new pool
    is constructed for the new config. Manual-control-between-runs is
    the load-bearing property (migration doc §2.1 goal #3): one
    :meth:`open` pays the adapter cold-open cost once; every subsequent
    arm/disarm reuses the same opened hardware.
    """

    def __init__(
        self,
        *,
        workers: Mapping[str, Worker],
        device_to_resource: Mapping[str, str],
        preview_bridges: Mapping[str, ThreadBridge[PreviewFrame]] | None = None,
    ) -> None:
        if not workers:
            raise ValueError("WorkerPool: workers map must not be empty")
        self._workers: dict[str, Worker] = dict(workers)
        self._device_to_resource: dict[str, str] = dict(device_to_resource)
        self._state: PoolState = PoolState.CLOSED
        # The state mutex serializes open() / close() against each other.
        # Run-lifetime methods (arm_all, etc.) don't touch the state — the
        # pool's state only changes at config boundaries.
        self._state_lock = asyncio.Lock()
        # Per-camera preview bridges. Constructed by :meth:`from_config`
        # when a consumer loop is provided; empty for headless runs.
        # Workers receive a partitioned view of this map at construction
        # via :func:`build_workers`.
        self._preview_bridges: dict[str, ThreadBridge[PreviewFrame]] = (
            dict(preview_bridges) if preview_bridges else {}
        )
        # Latches so attach_preview_consumers / close_preview_bridges
        # stay idempotent under reentrant teardown paths.
        self._preview_consumers_attached = False

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        config: ExperimentConfig,
        *,
        runner_factory: Callable[..., WorkerRunner] | None = None,
        outbound_capacity: int = 64,
        preview_consumer_loop: asyncio.AbstractEventLoop | None = None,
        preview_capacity: int = 4,
    ) -> WorkerPool:
        """Build a pool from a real :class:`ExperimentConfig`.

        Validation (resource conflicts) runs synchronously here; if it
        fails, the resulting :class:`ResourceConflict` is raised before
        any worker is constructed.

        ``preview_consumer_loop``: the loop that will drain preview
        bridges (the qasync UI loop in production; the test loop in
        integration tests). When ``None`` (headless), no preview bridges
        are constructed and the camera adapter's preview lifecycle
        no-ops — zero overhead for headless runs.
        """
        preview_bridges: dict[str, ThreadBridge[PreviewFrame]] = {}
        if preview_consumer_loop is not None:
            for cam in config.hardware.cameras:
                preview_bridges[cam.name] = ThreadBridge(
                    name=f"preview-{cam.name}",
                    capacity=preview_capacity,
                    consumer_loop=preview_consumer_loop,
                    policy=BridgePolicy.DROP_OLDEST,
                )
        workers, device_to_resource = build_workers(
            config,
            runner_factory=runner_factory,
            outbound_capacity=outbound_capacity,
            preview_bridges=preview_bridges,
        )
        return cls(
            workers=workers,
            device_to_resource=device_to_resource,
            preview_bridges=preview_bridges,
        )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def state(self) -> PoolState:
        return self._state

    @property
    def workers(self) -> Mapping[str, Worker]:
        """Immutable view of the worker map keyed by ``resource_id``."""
        return self._workers

    @property
    def device_names(self) -> tuple[str, ...]:
        return tuple(self._device_to_resource)

    def preview_bridges(self) -> Mapping[str, ThreadBridge[PreviewFrame]]:
        """Per-camera preview bridges, keyed by camera spec name.

        Empty when the pool was constructed without a UI consumer loop
        (headless runs). The mapping itself is read-only — callers that
        need to spawn drainers should iterate the entries.
        """
        return MappingProxyType(self._preview_bridges)

    def attach_preview_consumers(self) -> None:
        """Bind every preview bridge's consumer side to the running loop.

        Must be called from the consumer loop (the qasync UI loop) before
        :meth:`open` is awaited — workers attach their producers inside
        :meth:`Worker._open_all_impl` and a producer attach can race a
        consumer-side enqueue. Calling this first builds the
        :class:`asyncio.Queue` so the eventual producer always has a
        target.

        Idempotent: a second call is a no-op.
        """
        if self._preview_consumers_attached:
            return
        for bridge in self._preview_bridges.values():
            bridge.attach_consumer()
        self._preview_consumers_attached = True

    def close_preview_bridges(self) -> None:
        """Close every preview bridge. Called from :meth:`close` after
        all workers have closed, so no producer can write afterwards.

        Idempotent: closing an already-closed bridge is a no-op (see
        :meth:`ThreadBridge.close`).
        """
        for bridge in self._preview_bridges.values():
            bridge.close()

    def worker_for(self, device_name: str) -> Worker:
        """Return the worker hosting the named adapter.

        Raises :class:`UnknownDeviceError` if the name is not configured.
        """
        try:
            rid = self._device_to_resource[device_name]
        except KeyError as exc:
            raise UnknownDeviceError(device_name, configured_names=self.device_names) from exc
        return self._workers[rid]

    # ------------------------------------------------------------------
    # Pool state machine
    # ------------------------------------------------------------------

    def _transition(self, target: PoolState) -> None:
        edge = (self._state, target)
        if edge not in LEGAL_POOL_EDGES:
            raise PoolStateError(
                f"WorkerPool: illegal transition {self._state} → {target}",
                from_state=self._state,
                to_state=target,
            )
        _logger.debug(
            "pool.transition",
            from_state=self._state.value,
            to_state=target.value,
        )
        self._state = target

    # ------------------------------------------------------------------
    # Config-lifetime methods
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Start every worker; on first failure, roll back in reverse order.

        Workers start in parallel — bus-collision avoidance is the
        ``resource_id`` grouping job, not a sequential warm-up
        responsibility (migration doc §3.7 line 412). At 6 workers this
        cuts pool-open from a ~2 s sequential warm-up to ~500 ms.

        On any worker's start failure: every already-opened worker is
        closed in reverse order (LIFO of completion), then the original
        exception propagates. The pool returns to :attr:`PoolState.CLOSED`.
        """
        async with self._state_lock:
            self._transition(PoolState.OPENING)

            # Kick off every worker's start in parallel. We can't use
            # asyncio.gather directly because the futures are
            # concurrent.futures.Futures from the runner — wrap each.
            start_tasks: list[tuple[Worker, asyncio.Future[None]]] = []
            for worker in self._workers.values():
                task = asyncio.ensure_future(asyncio.wrap_future(worker.start()))
                start_tasks.append((worker, task))

            # Wait for ALL to complete (success or failure). We don't want
            # to abandon in-progress starts on first failure — they may
            # leave half-opened threads behind. Collect outcomes, then
            # decide rollback policy.
            await asyncio.gather(*(t for _, t in start_tasks), return_exceptions=True)

            # Categorize.
            opened: list[Worker] = []
            failed: list[tuple[Worker, BaseException]] = []
            for worker, task in start_tasks:
                exc = task.exception()
                if exc is None:
                    opened.append(worker)
                else:
                    failed.append((worker, exc))

            if not failed:
                self._transition(PoolState.OPEN)
                _logger.info(
                    "pool.open",
                    worker_count=len(self._workers),
                    resources=tuple(self._workers),
                )
                return

            # Rollback path. Close every successfully-opened worker in
            # REVERSE order so paired resources (think watlowlib + serial)
            # tear down LIFO. We swallow rollback errors — they cannot
            # mask the original cause, which is the first failure.
            _logger.warning(
                "pool.open_partial_failure",
                failed_count=len(failed),
                opened_count=len(opened),
                first_error=str(failed[0][1]),
            )
            self._transition(PoolState.CLOSING)
            for worker in reversed(opened):
                with contextlib.suppress(Exception):
                    await asyncio.wrap_future(worker.close(grace_s=5.0))
            # Close preview bridges too — producers are gone now, so any
            # pending UI-side drainer wakes with ThreadBridgeClosedError
            # and exits cleanly.
            self.close_preview_bridges()
            self._transition(PoolState.CLOSED)
            # Propagate the first failure — the caller wants the original
            # exception type and message, not a synthetic aggregate.
            raise failed[0][1]

    async def close(self) -> None:
        """Close every worker. Refuses if any worker is not IDLE.

        Migration doc §4.3 lines 759-775: pool close is the
        config-boundary teardown. Any active run must be disarmed first
        (the conductor's :meth:`Conductor.stop` is what does this in
        Phase 2; in Phase 1 the test driver does it).

        Close is idempotent — calling on an already-CLOSED pool is a
        no-op rather than an error.
        """
        async with self._state_lock:
            if self._state is PoolState.CLOSED:
                return

            non_idle = [
                (w.resource_id, w.state)
                for w in self._workers.values()
                if w.state is not WorkerState.IDLE
            ]
            if non_idle:
                raise PoolStateError(
                    "WorkerPool.close() while one or more workers are "
                    f"not IDLE: {non_idle}; disarm the active run first",
                    from_state=self._state,
                )

            self._transition(PoolState.CLOSING)
            # Close in parallel; each worker is independent. Failures are
            # logged but do not stop the sequence — every worker must get
            # the chance to release its hardware handle.
            close_tasks = [
                asyncio.ensure_future(asyncio.wrap_future(worker.close(grace_s=5.0)))
                for worker in self._workers.values()
            ]
            results = await asyncio.gather(*close_tasks, return_exceptions=True)
            for worker, result in zip(self._workers.values(), results, strict=True):
                if isinstance(result, BaseException):
                    _logger.warning(
                        "pool.close_worker_failed",
                        resource_id=worker.resource_id,
                        error=str(result),
                    )
            # Close preview bridges after every worker has closed so no
            # producer attempts to write to a closed bridge. The
            # UI-side drainer wakes with ThreadBridgeClosedError and
            # exits cleanly on its own loop.
            self.close_preview_bridges()
            self._transition(PoolState.CLOSED)
            _logger.info("pool.close", worker_count=len(self._workers))

    # ------------------------------------------------------------------
    # Run-lifecycle methods (called by the Conductor in Phase 2)
    # ------------------------------------------------------------------

    async def arm_all(self, run_context: RunContext) -> None:
        """Transition every worker IDLE → ARMED with the same run context.

        Parallel. On any failure, in-flight arms are awaited but the
        pool does not undo successful arms — that's the conductor's job
        via :meth:`disarm_all`. Migration doc §3.7 lines 393-407.
        """
        self._require_open()
        tasks = [
            asyncio.ensure_future(asyncio.wrap_future(worker.arm(run_context)))
            for worker in self._workers.values()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        first_exc: BaseException | None = None
        for worker, result in zip(self._workers.values(), results, strict=True):
            if isinstance(result, BaseException) and first_exc is None:
                first_exc = result
                _logger.warning(
                    "pool.arm_failed",
                    resource_id=worker.resource_id,
                    error=str(result),
                )
        if first_exc is not None:
            raise first_exc

    async def begin_sampling_all(
        self, *, consumer_loop: asyncio.AbstractEventLoop
    ) -> dict[str, ThreadBridge[WorkerEmission]]:
        """Transition every worker ARMED → SAMPLING. Return outbound bridges.

        Returns a dict keyed by ``resource_id``; the conductor's drain
        tasks iterate ``bridges.items()`` and spawn one drain coroutine
        per bridge (migration doc §4.5 line 1054).

        ``consumer_loop`` is the loop that will drain the bridges — the
        conductor's loop in Phase 2 production wiring; the test's loop
        in Phase 1 integration tests.
        """
        self._require_open()
        tasks = [
            (
                rid,
                asyncio.ensure_future(
                    asyncio.wrap_future(worker.begin_sampling(consumer_loop=consumer_loop))
                ),
            )
            for rid, worker in self._workers.items()
        ]
        bridges: dict[str, ThreadBridge[WorkerEmission]] = {}
        first_exc: BaseException | None = None
        for rid, task in tasks:
            try:
                bridges[rid] = await task
            except BaseException as exc:
                if first_exc is None:
                    first_exc = exc
                _logger.warning(
                    "pool.begin_sampling_failed",
                    resource_id=rid,
                    error=str(exc),
                )
        if first_exc is not None:
            # Some workers entered SAMPLING; the caller (conductor) will
            # call disarm_all to roll back. We don't unilaterally disarm
            # here because the conductor owns shutdown ordering.
            raise first_exc
        return bridges

    async def disarm_all(self, *, grace_s: float = 5.0) -> dict[str, DisarmResult]:
        """Transition every worker SAMPLING/ARMED → DRAINING → IDLE.

        Per-worker grace; the slowest worker bounds the overall time
        but every worker's disarm runs in parallel. The result maps
        resource_id → DisarmResult so the conductor can identify which
        workers force-cancelled.

        Workers that are already in IDLE (e.g. never armed) are skipped
        rather than erroring — disarm_all is idempotent over a
        not-fully-armed pool.
        """
        self._require_open()
        tasks: dict[str, asyncio.Task[DisarmResult]] = {}
        for rid, worker in self._workers.items():
            if worker.state in (WorkerState.ARMED, WorkerState.SAMPLING):
                tasks[rid] = asyncio.ensure_future(
                    asyncio.wrap_future(worker.disarm(grace_s=grace_s))
                )
        if not tasks:
            return {}
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        results: dict[str, DisarmResult] = {}
        for rid, task in tasks.items():
            exc = task.exception()
            if exc is not None:
                # A disarm exception is unusual — the worker.disarm body is
                # bounded and catches adapter.stop failures internally.
                # Record as FORCED for conservatism and surface in logs.
                _logger.warning(
                    "pool.disarm_failed",
                    resource_id=rid,
                    error=str(exc),
                )
                results[rid] = DisarmResult.FORCED
            else:
                results[rid] = task.result()
        return results

    # ------------------------------------------------------------------
    # Command-lifetime methods (PoolClient routes here in Phase 2)
    # ------------------------------------------------------------------

    def dispatch(self, device: str, cmd: DeviceCommand) -> Future[CommandResult]:
        """Route a command to the worker hosting ``device``.

        Synchronous facade (returns a :class:`concurrent.futures.Future`)
        — the PoolClient (Phase 2) async-wraps this. Used directly by
        tests in Phase 1.

        State-gating happens inside the worker (per worker's own state).
        Pool-level state is not checked here on purpose: a CLOSED pool
        has empty ``_device_to_resource`` so :meth:`worker_for` would
        raise :class:`UnknownDeviceError` first.
        """
        worker = self.worker_for(device)
        return worker.dispatch(device, cmd)

    def snapshot(self, device: str) -> Future[DeviceEmission]:
        """One-shot ``adapter.snapshot()`` on the worker hosting ``device``."""
        worker = self.worker_for(device)
        return worker.snapshot(device)

    def camera_metadata(self, device: str) -> Future[WebcamMetadata | None]:
        """Probe one camera's metadata on the worker that owns it.

        Future resolves to :class:`WebcamMetadata` when ``device`` is a
        webcam, ``None`` otherwise (IR cameras, non-camera adapters).
        Pool-level state isn't checked here — :meth:`worker_for` raises
        :class:`UnknownDeviceError` on a CLOSED pool's empty routing map
        before we get this far.
        """
        worker = self.worker_for(device)
        return worker.camera_metadata(device)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_open(self) -> None:
        if self._state is not PoolState.OPEN:
            raise PoolStateError(
                f"WorkerPool: operation requires OPEN, got {self._state}",
                from_state=self._state,
            )


__all__ = ["WorkerPool"]
