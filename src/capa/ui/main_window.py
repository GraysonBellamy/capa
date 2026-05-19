""":class:`MainWindow` — the top-level capa GUI shell.

``QMainWindow`` with central ``QTabWidget`` (Setup, Run) and dockable
Numerics + Events panels. Window state (geometry + dock layout) persists
to ``~/.capa/window_state.json``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Final

import structlog
from PySide6.QtCore import QByteArray, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QStatusBar,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
)

from capa.core.errors import CapaError
from capa.experiment.config import ExperimentConfig
from capa.experiment.procedures.base import procedure_uses_method
from capa.storage.catalog import RunCatalog
from capa.ui.config_progress import ConfigLoadProgress, ConfigLoadState, HardwareInitDialog
from capa.ui.docks.camera_preview import CameraPreviewDock
from capa.ui.docks.diagnostics import DiagnosticsDock
from capa.ui.docks.events import EventsDock
from capa.ui.docks.heat_flux_tune import HeatFluxTuneDock
from capa.ui.docks.log import LogDock
from capa.ui.docks.manual_control import ManualControlDock
from capa.ui.docks.numerics import NumericsDock
from capa.ui.document_coordinator import DocumentCoordinator
from capa.ui.recents import record_open
from capa.ui.shutdown import (
    ShutdownCoordinator,
    ShutdownResult,
    ShutdownStage,
    status_message_for_stage,
)
from capa.ui.state import RunController, RunUiResult, RunUiState
from capa.ui.statusbar import CapaStatusBar, OperatorIdProvider
from capa.ui.tabs.method import MethodTab
from capa.ui.tabs.run import RunTab
from capa.ui.tabs.setup import SetupTab
from capa.ui.welcome import SIMULATOR_CONFIG, WelcomeHero

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
        self._config_loading: bool = False
        self._open_config_action: QAction | None = None
        self._hardware_dialog: HardwareInitDialog | None = None
        # Default window state captured before the first restore so the
        # View → Reset window layout action has something to restore to.
        self._default_window_state: QByteArray | None = None
        self._default_window_geometry: QByteArray | None = None

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
        # coordinator's CLOSE_CATALOG stage can release the SQLite
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
        self._shutdown_coordinator.stage_changed.connect(self._on_shutdown_stage)
        self._shutdown_coordinator.completed.connect(self._on_shutdown_completed)

        self._setup_tab = SetupTab(controller=self._controller, parent=self)
        self._method_tab = MethodTab(self)
        self._run_tab = RunTab(controller=self._controller, parent=self)
        self._setup_tab.deviceActionRequested.connect(self._on_device_action)

        # Setup ↔ Method coordinator. Keeps the experiment's
        # method ref in lock-step with whatever MethodTab is showing —
        # without it, editing the method in Setup's Files view or saving
        # a method through MethodTab leaves the other side stale.
        self._document_coordinator = DocumentCoordinator(
            setup_tab=self._setup_tab,
            method_tab=self._method_tab,
            parent=self,
        )
        # Inject the coordinator into SetupTab so Apply & Connect can
        # compose the draft + Method-tab buffer.
        self._setup_tab.set_document_coordinator(self._document_coordinator)
        # Apply & Connect: SetupTab emits the composed config + path; we
        # route it through the same loader that File→Open already uses
        # so the Numerics / CameraPreview / Diagnostics / Manual docks
        # rebuild exactly once.
        self._setup_tab.applyRequested.connect(self._on_setup_apply_requested)

        self._tabs = QTabWidget(self)
        self._tabs.addTab(self._setup_tab, "Setup")
        self._tabs.addTab(self._method_tab, "Method")
        self._tabs.addTab(self._run_tab, "Run")

        # Central pane is a stack: welcome hero until a config loads,
        # then the tab widget. The two children are constructed up
        # front so swap()ing is just an index change.
        self._welcome_hero = WelcomeHero(self)
        self._welcome_hero.newRequested.connect(self._on_welcome_new)
        self._welcome_hero.openRequested.connect(self._on_open_config)
        self._welcome_hero.simulatorRequested.connect(self._on_welcome_simulator)
        self._welcome_hero.recentRequested.connect(self._on_welcome_recent)

        self._central = QStackedWidget(self)
        self._central.addWidget(self._welcome_hero)
        self._central.addWidget(self._tabs)
        self.setCentralWidget(self._central)
        self._central.setCurrentWidget(self._welcome_hero)

        # Keep the Method tab label in sync with whatever method is loaded.
        # The signal fires on load_method/clear, so opening an experiment
        # or a method file both flow through here.
        self._method_tab.methodChanged.connect(self._update_method_tab_title)

        # Cache of ``procedure_id -> uses_method`` derived from the
        # procedure registry. Populated lazily on the first
        # ``procedureChanged`` event; entries default to ``True`` for
        # procedure ids the registry doesn't know (so a typo in
        # experiment.yaml doesn't silently hide the Method tab). Refreshed
        # alongside ``_apply_loaded_config`` in case a plugin install
        # changed which procedures are registered.
        self._procedure_uses_method: dict[str, bool] = {}
        self._setup_tab.procedureChanged.connect(self._on_procedure_changed)

        # Numerics / Camera-preview docks are constructed when a config
        # loads — both need the parsed config (channels / cameras). Until
        # then, the events dock keeps the bottom area populated so the
        # layout stays consistent.
        self._numerics_dock: NumericsDock | None = None
        self._numerics_toggle: QAction | None = None
        self._camera_preview_dock: CameraPreviewDock | None = None
        self._camera_toggle: QAction | None = None
        self._diagnostics_dock: DiagnosticsDock | None = None
        self._diagnostics_toggle: QAction | None = None
        self._events_dock = EventsDock(self)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._events_dock)

        # Heat-Flux Tune dock — operator-command surface for the
        # supervisory tune procedure. Auto-shown when a tune is running,
        # hidden otherwise, so it doesn't clutter the layout when not
        # relevant.
        self._heat_flux_tune_dock = HeatFluxTuneDock(self._controller, self)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._heat_flux_tune_dock)
        self._heat_flux_tune_dock.setVisible(False)

        # Log dock — mirrors structlog stdout into the GUI so the
        # operator can diagnose pool/conductor/run-lifecycle activity
        # without watching the terminal. Sits side-by-side with the
        # Events dock at equal width via splitDockWidget + resizeDocks.
        self._log_dock = LogDock(self)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._log_dock)
        self.splitDockWidget(self._events_dock, self._log_dock, Qt.Orientation.Horizontal)
        self.resizeDocks([self._events_dock, self._log_dock], [1, 1], Qt.Orientation.Horizontal)

        # Manual control dock — single dock with per-device cards. The
        # View menu toggles visibility; first-launch starts it visible
        # so new operators discover the surface, but returning users
        # keep whatever they left it as.
        self._manual_dock = ManualControlDock(
            controller=self._controller,
            operator_provider=self._operator_provider,
            parent=self,
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._manual_dock)

        # Wire events dock to the controller.
        self._controller.event_received.connect(self._events_dock.append_event)
        # Manual-mode commands surface in the same events dock as engine
        # events so the audit trail is unified visually.
        self._controller.manual_event.connect(self._events_dock.append_event)
        self._controller.run_finished.connect(self._on_run_finished)
        self._controller.state_changed.connect(self._on_state)
        self._controller.config_load_started.connect(self._on_config_load_started)
        self._controller.config_load_progress.connect(self._on_config_load_progress)
        self._controller.config_load_finished.connect(self._on_config_load_finished)
        self._controller.hardware_ready_changed.connect(self._on_hardware_ready_changed)

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
        self._set_config_loading_ui(False)

        # Capture default state BEFORE restoring user state so View →
        # Reset window layout has a target to revert to. First-launch
        # detection (no prior state file) also keys off this — Manual
        # Control stays visible-but-empty so new users see the dock
        # surface exists. Returning users get whatever their last layout
        # was, including a hidden Manual Control dock if they closed it.
        is_first_launch = not WINDOW_STATE_PATH.exists()
        self._default_window_state = self.saveState()
        self._default_window_geometry = self.saveGeometry()
        if is_first_launch:
            self._manual_dock.show()
        else:
            self._manual_dock.hide()
        self._restore_window_state()

        # Optional initial config (when launched with a positional path).
        if initial_config is not None:
            QTimer.singleShot(
                0,
                lambda: self._apply_loaded_config(initial_config, initial_config_path),
            )

    # ------------------------------------------------------------------ build

    def _build_menus(self) -> None:
        menu = self.menuBar()
        file_menu = menu.addMenu("&File")
        open_action = QAction("&Open Config…", self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self._on_open_config)
        file_menu.addAction(open_action)
        self._open_config_action = open_action

        file_menu.addSeparator()

        quit_action = QAction("&Quit", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # View menu — every dock is reachable here, regardless of whether
        # the operator has accidentally closed one. The dock
        # toggleViewActions keep their checked state in sync with
        # visibility so closing via [×] flips the menu check off.
        # Config-driven docks (Numerics, Camera, Diagnostics) register
        # themselves into this menu via :meth:`_register_dock_view_action`
        # each time a config loads.
        self._view_menu = menu.addMenu("&View")
        self._view_actions_dynamic: list[QAction] = []
        # Static docks first (Events, Log, Manual Control).
        events_toggle = self._events_dock.toggleViewAction()
        if events_toggle is not None:
            events_toggle.setText("&Events")
            events_toggle.setShortcut("Ctrl+3")
            self._view_menu.addAction(events_toggle)
        log_toggle = self._log_dock.toggleViewAction()
        if log_toggle is not None:
            log_toggle.setText("&Log")
            log_toggle.setShortcut("Ctrl+4")
            self._view_menu.addAction(log_toggle)
        manual_toggle = self._manual_dock.toggleViewAction()
        if manual_toggle is not None:
            manual_toggle.setText("&Manual Control")
            manual_toggle.setShortcut("Ctrl+M")
            self._view_menu.addAction(manual_toggle)
        # Dynamic insertion anchor — Numerics / Camera / Diagnostics
        # actions get inserted before this separator each config-load.
        self._view_dynamic_anchor = self._view_menu.addSeparator()
        reset_action = QAction("Reset window layout", self)
        reset_action.triggered.connect(self._on_reset_window_layout)
        self._view_menu.addAction(reset_action)

        help_menu = menu.addMenu("&Help")
        quick_start_action = QAction("&Quick Start", self)
        quick_start_action.triggered.connect(self._open_quick_start)
        help_menu.addAction(quick_start_action)
        glossary_action = QAction("&Glossary", self)
        glossary_action.triggered.connect(self._show_glossary)
        help_menu.addAction(glossary_action)
        help_menu.addSeparator()
        logs_action = QAction("Open &logs folder", self)
        logs_action.triggered.connect(self._open_logs_folder)
        help_menu.addAction(logs_action)
        help_menu.addSeparator()
        about_action = QAction("&About capa", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    # ------------------------------------------------------------------ slots

    def _on_open_config(self) -> None:
        if self._config_loading:
            # 4 s timeout so the status bar's normal widgets (the pills)
            # reappear afterwards — Qt hides them for the duration of any
            # active showMessage. Same reason for every other transient
            # status message in this window.
            self._status.showMessage(
                "Config is still loading; wait for hardware initialization.", 4000
            )
            return
        if self._controller.shutdown_requested:
            self._status.showMessage("Shutdown is in progress; cannot open a new config.", 4000)
            return
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

    def _on_setup_apply_requested(self, cfg: object, path: object) -> None:
        """Apply & Connect from the Setup tab.

        ``SetupTab`` has already validated the draft and composed the
        config; we just need to drive the same loader that File→Open
        uses so the Numerics / CameraPreview / Diagnostics / Manual
        docks rebuild around the new config. ``SetupTab`` keys off
        ``RunController.config_load_finished`` for completion state, so
        we don't need to surface success/failure ourselves — the
        existing modal progress dialog covers the in-flight load.

        Pass ``reload_setup_tab=False`` so we don't re-read the file from
        disk into the Setup tab — that would clobber any unsaved edits
        the operator just composed into ``cfg`` and would reset the
        ``_apply_in_flight`` flag, causing ``_on_config_load_finished``
        to bail before flipping the strip into CONNECTED.
        """
        if not isinstance(cfg, ExperimentConfig):
            return
        resolved_path = path if isinstance(path, Path) else None
        self._apply_loaded_config(cfg, resolved_path, reload_setup_tab=False)

    def _apply_loaded_config(
        self,
        cfg: ExperimentConfig,
        path: Path | None,
        *,
        reload_setup_tab: bool = True,
    ) -> None:
        # Bind the controller's worker pool to the new config FIRST so
        # any consumer (manual control dock) that reacts to load_config
        # already sees the new pool.
        try:
            self._controller.set_active_config(cfg, config_path=path)
        except Exception as exc:
            QMessageBox.critical(self, "Config error", str(exc))
            _logger.warning(
                "ui.config_apply_failed",
                path=str(path) if path else None,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return

        if reload_setup_tab:
            self._setup_tab.load_config(cfg, path=path)
        self._run_tab.load_config(cfg)
        self._manual_dock.load_config(cfg)
        self._operator_provider.set_operator_id(cfg.operator.id)
        # Plugin set may have changed between configs (different
        # plugins.lock, different mode). Re-discover so the Method-tab
        # gate picks up any new procedures' ``uses_method`` values.
        self._refresh_procedure_uses_method_cache()

        # Method tab is populated by DocumentCoordinator._on_setup_draft_loaded
        # in response to setup_tab's draftLoaded signal — loading it again
        # here would fire methodChanged outside the coordinator's
        # _applying guard and falsely mark Files dirty (raw-TOML dict vs
        # Pydantic-canonical dump never compare equal).

        # Replace the numerics dock with one whose tile set matches this
        # config. Old dock (if any) is removed and deleted. The bare empty
        # registry is OK — tiles will pick up live values on the next run.
        if self._numerics_dock is not None:
            if self._numerics_toggle is not None:
                self._view_menu.removeAction(self._numerics_toggle)
                self._numerics_toggle = None
            self.removeDockWidget(self._numerics_dock)
            self._numerics_dock.deleteLater()
        self._numerics_dock = NumericsDock(
            registry=self._controller.buffers,
            channels=list(cfg.hardware.channels),
            parent=self,
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._numerics_dock)
        self._numerics_dock.start()
        self._numerics_toggle = self._register_dock_view_action(
            self._numerics_dock, "&Numerics", "Ctrl+1"
        )

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
            if self._camera_toggle is not None:
                self._view_menu.removeAction(self._camera_toggle)
                self._camera_toggle = None
            self.removeDockWidget(self._camera_preview_dock)
            self._camera_preview_dock.deleteLater()
        self._camera_preview_dock = CameraPreviewDock(
            cameras=list(cfg.hardware.cameras),
            parent=self,
        )
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._camera_preview_dock)
        self._controller.preview_received.connect(self._camera_preview_dock.update_preview)
        self._controller.camera_event_received.connect(self._camera_preview_dock.note_event)
        self._camera_toggle = self._register_dock_view_action(
            self._camera_preview_dock, "Camera &Preview", "Ctrl+2"
        )

        # Rebuild the acquisition diagnostics dock. The worker topology
        # (resource_id → adapter_names) is snapshotted once here; values
        # are polled live from the conductor at 1 Hz during a run. The
        # dock stays hidden by default — it's an opt-in diagnostic
        # surface, not part of normal operation — but it's reachable
        # from the View menu.
        if self._diagnostics_dock is not None:
            if self._diagnostics_toggle is not None:
                self._view_menu.removeAction(self._diagnostics_toggle)
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
        self._diagnostics_dock.hide()
        self._diagnostics_dock.start()
        self._diagnostics_toggle = self._register_dock_view_action(
            self._diagnostics_dock, "Acquisition &Diagnostics", "Ctrl+D"
        )

        title_path = f" — {path.name}" if path else ""
        self.setWindowTitle(f"capa{title_path}")
        # First successful load swaps the central pane from the welcome
        # hero to the tab widget. Subsequent loads keep showing the tabs.
        self._central.setCurrentWidget(self._tabs)
        if path is not None:
            record_open(path)
        _logger.info("ui.config_loaded", path=str(path) if path else None)

    def _on_welcome_new(self) -> None:
        """Welcome hero "New setup" — switch to Setup tab and open wizard."""
        self._central.setCurrentWidget(self._tabs)
        self._tabs.setCurrentWidget(self._setup_tab)
        self._setup_tab._on_new()

    def _on_welcome_simulator(self) -> None:
        """Welcome hero "Try a simulator" — load the bundled simulator config.

        The path is resolved against the repo root when available, then
        falls back to the working directory. Failure surfaces as a modal
        so the operator knows the bundled config isn't present.
        """
        candidates = [
            SIMULATOR_CONFIG,
            Path.cwd() / SIMULATOR_CONFIG,
        ]
        for candidate in candidates:
            if candidate.is_file():
                self._load_config_path(candidate)
                return
        QMessageBox.warning(
            self,
            "Simulator unavailable",
            f"Could not find {SIMULATOR_CONFIG}. The repo's bundled simulator "
            "config is missing from the working directory.",
        )

    def _on_welcome_recent(self, path: object) -> None:
        if isinstance(path, Path):
            self._load_config_path(path)

    def _load_config_path(self, path: Path) -> None:
        try:
            cfg = ExperimentConfig.load(path)
        except CapaError as exc:
            QMessageBox.critical(self, "Config error", str(exc))
            _logger.warning("ui.config_load_failed", path=str(path), error=str(exc))
            return
        self._apply_loaded_config(cfg, path)

    def _show_glossary(self) -> None:
        """Open the bundled glossary in a modal text browser."""
        glossary_path = self._locate_doc("glossary.md")
        if glossary_path is None or not glossary_path.is_file():
            QMessageBox.information(
                self,
                "Glossary unavailable",
                "Could not find docs/glossary.md. Reinstall capa or run from the repository root.",
            )
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("capa — Glossary")
        dialog.resize(640, 540)
        layout = QVBoxLayout(dialog)
        browser = QTextBrowser(dialog)
        browser.setOpenExternalLinks(True)
        browser.setMarkdown(glossary_path.read_text(encoding="utf-8"))
        layout.addWidget(browser)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=dialog)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.exec()

    def _open_quick_start(self) -> None:
        """Open docs/quick-start.md in the OS default markdown viewer / browser."""
        path = self._locate_doc("quick-start.md")
        if path is None or not path.is_file():
            QMessageBox.information(
                self,
                "Quick Start unavailable",
                "Could not find docs/quick-start.md.",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def _open_logs_folder(self) -> None:
        """Reveal ``~/.capa/`` in the OS file manager."""
        folder = Path.home() / ".capa"
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder.resolve())))

    def _show_about(self) -> None:
        try:
            from capa import __version__  # noqa: PLC0415
        except ImportError:
            version = "unknown"
        else:
            version = __version__
        QMessageBox.about(
            self,
            "About capa",
            f"<b>capa</b><br/>"
            f"Controlled-Atmosphere Cone Calorimeter<br/><br/>"
            f"Version: {version}<br/>",
        )

    def _locate_doc(self, filename: str) -> Path | None:
        """Find a doc by name relative to the repo root.

        Walks up from this module's __file__ looking for a ``docs/``
        directory. Returns ``None`` if no candidate exists — capa might
        be running from a wheel install without bundled docs.
        """
        here = Path(__file__).resolve()
        for parent in here.parents:
            candidate = parent / "docs" / filename
            if candidate.is_file():
                return candidate
        return None

    def _on_device_action(self, name: str) -> None:
        """Handle "Open Manual Control" from the Setup tab right-click menu."""
        if not self._controller.hardware_ready:
            self._status.showMessage(
                "Hardware is still initializing; manual controls are disabled.", 4000
            )
            return
        self._manual_dock.reveal(name)

    def _on_config_load_started(self, progress: object) -> None:
        if not self._config_load_ui_available():
            return
        self._set_config_loading_ui(True)
        self._status.showMessage("Preparing hardware…")
        if isinstance(progress, ConfigLoadProgress):
            self._show_or_update_hardware_dialog(progress)

    def _on_config_load_progress(self, progress: object) -> None:
        if isinstance(progress, ConfigLoadProgress):
            self._show_or_update_hardware_dialog(progress)

    def _on_config_load_finished(self, progress: object) -> None:
        if isinstance(progress, ConfigLoadProgress):
            self._show_or_update_hardware_dialog(progress)
            if progress.state is ConfigLoadState.READY:
                # Auto-clear so the status bar's pills become visible
                # again after the operator has had time to read the ack.
                self._status.showMessage("Hardware ready.", 3000)
            elif progress.state is ConfigLoadState.FAILED:
                # Failure: give the operator longer to read, but still
                # auto-clear so the live pills aren't permanently hidden.
                self._status.showMessage(progress.message, 8000)
            else:
                # Any other terminal state still drops us back to the
                # pills — clear the "Preparing hardware…" message that
                # was set in _on_config_load_started.
                self._status.clearMessage()
        else:
            self._status.clearMessage()
        self._set_config_loading_ui(False)

    def _on_hardware_ready_changed(self, ready: bool) -> None:
        if self._config_loading:
            return
        self._manual_dock.setEnabled(ready)

    def _set_config_loading_ui(self, loading: bool) -> None:
        self._config_loading = loading
        hardware_ready = self._controller.hardware_ready
        if self._open_config_action is not None:
            self._open_config_action.setEnabled(
                not loading
                and not self._shutdown_started
                and not self._controller.shutdown_requested
            )
        self._manual_dock.setEnabled(hardware_ready and not loading)

    def _register_dock_view_action(
        self,
        dock: QDockWidget,
        text: str,
        shortcut: str,
    ) -> QAction:
        """Insert a dock's ``toggleViewAction`` into the View menu.

        The action is inserted before the dynamic-anchor separator so
        config-driven docks (Numerics / Camera / Diagnostics) stay
        grouped after the static docks (Events / Log / Manual Control).
        The returned :class:`QAction` is owned by the dock; callers keep
        it so they can :meth:`removeAction` when the dock is rebuilt.
        """
        toggle = dock.toggleViewAction()
        toggle.setText(text)
        toggle.setShortcut(shortcut)
        self._view_menu.insertAction(self._view_dynamic_anchor, toggle)
        return toggle

    def _on_reset_window_layout(self) -> None:
        """View → Reset window layout — revert docks to the default state.

        Also deletes the persisted state file so the *next* launch sees
        a clean slate. The default layout was captured during
        ``__init__`` before user state was restored.
        """
        if self._default_window_state is not None:
            self.restoreState(self._default_window_state)
        if self._default_window_geometry is not None:
            self.restoreGeometry(self._default_window_geometry)
        with contextlib.suppress(OSError):
            if WINDOW_STATE_PATH.exists():
                WINDOW_STATE_PATH.unlink()

    def _config_load_ui_available(self) -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        return True

    def _show_or_update_hardware_dialog(self, progress: ConfigLoadProgress) -> None:
        if not self._config_load_ui_available():
            return
        if self._hardware_dialog is None:
            self._hardware_dialog = HardwareInitDialog(self)
            self._hardware_dialog.finished.connect(self._on_hardware_dialog_finished)
            self._hardware_dialog.open()
        self._hardware_dialog.update_progress(progress)

    def _on_hardware_dialog_finished(self, _result: int = 0) -> None:
        self._hardware_dialog = None

    def _update_method_tab_title(self) -> None:
        """Decorate the Method tab label with the loaded method's name so
        the operator can tell which method is active without switching to
        the tab. Falls back to ``"Method"`` when no method is loaded.

        When the active procedure ignores the method, the disabled-state
        label takes precedence — :meth:`_apply_method_tab_gate` rewrites
        the title to surface *why* the tab is unclickable, which is
        more useful than the method's name in that mode.
        """
        if not self._tabs.isTabEnabled(_METHOD_TAB_INDEX):
            return
        if self._method_tab.has_method():
            name = self._method_tab.current_method_name()
            self._tabs.setTabText(_METHOD_TAB_INDEX, f"Method — {name}")
        else:
            self._tabs.setTabText(_METHOD_TAB_INDEX, "Method")

    def _on_procedure_changed(self, procedure_id: object) -> None:
        """Setup-tab procedure selection changed — gate the Method tab.

        Looks up ``uses_method`` for the selected procedure (defaulting
        to ``True`` when the registry doesn't know the id, so a typo
        doesn't hide the tab) and flips the Method tab between enabled
        and disabled states. When disabling, also auto-switch the
        operator off the Method tab if they happen to be on it —
        leaving them parked on a disabled tab body would be confusing.
        """
        proc_id = str(procedure_id) if procedure_id is not None else ""
        uses_method = self._lookup_uses_method(proc_id)
        self._apply_method_tab_gate(uses_method, procedure_id=proc_id)

    def _apply_method_tab_gate(self, uses_method: bool, *, procedure_id: str) -> None:
        """Drive the Method tab's enabled / label / tooltip state.

        Split out so tests can drive the UI state directly without
        having to fake a procedure-registry discovery, and so the
        no-procedure-yet case (empty id from a fresh ``clear()``) and
        the explicit-selection case share one code path.
        """
        if uses_method:
            self._tabs.setTabEnabled(_METHOD_TAB_INDEX, True)
            self._tabs.setTabToolTip(_METHOD_TAB_INDEX, "")
            # Restore the label from the method-tab state (handles
            # both "Method" and "Method — <name>" forms).
            self._update_method_tab_title()
            return
        # Disabled state — the operator needs to know *why* the tab
        # they were just using is suddenly unclickable. A label hint
        # plus a hover tooltip with the procedure name covers both
        # the glance-at-the-tab and the hover-to-investigate flows.
        # Snapshot the current tab BEFORE disabling: ``setTabEnabled(False)``
        # on the active tab makes Qt auto-jump to the next enabled tab
        # (Run, on the right), which is even more disorienting than
        # staying put. We send the operator back to Setup explicitly.
        was_on_method = self._tabs.currentIndex() == _METHOD_TAB_INDEX
        readable = procedure_id or "the selected procedure"
        self._tabs.setTabText(_METHOD_TAB_INDEX, "Method (not used)")
        self._tabs.setTabToolTip(
            _METHOD_TAB_INDEX,
            f"{readable} does not use a method — it commands its own setpoints. "
            f"Switch to a method-driven procedure (e.g. capa.builtin.recipe_runner) "
            f"to edit method steps.",
        )
        self._tabs.setTabEnabled(_METHOD_TAB_INDEX, False)
        if was_on_method:
            self._tabs.setCurrentWidget(self._setup_tab)

    def _lookup_uses_method(self, procedure_id: str) -> bool:
        """Read ``uses_method`` for ``procedure_id`` from the registry cache.

        Unknown ids default to ``True`` (the safe choice — the tab
        stays visible). The cache is populated on first miss and on
        :meth:`_refresh_procedure_uses_method_cache`.
        """
        if not procedure_id:
            return True
        if procedure_id in self._procedure_uses_method:
            return self._procedure_uses_method[procedure_id]
        self._refresh_procedure_uses_method_cache()
        return self._procedure_uses_method.get(procedure_id, True)

    def _refresh_procedure_uses_method_cache(self) -> None:
        """Rebuild the ``procedure_id -> uses_method`` cache from the registry.

        Best-effort: any discovery failure leaves the cache empty so
        the gate falls back to the always-True default. The cache is
        rebuilt rather than amended to pick up plugin
        install/uninstall between calls.
        """
        try:
            from capa.core.plugins_runtime import (  # noqa: PLC0415
                ProcedureRegistry,
                resolve_mode,
            )

            registry = ProcedureRegistry.discover(mode=resolve_mode())
        except Exception as exc:
            _logger.warning("ui.procedure_registry_discover_failed", error=str(exc))
            self._procedure_uses_method = {}
            return
        cache: dict[str, bool] = {}
        for proc_id in registry.ids():
            loaded = registry.get(proc_id)
            cls = loaded.cls if loaded is not None else None
            cache[proc_id] = procedure_uses_method(cls) if cls is not None else True
        self._procedure_uses_method = cache

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
        # stage ordering, and the hard wall-clock fuse.
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

    def _on_shutdown_stage(self, stage: object) -> None:
        """Surface shutdown stages via the status bar.

        Connected to :attr:`ShutdownCoordinator.stage_changed`. The
        status bar is the operator's signal that the [×] click was
        received and shutdown is making progress.
        """
        if not isinstance(stage, ShutdownStage):
            return
        msg = status_message_for_stage(stage)
        if msg is not None:
            # Shutdown stages replace each other quickly; the 4 s
            # timeout matches the other transient status messages.
            self._status.showMessage(msg, 4000)

    def _on_shutdown_completed(self, result: object) -> None:
        """Flip the close-flow state machine and re-trigger window close.

        Connected to :attr:`ShutdownCoordinator.completed`. Logging the
        outcome here gives ops one structured event per shutdown attempt
        without needing to grep the coordinator's per-stage logs.

        After hiding the window we explicitly poke ``QApplication.quit``
        instead of relying on Qt's ``quitOnLastWindowClosed`` heuristic.
        That heuristic depends on every top-level dialog having no
        parent (a non-modal DiscoveryDialog with a Setup-tab parent is
        the typical counterexample), and on the event loop ticking
        once more after the slot returns. In practice, with the qasync
        loop wrapping ``app.exec()``, we've seen the loop sit idle past
        the last log line until SIGINT — explicit ``quit()`` makes the
        exit deterministic and is idempotent if Qt was already going to
        quit anyway.
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
        app = QApplication.instance()
        if app is not None:
            app.quit()

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
    def log_dock(self) -> LogDock:
        return self._log_dock

    @property
    def numerics_dock(self) -> NumericsDock | None:
        return self._numerics_dock

    @property
    def camera_preview_dock(self) -> CameraPreviewDock | None:
        return self._camera_preview_dock


__all__ = ["WINDOW_STATE_PATH", "MainWindow"]
