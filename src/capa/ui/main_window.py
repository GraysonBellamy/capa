""":class:`MainWindow` — the top-level capa GUI shell.

Plan §10. ``QMainWindow`` with central ``QTabWidget`` (Setup, Run) and
dockable Numerics + Events panels. Window state (geometry + dock layout)
persists to ``~/.capa/window_state.json``.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Final

import structlog
from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QTabWidget,
)

from capa.core.errors import CapaError
from capa.experiment.config import ExperimentConfig
from capa.storage.catalog import RunCatalog
from capa.ui.docks.camera_preview import CameraPreviewDock
from capa.ui.docks.diagnostics import DiagnosticsDock
from capa.ui.docks.events import EventsDock
from capa.ui.docks.manual_control import ManualControlDock
from capa.ui.docks.numerics import NumericsDock
from capa.ui.shutdown import (
    ShutdownCoordinator,
    ShutdownPhase,
    ShutdownResult,
    status_message_for_phase,
)
from capa.ui.state import RunController, RunUiResult, RunUiState
from capa.ui.statusbar import CapaStatusBar, OperatorIdProvider
from capa.ui.tabs.method import MethodTab
from capa.ui.tabs.run import RunTab
from capa.ui.tabs.setup import SetupTab

WINDOW_STATE_PATH: Final[Path] = Path.home() / ".capa" / "window_state.json"

_METHOD_TAB_INDEX: Final[int] = 1
"""Index of the Method tab in the central :class:`QTabWidget`. Used by
:meth:`MainWindow._update_method_tab_title` to decorate the tab label
with the currently loaded method's name."""

_logger = structlog.get_logger("capa.ui.main_window")


class MainWindow(QMainWindow):
    """Top-level window. Owns a :class:`RunController` shared by every
    component. Construct once per process; the same window handles every
    sequential run."""

    def __init__(
        self,
        *,
        runs_root: Path,
        catalog: RunCatalog | None = None,
        repo_root: Path | None = None,
        lockfile_source: Path | None = None,
        plugins_lock: object | None = None,  # capa.core.plugins_lock.PluginsLock
        configure_logging_for_bundle: bool = True,
        initial_config: ExperimentConfig | None = None,
        initial_config_path: Path | None = None,
        shutdown_coordinator: ShutdownCoordinator | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("capa")
        self.resize(1400, 900)

        self._runs_root: Path = runs_root
        self._operator_provider = OperatorIdProvider()

        # Close-flow state machine driven by ShutdownCoordinator. First
        # closeEvent calls ``begin_shutdown``; the coordinator's
        # ``completed`` signal flips ``_shutdown_complete`` and
        # re-triggers ``close()``, and the second closeEvent falls
        # through to ``super().closeEvent``. The coordinator owns
        # deadlines and the hard wall-clock fuse.
        self._shutdown_started: bool = False
        self._shutdown_complete: bool = False

        self._controller = RunController(
            runs_root=runs_root,
            plugins_lock=plugins_lock,  # type: ignore[arg-type]
            catalog=catalog,
            repo_root=repo_root,
            lockfile_source=lockfile_source,
            configure_logging_for_bundle=configure_logging_for_bundle,
            parent=self,
        )

        # Shutdown coordinator. ``catalog`` is passed in so the
        # coordinator's CLOSE_CATALOG phase can release the SQLite
        # handle. Tests inject a coordinator with a no-op hard_exit so
        # the fuse can be asserted without killing pytest.
        self._shutdown_coordinator: ShutdownCoordinator = (
            shutdown_coordinator
            if shutdown_coordinator is not None
            else ShutdownCoordinator(
                controller=self._controller,
                catalog=catalog,
                parent=self,
            )
        )
        self._shutdown_coordinator.phase_changed.connect(self._on_shutdown_phase)
        self._shutdown_coordinator.completed.connect(self._on_shutdown_completed)

        self._setup_tab = SetupTab(self)
        self._method_tab = MethodTab(self)
        self._run_tab = RunTab(controller=self._controller, parent=self)
        self._setup_tab.device_action_requested.connect(self._on_device_action)

        self._tabs = QTabWidget(self)
        self._tabs.addTab(self._setup_tab, "Setup")
        self._tabs.addTab(self._method_tab, "Method")
        self._tabs.addTab(self._run_tab, "Run")
        self.setCentralWidget(self._tabs)

        # Keep the Method tab label in sync with whatever method is loaded.
        # The signal fires on load_method/clear, so opening an experiment
        # or a method file both flow through here.
        self._method_tab.methodChanged.connect(self._update_method_tab_title)

        # Numerics / Camera-preview docks are constructed when a config
        # loads — both need the parsed config (channels / cameras). Until
        # then, the events dock keeps the bottom area populated so the
        # layout stays consistent.
        self._numerics_dock: NumericsDock | None = None
        self._camera_preview_dock: CameraPreviewDock | None = None
        self._diagnostics_dock: DiagnosticsDock | None = None
        self._diagnostics_toggle: QAction | None = None
        self._events_dock = EventsDock(self)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._events_dock)

        # Manual control dock — single dock with per-device cards. Hidden
        # by default; the Devices menu toggles visibility. Rebuilt on
        # config-load like the other config-driven docks.
        self._manual_dock = ManualControlDock(
            controller=self._controller,
            operator_provider=self._operator_provider,
            parent=self,
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._manual_dock)
        self._manual_dock.hide()

        # Wire events dock to the controller.
        self._controller.event_received.connect(self._events_dock.append_event)
        # Manual-mode commands surface in the same events dock as engine
        # events so the audit trail is unified visually.
        self._controller.manual_event.connect(self._events_dock.append_event)
        self._controller.run_finished.connect(self._on_run_finished)
        self._controller.state_changed.connect(self._on_state)

        # Status bar.
        status = CapaStatusBar(
            controller=self._controller,
            runs_root=runs_root,
            operator_id_provider=self._operator_provider,
            parent=self,
        )
        self.setStatusBar(status)
        self._status: QStatusBar = status

        # Menu bar.
        self._build_menus()

        # Restore prior layout if any.
        self._restore_window_state()

        # Optional initial config (when launched with a positional path).
        if initial_config is not None:
            self._apply_loaded_config(initial_config, initial_config_path)

    # ------------------------------------------------------------------ build

    def _build_menus(self) -> None:
        menu = self.menuBar()
        file_menu = menu.addMenu("&File")
        open_action = QAction("&Open Config…", self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self._on_open_config)
        file_menu.addAction(open_action)

        file_menu.addSeparator()

        quit_action = QAction("&Quit", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # Devices menu — gateway to the manual control dock and the
        # diagnostics dock. The dock toggleViewActions keep their checked
        # state in sync with visibility so closing via [×] flips the
        # menu check off. The diagnostics action is added/replaced each
        # time a config loads (the dock is rebuilt then).
        self._devices_menu = menu.addMenu("&Devices")
        toggle_action = self._manual_dock.toggleViewAction()
        if toggle_action is not None:
            toggle_action.setText("&Manual Control")
            toggle_action.setShortcut("Ctrl+M")
            self._devices_menu.addAction(toggle_action)

    # ------------------------------------------------------------------ slots

    def _on_open_config(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Open experiment config",
            str(self._runs_root.parent),
            "Configs (*.yaml *.yml *.toml);;All files (*)",
        )
        if not path_str:
            return
        path = Path(path_str)
        try:
            cfg = ExperimentConfig.load(path)
        except CapaError as exc:
            QMessageBox.critical(self, "Config error", str(exc))
            _logger.warning("ui.config_load_failed", path=str(path), error=str(exc))
            return
        self._apply_loaded_config(cfg, path)

    def _apply_loaded_config(self, cfg: ExperimentConfig, path: Path | None) -> None:
        # Bind the controller's DeviceRegistry to the new config FIRST so
        # any consumer (manual control dock) that reacts to load_config
        # already sees the new registry.
        self._controller.set_active_config(cfg)

        self._setup_tab.load_config(cfg)
        self._run_tab.load_config(cfg)
        self._manual_dock.load_config(cfg)
        self._operator_provider.set_operator_id(cfg.operator.id)

        # Method tab: mirror whatever the experiment declared. A method
        # attached as a string ref carries ``method_source_path`` so the
        # tab's Save writes back to the same file; an inlined method has
        # no source path and behaves like an unsaved buffer. Free runs
        # (no method) clear the tab so it agrees with the loaded config.
        if cfg.method is not None:
            self._method_tab.load_method(cfg.method, path=cfg.method_source_path)
        else:
            self._method_tab.clear()

        # Replace the numerics dock with one whose tile set matches this
        # config. Old dock (if any) is removed and deleted. The bare empty
        # registry is OK — tiles will pick up live values on the next run.
        if self._numerics_dock is not None:
            self.removeDockWidget(self._numerics_dock)
            self._numerics_dock.deleteLater()
        self._numerics_dock = NumericsDock(
            registry=self._controller.buffers,
            channels=list(cfg.hardware.channels),
            parent=self,
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._numerics_dock)
        self._numerics_dock.start()

        # Replace the camera-preview dock the same way. The dock
        # subscribes to ``preview_received``; the signal is driven by
        # the per-camera UI drainers spawned in
        # :meth:`RunController._open_pool` that read from pool-owned
        # :class:`ThreadBridge[PreviewFrame]` channels. Tiles only paint
        # when the adapter declares CameraCapability.LIVE_PREVIEW —
        # without it, :meth:`CameraDeviceAdapter.start_preview_channel`
        # never spawns a drainer and the bridge stays empty.
        if self._camera_preview_dock is not None:
            # ``disconnect`` raises ``TypeError`` when no slot was connected
            # (e.g. the prior dock was constructed but never lit up). Treat
            # that as a no-op rather than crashing config-reload.
            with contextlib.suppress(TypeError):
                self._controller.preview_received.disconnect(
                    self._camera_preview_dock.update_preview
                )
            with contextlib.suppress(TypeError):
                self._controller.camera_event_received.disconnect(
                    self._camera_preview_dock.note_event
                )
            self.removeDockWidget(self._camera_preview_dock)
            self._camera_preview_dock.deleteLater()
        self._camera_preview_dock = CameraPreviewDock(
            cameras=list(cfg.hardware.cameras),
            parent=self,
        )
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._camera_preview_dock)
        self._controller.preview_received.connect(self._camera_preview_dock.update_preview)
        self._controller.camera_event_received.connect(self._camera_preview_dock.note_event)

        # Rebuild the acquisition diagnostics dock. The worker topology
        # (resource_id → adapter_names) is snapshotted once here; values
        # are polled live from the conductor at 1 Hz during a run.
        if self._diagnostics_dock is not None:
            if self._diagnostics_toggle is not None:
                self._devices_menu.removeAction(self._diagnostics_toggle)
                self._diagnostics_toggle = None
            self.removeDockWidget(self._diagnostics_dock)
            self._diagnostics_dock.deleteLater()
        pool = self._controller.worker_pool
        worker_topology = (
            {rid: w.adapter_names for rid, w in pool.workers.items()} if pool is not None else {}
        )
        self._diagnostics_dock = DiagnosticsDock(
            controller=self._controller,
            worker_topology=worker_topology,
            parent=self,
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._diagnostics_dock)
        self._diagnostics_dock.start()
        self._diagnostics_toggle = self._diagnostics_dock.toggleViewAction()
        self._diagnostics_toggle.setText("&Acquisition Diagnostics")
        self._devices_menu.addAction(self._diagnostics_toggle)

        title_path = f" — {path.name}" if path else ""
        self.setWindowTitle(f"capa{title_path}")
        _logger.info("ui.config_loaded", path=str(path) if path else None)

    def _on_device_action(self, name: str) -> None:
        """Handle "Open Manual Control" from the Setup tab right-click menu."""
        self._manual_dock.reveal(name)

    def _update_method_tab_title(self) -> None:
        """Decorate the Method tab label with the loaded method's name so
        the operator can tell which method is active without switching to
        the tab. Falls back to ``"Method"`` when no method is loaded."""
        if self._method_tab.has_method():
            name = self._method_tab.current_method_name()
            self._tabs.setTabText(_METHOD_TAB_INDEX, f"Method — {name}")
        else:
            self._tabs.setTabText(_METHOD_TAB_INDEX, "Method")

    def _on_state(self, state: object) -> None:
        # When a run starts, the controller has rebuilt the buffer registry.
        # Rebind the numerics dock to point at the new one.
        if (
            isinstance(state, RunUiState)
            and state is RunUiState.RUNNING
            and self._numerics_dock is not None
        ):
            self._numerics_dock.set_registry(self._controller.buffers)

    def _on_run_finished(self, result: object) -> None:
        if not isinstance(result, RunUiResult):
            return
        if result.bundle_path is None:
            self._events_dock.append_run_marker(
                f"run refused: {result.exit_reason or 'unknown'}",
            )
        else:
            self._events_dock.append_run_marker(
                f"run {result.run_status} → bundle {result.bundle_status}: {result.bundle_path}",
            )

    # ------------------------------------------------------------------ window state

    def closeEvent(self, event: QCloseEvent | None) -> None:  # noqa: N802 — Qt override
        if event is None:
            return
        # Second pass: coordinator finished and re-triggered close().
        # Save window state and let Qt close the window for real.
        if self._shutdown_complete:
            self._save_window_state()
            super().closeEvent(event)
            return
        # Re-entry while the coordinator is still working (e.g. operator
        # double-clicks the [×]) — keep ignoring. ``begin_shutdown`` is
        # idempotent so we can call it again safely, but there's no
        # point.
        if self._shutdown_started:
            event.ignore()
            return
        # First close: confirm if a run is in flight, then hand control
        # to the ShutdownCoordinator. The coordinator owns deadlines,
        # phase ordering, and the hard wall-clock fuse.
        if self._controller.is_active:
            answer = QMessageBox.question(
                self,
                "Run in progress",
                "A run is active. Close anyway and abort it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._shutdown_started = True
        event.ignore()
        # ``begin_shutdown`` is idempotent — repeat callers (e.g. future
        # tray-menu quit) get the same in-flight task. The completed
        # signal wires us back into closeEvent via _on_shutdown_completed.
        self._shutdown_coordinator.begin_shutdown("window_close")

    def _on_shutdown_phase(self, phase: object) -> None:
        """Surface shutdown phases via the status bar.

        Connected to :attr:`ShutdownCoordinator.phase_changed`. The
        status bar is the operator's signal that the [×] click was
        received and shutdown is making progress.
        """
        if not isinstance(phase, ShutdownPhase):
            return
        msg = status_message_for_phase(phase)
        if msg is not None:
            self._status.showMessage(msg)

    def _on_shutdown_completed(self, result: object) -> None:
        """Flip the close-flow state machine and re-trigger window close.

        Connected to :attr:`ShutdownCoordinator.completed`. Logging the
        outcome here gives ops one structured event per shutdown attempt
        without needing to grep the coordinator's per-phase logs.
        """
        if isinstance(result, ShutdownResult):
            _logger.info(
                "ui.shutdown.completed",
                reason=result.reason,
                clean=result.clean,
                elapsed_s=result.elapsed_s,
                errors=result.errors,
            )
        self._shutdown_complete = True
        self.close()

    def _save_window_state(self) -> None:
        try:
            WINDOW_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "geometry": self.saveGeometry().data().hex(),
                "state": self.saveState().data().hex(),
            }
            WINDOW_STATE_PATH.write_text(json.dumps(payload, indent=2))
        except OSError as exc:
            _logger.warning("ui.window_state_save_failed", error=str(exc))

    def _restore_window_state(self) -> None:
        if not WINDOW_STATE_PATH.exists():
            return
        try:
            payload = json.loads(WINDOW_STATE_PATH.read_text())
            self.restoreGeometry(QByteArray.fromHex(payload["geometry"].encode()))
            self.restoreState(QByteArray.fromHex(payload["state"].encode()))
        except (OSError, ValueError, KeyError) as exc:
            _logger.warning("ui.window_state_restore_failed", error=str(exc))

    # ------------------------------------------------------------------ test helpers

    @property
    def controller(self) -> RunController:
        return self._controller

    @property
    def run_tab(self) -> RunTab:
        return self._run_tab

    @property
    def setup_tab(self) -> SetupTab:
        return self._setup_tab

    @property
    def method_tab(self) -> MethodTab:
        return self._method_tab

    @property
    def events_dock(self) -> EventsDock:
        return self._events_dock

    @property
    def numerics_dock(self) -> NumericsDock | None:
        return self._numerics_dock

    @property
    def camera_preview_dock(self) -> CameraPreviewDock | None:
        return self._camera_preview_dock


__all__ = ["WINDOW_STATE_PATH", "MainWindow"]
