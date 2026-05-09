"""Persistent status bar — operational health at a glance.

Plan §10.4. Always visible. Polls live data at 1 Hz from the controller's
:class:`~capa.experiment.engine.ExperimentEngine` and the OS (``psutil`` for
disk free). The same metrics are written into ``manifest.json``'s
``queue_health`` block at finalize.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Final

import psutil
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QLabel, QStatusBar, QWidget

from capa.experiment.engine import EngineResult, EngineState
from capa.ui.state import RunController
from capa.ui.theme import (
    COLOR_FAIL,
    COLOR_IDLE,
    COLOR_OK,
    COLOR_RUNNING,
    COLOR_WARN,
    monospace_font,
)

REFRESH_INTERVAL_MS: Final[int] = 1000

_STATE_COLORS = {
    EngineState.IDLE: COLOR_IDLE,
    EngineState.PREPARING: COLOR_WARN,
    EngineState.RUNNING: COLOR_RUNNING,
    EngineState.ABORTING: COLOR_WARN,
    EngineState.FINALIZING: COLOR_WARN,
    EngineState.SEALED: COLOR_OK,
    EngineState.FAILED: COLOR_FAIL,
}


class CapaStatusBar(QStatusBar):
    """Plan §10.4 readouts: state · elapsed · UI drops · sink lag · safety
    queue · disk free · camera health · operator · bundle path.

    The camera-health pill is a placeholder until P4. Safety queue is also
    a placeholder until P3+ adds :class:`SafetyMonitor`.
    """

    def __init__(
        self,
        *,
        controller: RunController,
        runs_root: Path,
        operator_id_provider: OperatorIdProvider,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller: RunController = controller
        self._runs_root: Path = runs_root
        self._operator_id_provider: OperatorIdProvider = operator_id_provider
        self._run_started_mono: float | None = None

        font = monospace_font(point_size=9)

        self._state_label = QLabel("idle", self)
        self._state_label.setFont(font)
        self.addWidget(self._state_label, 0)

        self._elapsed_label = self._add_pill("00:00:00", font)
        self._ui_drops_label = self._add_pill("UI drops 0", font)
        self._sink_lag_label = self._add_pill("sink lag 0 ms", font)
        self._safety_label = self._add_pill("safety queue 0", font)
        self._disk_label = self._add_pill("disk —", font)
        self._camera_label = self._add_pill("cam n/a", font)
        self._operator_label = self._add_pill("op: —", font)

        self._bundle_label = QLabel("", self)
        self._bundle_label.setFont(font)
        # Right-aligned permanent widget so the bundle path stays at the end
        # of the bar even when others grow.
        self.addPermanentWidget(self._bundle_label, 1)

        # React to the controller's state signal in addition to the timer
        # so transitions show up promptly, not on the next 1 s tick.
        self._controller.state_changed.connect(self._on_state)
        self._controller.run_finished.connect(self._on_run_finished)

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        self._refresh()

    def _add_pill(self, text: str, font: QFont, *, fixed_width: bool = True) -> QLabel:
        label = QLabel(text, self)
        label.setFont(font)
        if fixed_width:
            # A wide-enough min so the bar layout doesn't reflow on every
            # update.
            label.setMinimumWidth(120)
        self.addWidget(label, 0)
        return label

    # ------------------------------------------------------------------ slots

    def _on_state(self, state: EngineState) -> None:
        color = _STATE_COLORS.get(state, COLOR_IDLE)
        self._state_label.setText(str(state))
        self._state_label.setStyleSheet(f"color: {color.name()}; font-weight: bold;")
        if state is EngineState.RUNNING and self._run_started_mono is None:
            self._run_started_mono = time.monotonic()
        if state is EngineState.IDLE:
            # Reset elapsed on IDLE transition; SEALED/FAILED freeze it instead.
            self._run_started_mono = None

    def _on_run_finished(self, result: object) -> None:
        if isinstance(result, EngineResult) and result.bundle_path is not None:
            self._bundle_label.setText(f"bundle: {result.bundle_path}")
        else:
            self._bundle_label.setText("")

    # ------------------------------------------------------------------ refresh

    def _refresh(self) -> None:
        # Operator id (free-text in P1; full registry in P3).
        op = self._operator_id_provider.current_operator_id() or "—"
        self._operator_label.setText(f"op: {op}")

        # Elapsed.
        if self._run_started_mono is not None:
            elapsed = int(time.monotonic() - self._run_started_mono)
            h, rem = divmod(elapsed, 3600)
            m, s = divmod(rem, 60)
            self._elapsed_label.setText(f"{h:02d}:{m:02d}:{s:02d}")

        # UI ring buffer drops (rolling-cumulative; precise rolling window
        # comes with P6 polish).
        self._ui_drops_label.setText(f"UI drops {self._controller.buffers.total_dropped()}")

        # Sink writer lag — pull worst-case lag across queue collectors.
        engine = self._controller.engine
        lag_ms = 0.0
        worst_queue_depth = 0
        if engine is not None:
            for q in engine.metrics.queues.values():
                if q.lag_s_max * 1000.0 > lag_ms:
                    lag_ms = q.lag_s_max * 1000.0
                if q.depth_max > worst_queue_depth:
                    worst_queue_depth = q.depth_max
        self._sink_lag_label.setText(f"sink lag {lag_ms:.0f} ms · {worst_queue_depth}q")

        # Safety queue placeholder — P3+ when SafetyMonitor lands. Showing 0
        # honestly here makes the field's eventual non-zero values stand out.
        self._safety_label.setText("safety queue 0")

        # Disk free + projected video fill (no cameras in P1 → projection 0).
        try:
            usage = psutil.disk_usage(str(self._runs_root))
            free_gb = usage.free / (1024**3)
            self._disk_label.setText(f"disk {free_gb:.1f} GB")
            if usage.percent > 95:
                self._disk_label.setStyleSheet(f"color: {COLOR_FAIL.name()};")
            elif usage.percent > 85:
                self._disk_label.setStyleSheet(f"color: {COLOR_WARN.name()};")
            else:
                self._disk_label.setStyleSheet("")
        except OSError:
            self._disk_label.setText("disk —")

        # Camera health placeholder — P4 will plug into camera health probes.
        self._camera_label.setText("cam n/a")
        self._camera_label.setStyleSheet(f"color: {COLOR_IDLE.name()};")


class OperatorIdProvider:
    """Tiny indirection so the status bar doesn't import the operator widget
    or the main window directly."""

    __slots__ = ("_value",)

    def __init__(self, initial: str = "") -> None:
        self._value: str = initial

    def current_operator_id(self) -> str:
        return self._value

    def set_operator_id(self, value: str) -> None:
        self._value = value


__all__ = ["REFRESH_INTERVAL_MS", "CapaStatusBar", "OperatorIdProvider"]
