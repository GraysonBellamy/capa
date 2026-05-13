"""Persistent status bar — operational health at a glance.

Plan §10.4. Always visible. Polls live data at 1 Hz from the controller's
:class:`Conductor` (via :class:`RunController`'s readonly view) and the
OS (``psutil`` for disk free). The same metrics are written into
``manifest.json``'s ``queue_health`` block at finalize.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Final

import psutil
from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QLabel, QStatusBar, QWidget

from capa.core.ringbuffer import RingBufferRegistry
from capa.ui.state import RunController, RunUiResult, RunUiState
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
    RunUiState.IDLE: COLOR_IDLE,
    RunUiState.PREPARING: COLOR_WARN,
    RunUiState.RUNNING: COLOR_RUNNING,
    RunUiState.DRAINING: COLOR_WARN,
    RunUiState.FINALIZING: COLOR_WARN,
    RunUiState.SEALED: COLOR_OK,
    RunUiState.FAILED: COLOR_FAIL,
}


class CapaStatusBar(QStatusBar):
    """Pills: state · elapsed · UI overflow · saturation · loop lag · queue
    depth · safety queue · disk free · camera health · operator · bundle.

    Health philosophy: every pill shows a value that means something *right
    now*. Counters that conflate by-design events with real distress (the
    old "UI drops" total) and time-windowed statistics that linger past their
    relevance (the old sink-lag p99 over 1024 samples) have been replaced
    with current-state signals:

    * **UI overflow** shows ring-buffer rollover count; decimation is in
      the tooltip. Both are informational — they grow during normal
      operation once a ring reaches capacity, and neither indicates the
      UI thread is falling behind (use ``sat`` / ``loop`` / ``q`` for
      that).
    * **Saturation** reads ``blocked_since_ms`` per bridge and colors against
      the configured ``saturation_deadline_s`` (runtime-architecture.md §6.3).
    * **Loop lag** is the conductor's loop-lag p99 (warn 50ms, fail 200ms).
    * **Queue depth** shows current ``depth`` and lifetime ``depth_max``
      so historical worst is visible without pinning the current reading.

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
        self._ui_overflow_label = self._add_pill("UI overflow 0", font)
        self._saturation_label = self._add_pill("sat ok", font)
        self._loop_lag_label = self._add_pill("loop —", font)
        self._depth_label = self._add_pill("q —", font)
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

    def _on_state(self, state: object) -> None:
        if not isinstance(state, RunUiState):
            return
        color = _STATE_COLORS.get(state, COLOR_IDLE)
        self._state_label.setText(str(state))
        self._state_label.setStyleSheet(f"color: {color.name()}; font-weight: bold;")
        if state is RunUiState.RUNNING and self._run_started_mono is None:
            self._run_started_mono = time.monotonic()
        if state is RunUiState.IDLE:
            # Reset elapsed on IDLE transition; SEALED/FAILED freeze it instead.
            self._run_started_mono = None

    def _on_run_finished(self, result: object) -> None:
        if isinstance(result, RunUiResult) and result.bundle_path is not None:
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

        # UI ring buffer rollover counter — informational. Plot snapshots
        # are non-draining, so any ring will eventually start evicting its
        # oldest sample once it reaches capacity; that's the ring buffer
        # doing its job, not the UI thread falling behind. The actual
        # backpressure signals are loop / sat / q.
        buffers = self._controller.buffers
        overflow = buffers.total_overflow()
        decimated = buffers.total_decimated()
        self._ui_overflow_label.setText(f"UI overflow {overflow}")
        self._ui_overflow_label.setToolTip(self._build_buffer_tooltip(buffers))
        self._ui_overflow_label.setStyleSheet("")

        # Saturation, loop lag, depth — all pulled from conductor diagnostics.
        # Saturation is the load-bearing signal: any bridge with a non-None
        # blocked_since_ms means a producer is currently waiting for space.
        # As that approaches saturation_deadline_s the run will seal as
        # crashed_but_sealed (runtime-architecture.md §6.3).
        conductor = self._controller.conductor
        if conductor is None:
            self._saturation_label.setText("sat —")
            self._saturation_label.setStyleSheet("")
            self._loop_lag_label.setText("loop —")
            self._loop_lag_label.setStyleSheet("")
            self._depth_label.setText("q —")
            self._depth_label.setStyleSheet("")
        else:
            self._update_runtime_pills(conductor.runtime_diagnostics())

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

    def _build_buffer_tooltip(self, buffers: RingBufferRegistry) -> str:
        """Tooltip body for the UI-overflow pill.

        Lists per-channel ``overflow / decimated`` counts so the operator
        can attribute decimation events to a specific producer rather than
        seeing only the registry-wide totals. Channels with zero of both
        are omitted to keep the tooltip short on healthy runs. Sorted by
        decimated-desc then overflow-desc so the noisiest channel sits at
        the top.
        """
        per_channel = buffers.per_channel_drops()
        overflow_total = sum(o for o, _ in per_channel.values())
        decimated_total = sum(d for _, d in per_channel.values())
        header = (
            f"Overflow: {overflow_total} — ring rollovers, oldest sample evicted "
            "(normal once a buffer fills; not a backpressure signal).\n"
            f"Decimated: {decimated_total} — samples that arrived sooner than "
            "the channel's 1/decimate_to_hz interval since the last kept sample."
        )
        rows = [
            (name, ovf, dec) for name, (ovf, dec) in per_channel.items() if ovf or dec
        ]
        if not rows:
            return header
        rows.sort(key=lambda r: (r[2], r[1]), reverse=True)
        body_lines = ["", "per channel (overflow / decimated):"]
        # 8 channels covers the capa_real_full config with room to spare;
        # truncate beyond that so the tooltip stays readable.
        for name, ovf, dec in rows[:8]:
            body_lines.append(f"  {name}: {ovf} / {dec}")
        if len(rows) > 8:
            body_lines.append(f"  … (+{len(rows) - 8} more)")
        return header + "\n" + "\n".join(body_lines)

    def _update_runtime_pills(self, diag: dict[str, dict[str, float]]) -> None:
        """Pull the three runtime pills out of one diagnostics snapshot.

        Saturation: worst current ``blocked_since_ms`` across outbound
        bridges; colors against the configured ``saturation_deadline_s``
        so the pill turns yellow at 25% and red at 50% of the deadline.

        Loop lag: conductor loop p99 over its sliding window. Warn at
        50 ms, fail at 200 ms (runtime-architecture.md §14).

        Depth: worst current depth and lifetime max across bridges,
        rendered as ``cur/max`` for at-a-glance backlog. Current depth
        is what matters; max is shown for context only.
        """
        deadline_s = float(diag.get("runtime", {}).get("saturation_deadline_s", 10.0))
        worst_blocked_ms = -1.0
        worst_depth = 0
        worst_depth_max = 0
        for key, metrics in diag.items():
            if not key.startswith("bridge.outbound:"):
                continue
            blocked = float(metrics.get("blocked_since_ms", -1.0))
            if blocked > worst_blocked_ms:
                worst_blocked_ms = blocked
            depth = int(metrics.get("depth", 0))
            depth_max = int(metrics.get("depth_max", 0))
            if depth > worst_depth:
                worst_depth = depth
            if depth_max > worst_depth_max:
                worst_depth_max = depth_max

        if worst_blocked_ms < 0:
            self._saturation_label.setText("sat ok")
            self._saturation_label.setStyleSheet(f"color: {COLOR_OK.name()};")
        else:
            blocked_s = worst_blocked_ms / 1000.0
            self._saturation_label.setText(f"blocked {blocked_s:.1f}s")
            if blocked_s >= 0.5 * deadline_s:
                self._saturation_label.setStyleSheet(f"color: {COLOR_FAIL.name()};")
            elif blocked_s >= 0.25 * deadline_s:
                self._saturation_label.setStyleSheet(f"color: {COLOR_WARN.name()};")
            else:
                self._saturation_label.setStyleSheet("")

        cond = diag.get("loop.conductor", {})
        lag_p99 = float(cond.get("lag_p99_ms", 0.0))
        self._loop_lag_label.setText(f"loop {lag_p99:.0f} ms")
        if lag_p99 >= 200.0:
            self._loop_lag_label.setStyleSheet(f"color: {COLOR_FAIL.name()};")
        elif lag_p99 >= 50.0:
            self._loop_lag_label.setStyleSheet(f"color: {COLOR_WARN.name()};")
        else:
            self._loop_lag_label.setStyleSheet("")

        self._depth_label.setText(f"q {worst_depth}/{worst_depth_max}")


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
