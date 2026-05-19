""":class:`RunController` — adapter between :class:`Conductor` and Qt.

The controller owns:

* a long-lived :class:`WorkerPool` opened on config-load and reopened on
  config-reload;
* a :class:`Conductor` constructed per-run;
* a :class:`ThreadBridge` carrying :class:`WorkerEmission`\\ s from the
  conductor's thread back to the UI's qasync loop, drained into the
  :class:`RingBufferRegistry` / Qt signals the docks consume;
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
    RunResult,
)
from capa.runtime.dispatch import ManualClient, PoolDispatcher
from capa.runtime.emissions import ProcedureTick, WorkerEmission
from capa.runtime.lifecycle import PoolState
from capa.runtime.outcomes import read_bundle_status, run_status_for_outcome
from capa.runtime.pool import WorkerPool
from capa.runtime.preview import PreviewFrame
from capa.runtime.progress import DeviceInitProgress, DeviceInitStatus
from capa.runtime.session import RealRunSession
from capa.runtime.shutdown import PoolCloseResult
from capa.runtime.state import ConductorState
from capa.storage.catalog import RunCatalog
from capa.ui.config_progress import ConfigLoadProgress, ConfigLoadState
from capa.ui.lifecycle import LifecycleKind, LifecycleRegistry

if TYPE_CHECKING:
    from capa.runtime.conductor import RunSession
    from capa.runtime.runcontext import RunContext

UI_BRIDGE_CAPACITY: Final[int] = 4096
"""Fallback capacity of the Conductor → UI :class:`ThreadBridge`.

The active value at run-start is :attr:`RuntimeConfig.ui_bridge_capacity`
off the loaded :class:`ExperimentConfig`; this constant is retained only
for legacy/test paths that build a bridge without a config in scope.
``DROP_OLDEST`` policy keeps the conductor loop from ever blocking on a
slow UI subscriber."""

ABORT_GRACE_S: Final[float] = 5.0
"""Fallback maximum time the conductor's disarm stage will wait for
workers to finish their stream tasks. The active value comes from
:attr:`RuntimeConfig.shutdown_grace_s` via :meth:`ConductorConfig.from_runtime`."""

_logger = structlog.get_logger("capa.ui.controller")


def _running_loop_or_none() -> asyncio.AbstractEventLoop | None:
    """Return the running asyncio loop, or ``None`` when no loop is
    running.

    The controller's :meth:`set_active_config` runs from a Qt slot
    under qasync in production (so the qasync loop is the running
    loop). Tests that construct the controller without qasync are
    exercising pure bookkeeping paths — they don't need lifecycle
    tasks to actually run. Returning ``None`` lets those paths skip
    scheduling without raising.
    """
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


# ---------------------------------------------------------------------------
# UI-facing state vocabulary
# ---------------------------------------------------------------------------


class RunUiState(enum.StrEnum):
    """UI-facing state of the controller.

    Mirrors :class:`~capa.runtime.state.ConductorState` plus an ``IDLE``
    sentinel for the between-runs / no-conductor case. Consumers (status
    bar, run tab badge, manual-card gate) read this enum rather than
    reaching into the runtime's state directly.
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


# State sets used by the manual-card write gate.
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

    The conductor's :class:`RunResult` carries a runtime-typed enum
    (:class:`RunOutcome`) and the conductor's terminal state. The UI's
    status bar and run-tab readouts want strings, so we translate at
    the controller boundary.
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
    procedure_tick_received = Signal(object)
    """``ProcedureTick`` — emitted whenever a long-running procedure
    publishes a live-numerics tick onto the UI sink. Procedure-id-
    filtered on the consumer side (each dock subscribes and ignores
    ticks from other procedures)."""
    run_finished = Signal(object)
    manual_event = Signal(object)
    """``DeviceEvent`` — synthesized by manual-control cards on each
    out-of-run command dispatch. In-run dispatches surface as regular
    events via :attr:`event_received`."""
    pool_changed = Signal(object)
    """``WorkerPool | None`` — fires when the pool is rebuilt on
    config-load or cleared on aclose."""

    config_load_started = Signal(object)
    """``ConfigLoadProgress`` emitted when a config begins hardware prep."""
    config_load_progress = Signal(object)
    """``ConfigLoadProgress`` emitted as devices move through open states."""
    config_load_finished = Signal(object)
    """Terminal ``ConfigLoadProgress`` for ready or failed initialization."""
    hardware_ready_changed = Signal(bool)
    """``True`` when the loaded config's worker pool is open and usable."""

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
        # Set by the conductor's runner_factory when a run is launched;
        # cleared at run-completion cleanup. Exposed to the UI through
        # :meth:`send_operator_command` and :attr:`active_procedure_id`
        # so per-procedure docks (e.g. HeatFluxTuneDock) can push
        # operator commands and visibility-gate on the active procedure.
        self._active_procedure_runner: Any = None
        self._active_run_config: ExperimentConfig | None = None
        self._buffers: RingBufferRegistry = RingBufferRegistry()
        self._last_result: RunUiResult | None = None
        self._ui_state: RunUiState = RunUiState.IDLE
        self._state_poll_task: asyncio.Task[None] | None = None
        self._hardware_ready: bool = False
        self._config_load_state: ConfigLoadState = ConfigLoadState.IDLE
        self._config_load_path: Path | None = None
        self._config_load_rows: dict[str, DeviceInitProgress] = {}
        # Latched abort request from before the conductor exists.
        # ``request_abort()`` sets this when ``_conductor is None`` but
        # ``_task`` is still active (i.e. ``_run()`` is preparing the
        # conductor). ``_run()`` checks the latch immediately after
        # constructing the conductor and forwards a ``conductor.stop()``.
        self._pending_abort_reason: str | None = None
        # Hard gate set by the ShutdownCoordinator before it starts its
        # shutdown sequence. While true, ``start()``, ``set_active_config()``,
        # and ``request_abort()`` refuse new lifecycle-creating work so
        # the coordinator can drain the registry without a slot creating
        # fresh tasks behind its back.
        self._shutdown_requested: bool = False
        # Lifecycle-task registry. The coordinator iterates this
        # snapshot to know what to cancel/await.
        self._lifecycle: LifecycleRegistry = LifecycleRegistry()

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
        the current config's pool is open."""
        return self._manual_client

    @property
    def hardware_ready(self) -> bool:
        """``True`` once the current config's pool is open and dispatchable."""
        pool = self._worker_pool
        return self._hardware_ready or (pool is not None and pool.state is PoolState.OPEN)

    @property
    def config_load_state(self) -> ConfigLoadState:
        return self._config_load_state

    @property
    def conductor(self) -> Conductor | None:
        """Active conductor instance, or ``None`` when no run is in
        flight."""
        return self._conductor

    @property
    def active_config(self) -> ExperimentConfig | None:
        return self._active_config

    @property
    def lifecycle(self) -> LifecycleRegistry:
        """Live registry of lifecycle tasks. The
        :class:`~capa.ui.shutdown.ShutdownCoordinator` snapshots this in
        its CANCEL_LIFECYCLE_TASKS stage."""
        return self._lifecycle

    @property
    def shutdown_requested(self) -> bool:
        """``True`` once the ShutdownCoordinator has called
        :meth:`enter_shutdown_mode`. UI slots use this to refuse new
        lifecycle-creating work (Start, Open Config, manual writes)."""
        return self._shutdown_requested

    @property
    def active_run_id(self) -> str | None:
        """ID of the currently-running run, or ``None`` when idle. Read
        by the :class:`~capa.ui.shutdown.ShutdownCoordinator` into its
        diagnostic payload."""
        c = self._conductor
        return c.run_id if c is not None else None

    @property
    def active_bundle_path(self) -> Path | None:
        """Bundle path of the currently-running run, or ``None``. Read by
        the :class:`~capa.ui.shutdown.ShutdownCoordinator` into its
        hard-exit diagnostic payload so a wedged shutdown still names
        the recoverable bundle."""
        c = self._conductor
        return c.bundle_path if c is not None else None

    def enter_shutdown_mode(self) -> None:
        """Flip ``_shutdown_requested`` so subsequent ``start()`` /
        ``set_active_config()`` / ``request_abort()`` calls are gated.
        Idempotent. Called by the :class:`ShutdownCoordinator` at the
        DISABLE_UI stage."""
        self._shutdown_requested = True

    # ------------------------------------------------------------------ config lifecycle

    def _set_hardware_ready(self, ready: bool) -> None:
        if ready == self._hardware_ready:
            return
        self._hardware_ready = ready
        self.hardware_ready_changed.emit(ready)

    def _pending_progress_rows(self, pool: WorkerPool) -> dict[str, DeviceInitProgress]:
        rows: dict[str, DeviceInitProgress] = {}
        for worker in pool.workers.values():
            for adapter in worker.adapters.values():
                rows[adapter.name] = DeviceInitProgress(
                    name=adapter.name,
                    adapter=type(adapter).__name__,
                    resource_id=worker.resource_id,
                    status=DeviceInitStatus.PENDING,
                    detail="waiting",
                )
        return rows

    def _progress_snapshot(
        self,
        state: ConfigLoadState,
        message: str,
    ) -> ConfigLoadProgress:
        self._config_load_state = state
        return ConfigLoadProgress(
            state=state,
            message=message,
            path=self._config_load_path,
            devices=tuple(self._config_load_rows.values()),
        )

    def _emit_config_progress(
        self,
        state: ConfigLoadState,
        message: str,
    ) -> ConfigLoadProgress:
        progress = self._progress_snapshot(state, message)
        self.config_load_progress.emit(progress)
        return progress

    def _record_device_progress(self, row: DeviceInitProgress) -> None:
        self._config_load_rows[row.name] = row
        self.config_load_progress.emit(
            self._progress_snapshot(
                self._config_load_state,
                row.detail or row.status.value.replace("_", " "),
            )
        )

    def set_active_config(
        self,
        config: ExperimentConfig,
        *,
        config_path: Path | None = None,
    ) -> None:
        """Bind a freshly-loaded config: rebuild the :class:`WorkerPool`.

        Builds a new pool synchronously and schedules its async
        :meth:`WorkerPool.open` on the qasync loop as a registered
        lifecycle task. The old pool (if any) is closed via a separate
        registered :attr:`LifecycleKind.OLD_POOL_CLOSE` task so the
        shutdown coordinator can see both lifecycle tasks independently.

        The new pool is published via :attr:`pool_changed` once
        :meth:`WorkerPool.open` resolves, so manual cards know not to
        dispatch against a half-open pool. Until then dispatches will
        raise :class:`UnknownDeviceError` (no worker registered yet) and
        cards surface the failure in their inline status label.
        """
        if self._shutdown_requested:
            _logger.info("ui.controller.set_active_config_during_shutdown_ignored")
            return
        old_pool = self._worker_pool
        old_manual_client = self._manual_client
        old_ready = self._hardware_ready
        self._config_load_path = config_path
        self._config_load_rows = {}
        self._set_hardware_ready(False)
        self._manual_client = None
        self.config_load_started.emit(
            self._progress_snapshot(
                ConfigLoadState.BUILDING_POOL,
                "Building worker pool",
            )
        )
        # set_active_config runs on the qasync loop (called from a UI
        # signal handler), so the running loop is the consumer loop for
        # preview bridges. Pass it so from_config wires the bridges
        # against the right loop.
        try:
            ui_loop = asyncio.get_running_loop()
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
            self._manual_client = old_manual_client
            self._hardware_ready = old_ready
            self.hardware_ready_changed.emit(self.hardware_ready)
            failed = self._progress_snapshot(
                ConfigLoadState.FAILED,
                f"Worker pool build failed: {exc}",
            )
            self.config_load_finished.emit(failed)
            # Leave the previous pool in place; surface to the menu/dialog
            # caller by re-raising.
            raise
        self._active_config = config
        self._worker_pool = new_pool
        self._manual_client = None
        self._config_load_rows = self._pending_progress_rows(new_pool)
        self._emit_config_progress(
            ConfigLoadState.OPENING_DEVICES,
            "Opening devices",
        )

        # Old-pool close runs as its OWN registered task so the shutdown
        # coordinator can see it separately from the new pool's open.
        # The old pool's close must complete before the new pool's open
        # touches any shared serial bus, so the open task awaits the
        # close task's future explicitly (not via schedule order).
        #
        # ``_running_loop_or_none`` returns ``None`` in unit tests that
        # construct the controller outside qasync (manual-control card
        # tests, etc.) — in that case there's no loop to schedule on,
        # so we close the coroutine immediately. The exception is the
        # legitimate runtime path where ``set_active_config`` must
        # always be called from the qasync loop.
        loop = _running_loop_or_none()
        old_close_task: asyncio.Task[None] | None = None
        if old_pool is not None and loop is not None:
            old_close_task = loop.create_task(
                self._close_old_pool(old_pool),
                name="ui-old-pool-close",
            )
            self._lifecycle.register(
                LifecycleKind.OLD_POOL_CLOSE,
                "old-pool-close",
                old_close_task,
                critical=True,
            )

        # Pool open task is critical (the coordinator awaits it before
        # closing the pool — opening then immediately closing is the
        # well-defined case; cancelling mid-open could leak threads).
        if loop is not None:
            open_task = loop.create_task(
                self._open_pool(new_pool, old_close_task),
                name="ui-pool-open",
            )
            self._lifecycle.register(
                LifecycleKind.POOL_OPEN,
                "pool-open",
                open_task,
                critical=True,
            )
        else:
            # No loop running — close the coroutine to avoid the
            # "coroutine was never awaited" RuntimeWarning. Tests that
            # construct a controller without a loop are doing pure
            # bookkeeping work; they don't need the pool open task.
            self._open_pool(new_pool, old_close_task).close()

    async def _close_old_pool(self, old_pool: WorkerPool) -> None:
        """Tear down the previous config's pool before the new pool's
        ``open()`` touches a shared bus.

        Split out of :meth:`_open_pool` so the
        :class:`ShutdownCoordinator` can see this work as its own
        registered :attr:`LifecycleKind.OLD_POOL_CLOSE` entry instead of
        being buried inside the pool-open task.
        """
        await self._stop_preview_drainers()
        try:
            await old_pool.close()
        except Exception as exc:
            _logger.warning(
                "ui.controller.old_pool_close_failed",
                error=str(exc),
            )

    async def _open_pool(
        self,
        new_pool: WorkerPool,
        old_close_task: asyncio.Task[None] | None,
    ) -> None:
        # 1. Await the old-pool close task if any. We don't share
        #    teardown with the close task itself — that's its own
        #    registered lifecycle entry, but we MUST wait for it to
        #    finish before the new pool touches a shared serial bus.
        if old_close_task is not None:
            self._emit_config_progress(
                ConfigLoadState.CLOSING_PREVIOUS,
                "Closing previous hardware",
            )
            with contextlib.suppress(asyncio.CancelledError):
                await old_close_task
            self._emit_config_progress(
                ConfigLoadState.OPENING_DEVICES,
                "Opening devices",
            )

        # 2. Attach preview consumers BEFORE pool.open. The worker
        #    threads attach producers inside _open_all_impl; the
        #    consumer-side asyncio.Queue must already exist or the
        #    producer's first put would crash on a None queue.
        new_pool.attach_preview_consumers()

        # 3. Open the pool. On failure, close the (still-attached)
        #    bridges so any pending consumer-side iteration unwinds.
        ui_loop = asyncio.get_running_loop()

        def _progress_from_worker(row: DeviceInitProgress) -> None:
            ui_loop.call_soon_threadsafe(self._record_device_progress, row)

        try:
            await new_pool.open(progress_callback=_progress_from_worker)
        except Exception as exc:
            await asyncio.sleep(0)
            _logger.error(
                "ui.controller.pool_open_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            new_pool.close_preview_bridges()
            self._worker_pool = None
            self._manual_client = None
            self._active_config = None
            self._set_hardware_ready(False)
            self.pool_changed.emit(None)
            failed = self._progress_snapshot(
                ConfigLoadState.FAILED,
                f"Device initialization failed: {exc}",
            )
            self.config_load_finished.emit(failed)
            return
        await asyncio.sleep(0)

        # 4. Spawn one UI-side drainer per preview bridge. Each loops
        #    over PreviewFrame items and re-emits preview_received on
        #    the Qt thread. Lifetime = until close_preview_bridges
        #    fires ThreadBridgeClosedError or _stop_preview_drainers
        #    cancels. Each drainer is registered as a non-critical
        #    lifecycle task so the shutdown coordinator can cancel them
        #    en masse without blocking on the close.
        self._preview_drainers = {}
        for name, bridge in new_pool.preview_bridges().items():
            task = asyncio.create_task(
                self._drain_preview(bridge),
                name=f"ui-preview-{name}",
            )
            self._preview_drainers[name] = task
            self._lifecycle.register(
                LifecycleKind.PREVIEW_DRAIN,
                f"preview-{name}",
                task,
                critical=False,
            )
        self._manual_client = ManualClient(
            pool=new_pool,
            conductor_provider=lambda: self._conductor,
        )
        self.pool_changed.emit(new_pool)
        self._set_hardware_ready(True)
        ready = self._progress_snapshot(
            ConfigLoadState.READY,
            "All devices initialized",
        )
        self.config_load_finished.emit(ready)

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

    async def aclose_pool(self) -> PoolCloseResult | None:
        """Close the active pool and clear the binding via the
        best-effort :meth:`WorkerPool.shutdown_close` path.

        Returns the structured :class:`PoolCloseResult` so the
        :class:`ShutdownCoordinator` can record any degraded outcome in
        its :class:`ShutdownResult.errors`. ``None`` when no pool was
        bound. Exceptions are not swallowed — shutdown-critical results
        must reach the coordinator.
        """
        old = self._worker_pool
        self._worker_pool = None
        self._manual_client = None
        self._active_config = None
        self._set_hardware_ready(False)
        self.pool_changed.emit(None)
        # Cancel UI-side preview drainers before pool.close so they
        # don't observe a half-closed bridge mid-iteration.
        await self._stop_preview_drainers()
        if old is None:
            return None
        return await old.shutdown_close()

    def emit_manual_event(self, event: DeviceEvent) -> None:
        """Surface a manual-command :class:`DeviceEvent` to the events dock."""
        self.manual_event.emit(event)

    # ------------------------------------------------------------------ operator commands

    @property
    def active_procedure_id(self) -> str | None:
        """Procedure id of the currently-armed/running run, or ``None``.

        Read off the most recently launched :class:`ExperimentConfig`.
        Stays populated through SEALED so a finishing-summary modal can
        still introspect what just ran; cleared when the controller is
        torn down.
        """
        config = getattr(self, "_active_run_config", None)
        if config is None:
            return None
        procedure = getattr(config, "procedure", None)
        return getattr(procedure, "id", None) if procedure is not None else None

    def send_operator_command(self, cmd: OperatorCommand) -> bool:
        """Forward an operator command to the active procedure runner.

        Returns ``True`` when the command was queued. Returns ``False``
        when no run is active, the runner doesn't support the
        command stream (older procedures), or the runner's send buffer
        rejected the command (would-block / closed).

        UI buttons should treat ``False`` as a transient — flash the
        status briefly and let the next click retry. The command stream
        is intentionally non-blocking so a wedged consumer cannot freeze
        the UI thread.
        """
        runner = getattr(self, "_active_procedure_runner", None)
        if runner is None:
            return False
        send = getattr(runner, "send_operator_command", None)
        if send is None:
            return False
        return bool(send(cmd))

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
        if self._shutdown_requested:
            raise RuntimeError("shutdown in progress; cannot start a new run")
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
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._run(config), name="ui-run")
        self._lifecycle.register(
            LifecycleKind.RUN,
            "run",
            self._task,
            critical=True,
        )

    def request_abort(self, *, mode: str = "safe_shutdown") -> None:
        """Forward the abort request to the active conductor; latch it
        when the conductor doesn't exist yet.

        ``mode`` is recorded in the run's ``exit_reason`` for audit
        purposes (``"safe_shutdown"`` vs ``"immediate"``); the conductor
        treats both the same way — its ``SafeShutdownStep`` runs
        unconditionally before disarm.

        Behavior:

        * Conductor exists → forward the stop immediately.
        * Conductor is ``None`` but ``_run()`` is in flight (preparing
          the conductor) → latch the reason. ``_run()`` consumes the
          latch right after assigning ``self._conductor`` so the
          operator's abort is honored even if Start was clicked and
          Abort hit before the conductor was constructed.
        * No active run at all → no-op.
        """
        conductor = self._conductor
        if conductor is not None:
            conductor.stop(reason=f"operator_{mode}")
            return
        task = self._task
        if task is not None and not task.done():
            self._pending_abort_reason = f"operator_{mode}"
            _logger.info("runcontroller.abort_latched", reason=self._pending_abort_reason)

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
            ui_sink: Any = None
            if _conductor_holder:
                stop_signal = _conductor_holder[0].completion_event
                # Wire the UI-only telemetry sink so procedures emitting
                # ProcedureTicks (heat-flux tune's live numerics) reach
                # the dock through the existing UI bridge. The sink is
                # a no-op if no UI bridge is attached (headless tests).
                ui_sink = _conductor_holder[0].procedure_ui_sink()

            runner = ProcedureRunner(
                procedure=procedure,
                config=config,
                channel_registry=channel_registry,
                dispatcher=dispatcher,
                authorization=s.authorization,
                adapters=adapter_by_device,
                bundle_writer=s.bundle_writer,
                method_executor=method_executor,
                stop_signal=stop_signal,
                ui_sink=ui_sink,
            )
            # Stash the runner + the launched config so the UI's operator
            # command surface (Pause / Accept Current / etc.) can route to
            # this run. Cleared in :meth:`_run` cleanup so a stale runner
            # can't accept commands after the conductor has shut down.
            self._active_procedure_runner = runner
            self._active_run_config = config
            return runner

        # Conductor knobs come from the user-facing RuntimeConfig.
        # ``ABORT_GRACE_S`` is retained as a UI-side fallback constant
        # only — the configured value wins so an operator can extend
        # the disarm grace per experiment.
        conductor_config = ConductorConfig.from_runtime(config.runtime)
        conductor = Conductor(
            pool=pool,
            session=session,
            runner_factory=_runner_factory,
            config=conductor_config,
        )
        _conductor_holder.append(conductor)
        self._conductor = conductor

        # Apply any abort that landed before the conductor existed.
        # Cleared whether or not consumed so a stale latch can't bleed
        # into the next run.
        pending_reason = self._pending_abort_reason
        self._pending_abort_reason = None
        if pending_reason is not None:
            _logger.info("runcontroller.pending_abort_applied", reason=pending_reason)
            conductor.stop(reason=pending_reason)

        # UI bridge — Conductor → UI thread channel. DROP_OLDEST so the
        # conductor never blocks on UI subscribers.
        ui_bridge: ThreadBridge[WorkerEmission] = ThreadBridge(
            name="ui",
            capacity=config.runtime.ui_bridge_capacity,
            consumer_loop=asyncio.get_running_loop(),
            policy=BridgePolicy.DROP_OLDEST,
        )
        ui_bridge.attach_consumer()
        conductor.attach_ui_bridge(ui_bridge)

        # State-polling task surfaces conductor transitions as Qt signals.
        # Non-critical lifecycle entry: the coordinator cancels it during
        # the CANCEL_LIFECYCLE_TASKS stage rather than waiting.
        self._set_ui_state(RunUiState.PREPARING)
        self._state_poll_task = asyncio.create_task(
            self._poll_conductor_state(conductor),
            name="ui-state-poll",
        )
        self._lifecycle.register(
            LifecycleKind.STATE_POLL,
            "state-poll",
            self._state_poll_task,
            critical=False,
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
            # Release the runner + config back-refs so a stale operator
            # command after the run can't reach a torn-down procedure.
            # ``active_procedure_id`` returning ``None`` is the dock's
            # hide-trigger.
            self._active_procedure_runner = None
            self._active_run_config = None
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
        * :class:`ProcedureTick` → :attr:`procedure_tick_received` (per-
          procedure live-numerics docks; the dock filters by
          ``procedure_id``).
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
        if isinstance(emission, ProcedureTick):
            # Procedure-side live-numerics tick. Docks filter by
            # ``tick.procedure_id`` on receipt — the controller does
            # not know about specific procedures.
            self.procedure_tick_received.emit(emission)
            return
        if isinstance(emission, FrameReceipt):
            # Frame receipts are durable-only at the UI boundary —
            # preview JPEGs are delivered via :attr:`preview_received`
            # from the per-camera preview bridges. The camera-side
            # ``preview_stream()`` surface is unchanged; only the
            # cross-thread plumbing is new.
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
    bundle_status, integrity_status = read_bundle_status(result.bundle_path)
    return RunUiResult(
        run_id=result.run_id,
        bundle_path=result.bundle_path,
        run_status=run_status_for_outcome(result.outcome),
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
