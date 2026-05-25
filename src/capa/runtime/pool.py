""":class:`WorkerPool` — config-lifetime container for :class:`Worker`\\ s.

The pool owns every worker the loaded config produces, opens hardware
once per config, lives across runs, and exposes manual dispatch when no
run is armed.

State machine: ``CLOSED → OPENING → OPEN → CLOSING → CLOSED``
(edges enumerated in :data:`~capa.runtime.lifecycle.LEGAL_POOL_EDGES`).
Runs may arm/sample/disarm any number of times while the pool stays in
:attr:`PoolState.OPEN`; the pool's state itself does not change during a
run.

Surface:

* ``open`` / ``close`` lifecycle with reverse-order rollback on partial
  open failure.
* ``arm_all`` / ``begin_sampling_all`` / ``disarm_all`` — parallel
  per-worker driving; the conductor calls these.
* ``dispatch`` / ``snapshot`` — synchronous facade for the manual-control
  surface.
* ``worker_for`` — device-name → worker lookup.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future
from types import MappingProxyType
from typing import Any

import structlog

from capa.devices.adapter import CommandResult, DeviceAdapter, DeviceCommand
from capa.devices.camera.metadata import WebcamMetadata
from capa.devices.records import DeviceEmission
from capa.experiment.config import ExperimentConfig
from capa.runtime.bridge import BridgePolicy, ThreadBridge
from capa.runtime.emissions import WorkerEmission
from capa.runtime.errors import PoolStateError, UnknownDeviceError
from capa.runtime.lifecycle import (
    LEGAL_POOL_EDGES,
    PoolState,
    WorkerState,
)
from capa.runtime.metrics import DisarmResult
from capa.runtime.preview import PreviewFrame
from capa.runtime.progress import DeviceInitProgress, DeviceInitStatus, OpenProgressCallback
from capa.runtime.runcontext import RunContext
from capa.runtime.runner import WorkerRunner
from capa.runtime.shutdown import PoolCloseResult, WorkerCloseResult
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
    the load-bearing property: one :meth:`open` pays the adapter
    cold-open cost once; every subsequent arm/disarm reuses the same
    opened hardware.
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
        preview_consumer_loop: asyncio.AbstractEventLoop | None = None,
        preview_capacity: int = 4,
    ) -> WorkerPool:
        """Build a pool from a real :class:`ExperimentConfig`.

        Materialization and validation run synchronously here.
        :class:`~capa.devices.materialize.ConfigMaterializationError`
        surfaces adapter construction failures; :class:`ResourceConflict`
        surfaces grouping conflicts. Both fire before any worker is
        constructed, so a misconfigured config has no hardware side
        effects.

        ``preview_consumer_loop``: the loop that will drain preview
        bridges (the qasync UI loop in production; the test loop in
        integration tests). When ``None`` (headless), no preview bridges
        are constructed and the camera adapter's preview lifecycle
        no-ops — zero overhead for headless runs.

        Outbound bridge capacity is derived per worker from each
        adapter's :attr:`DeviceAdapter.expected_emission_rate_hz` inside
        :func:`build_workers`; the previous ``outbound_capacity`` kwarg
        has been retired.
        """
        from capa.devices.materialize import materialize_adapters  # noqa: PLC0415
        from capa.runtime.build import build_workers  # noqa: PLC0415

        materialized = materialize_adapters(config)
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
            materialized,
            runner_factory=runner_factory,
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
        """Current :class:`PoolState` — ``IDLE``, ``OPEN``, or ``DRAINING``."""
        return self._state

    @property
    def workers(self) -> Mapping[str, Worker]:
        """Immutable view of the worker map keyed by ``resource_id``."""
        return self._workers

    @property
    def device_names(self) -> tuple[str, ...]:
        """All device names owned by any worker in the pool, in registration order."""
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

    def _emit_open_progress(
        self,
        callback: OpenProgressCallback | None,
        *,
        worker: Worker,
        adapter: DeviceAdapter,
        status: DeviceInitStatus,
        detail: str,
    ) -> None:
        if callback is None:
            return
        try:
            callback(
                DeviceInitProgress(
                    name=adapter.name,
                    adapter=type(adapter).__name__,
                    resource_id=worker.resource_id,
                    status=status,
                    detail=detail,
                )
            )
        except Exception as exc:
            _logger.warning(
                "pool.open_progress_callback_failed",
                resource_id=worker.resource_id,
                adapter=adapter.name,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Config-lifetime methods
    # ------------------------------------------------------------------

    async def _close_workers_parallel(
        self, workers: Iterable[Worker], *, grace_s: float = 5.0
    ) -> tuple[list[WorkerCloseResult], list[str]]:
        """Close workers in parallel and collect results.

        Returns (worker_results, pool_errors). Errors are captured as
        strings rather than raised so all workers are given the chance
        to release hardware. Caller is responsible for logging details.
        """
        close_tasks: list[tuple[Worker, asyncio.Task[WorkerCloseResult]]] = [
            (worker, asyncio.create_task(worker.async_close(grace_s=grace_s))) for worker in workers
        ]
        pool_errors: list[str] = []
        if not close_tasks:
            return [], []

        await asyncio.gather(*(t for _, t in close_tasks), return_exceptions=True)

        worker_results: list[WorkerCloseResult] = []
        for worker, task in close_tasks:
            exc = task.exception()
            if exc is not None:
                pool_errors.append(f"worker {worker.resource_id!r} close crashed: {exc!r}")
                _logger.warning(
                    "pool.close_worker_failed",
                    resource_id=worker.resource_id,
                    error=str(exc),
                )
                continue
            result = task.result()
            worker_results.append(result)
            if result.adapter_close_errors or result.adapter_stop_errors:
                _logger.warning(
                    "pool.close_worker_degraded",
                    resource_id=worker.resource_id,
                    adapter_stop_errors=result.adapter_stop_errors,
                    adapter_close_errors=result.adapter_close_errors,
                )
            if not result.runner_stop.joined:
                _logger.warning(
                    "pool.close_worker_runner_not_joined",
                    resource_id=worker.resource_id,
                    thread_ident=result.runner_stop.thread_ident,
                )
        return worker_results, pool_errors

    async def open(self, progress_callback: OpenProgressCallback | None = None) -> None:
        """Start every worker; on first failure, roll back in reverse order.

        Workers start in parallel — bus-collision avoidance is the
        ``resource_id`` grouping job, not a sequential warm-up
        responsibility. At 6 workers this cuts pool-open from a ~2 s
        sequential warm-up to ~500 ms.

        On any worker's start failure: every already-opened worker is
        closed in reverse order (LIFO of completion), then the original
        exception propagates. The pool returns to :attr:`PoolState.CLOSED`.
        """
        async with self._state_lock:
            self._transition(PoolState.OPENING)

            # Kick off every worker's start in parallel. Use async_start() so
            # we don't need to wrap futures — we get native coroutines that
            # can be gathered directly.
            start_tasks: list[tuple[Worker, asyncio.Task[None]]] = []
            for worker in self._workers.values():
                task = asyncio.create_task(worker.async_start(progress_callback=progress_callback))
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
                # async_close() now returns a structured result rather than
                # raising on adapter-level errors. Any unexpected
                # exception (impl crash) gets swallowed here so the
                # original open failure can propagate; degraded close
                # outcomes are logged but never mask the open cause.
                try:
                    rollback_result = await worker.async_close(grace_s=5.0)
                except Exception as rollback_exc:
                    _logger.warning(
                        "pool.open_rollback_close_crashed",
                        resource_id=worker.resource_id,
                        error=str(rollback_exc),
                    )
                    for adapter in worker.adapters.values():
                        self._emit_open_progress(
                            progress_callback,
                            worker=worker,
                            adapter=adapter,
                            status=DeviceInitStatus.ROLLED_BACK,
                            detail=f"rollback close failed: {rollback_exc}",
                        )
                    continue
                if not rollback_result.runner_stop.joined or (rollback_result.adapter_close_errors):
                    _logger.warning(
                        "pool.open_rollback_close_degraded",
                        resource_id=worker.resource_id,
                        adapter_close_errors=rollback_result.adapter_close_errors,
                        runner_joined=rollback_result.runner_stop.joined,
                    )
                detail = "rolled back after another device failed"
                if rollback_result.adapter_close_errors or not rollback_result.runner_stop.joined:
                    detail = "rollback degraded; see logs"
                for adapter in worker.adapters.values():
                    self._emit_open_progress(
                        progress_callback,
                        worker=worker,
                        adapter=adapter,
                        status=DeviceInitStatus.ROLLED_BACK,
                        detail=detail,
                    )
            # Close preview bridges too — producers are gone now, so any
            # pending UI-side drainer wakes with ThreadBridgeClosedError
            # and exits cleanly.
            self.close_preview_bridges()
            self._transition(PoolState.CLOSED)
            # Propagate the first failure — the caller wants the original
            # exception type and message, not a synthetic aggregate.
            raise failed[0][1]

    async def close(self) -> PoolCloseResult:
        """Close every worker. Refuses if any worker is not IDLE.

        Pool close is the config-boundary teardown. Any active run must
        be disarmed first — the conductor's :meth:`Conductor.stop` is
        what does this in production; tests drive it directly.

        Close is idempotent — calling on an already-CLOSED pool returns
        a clean empty result rather than erroring.

        Returns a :class:`PoolCloseResult` aggregating each worker's
        :class:`WorkerCloseResult`. The result's ``clean`` flag is the
        load-bearing handle for the shutdown coordinator: ``True`` only
        when every worker reported no adapter-stop / adapter-close
        errors AND its runner thread joined within grace. The best-
        effort :meth:`shutdown_close` variant does NOT require IDLE
        and is what the :class:`~capa.ui.shutdown.ShutdownCoordinator`
        calls; :meth:`close` is for the strict config-reload boundary.
        """
        async with self._state_lock:
            if self._state is PoolState.CLOSED:
                return PoolCloseResult(clean=True, worker_results=(), errors=())

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
            worker_results, pool_errors = await self._close_workers_parallel(
                self._workers.values(), grace_s=5.0
            )

            # Close preview bridges after every worker has closed so no
            # producer attempts to write to a closed bridge. The
            # UI-side drainer wakes with ThreadBridgeClosedError and
            # exits cleanly on its own loop.
            self.close_preview_bridges()
            self._transition(PoolState.CLOSED)
            clean = not pool_errors and all(
                not w.adapter_close_errors and not w.adapter_stop_errors and w.runner_stop.joined
                for w in worker_results
            )
            _logger.info(
                "pool.close",
                worker_count=len(self._workers),
                clean=clean,
                pool_errors=tuple(pool_errors),
            )
            return PoolCloseResult(
                clean=clean,
                worker_results=tuple(worker_results),
                errors=tuple(pool_errors),
            )

    async def shutdown_close(self, *, grace_s: float = 5.0) -> PoolCloseResult:
        """Best-effort close for the :class:`ShutdownCoordinator`.

        Unlike :meth:`close`, this does NOT refuse on non-IDLE workers.
        The coordinator has already attempted to abort the active run by
        the time it calls here, but a worker may still be in
        ``ARMED``/``SAMPLING`` if the conductor's drain itself wedged.
        This method:

        1. Disarms any worker still in ARMED/SAMPLING (parallel, bounded
           by ``grace_s``). Disarm failures are captured as pool errors,
           never raised — the goal is to release as much hardware as we
           can before the coordinator's hard fuse fires.
        2. Closes every worker in parallel, exactly like :meth:`close`.
        3. Returns the aggregate :class:`PoolCloseResult` so the
           coordinator can record non-IDLE entry and any disarm
           timeouts in its :class:`ShutdownResult.errors`.

        The strict :meth:`close` stays for config-reload teardown where
        IDLE is a real invariant.
        """
        async with self._state_lock:
            if self._state is PoolState.CLOSED:
                return PoolCloseResult(clean=True, worker_results=(), errors=())

            pool_errors: list[str] = []

            # Step 1: disarm any non-IDLE workers, in parallel. Workers
            # that are already IDLE skip this step (worker.async_disarm() would
            # raise). Failures are recorded as pool errors but never
            # short-circuit — we still want to close every adapter we can.
            disarm_tasks: dict[str, asyncio.Task[DisarmResult]] = {}
            for rid, worker in self._workers.items():
                if worker.state in (WorkerState.ARMED, WorkerState.SAMPLING):
                    disarm_tasks[rid] = asyncio.create_task(worker.async_disarm(grace_s=grace_s))
            if disarm_tasks:
                await asyncio.gather(*disarm_tasks.values(), return_exceptions=True)
                for rid, task in disarm_tasks.items():
                    exc = task.exception()
                    if exc is not None:
                        msg = f"worker {rid!r} shutdown disarm failed: {exc!r}"
                        pool_errors.append(msg)
                        _logger.warning(
                            "pool.shutdown_close_disarm_failed",
                            resource_id=rid,
                            error=str(exc),
                        )

            # Capture any worker that remained non-IDLE after the disarm
            # attempt — these may still have running stream tasks that
            # close() will see; we record but proceed.
            still_non_idle = [
                (w.resource_id, w.state.value)
                for w in self._workers.values()
                if w.state is not WorkerState.IDLE
            ]
            if still_non_idle:
                pool_errors.append(f"workers non-IDLE entering close: {still_non_idle}")
                _logger.warning(
                    "pool.shutdown_close_workers_non_idle",
                    non_idle=tuple(still_non_idle),
                )

            self._transition(PoolState.CLOSING)

            # Step 2: close every worker. Only close IDLE workers; non-IDLE
            # workers are recorded as errors.
            idle_workers = [w for w in self._workers.values() if w.state is WorkerState.IDLE]
            for worker in self._workers.values():
                if worker.state is not WorkerState.IDLE:
                    pool_errors.append(f"worker {worker.resource_id!r} non-IDLE; close skipped")

            close_results, close_errors = await self._close_workers_parallel(
                idle_workers, grace_s=grace_s
            )
            pool_errors.extend(close_errors)

            self.close_preview_bridges()
            self._transition(PoolState.CLOSED)
            clean = not pool_errors and all(
                not w.adapter_close_errors and not w.adapter_stop_errors and w.runner_stop.joined
                for w in close_results
            )
            _logger.info(
                "shutdown.pool_close_result",
                worker_count=len(self._workers),
                clean=clean,
                pool_errors=tuple(pool_errors),
            )
            return PoolCloseResult(
                clean=clean,
                worker_results=tuple(close_results),
                errors=tuple(pool_errors),
            )

    # ------------------------------------------------------------------
    # Run-lifecycle methods (called by the Conductor)
    # ------------------------------------------------------------------

    async def arm_all(self, run_context: RunContext) -> None:
        """Transition every worker IDLE → ARMED with the same run context.

        Parallel. On any failure, in-flight arms are awaited but the
        pool does not undo successful arms — that's the conductor's job
        via :meth:`disarm_all`.
        """
        self._require_open()
        tasks = [
            asyncio.create_task(worker.async_arm(run_context)) for worker in self._workers.values()
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
        per bridge.

        ``consumer_loop`` is the loop that will drain the bridges — the
        conductor's loop in production; the test's loop in integration
        tests.
        """
        self._require_open()
        tasks = [
            (
                rid,
                asyncio.create_task(worker.async_begin_sampling(consumer_loop=consumer_loop)),
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
                tasks[rid] = asyncio.create_task(worker.async_disarm(grace_s=grace_s))
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
    # Command-lifetime methods (PoolClient routes here)
    # ------------------------------------------------------------------

    def dispatch(self, device: str, cmd: DeviceCommand) -> Future[CommandResult]:
        """Route a command to the worker hosting ``device``.

        Synchronous facade (returns a :class:`concurrent.futures.Future`)
        — the PoolClient async-wraps this. Used directly by tests.

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

    def device_readback(self, device: str) -> Future[Any]:
        """Probe one device's ``read_state_snapshot()`` on the owning worker.

        Future resolves to whatever the adapter returns from its
        :meth:`read_state_snapshot` (e.g. :class:`WatlowStateSnapshot`),
        or ``None`` when the adapter doesn't implement that surface.
        Used by manual-control cards to prefill their widgets with the
        device's current operator-facing values.
        """
        worker = self.worker_for(device)
        return worker.device_readback(device)

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
