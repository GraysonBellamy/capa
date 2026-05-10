"""Run tab — the instrument-console view.

Plan §10.1. Header with state badge + Start / Abort controls; live
PyQtGraph plot panes filling the body. Numerics live in a dock managed by
:class:`MainWindow`, not here.

P1 collapses Arm/Start into a single Start button — preflight runs inside
:meth:`ExperimentEngine.run` (PREPARING state) and any failure is surfaced
as the run's :class:`EngineResult`. The Method tab and a real Arm phase
arrive in P3.
"""

from __future__ import annotations

import time
from typing import Final

from PySide6.QtCore import QSize, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from capa.core.ringbuffer import RingBufferRegistry
from capa.experiment.config import ExperimentConfig
from capa.experiment.engine import EngineResult, EngineState
from capa.ui.plots.pane import PlotPane
from capa.ui.state import RunController
from capa.ui.theme import (
    COLOR_FAIL,
    COLOR_IDLE,
    COLOR_OK,
    COLOR_RUNNING,
    COLOR_WARN,
    monospace_font,
)

ELAPSED_REFRESH_MS: Final[int] = 1000

_STATE_TEXT = {
    EngineState.IDLE: "Idle",
    EngineState.PREPARING: "Preparing…",
    EngineState.RUNNING: "Running",
    EngineState.ABORTING: "Aborting",
    EngineState.FINALIZING: "Finalizing…",
    EngineState.SEALED: "Sealed",
    EngineState.FAILED: "Failed",
}

_STATE_COLOR = {
    EngineState.IDLE: COLOR_IDLE,
    EngineState.PREPARING: COLOR_WARN,
    EngineState.RUNNING: COLOR_RUNNING,
    EngineState.ABORTING: COLOR_WARN,
    EngineState.FINALIZING: COLOR_WARN,
    EngineState.SEALED: COLOR_OK,
    EngineState.FAILED: COLOR_FAIL,
}


class RunTab(QWidget):
    """Live run console.

    Owns the state header, control buttons, and the plot pane stack. Numerics
    and events docks are siblings managed by the main window — the run tab
    only owns the central widget.
    """

    def __init__(
        self,
        *,
        controller: RunController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller: RunController = controller
        self._config: ExperimentConfig | None = None
        self._run_started_mono: float | None = None
        self._plot_pane: PlotPane | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        layout.addWidget(self._build_header())

        self._plot_container = QWidget(self)
        self._plot_layout = QVBoxLayout(self._plot_container)
        self._plot_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._plot_container, 1)

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(ELAPSED_REFRESH_MS)
        self._elapsed_timer.timeout.connect(self._refresh_elapsed)

        self._controller.state_changed.connect(self._on_state)
        self._controller.run_finished.connect(self._on_run_finished)

    # ------------------------------------------------------------------ build

    def _build_header(self) -> QWidget:
        header = QWidget(self)
        h = QHBoxLayout(header)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(12)

        self._state_label = QLabel("Idle", header)
        font = monospace_font(point_size=14)
        font.setBold(True)
        self._state_label.setFont(font)
        self._state_label.setMinimumWidth(140)
        self._state_label.setStyleSheet(f"color: {COLOR_IDLE.name()};")
        h.addWidget(self._state_label)

        self._run_id_label = QLabel("—", header)
        self._run_id_label.setFont(monospace_font(point_size=10))
        self._run_id_label.setStyleSheet(f"color: {COLOR_IDLE.name()};")
        h.addWidget(self._run_id_label)

        self._elapsed_label = QLabel("00:00:00", header)
        self._elapsed_label.setFont(monospace_font(point_size=12))
        h.addWidget(self._elapsed_label)

        h.addStretch(1)

        self._start_btn = QPushButton("Start", header)
        self._start_btn.setMinimumSize(QSize(96, 36))
        self._start_btn.clicked.connect(self._on_start_clicked)
        h.addWidget(self._start_btn)

        # Abort = QToolButton with a popup so Safe-Shutdown vs Emergency are
        # one click apart, matching plan §9 / §13.2.
        self._abort_btn = QToolButton(header)
        self._abort_btn.setText("Abort ▾")
        self._abort_btn.setMinimumSize(QSize(110, 36))
        self._abort_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        abort_menu = QMenu(self._abort_btn)
        safe_action = QAction("Safe Shutdown", self)
        safe_action.triggered.connect(lambda: self._on_abort_clicked("safe_shutdown"))
        emergency_action = QAction("Emergency Abort", self)
        emergency_action.triggered.connect(lambda: self._on_abort_clicked("immediate"))
        abort_menu.addAction(safe_action)
        abort_menu.addAction(emergency_action)
        self._abort_btn.setMenu(abort_menu)
        # Default action (clicking the body of the button) = Safe Shutdown,
        # the lower-blast-radius option per plan §9.
        self._abort_btn.setDefaultAction(safe_action)
        self._abort_btn.setEnabled(False)
        h.addWidget(self._abort_btn)

        return header

    # ------------------------------------------------------------------ public

    def load_config(self, config: ExperimentConfig) -> None:
        """Stage a config for the next :meth:`_on_start_clicked`. Replaces
        the live plot pane with one matching the new channel set."""
        self._config = config
        self._run_id_label.setText(f"sample: {config.sample.id}  procedure: {config.procedure.id}")
        # Build a placeholder plot pane against an empty registry so the UI
        # has axes/legends visible before Start is clicked.
        empty = RingBufferRegistry()
        for ch in config.hardware.channels:
            empty.register(ch.name, decimate_to_hz=ch.decimate_to_hz)
        self._set_plot_pane(empty, config)

    def can_start(self) -> bool:
        return self._config is not None and not self._controller.is_active

    # ------------------------------------------------------------------ control

    def _on_start_clicked(self) -> None:
        if self._config is None or self._controller.is_active:
            return
        # Hand the controller a fresh start; it will rebuild buffers and
        # signal state transitions. The plot pane is rebound to the new
        # registry on the EngineState.RUNNING transition (see _on_state),
        # which matches the numerics dock — by the time RUNNING fires,
        # the controller has finished rebuilding buffers.
        self._controller.start(self._config)
        self._run_started_mono = time.monotonic()
        self._elapsed_timer.start()
        self._start_btn.setEnabled(False)
        self._abort_btn.setEnabled(True)

    def _on_abort_clicked(self, mode: str) -> None:
        self._controller.request_abort(mode=mode)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ slots

    def _on_state(self, state: EngineState) -> None:
        self._state_label.setText(_STATE_TEXT.get(state, str(state)))
        color = _STATE_COLOR.get(state, COLOR_IDLE)
        self._state_label.setStyleSheet(f"color: {color.name()};")
        if state is EngineState.RUNNING:
            self._abort_btn.setEnabled(True)
            # Buffers were rebuilt during controller.start(); rebind the
            # plot pane now that the new registry is populated.
            if self._config is not None:
                self._set_plot_pane(self._controller.buffers, self._config)
        if state in (EngineState.SEALED, EngineState.FAILED):
            self._abort_btn.setEnabled(False)

    def _on_run_finished(self, result: object) -> None:
        if isinstance(result, EngineResult):
            if result.bundle_path is not None:
                self._run_id_label.setText(f"run: {result.run_id}  ({result.run_status})")
            else:
                self._run_id_label.setText(f"run refused: {result.exit_reason or 'unknown'}")
        self._elapsed_timer.stop()
        # Defer the start-button re-enable: this slot fires from inside the
        # controller's task finally block, so the task is not yet `done()`
        # and `can_start()` would return False. One event-loop tick later
        # the task is finished and the check is accurate.
        QTimer.singleShot(0, lambda: self._start_btn.setEnabled(self.can_start()))
        self._abort_btn.setEnabled(False)
        if self._plot_pane is not None:
            self._plot_pane.stop()

    def _refresh_elapsed(self) -> None:
        if self._run_started_mono is None:
            return
        elapsed = int(time.monotonic() - self._run_started_mono)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        self._elapsed_label.setText(f"{h:02d}:{m:02d}:{s:02d}")

    # ------------------------------------------------------------------ internal

    def _set_plot_pane(self, registry: RingBufferRegistry, config: ExperimentConfig) -> None:
        # Tear down the old pane, build a new one bound to ``registry``.
        if self._plot_pane is not None:
            self._plot_pane.stop()
            self._plot_layout.removeWidget(self._plot_pane)
            self._plot_pane.deleteLater()
        pane = PlotPane(
            registry=registry,
            channels=list(config.hardware.channels),
            parent=self._plot_container,
        )
        self._plot_layout.addWidget(pane)
        self._plot_pane = pane
        pane.start()


__all__ = ["RunTab"]
