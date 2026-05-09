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
from PyQt6.QtCore import QByteArray, Qt
from PyQt6.QtGui import QAction, QCloseEvent
from PyQt6.QtWidgets import (
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QTabWidget,
)

from capa.core.errors import CapaError
from capa.experiment.config import ExperimentConfig
from capa.experiment.engine import EngineResult, EngineState
from capa.storage.catalog import RunCatalog
from capa.ui.docks.camera_preview import CameraPreviewDock
from capa.ui.docks.events import EventsDock
from capa.ui.docks.numerics import NumericsDock
from capa.ui.state import RunController
from capa.ui.statusbar import CapaStatusBar, OperatorIdProvider
from capa.ui.tabs.run import RunTab
from capa.ui.tabs.setup import SetupTab

WINDOW_STATE_PATH: Final[Path] = Path.home() / ".capa" / "window_state.json"

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
    ) -> None:
        super().__init__()
        self.setWindowTitle("capa")
        self.resize(1400, 900)

        self._runs_root: Path = runs_root
        self._operator_provider = OperatorIdProvider()

        self._controller = RunController(
            runs_root=runs_root,
            plugins_lock=plugins_lock,  # type: ignore[arg-type]
            catalog=catalog,
            repo_root=repo_root,
            lockfile_source=lockfile_source,
            configure_logging_for_bundle=configure_logging_for_bundle,
            parent=self,
        )

        self._setup_tab = SetupTab(self)
        self._run_tab = RunTab(controller=self._controller, parent=self)

        self._tabs = QTabWidget(self)
        self._tabs.addTab(self._setup_tab, "Setup")
        self._tabs.addTab(self._run_tab, "Run")
        self.setCentralWidget(self._tabs)

        # Numerics / Camera-preview docks are constructed when a config
        # loads — both need the parsed config (channels / cameras). Until
        # then, the events dock keeps the bottom area populated so the
        # layout stays consistent.
        self._numerics_dock: NumericsDock | None = None
        self._camera_preview_dock: CameraPreviewDock | None = None
        self._events_dock = EventsDock(self)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._events_dock)

        # Wire events dock to the controller.
        self._controller.event_received.connect(self._events_dock.append_event)
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
        if menu is None:
            return
        file_menu = menu.addMenu("&File")
        if file_menu is None:
            return
        open_action = QAction("&Open Config…", self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self._on_open_config)
        file_menu.addAction(open_action)

        file_menu.addSeparator()

        quit_action = QAction("&Quit", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

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
        self._setup_tab.load_config(cfg)
        self._run_tab.load_config(cfg)
        self._operator_provider.set_operator_id(cfg.operator.id)

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

        # Replace the camera-preview dock the same way. Subscribes to the
        # controller's preview_received signal; tiles only render frames
        # for cameras whose adapters declare CameraCapability.LIVE_PREVIEW
        # (filtering happens engine-side in _drain_preview).
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

        title_path = f" — {path.name}" if path else ""
        self.setWindowTitle(f"capa{title_path}")
        _logger.info("ui.config_loaded", path=str(path) if path else None)

    def _on_state(self, state: object) -> None:
        # When a run starts, the controller has rebuilt the buffer registry.
        # Rebind the numerics dock to point at the new one.
        if (
            isinstance(state, EngineState)
            and state is EngineState.RUNNING
            and self._numerics_dock is not None
        ):
            self._numerics_dock.set_registry(self._controller.buffers)

    def _on_run_finished(self, result: object) -> None:
        if not isinstance(result, EngineResult):
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
            self._controller.request_abort(mode="immediate")
        self._save_window_state()
        super().closeEvent(event)

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
    def events_dock(self) -> EventsDock:
        return self._events_dock

    @property
    def numerics_dock(self) -> NumericsDock | None:
        return self._numerics_dock

    @property
    def camera_preview_dock(self) -> CameraPreviewDock | None:
        return self._camera_preview_dock


__all__ = ["WINDOW_STATE_PATH", "MainWindow"]
