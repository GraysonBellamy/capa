"""Run tab — the instrument-console view.

Header with state badge + Start / Abort controls; live PyQtGraph plot
panes filling the body. Numerics live in a dock managed by
:class:`MainWindow`, not here.

Arm/Start is collapsed into a single Start button — preflight runs
inside :meth:`Conductor.start` (PREPARING state) and any failure is
surfaced as the run's :class:`RunUiResult`.
"""

from __future__ import annotations

import time
from typing import Final

from PySide6.QtCore import QSize, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from capa.core.ringbuffer import RingBufferRegistry
from capa.experiment.config import ExperimentConfig
from capa.runtime.lifecycle import PoolState
from capa.ui.hold_to_confirm import HoldToConfirmButton
from capa.ui.plots.pane import PlotPane
from capa.ui.state import RunController, RunUiResult, RunUiState
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
    RunUiState.IDLE: "Idle",
    RunUiState.PREPARING: "Preparing…",
    RunUiState.RUNNING: "Running",
    RunUiState.DRAINING: "Draining…",
    RunUiState.FINALIZING: "Finalizing…",
    RunUiState.SEALED: "Sealed",
    RunUiState.FAILED: "Failed",
}

_STATE_COLOR = {
    RunUiState.IDLE: COLOR_IDLE,
    RunUiState.PREPARING: COLOR_WARN,
    RunUiState.RUNNING: COLOR_RUNNING,
    RunUiState.DRAINING: COLOR_WARN,
    RunUiState.FINALIZING: COLOR_WARN,
    RunUiState.SEALED: COLOR_OK,
    RunUiState.FAILED: COLOR_FAIL,
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
        # Re-evaluate the Start button whenever the pool finishes opening
        # (or is torn down) so the operator can't click Start while the
        # pool is still in PoolState.OPENING — which would crash the
        # conductor's preparation state with PoolStateError.
        self._controller.pool_changed.connect(self._on_pool_changed)
        ready_signal = getattr(self._controller, "hardware_ready_changed", None)
        if ready_signal is not None:
            ready_signal.connect(self._on_hardware_ready_changed)

    # ------------------------------------------------------------------ build

    def _build_header(self) -> QWidget:
        header = QWidget(self)
        header.setObjectName("run_header")
        h = QHBoxLayout(header)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(12)

        self._state_label = QLabel("Idle", header)
        self._state_label.setObjectName("run_state_badge")
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
        self._start_btn.setObjectName("run_start_button")
        self._start_btn.setMinimumSize(QSize(96, 36))
        self._start_btn.setEnabled(False)
        self._start_btn.clicked.connect(self._on_start_clicked)
        h.addWidget(self._start_btn)

        # Stop run — safe graceful shutdown. Single click is fine
        # because it runs the method's safe-shutdown step (or a default
        # cooldown for free-run) and seals the bundle. Non-destructive.
        self._stop_btn = QPushButton("Stop run", header)
        self._stop_btn.setObjectName("run_stop_button")
        self._stop_btn.setMinimumSize(QSize(110, 36))
        self._stop_btn.setToolTip(
            "Graceful shutdown — runs the safe-shutdown step (or default "
            "cooldown for free-run), seals the bundle."
        )
        self._stop_btn.clicked.connect(lambda: self._on_abort_clicked("safe_shutdown"))
        self._stop_btn.setEnabled(False)
        h.addWidget(self._stop_btn)

        # Emergency stop — hold-to-confirm. Mis-clicks during a long
        # run can lose hours of data, so a single click is not enough.
        # Holding for 1 second fills the button with a progress bar;
        # release before that cancels.
        self._emergency_btn = HoldToConfirmButton(
            "⛔  Emergency stop",
            accent=COLOR_FAIL,
            parent=header,
        )
        self._emergency_btn.setObjectName("run_emergency_button")
        self._emergency_btn.setMinimumSize(QSize(160, 36))
        self._emergency_btn.setToolTip(
            "Hold for 1 second to immediately abort the run. Skips the safe "
            "shutdown step; bundle is still sealed."
        )
        self._emergency_btn.confirmed.connect(lambda: self._on_abort_clicked("immediate"))
        self._emergency_btn.setEnabled(False)
        h.addWidget(self._emergency_btn)

        return header

    # ------------------------------------------------------------------ public

    def load_config(self, config: ExperimentConfig) -> None:
        """Stage a config for the next :meth:`_on_start_clicked`. Replaces
        the live plot pane with one matching the new channel set."""
        self._config = config
        self._run_id_label.setText(self._build_run_id_text(config))
        # Build a placeholder plot pane against an empty registry so the UI
        # has axes/legends visible before Start is clicked.
        empty = RingBufferRegistry()
        for ch in config.hardware.channels:
            empty.register(ch.name, decimate_to_hz=ch.decimate_to_hz)
        self._set_plot_pane(empty, config)
        # The pool may already be open (config reload reusing an OPEN
        # pool) or still opening; sync the Start button either way.
        self._start_btn.setEnabled(self.can_start())

    def can_start(self) -> bool:
        """``True`` if the Run tab's preconditions are satisfied."""
        if self._config is None or self._controller.is_active:
            return False
        if not bool(getattr(self._controller, "hardware_ready", True)):
            return False
        pool = self._controller.worker_pool
        return pool is not None and pool.state is PoolState.OPEN

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
        self._set_abort_buttons_enabled(True)

    def _on_abort_clicked(self, mode: str) -> None:
        self._controller.request_abort(mode=mode)

    def _set_abort_buttons_enabled(self, enabled: bool) -> None:
        """Toggle Stop run + Emergency stop together.

        Both buttons share the same enabled state: either there's a run
        to stop or there isn't.
        """
        self._stop_btn.setEnabled(enabled)
        self._emergency_btn.setEnabled(enabled)

    def _on_pool_changed(self, _pool: object) -> None:
        """``RunController.pool_changed`` fires when the pool finishes
        opening (with the pool) or is torn down (with ``None``). Sync the
        Start button to the new readiness state in either case."""
        self._start_btn.setEnabled(self.can_start())

    def _on_hardware_ready_changed(self, _ready: bool) -> None:
        self._start_btn.setEnabled(self.can_start())

    # ------------------------------------------------------------------ slots

    def _on_state(self, state: object) -> None:
        if not isinstance(state, RunUiState):
            return
        self._state_label.setText(_STATE_TEXT.get(state, str(state)))
        color = _STATE_COLOR.get(state, COLOR_IDLE)
        self._state_label.setStyleSheet(f"color: {color.name()};")
        if state is RunUiState.RUNNING:
            self._set_abort_buttons_enabled(True)
            # Buffers were rebuilt during controller.start(); rebind the
            # plot pane now that the new registry is populated.
            if self._config is not None:
                self._set_plot_pane(self._controller.buffers, self._config)
        if state in (RunUiState.SEALED, RunUiState.FAILED):
            self._set_abort_buttons_enabled(False)

    def _on_run_finished(self, result: object) -> None:
        if isinstance(result, RunUiResult):
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
        self._set_abort_buttons_enabled(False)
        if self._plot_pane is not None:
            self._plot_pane.stop()

    def _refresh_elapsed(self) -> None:
        if self._run_started_mono is None:
            return
        elapsed = int(time.monotonic() - self._run_started_mono)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        self._elapsed_label.setText(f"{h:02d}:{m:02d}:{s:02d}")

    def _build_run_id_text(self, config: ExperimentConfig) -> str:
        """Compose the header's run-id label.

        Folds the sample / procedure / mode triplet into one line.
        Mode is *Free run* when no method is loaded or *Method: <name>*
        otherwise — the same translation the old status strip carried,
        now collapsed into the header so the Run tab only has one
        "what's loaded" surface above the plot.
        """
        if config.method is None:
            mode = "Free run"
        else:
            mode = f"Method: {getattr(config.method, 'name', None) or 'method'}"
        return f"sample: {config.sample.id}  ·  procedure: {config.procedure.id}  ·  {mode}"

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
