""":class:`RunController` — adapter between :class:`Conductor` and Qt.

Migration doc §3 / §3.7 / §4.9. The controller owns:

* a long-lived :class:`WorkerPool` opened on config-load and reopened on
  config-reload (replaces the legacy :class:`DeviceRegistry`);
* a :class:`Conductor` constructed per-run (replaces
  :class:`ExperimentEngine`);
* a :class:`ThreadBridge` carrying :class:`WorkerEmission`\\ s from the
  conductor's thread back to the UI's qasync loop, drained into the same
  :class:`RingBufferRegistry` / Qt signals the legacy UI already
  consumes;
* the :class:`ManualClient` UI cards dispatch through — it routes to the
  conductor while a run is armed (records into the bundle, gates by
  state) or to the pool when no run exists.

Threading: qasync makes the asyncio loop the same thread as Qt's main
thread, so UI-side signal emits go direct. Conductor signals fire on the
conductor's thread; we re-emit them onto the Qt loop via
:meth:`Conductor.state` polling inside our own state-change machinery
(the conductor's state is an atomic int read; we surface transitions
into a :class:`RunUiState` enum on the controller's signals).
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import structlog
from PySide6.QtCore import QObject, Signal

from capa.core.plugins_lock import PluginsLock
from capa.core.ringbuffer import RingBufferRegistry
from capa.devices.camera.base import CameraEvent, FrameReceipt
from capa.devices.records import ChannelSample, DeviceEvent
from capa.experiment.config import ExperimentConfig
from capa.runtime.bridge import BridgePolicy, ThreadBridge, ThreadBridgeClosedError
from capa.runtime.camera_adapter import CameraDeviceAdapter
from capa.runtime.conductor import (
    Conductor,
    ConductorConfig,
    RunOutcome,
    RunResult,
)
from capa.runtime.dispatch import ManualClient, PoolDispatcher
from capa.runtime.emissions import WorkerEmission
from capa.runtime.lifecycle import PoolState
from capa.runtime.pool import WorkerPool
from capa.runtime.preview import PreviewFrame
from capa.runtime.session import RealRunSession
from capa.runtime.state import ConductorState
from capa.storage.catalog import RunCatalog
from capa.storage.manifest import BundleManifest
from capa.ui.async_util import schedule_bg

if TYPE_CHECKING:
    from capa.runtime.conductor import RunSession
    from capa.runtime.runcontext import RunContext

UI_BRIDGE_CAPACITY: Final[int] = 4096
"""Capacity of the Conductor → UI :class:`ThreadBridge`. Migration doc
§7.2 ``ui_bridge_capacity``. DROP_OLDEST policy so the conductor loop
never blocks on a slow UI subscriber."""

ABORT_GRACE_S: Final[float] = 5.0
"""Maximum time the conductor's disarm phase will wait for workers to
finish their stream tasks. Matches :data:`ConductorConfig.shutdown_grace_s`."""

_logger = structlog.get_logger("capa.ui.controller")


# ---------------------------------------------------------------------------
# UI-facing state vocabulary
# ---------------------------------------------------------------------------


class RunUiState(enum.StrEnum):
    """UI-facing state of the controller.

    Mirrors :class:`~capa.runtime.state.ConductorState` plus an ``IDLE``
    sentinel for the between-runs / no-conductor case. The legacy
    :class:`EngineState` had the same shape; consumers (status bar, run
    tab badge, manual-card gate) read this enum rather than reaching into
    the runtime's state directly.
    """

    IDLE = "idle"
    """No conductor exists. Pool may be open (between runs) or closed
    (before first config-load)."""

    PREPARING = "preparing"
    """Conductor is opening the session and arming workers."""

    RUNNING = "running"
    """Procedure is running; samples flowing; manual commands route
    through the conductor."""

    DRAINING = "draining"
    """Procedure ended or operator stopped; workers are disarming.
    Manual writes refused."""

    FINALIZING = "finalizing"
    """Bundle is being finalized (Parquet rewrite, manifest seal)."""

    SEALED = "sealed"
    """Bundle finalized cleanly. The previous run's result is
    inspectable."""

    FAILED = "failed"
    """Bundle finalize itself failed or the run never reached
    SAMPLING (preflight refusal, pool-open failure)."""


# State sets used by the manual-card write gate. The legacy ``EngineState``
# treated PREPARING / RUNNING / ABORTING / FINALIZING as write-blocked;
# DRAINING is the ConductorState equivalent of the old ABORTING phase.
_WRITE_BLOCKED_STATES: Final[frozenset[RunUiState]] = frozenset(
    {
        RunUiState.PREPARING,
        RunUiState.RUNNING,
        RunUiState.DRAINING,
        RunUiState.FINALIZING,
    }
)


def _conductor_state_to_ui(state: ConductorState) -> RunUiState:
    """Map :class:`ConductorState` to the UI enum.

    The names line up 1:1 — :class:`ConductorState` doesn't have an IDLE
    member (a conductor doesn't exist when no run is armed), so the
    caller is responsible for emitting :data:`RunUiState.IDLE` when
    ``self._conductor is None``.
    """
    return RunUiState(state.value)


# ---------------------------------------------------------------------------
# UI-facing result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunUiResult:
    """Outcome of one run as the UI consumes it.

    Migration doc §3.5: the conductor's :class:`RunResult` carries a
    runtime-typed enum (:class:`RunOutcome`) and the conductor's terminal
    state. The UI's status bar and run-tab readouts want strings —
    matching the legacy ``EngineResult`` shape — so we translate at the
    controller boundary.
    """

    run_id: str
    bundle_path: Path | None
    run_status: str
    """One of ``"completed"`` / ``"aborted"`` / ``"crashed"``."""
    bundle_status: str
    """``"sealed"`` / ``"open"`` / ``"verification_failed"`` (read off the
    bundle manifest), or ``"unknown"`` if the manifest can't be read."""
    integrity_status: str
    """``"ok"`` / ``"verification_failed"`` / ``"unknown"`` — mirrors the
    manifest's ``integrity.status`` field."""
    exit_reason: str | None = None


def _outcome_to_run_status(outcome: RunOutcome) -> str:
    match outcome:
        case RunOutcome.COMPLETED:
            return "completed"
        case RunOutcome.ABORTED:
            return "aborted"
        case RunOutcome.CRASHED | RunOutcome.CRASHED_BUT_SEALED:
            return "crashed"


def _read_bundle_status(bundle_path: Path | None) -> tuple[str, str]:
    if bundle_path is None:
        return "open", "unknown"
    try:
        manifest = BundleManifest.read(bundle_path / "manifest.json")
        return str(manifest.bundle_status), str(manifest.integrity.status)
    except Exception:
        return "open", "unknown"


# ---------------------------------------------------------------------------
# RunController
# ---------------------------------------------------------------------------


class RunController(QObject):
    """Owns one :class:`Conductor` per run and one :class:`WorkerPool`
    across the lifetime of a loaded config.

    Construct once per :class:`MainWindow`. :meth:`set_active_config`
    swaps the pool on config-load; :meth:`start` builds a fresh
    conductor per run; :meth:`aclose_pool` releases the pool on app-quit.

    Signals (all fire on the Qt main thread):

    * :attr:`state_changed` — every :class:`RunUiState` transition,
      including the synthetic ``IDLE`` reset between runs.
    * :attr:`event_received` — :class:`DeviceEvent` /
      :class:`CameraEvent` instances drained from the UI bridge, for
      the events dock.
    * :attr:`run_finished` — :class:`RunUiResult` once the run is sealed
      (or failed).
    * :attr:`pool_changed` — fires with the active :class:`WorkerPool`
      when a config loads, or ``None`` when the pool is closed. Manual-
      control dock listens for this to rebuild cards.
    * :attr:`manual_event` — manual-command events synthesized by the
      cards on each dispatch (out-of-run only; in-run commands go
      through the conductor and appear via :attr:`event_received`).
    """

    state_changed = Signal(object)
    event_received = Signal(object)
    camera_event_received = Signal(object)
    """Camera-only event drained from the UI bridge. Routed to the
    camera-preview dock for live border-color reactions."""
    preview_received = Signal(str, bytes)
    """``(camera_name, jpeg_bytes)`` — emitted by per-camera preview
    drain tasks at the adapter's throttled cadence."""
    run_finished = Signal(object)
    manual_event = Signal(object)
    """``DeviceEvent`` — synthesized by manual-control cards on each
    out-of-run command dispatch. In-run dispatches surface as regular
    events via :attr:`event_received`."""
    pool_changed = Signal(object)
    """``WorkerPool | None`` — fires when the pool is rebuilt on
    config-load or cleared on aclose."""

    def __init__(
        self,
        *,
        runs_root: Path,
        plugins_lock: PluginsLock | None = None,
        catalog: RunCatalog | None = None,
        repo_root: Path | None = None,
        lockfile_source: Path | None = None,
        configure_logging_for_bundle: bool = True,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._runs_root: Path = runs_root
        self._plugins_lock: PluginsLock | None = plugins_lock
        self._catalog: RunCatalog | None = catalog
        self._repo_root: Path | None = repo_root
        self._lockfile_source: Path | None = lockfile_source
        self._configure_logging_for_bundle: bool = configure_logging_for_bundle

        self._worker_pool: WorkerPool | None = None
        self._active_config: ExperimentConfig | None = None
        self._conductor: Conductor | None = None
        self._task: asyncio.Task[None] | None = None
        self._buffers: RingBufferRegistry = RingBufferRegistry()
        self._last_result: RunUiResult | None = None
        self._ui_state: RunUiState = RunUiState.IDLE
        self._state_poll_task: asyncio.Task[None] | None = None

        # ManualClient — single facade for UI manual cards. Built lazily
        # on first pool open; reconstructed if the pool is swapped.
        self._manual_client: ManualClient | None = None

        # Per-camera preview drainer tasks. One task per camera, alive
        # for the full pool lifetime. Spawned in :meth:`_open_pool`
        # after the pool transitions to OPEN; cancelled in
        # :meth:`_stop_preview_drainers` on pool-close.
        self._preview_drainers: dict[str, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------ properties

    @property
    def buffers(self) -> RingBufferRegistry:
        """Ring buffers for the active run. Cleared and re-registered on
        each :meth:`start`."""
        return self._buffers

    @property
    def state(self) -> RunUiState:
        return self._ui_state

    @property
    def last_result(self) -> RunUiResult | None:
        return self._last_result

    @property
    def is_active(self) -> bool:
        """``True`` while a run is between Start and the SEALED/FAILED
        terminal state."""
        return self._task is not None and not self._task.done()

    @property
    def worker_pool(self) -> WorkerPool | None:
        """Live :class:`WorkerPool` bound to the currently-loaded config,
        or ``None`` if no config is loaded."""
        return self._worker_pool

    @property
    def manual_client(self) -> ManualClient | None:
        """Single :class:`ManualClient` for UI cards. ``None`` until
        :meth:`set_active_config` has been called (which builds the
        pool)."""
        return self._manual_client

    @property
    def conductor(self) -> Conductor | None:
        """Active conductor instance, or ``None`` when no run is in
        flight."""
        return self._conductor

    @property
    def active_config(self) -> ExperimentConfig | None:
        return self._active_config

    # ------------------------------------------------------------------ config lifecycle

    def set_active_config(self, config: ExperimentConfig) -> None:
        """Bind a freshly-loaded config: rebuild the :class:`WorkerPool`.

        Builds a new pool synchronously and schedules its async
        :meth:`WorkerPool.open` on the qasync loop. The old pool (if any)
        is closed via :func:`schedule_bg` — fire-and-forget so the UI
        does not block while serial ports release.

        The new pool is published via :attr:`pool_changed` once
        :meth:`WorkerPool.open` resolves, so manual cards know not to
        dispatch against a half-open pool. Until then dispatches will
        raise :class:`UnknownDeviceError` (no worker registered yet) and
        cards surface the failure in their inline status label.
        """
        old_pool = self._worker_pool
        # set_active_config runs on the qasync loop (called from a UI
        # signal handler), so the running loop is the consumer loop for
        # preview bridges. Pass it so from_config wires the bridges
        # against the right loop.
        try:
            ui_loop = asyncio.get_event_loop()
        except RuntimeError:
            ui_loop = None
        try:
            new_pool = WorkerPool.from_config(
                config,
                preview_consumer_loop=ui_loop,
            )
        except Exception as exc:
            _logger.error(
                "ui.controller.pool_build_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            # Leave the previous pool in place; surface to the menu/dialog
            # caller by re-raising.
            raise
        self._active_config = config
        self._worker_pool = new_pool
        self._manual_client = ManualClient(
            pool=new_pool,
            conductor_provider=lambda: self._conductor,
        )

        # Spawn an async open + signal. The pool_changed signal fires
        # once on completion (success or failure); subscribers should
        # tolerate it firing on the next event-loop tick rather than
        # synchronously.
        schedule_bg(self._open_pool(new_pool, old_pool))

    async def _open_pool(
        self,
        new_pool: WorkerPool,
        old_pool: WorkerPool | None,
    ) -> None:
        # 1. Tear down old pool's preview machinery FIRST: drainers
        #    reference bridges that pool.close() will invalidate.
        if old_pool is not None:
            await self._stop_preview_drainers()
            try:
                await old_pool.close()
            except Exception as exc:
                _logger.warning(
                    "ui.controller.old_pool_close_failed",
                    error=str(exc),
                )

        # 2. Attach preview consumers BEFORE pool.open. The worker
        #    threads attach producers inside _open_all_impl; the
        #    consumer-side asyncio.Queue must already exist or the
        #    producer's first put would crash on a None queue.
        new_pool.attach_preview_consumers()

        # 3. Open the pool. On failure, close the (still-attached)
        #    bridges so any pending consumer-side iteration unwinds.
        try:
            await new_pool.open()
        except Exception as exc:
            _logger.error(
                "ui.controller.pool_open_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            new_pool.close_preview_bridges()
            self._worker_pool = None
            self._manual_client = None
            self.pool_changed.emit(None)
            return

        # 4. Spawn one UI-side drainer per preview bridge. Each loops
        #    over PreviewFrame items and re-emits preview_received on
        #    the Qt thread. Lifetime = until close_preview_bridges
        #    fires ThreadBridgeClosedError or _stop_preview_drainers
        #    cancels.
        self._preview_drainers = {
            name: asyncio.create_task(
                self._drain_preview(bridge),
                name=f"ui-preview-{name}",
            )
            for name, bridge in new_pool.preview_bridges().items()
        }
        self.pool_changed.emit(new_pool)

    async def _drain_preview(self, bridge: ThreadBridge[PreviewFrame]) -> None:
        """Drain one preview bridge; emit :attr:`preview_received` per frame.

        Exits cleanly when the bridge is closed (ThreadBridgeClosedError
        or natural iterator exhaustion) or when the task is cancelled.
        Per-frame exceptions are logged and the loop continues — a
        decode failure in a Qt slot must not kill the long-lived drain.
        """
        try:
            async for pf in bridge:
                try:
                    self.preview_received.emit(pf.name, pf.jpeg)
                except Exception as exc:
                    _logger.warning(
                        "ui.controller.preview_emit_failed",
                        camera=pf.name,
                        error=str(exc),
                    )
        except ThreadBridgeClosedError:
            return

    async def _stop_preview_drainers(self) -> None:
        """Cancel every preview drainer task and await its exit.

        Called by :meth:`_open_pool` (before swapping pools) and by
        :meth:`aclose_pool`. Safe to call when no drainers are active.
        """
        drainers = self._preview_drainers
        if not drainers:
            return
        self._preview_drainers = {}
        for task in drainers.values():
            task.cancel()
        await asyncio.gather(*drainers.values(), return_exceptions=True)

    async def aclose_pool(self) -> None:
        """Close the active pool and clear the binding. Used by
        :meth:`MainWindow.closeEvent` to release serial ports cleanly on
        app-quit. Safe to call when no pool is bound."""
        old = self._worker_pool
        self._worker_pool = None
        self._manual_client = None
        self._active_config = None
        self.pool_changed.emit(None)
        # Cancel UI-side preview drainers before pool.close so they
        # don't observe a half-closed bridge mid-iteration.
        await self._stop_preview_drainers()
        if old is not None:
            with contextlib.suppress(Exception):
                await old.close()

    def emit_manual_event(self, event: DeviceEvent) -> None:
        """Surface a manual-command :class:`DeviceEvent` to the events dock."""
        self.manual_event.emit(event)

    # ------------------------------------------------------------------ control

    def start(self, config: ExperimentConfig) -> None:
        """Schedule a new run on the asyncio loop. Returns immediately.

        Raises :class:`RuntimeError` if a run is already in flight, no
        pool has been built yet, or the pool's async ``open()`` has not
        finished — the UI is expected to disable the Start button until
        :attr:`pool_changed` fires with a non-``None`` pool, but we also
        guard here so a stale callback can't slip a run past an
        :class:`PoolState.OPENING` pool (which would crash inside
        ``pool.arm_all`` with a :class:`PoolStateError`).
        """
        if self.is_active:
            raise RuntimeError("a run is already active; abort it first")
        pool = self._worker_pool
        if pool is None:
            raise RuntimeError("no config loaded — open a config first")
        if pool.state is not PoolState.OPEN:
            raise RuntimeError(
                f"worker pool is {pool.state.value!r}; wait for the config "
                "to finish loading before starting a run"
            )
        loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._run(config))

    def request_abort(self, *, mode: str = "safe_shutdown") -> None:
        """Forward the abort request to the active conductor. No-op if no
        run is active. ``mode`` is kept for API compatibility with the
        legacy engine; the conductor's :meth:`Conductor.stop` does the
        same thing regardless of mode (the procedure executor's
        ``SafeShutdownStep`` runs unconditionally before disarm)."""
        conductor = self._conductor
        if conductor is None:
            return
        conductor.stop(reason=f"operator_{mode}")

    async def await_active_run(self) -> None:
        """Await the in-flight :meth:`_run` task, if any.

        Used by :meth:`MainWindow.closeEvent` to block app-quit until the
        conductor has actually finished disarming workers — otherwise
        :meth:`aclose_pool` races the conductor and finds workers still
        SAMPLING, :class:`PoolStateError`-fails, and leaks the worker
        :class:`ThreadedRunner` threads (which are non-daemon by design).
        """
        task = self._task
        if task is None or task.done():
            return
        with contextlib.suppress(BaseException):
            await task

    # ------------------------------------------------------------------ internal

    async def _run(self, config: ExperimentConfig) -> None:
        """Build the conductor stack and drive one run end-to-end."""
        pool = self._worker_pool
        assert pool is not None, "start() guarded by pool presence"

        # Reset transient state so the new run starts clean.
        self._buffers = RingBufferRegistry()
        for ch in config.hardware.channels:
            self._buffers.register(ch.name, decimate_to_hz=ch.decimate_to_hz)

        # Snapshot adapter handles for the session's equipment-identity
        # block. Mirrors the headless flow in
        # :func:`capa.runtime.headless.run_headless`.
        adapter_by_device: dict[str, Any] = {}
        adapter_by_camera: dict[str, Any] = {}
        for worker in pool.workers.values():
            for name, adapter in worker.adapters.items():
                adapter_obj: object = adapter
                if isinstance(adapter_obj, CameraDeviceAdapter):
                    adapter_by_camera[name] = adapter_obj.camera
                else:
                    adapter_by_device[name] = adapter

        session = RealRunSession(
            config=config,
            runs_root=self._runs_root,
            plugins_lock=self._plugins_lock,
            repo_root=self._repo_root,
            lockfile_source=self._lockfile_source,
            adapter_by_device=adapter_by_device,
            adapter_by_camera=adapter_by_camera,
            catalog=self._catalog,
        )

        # Build the conductor with a deferred runner factory. The factory
        # creates a :class:`ProcedureRunner` that captures the conductor's
        # databus so the procedure's ``_wait_for`` subscribers see live
        # samples. Mirrors :func:`run_headless`'s factory.
        _conductor_holder: list[Conductor] = []

        def _runner_factory(s: RunSession, ctx: RunContext) -> Any:
            # Local imports keep startup cheap when no UI run has been
            # triggered yet.
            from capa.channels.registry import ChannelRegistry  # noqa: PLC0415
            from capa.experiment.executor import MethodExecutor  # noqa: PLC0415
            from capa.experiment.procedures.builtin.batch import Batch as _Batch  # noqa: PLC0415
            from capa.runtime.headless import (  # noqa: PLC0415
                _build_method_executor_for_runner,
            )
            from capa.runtime.procedure import ProcedureRunner  # noqa: PLC0415

            assert isinstance(s, RealRunSession)
            procedure = _resolve_procedure(config, self._plugins_lock)
            if isinstance(procedure, _Batch):
                procedure.configure_runs_root(self._runs_root)

            channel_registry = ChannelRegistry.from_specs(list(config.hardware.channels))
            channel_registry.freeze()
            dispatcher = PoolDispatcher(pool)

            method_executor: MethodExecutor | None = None
            if config.method is not None:
                assert _conductor_holder, "runner_factory invoked before conductor was holdered"
                bus = _conductor_holder[0].databus
                assert bus is not None
                method_executor = _build_method_executor_for_runner(
                    config=config,
                    clock=s.clock,
                    bundle_writer=s.bundle_writer,
                    databus=bus,
                    channel_registry=channel_registry,
                    adapter_by_device=adapter_by_device,
                    dispatcher=dispatcher,
                    authorization=s.authorization,
                )

            stop_signal: asyncio.Event | None = None
            if _conductor_holder:
                stop_signal = _conductor_holder[0].completion_event

            return ProcedureRunner(
                procedure=procedure,
                config=config,
                channel_registry=channel_registry,
                dispatcher=dispatcher,
                authorization=s.authorization,
                adapters=adapter_by_device,
                bundle_writer=s.bundle_writer,
                method_executor=method_executor,
                stop_signal=stop_signal,
            )

        conductor = Conductor(
            pool=pool,
            session=session,
            runner_factory=_runner_factory,
            config=ConductorConfig(shutdown_grace_s=ABORT_GRACE_S),
        )
        _conductor_holder.append(conductor)
        self._conductor = conductor

        # UI bridge — Conductor → UI thread channel. DROP_OLDEST so the
        # conductor never blocks on UI subscribers.
        ui_bridge: ThreadBridge[WorkerEmission] = ThreadBridge(
            name="ui",
            capacity=UI_BRIDGE_CAPACITY,
            consumer_loop=asyncio.get_running_loop(),
            policy=BridgePolicy.DROP_OLDEST,
        )
        ui_bridge.attach_consumer()
        conductor.attach_ui_bridge(ui_bridge)

        # State-polling task surfaces conductor transitions as Qt signals.
        self._set_ui_state(RunUiState.PREPARING)
        self._state_poll_task = asyncio.create_task(
            self._poll_conductor_state(conductor),
            name="ui-state-poll",
        )

        # Start the conductor (spawns its own thread + loop) and drain
        # the UI bridge until the conductor signals SEALED/FAILED.
        conductor.start()
        try:
            await self._drain_ui_bridge(ui_bridge)
            # Block until conductor finishes — drain may exit first if
            # the bridge closes before the result lands.
            result: RunResult = await asyncio.wrap_future(conductor.result_future)
            conductor.join(timeout=5.0)
        except BaseException:
            _logger.exception("ui.controller.run_failed")
            with contextlib.suppress(BaseException):
                conductor.stop(reason="ui_crash")
            raise
        finally:
            # Stop the state-poll task; the conductor has already
            # transitioned to SEALED or FAILED.
            if self._state_poll_task is not None:
                self._state_poll_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._state_poll_task
                self._state_poll_task = None
            self._conductor = None
            # Final state transition: read the conductor's terminal state
            # one more time and translate (FAILED bypasses SEALED).
            self._set_ui_state(_conductor_state_to_ui(conductor.state))

        ui_result = _translate_result(result)
        self._last_result = ui_result
        self.run_finished.emit(ui_result)

    async def _drain_ui_bridge(self, bridge: ThreadBridge[WorkerEmission]) -> None:
        """Pump emissions from the UI bridge into the UI ringbuffer +
        Qt signals.

        Returns when the bridge closes (conductor finished) or when
        :class:`ThreadBridgeClosedError` is raised from a producer-side
        race during teardown. Exceptions are logged and swallowed so a
        single bad emission does not kill the run.
        """
        try:
            async for emission in bridge:
                try:
                    self._dispatch_ui_emission(emission)
                except Exception as exc:
                    _logger.warning(
                        "ui.controller.emission_dispatch_failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
        except ThreadBridgeClosedError:
            return

    def _dispatch_ui_emission(self, emission: WorkerEmission) -> None:
        """Route one emission to the correct UI sink.

        * :class:`ChannelSample` → ring buffer (plots + numerics dock).
        * :class:`DeviceEvent` → :attr:`event_received` signal (events dock).
        * :class:`CameraEvent` → :attr:`camera_event_received` (preview dock).
        * :class:`FrameReceipt` → dropped at the UI boundary; frame
          receipts are a durable-only artifact (parquet frame index).
          Preview JPEGs travel a parallel channel: per-camera
          :class:`~capa.runtime.bridge.ThreadBridge` owned by the pool
          and drained by :meth:`_drain_preview`, which fires
          :attr:`preview_received`.
        """
        if isinstance(emission, ChannelSample):
            self._buffers.push(emission)
            return
        if isinstance(emission, DeviceEvent):
            self.event_received.emit(emission)
            return
        if isinstance(emission, CameraEvent):
            self.camera_event_received.emit(emission)
            return
        if isinstance(emission, FrameReceipt):
            # Frame receipts are durable-only at the UI boundary —
            # preview JPEGs are delivered via :attr:`preview_received`
            # from the per-camera preview bridges (migration doc §6.2:
            # the camera-side ``preview_stream()`` surface is unchanged;
            # only the cross-thread plumbing is new).
            return

    async def _poll_conductor_state(self, conductor: Conductor) -> None:
        """Surface conductor state transitions as Qt signals.

        :class:`Conductor` doesn't expose a state-change callback (its
        thread layout makes one awkward); instead we poll the atomic
        ``state`` field at 200 Hz. The poll rate is chosen to catch
        short-lived transient states (FINALIZING typically lasts a few
        tens of ms on sim runs); the signal cost is the cheapest path —
        an enum compare plus a `state_changed` emit only when the value
        changes. CPU cost of the polling loop itself is negligible
        relative to the conductor's drain throughput.
        """
        prev = conductor.state
        try:
            while True:
                await asyncio.sleep(0.005)
                current = conductor.state
                if current != prev:
                    prev = current
                    self._set_ui_state(_conductor_state_to_ui(current))
                if current in (ConductorState.SEALED, ConductorState.FAILED):
                    return
        except asyncio.CancelledError:
            return

    def _set_ui_state(self, state: RunUiState) -> None:
        if state == self._ui_state:
            return
        self._ui_state = state
        self.state_changed.emit(state)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_procedure(
    config: ExperimentConfig,
    plugins_lock: PluginsLock | None,
) -> Any:
    """Resolve the procedure plugin from the config + plugin trust state.

    Mirrors :func:`capa.runtime.headless.run_headless`'s preflight step.
    Raised :class:`ProcedureError` is allowed to propagate — the caller
    surfaces it via the conductor's failure path.
    """
    from capa.core.plugins_runtime import ProcedureRegistry, resolve_mode  # noqa: PLC0415
    from capa.experiment.procedures.base import ProcedureError  # noqa: PLC0415

    plugin_mode = resolve_mode()
    registry = ProcedureRegistry.discover(plugins_lock=plugins_lock, mode=plugin_mode)
    plugin_id = config.procedure.id
    if plugin_id not in registry:
        available = ", ".join(registry.ids()) or "<none>"
        raise ProcedureError(
            f"procedure {plugin_id!r} is not in the trusted registry "
            f"(mode={plugin_mode}); available: {available}"
        )
    return registry.instantiate(plugin_id, config.procedure.config)


def _translate_result(result: RunResult) -> RunUiResult:
    bundle_status, integrity_status = _read_bundle_status(result.bundle_path)
    return RunUiResult(
        run_id=result.run_id,
        bundle_path=result.bundle_path,
        run_status=_outcome_to_run_status(result.outcome),
        bundle_status=bundle_status,
        integrity_status=integrity_status,
        exit_reason=result.exit_reason,
    )


# Re-exported so callers don't need to import RunUiState from this module
# to type a slot signature.
StateSlot = Callable[[RunUiState], None]


__all__ = [
    "ABORT_GRACE_S",
    "UI_BRIDGE_CAPACITY",
    "RunController",
    "RunUiResult",
    "RunUiState",
    "StateSlot",
]
