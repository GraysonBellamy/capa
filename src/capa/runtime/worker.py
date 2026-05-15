""":class:`Worker` — one thread + one asyncio loop owning one hardware resource.

The worker hosts one or more adapters that share a hardware contention
domain (identified by ``resource_id``) and exposes async lifecycle methods
for the :class:`Conductor` and the manual-control surface.

The state machine (CLOSED → IDLE → ARMED → SAMPLING → DRAINING → IDLE,
edges enumerated in :data:`~capa.runtime.lifecycle.LEGAL_WORKER_EDGES`) is
the contract. Every public method either drives a specific edge or is gated
by a specific state set.

Construction does no I/O. :meth:`async_start` brings the runner up, opens
every adapter inside the worker loop, and resolves when the worker reaches
IDLE. :meth:`async_close` reverses this, after asserting IDLE.

Lifecycle methods (``async_start``, ``async_close``, ``async_arm``,
``async_begin_sampling``, ``async_disarm``) are coroutines — every caller
(Conductor, PoolClient, UI cards) is on a *different* loop than the worker,
and these methods bridge to the worker loop via ``asyncio.wrap_future`` of
the runner's submit futures.

Per-command methods (``dispatch``, ``snapshot``, ``camera_metadata``,
``device_readback``) remain sync and return :class:`concurrent.futures.Future`
— they're called by sync UI paths on different loops where awaiting isn't
an option; async callers wrap them with :func:`asyncio.wrap_future`.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import Future
from typing import Any

import structlog

from capa.devices.adapter import CommandResult, DeviceAdapter, DeviceCommand
from capa.devices.camera.metadata import WebcamMetadata
from capa.devices.records import DeviceEmission, SourceRecord
from capa.runtime.bridge import BridgePolicy, ThreadBridge
from capa.runtime.camera_adapter import CameraDeviceAdapter
from capa.runtime.emissions import WorkerEmission
from capa.runtime.errors import (
    UnknownDeviceError,
    WorkerStateError,
)
from capa.runtime.lifecycle import (
    LEGAL_WORKER_EDGES,
    WorkerState,
)
from capa.runtime.metrics import DisarmResult, WorkerMetrics
from capa.runtime.preview import PreviewFrame
from capa.runtime.progress import (
    DeviceInitProgress,
    DeviceInitStatus,
    OpenProgressCallback,
    identity_from_device_info,
    identity_summary,
)
from capa.runtime.runcontext import RunContext
from capa.runtime.runner import ThreadedRunner, WorkerRunner
from capa.runtime.shutdown import (
    WorkerCloseResult,
    WorkerShutdownConfig,
)

_logger = structlog.get_logger("capa.runtime.worker")


class Worker:
    """One thread + loop hosting one resource's adapters.

    The constructor is sync and does no I/O — it just records the
    configuration. Bring the worker up with :meth:`async_start`; tear it down
    with :meth:`async_close`.

    Adapters passed to the constructor MUST share the same
    :attr:`DeviceAdapter.resource_id`. Validating this is the
    :func:`~capa.runtime.build.build_workers` job (next PR); this class
    trusts its input.
    """

    def __init__(
        self,
        *,
        resource_id: str,
        adapters: Sequence[DeviceAdapter],
        runner: WorkerRunner | None = None,
        outbound_capacity: int = 64,
        preview_bridges: Mapping[str, ThreadBridge[PreviewFrame]] | None = None,
        shutdown_config: WorkerShutdownConfig | None = None,
    ) -> None:
        if not adapters:
            raise ValueError(f"Worker {resource_id!r}: must have at least one adapter")
        if outbound_capacity < 1:
            raise ValueError(
                f"Worker {resource_id!r}: outbound_capacity must be >= 1, got {outbound_capacity}"
            )
        self._shutdown_config: WorkerShutdownConfig = shutdown_config or WorkerShutdownConfig()
        self._resource_id = resource_id
        # dict for name-based lookup; tuple preserves declaration order for
        # the rare adapter that cares (e.g. open ordering on a shared bus).
        self._adapters: dict[str, DeviceAdapter] = {a.name: a for a in adapters}
        self._adapter_list: tuple[DeviceAdapter, ...] = tuple(adapters)
        self._runner: WorkerRunner = runner or ThreadedRunner(name=f"worker-{resource_id}")
        self._outbound_capacity = outbound_capacity
        # Preview bridges, one per camera adapter hosted by this worker.
        # Non-camera workers (Watlow, Alicat, NI-DAQ) receive an empty map
        # and never touch it. The pool partitions bridges by adapter name
        # in build_workers.
        self._preview_bridges: dict[str, ThreadBridge[PreviewFrame]] = (
            dict(preview_bridges) if preview_bridges else {}
        )

        # State (read from any thread; written only inside the worker loop).
        # Atomic under the GIL — no lock required for readers.
        self._state: WorkerState = WorkerState.CLOSED
        self._run_context: RunContext | None = None
        self._outbound: ThreadBridge[WorkerEmission] | None = None
        # One stream task per adapter when SAMPLING. Bounded by the adapter
        # count, typically 1.
        self._stream_tasks: list[asyncio.Task[None]] = []
        # The disarm-signal event lives on the worker loop; the disarm path
        # sets it from the worker loop. Stream tasks await it as a cooperative
        # stop signal.
        self._disarm_event: asyncio.Event | None = None
        self._fatal_error: BaseException | None = None
        # Most recent disarm's per-adapter stop errors as human-readable
        # strings. Cleared at every disarm start; consumed by
        # :meth:`_close_all_impl` when close runs after a disarm so the
        # WorkerCloseResult surfaces the disarm-side errors alongside any
        # close-side ones. :meth:`WorkerPool.shutdown_close` drives the
        # disarm-then-close sequence for non-IDLE workers.
        self._last_adapter_stop_errors: list[str] = []
        # Tracks the most recent disarm's outcome (DisarmResult value)
        # so close can include it in WorkerCloseResult. ``None`` when
        # the worker has never been disarmed in its current open cycle.
        self._last_disarm_result: DisarmResult | None = None

        self._metrics = WorkerMetrics(
            resource_id=resource_id,
            adapter_names=tuple(a.name for a in adapters),
        )

    # ---- introspection (any thread) -------------------------------------

    @property
    def resource_id(self) -> str:
        return self._resource_id

    @property
    def adapter_names(self) -> tuple[str, ...]:
        return tuple(self._adapters)

    @property
    def adapters(self) -> Mapping[str, DeviceAdapter]:
        """Read-only view of this worker's adapters keyed by device name.

        Exposed so the conductor / session can introspect adapter metadata
        for equipment-block construction.
        The mapping is a live view: keys never change after construction,
        but adapter state (open/closed) reflects the worker's lifecycle.
        Commands MUST still flow through :meth:`dispatch` — direct
        ``adapter.command()`` calls bypass the worker's loop and the
        cancellation shield.
        """
        return self._adapters

    @property
    def state(self) -> WorkerState:
        """Current state. Atomic single-attribute read; no lock required.

        Readers should treat this as advisory — by the time the read
        returns, the worker may have transitioned. The canonical gate lives
        inside the worker loop (see :meth:`_dispatch_impl`).
        """
        return self._state

    @property
    def metrics(self) -> WorkerMetrics:
        return self._metrics

    @property
    def fatal_error(self) -> BaseException | None:
        """The exception that ejected the worker out of SAMPLING, if any.
        Read by the :class:`Conductor` after a clean disarm to decide
        whether the run is degraded."""
        return self._fatal_error

    # ---- command-lifetime sync facade -----------------------------------

    def dispatch(self, adapter_name: str, cmd: DeviceCommand) -> Future[CommandResult]:
        """Submit a command to one of this worker's adapters.

        State-gate is enforced inside the worker loop (see
        :meth:`_dispatch_impl`) — the caller-side state read is advisory
        only. This avoids a "caller saw SAMPLING just before worker
        transitioned to DRAINING" race; the worker is the single authority.

        The shielded ``adapter.command`` call is the load-bearing
        cancellation-shield rule — see :meth:`_dispatch_impl`.
        """
        if adapter_name not in self._adapters:
            return _failed_future(
                UnknownDeviceError(adapter_name, configured_names=tuple(self._adapters))
            )
        return self._runner.submit(lambda: self._dispatch_impl(adapter_name, cmd))

    def snapshot(self, adapter_name: str) -> Future[DeviceEmission]:
        """Read one ``adapter.snapshot()`` on the worker loop."""
        if adapter_name not in self._adapters:
            return _failed_future(
                UnknownDeviceError(adapter_name, configured_names=tuple(self._adapters))
            )
        return self._runner.submit(lambda: self._snapshot_impl(adapter_name))

    def camera_metadata(self, adapter_name: str) -> Future[WebcamMetadata | None]:
        """Read one ``CameraDeviceAdapter.camera_metadata()`` on the worker loop.

        Cards subscribe to :attr:`RunController.pool_changed` and call
        this to refresh their widgets from the live probe data without
        crossing loops. Returns ``None`` (inside the future) for adapters
        whose camera doesn't expose a metadata snapshot — IR cameras
        and any non-camera adapter routed here by mistake.

        No state-gate: metadata is a frozen read against fields populated
        at ``adapter.open()``. Safe in any worker state including
        DRAINING / CLOSED — the underlying attributes outlive recording.
        """
        if adapter_name not in self._adapters:
            return _failed_future(
                UnknownDeviceError(adapter_name, configured_names=tuple(self._adapters))
            )
        return self._runner.submit(lambda: self._camera_metadata_impl(adapter_name))

    def device_readback(self, adapter_name: str) -> Future[Any]:
        """Probe an adapter's ``read_state_snapshot()`` on the worker loop.

        Operator-facing live values (current setpoint, PV, etc.) that the
        manual-control card uses to prefill its widgets. Returns ``None``
        for adapters that don't expose ``read_state_snapshot`` — the card
        falls back to its static defaults in that case.

        Read-only, but does involve I/O against the device, so the worker
        state is gated (DRAINING / CLOSED reject). The watlowlib session
        serializes commands so this won't race a streaming recorder on
        the same bus.
        """
        if adapter_name not in self._adapters:
            return _failed_future(
                UnknownDeviceError(adapter_name, configured_names=tuple(self._adapters))
            )
        return self._runner.submit(lambda: self._device_readback_impl(adapter_name))

    # ---- async lifecycle facade (for async callers on any loop) ---------------

    async def async_start(self, progress_callback: OpenProgressCallback | None = None) -> None:
        """Bring the runner up; open every adapter inside the worker loop.

        Resolves when the worker reaches :attr:`WorkerState.IDLE`. On any
        :meth:`adapter.open` failure, the runner is cleanly stopped before
        the exception propagates — no half-opened adapter or leaked
        worker thread.

        Raises :class:`WorkerStateError` if the worker is not in CLOSED —
        re-starting a closed worker is not supported; construct a new one.
        """
        if self._state is not WorkerState.CLOSED:
            raise WorkerStateError(
                f"Worker {self._resource_id!r}: start() requires CLOSED, got {self._state}",
                from_state=self._state,
                resource_id=self._resource_id,
            )
        await asyncio.wrap_future(self._runner.start())
        try:
            await asyncio.wrap_future(
                self._runner.submit(lambda: self._open_all_impl(progress_callback))
            )
        except BaseException:
            await asyncio.wrap_future(self._runner.stop(grace_s=2.0))
            raise

    async def async_close(self, *, grace_s: float = 5.0) -> WorkerCloseResult:
        """Close every adapter and stop the runner.

        Requires :attr:`WorkerState.IDLE`. Raises :class:`WorkerStateError`
        if the worker is not IDLE. The :class:`WorkerPool` is responsible
        for disarming any active run before closing the worker.

        Returns a :class:`WorkerCloseResult` describing the outcome.
        Adapter-level errors are captured in the result's error tuples
        rather than raised — every adapter is given the chance to release
        its bus, and the caller (pool, shutdown coordinator) aggregates
        per-worker outcomes.

        ``grace_s`` bounds the runner-stop deadline; the per-adapter
        :class:`WorkerShutdownConfig` is what bounds the adapter-close
        calls.
        """
        if self._state is not WorkerState.IDLE:
            raise WorkerStateError(
                f"Worker {self._resource_id!r}: close() requires IDLE, got {self._state}",
                from_state=self._state,
                resource_id=self._resource_id,
            )
        state_before = self._state.value
        adapter_stop_errors = tuple(self._last_adapter_stop_errors)
        disarm_result_str = (
            self._last_disarm_result.value if self._last_disarm_result is not None else None
        )
        close_errors = await asyncio.wrap_future(self._runner.submit(self._close_all_impl))
        runner_stop = await asyncio.wrap_future(self._runner.stop(grace_s=grace_s))
        return WorkerCloseResult(
            resource_id=self._resource_id,
            state_before=state_before,
            adapter_stop_errors=adapter_stop_errors,
            adapter_close_errors=close_errors,
            disarm_result=disarm_result_str,
            runner_stop=runner_stop,
        )

    async def async_arm(self, run_context: RunContext) -> None:
        """Transition IDLE → ARMED with ``run_context`` installed.

        Streams are not yet running; dispatch is permitted; commands route
        through the worker loop with the run context's writer for event
        recording.
        """
        if self._state is not WorkerState.IDLE:
            raise WorkerStateError(
                f"Worker {self._resource_id!r}: arm() requires IDLE, got {self._state}",
                from_state=self._state,
                resource_id=self._resource_id,
            )
        await asyncio.wrap_future(self._runner.submit(lambda: self._arm_impl(run_context)))

    async def async_begin_sampling(
        self, *, consumer_loop: asyncio.AbstractEventLoop
    ) -> ThreadBridge[WorkerEmission]:
        """Transition ARMED → SAMPLING; return the outbound emission bridge.

        ``consumer_loop`` is the loop that will drain the bridge — for
        :class:`Conductor`, the conductor's own loop. The bridge
        constructor records this; :meth:`ThreadBridge.attach_consumer`
        will assert the running loop matches. Making the loop reference
        explicit keeps the worker decoupled from Conductor wiring.
        """
        if self._state is not WorkerState.ARMED:
            raise WorkerStateError(
                f"Worker {self._resource_id!r}: begin_sampling() requires ARMED, got {self._state}",
                from_state=self._state,
                resource_id=self._resource_id,
            )
        return await asyncio.wrap_future(
            self._runner.submit(lambda: self._begin_sampling_impl(consumer_loop=consumer_loop))
        )

    async def async_disarm(self, *, grace_s: float = 5.0) -> DisarmResult:
        """Transition SAMPLING/ARMED → DRAINING → IDLE.

        Resolves with :attr:`DisarmResult.OK` if all streams exit and all
        adapter stops complete within ``grace_s``. Resolves with
        :attr:`DisarmResult.FORCED` if any stream task had to be cancelled
        on grace expiry.
        """
        if self._state not in (WorkerState.ARMED, WorkerState.SAMPLING):
            raise WorkerStateError(
                f"Worker {self._resource_id!r}: disarm() requires ARMED or "
                f"SAMPLING, got {self._state}",
                from_state=self._state,
                resource_id=self._resource_id,
            )
        return await asyncio.wrap_future(
            self._runner.submit(lambda: self._disarm_impl(grace_s=grace_s))
        )

    # =========================================================================
    # Worker-loop implementations. Every method here runs on the worker loop.
    # =========================================================================

    def _transition(self, target: WorkerState) -> None:
        """Move to ``target`` if the edge is legal; raise otherwise.

        This is the only mutator of ``self._state``. Called from inside
        the worker loop so writes serialize naturally.
        """
        edge = (self._state, target)
        if edge not in LEGAL_WORKER_EDGES:
            raise WorkerStateError(
                f"Worker {self._resource_id!r}: illegal transition {self._state} → {target}",
                from_state=self._state,
                to_state=target,
                resource_id=self._resource_id,
            )
        _logger.debug(
            "worker.transition",
            resource_id=self._resource_id,
            from_state=self._state.value,
            to_state=target.value,
        )
        self._state = target
        self._metrics.state = target

    def _emit_open_progress(
        self,
        callback: OpenProgressCallback | None,
        adapter: DeviceAdapter,
        status: DeviceInitStatus,
        *,
        detail: str = "",
        error_type: str | None = None,
    ) -> None:
        """Emit one adapter-open progress event.

        Runs on the worker loop/thread. Callbacks provided by UI code must be
        thread-safe; :class:`RunController` marshals these back onto qasync.
        """
        if callback is None:
            return
        identity = None
        if status is DeviceInitStatus.READY:
            identity = identity_from_device_info(getattr(adapter, "device_info", None))
            if not detail:
                detail = identity_summary(identity)
        try:
            callback(
                DeviceInitProgress(
                    name=adapter.name,
                    adapter=type(adapter).__name__,
                    resource_id=self._resource_id,
                    status=status,
                    detail=detail,
                    error_type=error_type,
                    identity=identity,
                )
            )
        except Exception as exc:
            _logger.warning(
                "worker.open_progress_callback_failed",
                resource_id=self._resource_id,
                adapter=adapter.name,
                error=str(exc),
            )

    async def _open_all_impl(self, progress_callback: OpenProgressCallback | None) -> None:
        """CLOSED → IDLE. Open every adapter in declaration order.

        On any failure: close already-opened adapters in reverse order,
        leave state at CLOSED, propagate the original exception.

        For each :class:`CameraDeviceAdapter`, after :meth:`open` succeeds,
        attach the per-camera preview bridge (if one was provided by the
        pool) and start the idle preview source. The channel drainer
        survives every subsequent arm/sample/disarm cycle; the source is
        only live in IDLE and is brought down/up by
        :meth:`_begin_sampling_impl` and :meth:`_disarm_impl`.
        """
        opened: list[DeviceAdapter] = []
        try:
            for adapter in self._adapter_list:
                self._emit_open_progress(
                    progress_callback,
                    adapter,
                    DeviceInitStatus.OPENING,
                    detail="opening connection",
                )
                try:
                    await adapter.open()
                    opened.append(adapter)
                    camera_adapter = _as_camera_adapter(adapter)
                    if camera_adapter is not None:
                        bridge = self._preview_bridges.get(camera_adapter.name)
                        if bridge is not None:
                            await camera_adapter.start_preview_channel(bridge)
                except BaseException as exc:
                    self._emit_open_progress(
                        progress_callback,
                        adapter,
                        DeviceInitStatus.FAILED,
                        detail=str(exc),
                        error_type=type(exc).__name__,
                    )
                    raise
                self._emit_open_progress(
                    progress_callback,
                    adapter,
                    DeviceInitStatus.READY,
                )
        except BaseException:
            for adapter in reversed(opened):
                rollback_detail = "rolled back after open failure"
                camera_adapter = _as_camera_adapter(adapter)
                if camera_adapter is not None:
                    try:
                        await camera_adapter.stop_preview_channel()
                    except BaseException as ch_exc:
                        rollback_detail = f"rollback degraded: {ch_exc!r}"
                        _logger.warning(
                            "worker.open_rollback_stop_channel_failed",
                            resource_id=self._resource_id,
                            adapter=camera_adapter.name,
                            error=str(ch_exc),
                        )
                try:
                    await adapter.close()
                except BaseException as close_exc:
                    rollback_detail = f"rollback close failed: {close_exc!r}"
                    _logger.warning(
                        "worker.open_rollback_close_failed",
                        resource_id=self._resource_id,
                        adapter=adapter.name,
                        error=str(close_exc),
                    )
                self._emit_open_progress(
                    progress_callback,
                    adapter,
                    DeviceInitStatus.ROLLED_BACK,
                    detail=rollback_detail,
                )
            raise
        self._transition(WorkerState.IDLE)

    async def _close_all_impl(self) -> tuple[str, ...]:
        """IDLE → CLOSED. Close every adapter; return per-adapter errors.

        Close failures are captured as strings and do NOT abort the
        close sequence — every adapter gets the chance to release its
        bus. Each ``adapter.close()`` is bounded by
        ``adapter_close_grace_s`` so a vendor close that wedges in a
        native call cannot pin the worker thread; on timeout the error
        is recorded and the next adapter is attempted.

        For each :class:`CameraDeviceAdapter` we cancel the long-lived
        preview drainer before :meth:`Camera.close` so the camera owns
        the input container during teardown.
        """
        errors: list[str] = []
        cfg = self._shutdown_config
        for adapter in reversed(self._adapter_list):
            camera_adapter = _as_camera_adapter(adapter)
            if camera_adapter is not None:
                try:
                    await asyncio.wait_for(
                        camera_adapter.stop_preview_channel(),
                        timeout=cfg.adapter_close_grace_s,
                    )
                except TimeoutError:
                    errors.append(
                        f"adapter {camera_adapter.name!r} stop_preview_channel "
                        f"timeout after {cfg.adapter_close_grace_s}s"
                    )
                    _logger.warning(
                        "worker.close_stop_channel_timeout",
                        resource_id=self._resource_id,
                        adapter=camera_adapter.name,
                        grace_s=cfg.adapter_close_grace_s,
                    )
                except BaseException as ch_exc:
                    errors.append(
                        f"adapter {camera_adapter.name!r} stop_preview_channel failed: {ch_exc!r}"
                    )
                    _logger.warning(
                        "worker.close_stop_channel_failed",
                        resource_id=self._resource_id,
                        adapter=camera_adapter.name,
                        error=str(ch_exc),
                    )
            try:
                await asyncio.wait_for(adapter.close(), timeout=cfg.adapter_close_grace_s)
            except TimeoutError:
                errors.append(
                    f"adapter {adapter.name!r} close timeout after {cfg.adapter_close_grace_s}s"
                )
                _logger.warning(
                    "worker.close_timeout",
                    resource_id=self._resource_id,
                    adapter=adapter.name,
                    grace_s=cfg.adapter_close_grace_s,
                )
            except BaseException as exc:
                errors.append(f"adapter {adapter.name!r} close failed: {exc!r}")
                _logger.warning(
                    "worker.close_failed",
                    resource_id=self._resource_id,
                    adapter=adapter.name,
                    error=str(exc),
                )
        self._transition(WorkerState.CLOSED)
        return tuple(errors)

    async def _arm_impl(self, run_context: RunContext) -> None:
        """IDLE → ARMED. Install run context. No I/O."""
        self._run_context = run_context
        self._fatal_error = None
        self._transition(WorkerState.ARMED)

    async def _begin_sampling_impl(
        self, *, consumer_loop: asyncio.AbstractEventLoop
    ) -> ThreadBridge[WorkerEmission]:
        """ARMED → SAMPLING. Build the outbound bridge; spawn stream tasks.

        The bridge is constructed on the worker loop. The producer side is
        attached here; the consumer side is attached by scheduling
        :meth:`ThreadBridge.attach_consumer` onto ``consumer_loop`` via
        :meth:`asyncio.AbstractEventLoop.call_soon_threadsafe`. We **wait
        for that attach to complete** before spawning stream tasks — without
        the wait, a stream task could put an emission onto a bridge whose
        consumer queue (``asyncio.Queue``, loop-affine to the consumer) does
        not yet exist, and the consumer-side callback would crash.

        Doing the consumer attach from the worker (rather than asking the
        caller to do it after ``begin_sampling`` returns) eliminates this
        race entirely: callers receive a fully-wired bridge.
        """
        assert self._run_context is not None, "ARMED implies run_context set"

        bridge: ThreadBridge[WorkerEmission] = ThreadBridge(
            name=f"worker-{self._resource_id}-outbound",
            capacity=self._outbound_capacity,
            consumer_loop=consumer_loop,
            policy=BridgePolicy.BLOCK,
        )
        bridge.attach_producer(asyncio.get_running_loop())

        # Attach the consumer on its own loop and wait for confirmation.
        worker_loop = asyncio.get_running_loop()
        consumer_attached: asyncio.Event = asyncio.Event()

        def _attach_on_consumer() -> None:
            try:
                bridge.attach_consumer()
            finally:
                worker_loop.call_soon_threadsafe(consumer_attached.set)

        consumer_loop.call_soon_threadsafe(_attach_on_consumer)
        await consumer_attached.wait()

        self._outbound = bridge
        # Disarm event lives on the worker loop; stream tasks race it
        # against adapter.stream() exhaustion.
        self._disarm_event = asyncio.Event()

        # Start each adapter. If any start() raises, stop the ones that
        # started, drop the bridge, and bubble the exception.
        started: list[DeviceAdapter] = []
        try:
            for adapter in self._adapter_list:
                await self._adapter_start(adapter)
                started.append(adapter)
        except BaseException:
            for adapter in reversed(started):
                try:
                    await adapter.stop()
                except BaseException as stop_exc:
                    _logger.warning(
                        "worker.begin_sampling_rollback_stop_failed",
                        resource_id=self._resource_id,
                        adapter=adapter.name,
                        error=str(stop_exc),
                    )
            self._outbound = None
            self._disarm_event = None
            bridge.close()
            raise

        # Spawn one stream task per adapter. The tasks reference
        # self._outbound and self._disarm_event by attribute, not by capture,
        # so a later disarm/cleanup sees the right values.
        loop = asyncio.get_running_loop()
        self._stream_tasks = [
            loop.create_task(
                self._stream_task(adapter),
                name=f"worker-{self._resource_id}-stream-{adapter.name}",
            )
            for adapter in self._adapter_list
        ]

        self._transition(WorkerState.SAMPLING)
        return bridge

    async def _adapter_start(self, adapter: DeviceAdapter) -> None:
        """Dispatch ``adapter.start`` against the signature it declares.

        Three flavors of ``start`` co-exist in this codebase:

        * :class:`~capa.runtime.camera_adapter.CameraDeviceAdapter`'s
          ``start(run_context: RunContext)`` — needs the full context so
          it can compute the camera output_path from
          ``bundle.root + run_id``.
        * Device adapters (Watlow, Alicat, Sartorius, NI-DAQ) declare
          ``start(self, clock: RunClock | None = None)`` — need the
          clock for emission timestamps.
        * The :class:`DeviceAdapter` Protocol itself is ``start(self)``
          (zero positional args beyond self).

        We inspect the bound method's signature and call the right
        shape. Inspection is one call per arm — negligible cost. The
        previous TypeError-fallback chain conflated binding mismatches
        with runtime TypeErrors from inside the adapter body; the
        signature probe avoids that ambiguity.

        Adapters that declare a single non-self parameter receive the
        ``RunContext`` when it's named ``run_context`` / ``ctx`` /
        ``context``; otherwise the parameter is treated as the clock.
        Names match by exact string; this is enough to disambiguate
        without making adapters import a marker type.
        """
        assert self._run_context is not None
        bound = adapter.start
        try:
            sig = inspect.signature(bound)
        except (TypeError, ValueError):
            # Builtins / C-implemented methods can refuse introspection.
            # Fall back to the historical clock-first call.
            await bound(self._run_context.clock)  # type: ignore[call-arg]
            return
        params = list(sig.parameters.values())
        if not params:
            await bound()
            return
        first = params[0]
        if first.name in {"run_context", "ctx", "context"}:
            await bound(self._run_context)  # type: ignore[call-arg]
            return
        await bound(self._run_context.clock)  # type: ignore[call-arg]

    async def _disarm_impl(self, *, grace_s: float) -> DisarmResult:
        """SAMPLING/ARMED → DRAINING → IDLE.

        First, signal stream tasks to stop (``adapter.stop()`` wrapped in
        ``asyncio.wait_for`` with ``adapter_stop_grace_s``), then await
        each stream task with ``grace_s``. If any task is still running,
        cancel it and await cancellation with a secondary
        ``stream_cancel_grace_s`` bound — a stream task that ignores its
        cancellation past this bound is wedged in a non-cancellable native
        call, and the :class:`~capa.ui.shutdown.ShutdownCoordinator`'s
        hard wall-clock fuse takes over.
        """
        from_state = self._state
        self._transition(WorkerState.DRAINING)
        assert self._disarm_event is not None or from_state is WorkerState.ARMED

        cfg = self._shutdown_config
        result = DisarmResult.OK
        # Cooperative stop: adapter.stop() flips the adapter's
        # lifecycle so stream() exits naturally on its next yield. Each
        # call is bounded — a stop that ignores its deadline is recorded
        # and disarm continues; the disarm event still fires below so
        # stream tasks can observe it.
        self._last_adapter_stop_errors = []
        for adapter in reversed(self._adapter_list):
            try:
                await asyncio.wait_for(adapter.stop(), timeout=cfg.adapter_stop_grace_s)
            except TimeoutError as exc:
                err = f"adapter {adapter.name!r} stop timeout after {cfg.adapter_stop_grace_s}s"
                self._last_adapter_stop_errors.append(err)
                result = DisarmResult.FORCED
                _logger.warning(
                    "worker.adapter_stop_timeout",
                    resource_id=self._resource_id,
                    adapter=adapter.name,
                    grace_s=cfg.adapter_stop_grace_s,
                    error=str(exc),
                )
            except BaseException as exc:
                self._last_adapter_stop_errors.append(
                    f"adapter {adapter.name!r} stop failed: {exc!r}"
                )
                _logger.warning(
                    "worker.adapter_stop_failed",
                    resource_id=self._resource_id,
                    adapter=adapter.name,
                    error=str(exc),
                )

        if self._disarm_event is not None:
            self._disarm_event.set()

        # Wait for stream tasks to exit with grace. Any still-running after
        # grace expiry are cancelled; cancellation itself is then bounded
        # so a CancelledError-swallowing stream task can't wedge disarm.
        if self._stream_tasks:
            done, pending = await asyncio.wait(self._stream_tasks, timeout=grace_s)
            if pending:
                _logger.warning(
                    "worker.disarm_grace_expired",
                    resource_id=self._resource_id,
                    pending_count=len(pending),
                )
                for t in pending:
                    t.cancel()
                result = DisarmResult.FORCED
                # Secondary bounded wait on the cancellations themselves.
                # A stream task that catches CancelledError and keeps
                # running (vendor code with a slow finally block, native
                # blocking call that didn't observe the cancel) is the
                # canonical case here. We can't kill it from here; the
                # in-process coordinator's hard fuse is what closes the
                # gap if it never returns.
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True),
                        timeout=cfg.stream_cancel_grace_s,
                    )
                except TimeoutError:
                    unfinished = [t for t in pending if not t.done()]
                    _logger.warning(
                        "worker.disarm_cancel_timeout",
                        resource_id=self._resource_id,
                        grace_s=cfg.stream_cancel_grace_s,
                        unfinished_task_names=tuple(t.get_name() for t in unfinished),
                        thread_ident=self._runner.thread_ident,
                    )
            # Surface in-stream exceptions (best-effort) as fatal_error.
            for t in done:
                if t.cancelled():
                    continue
                task_exc = t.exception()
                if task_exc is not None and self._fatal_error is None:
                    self._fatal_error = task_exc

        # Close the bridge — consumer drains pending items then sees EOF.
        if self._outbound is not None:
            self._outbound.close()
            self._outbound = None
        self._stream_tasks = []
        self._disarm_event = None
        self._run_context = None
        self._transition(WorkerState.IDLE)
        self._last_disarm_result = result
        return result

    async def _stream_task(self, adapter: DeviceAdapter) -> None:
        """One per-adapter stream loop. Runs only during SAMPLING.

        Exits cleanly when ``adapter.stream()`` returns (the adapter's
        lifecycle flipped to ``open`` via ``adapter.stop()``); exits with
        the exception when ``adapter.stream()`` raises. The worker's
        :meth:`_disarm_impl` is responsible for triggering the natural exit
        via ``adapter.stop()``.

        The bridge ``put`` is BLOCK by policy. Sustained block surfaces at
        the Conductor's saturation deadline — the worker itself doesn't
        escalate; it just stays parked at the put.
        """
        outbound = self._outbound
        run_context = self._run_context
        assert outbound is not None and run_context is not None
        last_emit_mono = time.monotonic()
        try:
            async for emission in adapter.stream():
                now_mono = time.monotonic()
                self._metrics.observe_tick_duration((now_mono - last_emit_mono) * 1000.0)
                last_emit_mono = now_mono
                # Per-poll cadence: stamp on the SourceRecord that opens
                # an acquisition tick. Most adapters emit exactly one
                # SourceRecord per tick, so the default is "every
                # SourceRecord opens its own tick"; Watlow emits one per
                # polled parameter and flags only the first of each batch
                # with ``metadata["tick_first"] = True`` so the worker
                # doesn't double-count the per-parameter fanout. The
                # ``True`` default preserves correct behavior for adapters
                # that don't set the flag.
                if isinstance(emission, SourceRecord) and emission.metadata.get("tick_first", True):
                    self._metrics.observe_poll_emitted(t_mono_s=now_mono)
                await outbound.put(emission)
                self._metrics.observe_sample_emitted()
        except asyncio.CancelledError:
            # Forced disarm. Re-raise so the task's outcome is "cancelled."
            raise
        except BaseException as exc:
            # Record event into bundle, mark fatal, signal disarm by
            # exiting the stream loop. The conductor's per-worker watchdog
            # is what escalates to run abort for adapters with
            # ``on_failure = abort``.
            self._fatal_error = exc
            try:
                await run_context.writer.write_event(
                    kind="worker_adapter_error",
                    message=f"adapter {adapter.name!r} raised: {exc!r}",
                    metadata={
                        "resource_id": self._resource_id,
                        "adapter": adapter.name,
                        "error_type": type(exc).__name__,
                    },
                )
            except BaseException as write_exc:
                _logger.warning(
                    "worker.error_event_write_failed",
                    resource_id=self._resource_id,
                    adapter=adapter.name,
                    original=str(exc),
                    write_error=str(write_exc),
                )
            raise

    async def _dispatch_impl(self, adapter_name: str, cmd: DeviceCommand) -> CommandResult:
        """Worker-side dispatch. Enforces the dispatch state-gate and the
        cancellation shield.

        The load-bearing rule: ``adapter.command`` is called inside
        :func:`asyncio.shield`. The caller's future may be cancelled at
        any time without interrupting the in-flight hardware transaction;
        the worker-side coroutine runs to completion. If the caller
        cancelled, the result is dropped on the floor — but the hardware
        is in a known state and the next dispatch reads clean.

        The state check runs *here*, not at the caller side, so a
        DRAINING-vs-SAMPLING race resolves authoritatively on the worker
        loop.
        """
        if self._state in (WorkerState.DRAINING, WorkerState.CLOSED):
            raise WorkerStateError(
                f"Worker {self._resource_id!r}: dispatch refused in state {self._state}",
                from_state=self._state,
                resource_id=self._resource_id,
            )

        adapter = self._adapters[adapter_name]
        self._metrics.observe_command_accepted()
        failed = True
        try:
            # asyncio.shield: even if this _dispatch_impl coroutine itself
            # is cancelled (e.g. by a task-group cancel during disarm),
            # adapter.command runs to completion. Cancellation propagates
            # to the caller's future but not into the hardware transaction.
            result = await asyncio.shield(adapter.command(cmd))
            failed = False
            return result
        finally:
            self._metrics.observe_command_completed(failed=failed)

    async def _snapshot_impl(self, adapter_name: str) -> DeviceEmission:
        """Worker-side ``adapter.snapshot()``.

        Same state-gate as ``dispatch_impl``. No shield: snapshots are
        read-only and have no half-transaction failure mode.
        """
        if self._state in (WorkerState.DRAINING, WorkerState.CLOSED):
            raise WorkerStateError(
                f"Worker {self._resource_id!r}: snapshot refused in state {self._state}",
                from_state=self._state,
                resource_id=self._resource_id,
            )
        adapter = self._adapters[adapter_name]
        return await adapter.snapshot()

    async def _camera_metadata_impl(self, adapter_name: str) -> WebcamMetadata | None:
        """Worker-side metadata probe.

        Non-camera adapters and IR cameras both return ``None`` — the
        wrapper's :meth:`CameraDeviceAdapter.camera_metadata` already
        ``getattr``-probes for ``snapshot_metadata``. Adapters that
        aren't a :class:`CameraDeviceAdapter` at all (Watlow, Alicat)
        fall through the same way and return ``None``.

        No state-gate (read-only against pool-open data). No shield (no
        I/O — pure attribute read).
        """
        adapter = self._adapters[adapter_name]
        probe = getattr(adapter, "camera_metadata", None)
        if not callable(probe):
            return None
        result = probe()
        if not isinstance(result, WebcamMetadata):
            return None
        return result

    async def _device_readback_impl(self, adapter_name: str) -> Any:
        """Worker-side ``adapter.read_state_snapshot()`` probe.

        Adapters that don't implement the duck-typed coroutine return
        ``None``. State-gated (DRAINING / CLOSED reject) because this
        does I/O against the device — see :meth:`device_readback`.
        """
        if self._state in (WorkerState.DRAINING, WorkerState.CLOSED):
            return None
        adapter = self._adapters[adapter_name]
        probe = getattr(adapter, "read_state_snapshot", None)
        if not callable(probe):
            return None
        return await probe()


def _failed_future(exc: BaseException) -> Future[Any]:
    """Build a future that is already in the failed state.

    Useful for sync-facade methods that need to surface a precondition
    failure without crossing the thread seam — the caller awaits the
    future and observes the same exception they would observe from any
    other failed dispatch.
    """
    f: Future[Any] = Future()
    f.set_exception(exc)
    return f


def _as_camera_adapter(adapter: DeviceAdapter) -> CameraDeviceAdapter | None:
    """Return ``adapter`` as a camera wrapper when it is one.

    ``CameraDeviceAdapter.start(run_context)`` is intentionally more specific
    than the generic ``DeviceAdapter.start()`` protocol; the worker dispatches
    the correct call shape in :meth:`_adapter_start`. Narrow through
    ``object`` so mypy does not treat this runtime wrapper check as
    unreachable because of that one signature difference.
    """
    adapter_obj: object = adapter
    if isinstance(adapter_obj, CameraDeviceAdapter):
        return adapter_obj
    return None


__all__ = ["Worker"]
