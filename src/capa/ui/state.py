""":class:`RunController` — adapter between :class:`ExperimentEngine` and Qt.

Plan §3, §7, §10. The controller owns one engine per run. It:

* subscribes to :attr:`engine.databus` *before* :meth:`engine.run` starts so
  no early sample is missed;
* pumps :class:`~capa.devices.records.ChannelSample`\\ s into a
  :class:`~capa.core.ringbuffer.RingBufferRegistry` for the plots and
  numerics dock;
* re-emits :class:`~capa.devices.records.DeviceEvent`\\ s as the
  :attr:`event_received` Qt signal for the events dock;
* mirrors :class:`EngineState` transitions onto the :attr:`state_changed`
  Qt signal so the Run-tab header badge updates live;
* surfaces operator abort via :meth:`request_abort`, threading the abort
  ``mode`` through to the engine.

Threading: qasync makes the asyncio loop the same thread as Qt's main
thread. Engine state callbacks and DataBus pump work both run on that loop,
so signal emits go direct (no QueuedConnection wrapper). UI consumers that
live on other threads (none in P1) would need explicit ``Qt.ConnectionType
.QueuedConnection`` on connect.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Final

import anyio
import structlog
from PySide6.QtCore import QObject, Signal

from capa.core.backpressure import BackpressurePolicy
from capa.core.plugins_lock import PluginsLock
from capa.core.ringbuffer import RingBufferRegistry
from capa.devices.camera.base import CameraEvent
from capa.devices.records import ChannelSample, DeviceEvent
from capa.devices.registry import DeviceRegistry
from capa.experiment.config import ExperimentConfig
from capa.experiment.engine import (
    AbortMode,
    EngineResult,
    EngineState,
    ExperimentEngine,
)
from capa.storage.catalog import RunCatalog
from capa.ui.async_util import schedule_bg

if TYPE_CHECKING:
    from capa.core.databus import Subscription

UI_PUMP_CAPACITY: Final[int] = 4096
"""Per-subscription buffer for the UI pump. Sized large enough that a slow
plot repaint cycle (~10 Hz) does not lose samples in the steady state, but
small enough that overflow surfaces quickly under genuine backpressure."""

_logger = structlog.get_logger("capa.ui.controller")


class RunController(QObject):
    """Owns one :class:`ExperimentEngine` per run. Construct once per
    :class:`MainWindow`; each :meth:`start` call begins a fresh run.

    Signals (all fire on the Qt main thread):

    * :attr:`state_changed` — every :class:`EngineState` transition,
      including the synthetic ``IDLE`` reset between runs.
    * :attr:`event_received` — :class:`DeviceEvent` instances from the
      DataBus, intended for the events dock.
    * :attr:`run_finished` — :class:`EngineResult` once the run is sealed
      (or failed). After this, :attr:`engine` retains a reference to the
      finished engine until the next :meth:`start`.
    """

    state_changed = Signal(object)
    event_received = Signal(object)
    preview_received = Signal(str, bytes)
    """``(camera_name, jpeg_bytes)`` — emitted by the engine's per-camera
    preview drain task at the adapter's throttled cadence (plan §10.2; ~2 Hz
    for ``WebcamAdapter``). The Qt main thread is the asyncio loop under
    ``qasync``, so this fires from the same thread the dock receives on."""
    camera_event_received = Signal(object)
    """``CameraEvent`` — emitted alongside the durable write to
    ``events.sqlite`` so the camera-preview dock can reflect
    ``pump_warning`` (drops counter + red border) and ``pump_failed``
    (failed border) live. Routed by ``event.name``."""
    run_finished = Signal(object)
    manual_event = Signal(object)
    """``DeviceEvent`` — synthesized by the manual control panel on each
    command dispatch (accepted or rejected) so the events dock reflects
    out-of-run operator actions alongside engine-driven events. Distinct
    from :attr:`event_received` because manual events are not bundle-backed
    (no run is open when they fire) — they exist for live audit only."""
    registry_changed = Signal(object)
    """``DeviceRegistry | None`` — fired when the registry is swapped on
    config-load or cleared on aclose. Consumers (manual control dock)
    rebind their adapter views to the new registry."""

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
        self._engine: ExperimentEngine | None = None
        self._task: asyncio.Task[None] | None = None
        self._buffers: RingBufferRegistry = RingBufferRegistry()
        self._last_result: EngineResult | None = None
        # DeviceRegistry — long-lived adapter pool shared between the engine
        # (start/stop sampling) and the manual control panel (command()
        # dispatch and one-shot reads). Constructed on each config-load and
        # closed on the next config-load or on app-quit. Plan §5.2 separates
        # open/close from start/stop; this is the open/close owner.
        self._device_registry: DeviceRegistry | None = None
        self._active_config: ExperimentConfig | None = None

    # ------------------------------------------------------------------ properties

    @property
    def engine(self) -> ExperimentEngine | None:
        """Engine of the most recent (running or finished) run, or
        ``None`` before the first :meth:`start`."""
        return self._engine

    @property
    def buffers(self) -> RingBufferRegistry:
        """Ring buffers for the active run. Cleared and re-registered on
        each :meth:`start`."""
        return self._buffers

    @property
    def state(self) -> EngineState:
        if self._engine is None:
            return EngineState.IDLE
        return self._engine.state

    @property
    def last_result(self) -> EngineResult | None:
        return self._last_result

    @property
    def is_active(self) -> bool:
        """``True`` while a run is between Start and the SEALED/FAILED
        terminal state."""
        return self._task is not None and not self._task.done()

    @property
    def device_registry(self) -> DeviceRegistry | None:
        """Live :class:`DeviceRegistry` bound to the currently-loaded config,
        or ``None`` if no config is loaded. Consumers (manual control dock)
        use this to acquire adapters between runs."""
        return self._device_registry

    @property
    def active_config(self) -> ExperimentConfig | None:
        """The :class:`ExperimentConfig` most recently passed to
        :meth:`set_active_config`. ``None`` before any config-load."""
        return self._active_config

    # ------------------------------------------------------------------ config lifecycle

    def set_active_config(self, config: ExperimentConfig) -> None:
        """Bind a freshly-loaded config: construct a new
        :class:`DeviceRegistry` and schedule the old one's close.

        Called from :class:`MainWindow` on every config-load. The old
        registry's :meth:`DeviceRegistry.aclose` is scheduled as a
        fire-and-forget task on the event loop so the UI does not block
        while serial ports release. A new registry is installed
        synchronously so the manual control dock can rebind immediately.
        """
        old_registry = self._device_registry
        new_registry = DeviceRegistry(config)
        self._active_config = config
        self._device_registry = new_registry
        self.registry_changed.emit(new_registry)
        if old_registry is not None:
            # Fire-and-forget; the registry's aclose is best-effort and
            # logs its own failures. Cannot await here — Qt slot is sync.
            # schedule_bg returns None when no loop is running (tests),
            # in which case the adapters are GC'd without an explicit close.
            schedule_bg(old_registry.aclose())

    async def aclose_registry(self) -> None:
        """Close the active registry and clear the binding. Used by
        :class:`MainWindow.closeEvent` to release serial ports cleanly on
        app-quit. Safe to call when no registry is bound."""
        old = self._device_registry
        self._device_registry = None
        self._active_config = None
        self.registry_changed.emit(None)
        if old is not None:
            await old.aclose()

    def emit_manual_event(self, event: DeviceEvent) -> None:
        """Surface a manual-command :class:`DeviceEvent` to the events
        dock. The manual control panel calls this after each dispatch
        (success or failure) so out-of-run operator actions appear in the
        same audit surface as engine-driven events."""
        self.manual_event.emit(event)

    # ------------------------------------------------------------------ control

    def start(self, config: ExperimentConfig) -> None:
        """Schedule a new run on the asyncio loop. Returns immediately.

        Raises :class:`RuntimeError` if a run is already in flight — the
        UI is expected to disable the Start button while
        :attr:`is_active` is true.
        """
        if self.is_active:
            raise RuntimeError("a run is already active; abort it first")
        loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._run(config))

    def request_abort(self, *, mode: AbortMode = "safe_shutdown") -> None:
        """Forward the abort request to the engine. No-op if no run is
        active. The mode is recorded by the engine for downstream phases
        (P3 cooldown, P5 calibration teardown)."""
        engine = self._engine
        if engine is None:
            return
        engine.request_abort(mode=mode)

    # ------------------------------------------------------------------ internal

    async def _run(self, config: ExperimentConfig) -> None:
        # Reset transient state so the new run starts clean. The previous
        # engine reference is replaced; ring buffers are rebuilt from the
        # current config.
        self._buffers = RingBufferRegistry()
        for ch in config.hardware.channels:
            self._buffers.register(ch.name, decimate_to_hz=ch.decimate_to_hz)

        # Pass the registry through to the engine when one is bound (the
        # config-load path always binds one before start()). The engine
        # borrows: it acquires/starts/stops, but does NOT close the
        # connections — that's the registry owner's job. Cleared at
        # config-reload or app-quit via aclose_registry().
        engine = ExperimentEngine(
            on_state_changed=self._emit_state,
            device_registry=self._device_registry,
        )
        engine.preview_callback = self._emit_preview
        engine.camera_event_callback = self._emit_camera_event
        self._engine = engine
        # Synthetic IDLE→PREPARING boundary so the UI shows the transition
        # without waiting for the engine's first internal callback.
        self.state_changed.emit(EngineState.IDLE)

        sub = engine.databus.subscribe_all(
            "ui-pump",
            capacity=UI_PUMP_CAPACITY,
            policy=BackpressurePolicy.DROP_OLDEST,
        )
        result: EngineResult | None = None
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(self._pump, sub)
                result = await engine.run(
                    config,
                    runs_root=self._runs_root,
                    plugins_lock=self._plugins_lock,
                    catalog=self._catalog,
                    repo_root=self._repo_root,
                    lockfile_source=self._lockfile_source,
                    configure_logging_for_bundle=self._configure_logging_for_bundle,
                )
                # When run() returns, the engine's finally has already closed
                # databus. The pump's queue.get() raises RuntimeError, the
                # pump exits, and the task group joins cleanly.
        except BaseException:
            _logger.exception("run_controller.run_failed")
            raise
        finally:
            self._last_result = result
            if result is not None:
                self.run_finished.emit(result)

    async def _pump(self, sub: Subscription) -> None:
        """Drain ``sub`` until the bus closes, routing each emission."""
        try:
            async for emission in sub:
                if isinstance(emission, ChannelSample):
                    self._buffers.push(emission)
                elif isinstance(emission, DeviceEvent):
                    self.event_received.emit(emission)
                # SourceRecord and DeviceSnapshot are durable-only in P1.
        except RuntimeError:
            # databus.close() during finalize. Normal shutdown.
            return

    def _emit_state(self, state: EngineState) -> None:
        # Runs synchronously inside the engine task on the asyncio loop;
        # qasync ties that loop to the Qt main thread, so the signal emits
        # cross thread-safely without QueuedConnection.
        self.state_changed.emit(state)

    def _emit_preview(self, camera_name: str, jpeg: bytes) -> None:
        self.preview_received.emit(camera_name, jpeg)

    def _emit_camera_event(self, event: CameraEvent) -> None:
        self.camera_event_received.emit(event)


# Re-exported so callers don't need to import EngineState from the engine
# module to type a slot signature.
StateSlot = Callable[[EngineState], None]


__all__ = [
    "UI_PUMP_CAPACITY",
    "RunController",
    "StateSlot",
]
