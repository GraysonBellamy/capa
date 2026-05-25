"""Acquisition diagnostics dock — per-worker rate, jitter, and health.

Reads conductor.runtime_diagnostics() at 1 Hz and displays, for each
worker: the actual poll rate, poll-period p50 / jitter, and time since
the last poll. Rows turn yellow/red when thresholds are exceeded so the
operator can spot a stalled or degraded device at a glance.

The rate / period / age values are all keyed on ``SourceRecord``
emissions (one per actual poll), NOT on every ``adapter.stream()`` yield.
Every adapter yields a burst of emissions per poll — 1 ``SourceRecord``
plus N ``ChannelSample``s plus the occasional ``DeviceSnapshot`` — so a
naive 1/tick-duration would report tens of thousands of Hz for a 1 Hz
device. The dock derives rate from :attr:`WorkerMetrics.poll_rate_hz`
(inverse of poll-period p50) instead. See :mod:`capa.runtime.metrics`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDockWidget,
    QGridLayout,
    QLabel,
    QScrollArea,
    QWidget,
)

from capa.ui.theme import COLOR_FAIL, COLOR_IDLE, COLOR_OK, COLOR_WARN, monospace_font

if TYPE_CHECKING:
    from capa.ui.state import RunController

REFRESH_INTERVAL_MS: Final[int] = 1000

_HEADERS: Final = ("Device", "Rate (Hz)", "p50 (ms)", "Jitter (ms)", "Age (s)")
_COL_WIDTHS: Final = (None, 82, 82, 90, 62)  # None = stretch column
_TOOLTIPS: Final = (
    "Adapter name(s) hosted by this worker.",
    "Actual poll rate — inverse of poll-period p50 (one observation per "
    "SourceRecord, not per emission).",
    "Poll period p50, ms. The typical wall-clock gap between consecutive polls.",
    "Poll period p99 − p50, ms. Long tail of poll lateness.",
    "Wall-clock seconds since the most recent poll. Turns yellow at 2 s, red at 5 s.",
)

# Health thresholds
_AGE_FAIL_S: Final = 5.0
_AGE_WARN_S: Final = 2.0
_DEFAULT_LOOP_LAG_WARN_MS: Final = 50.0


def _val_label(font: object, width: int) -> QLabel:
    lbl = QLabel("—")
    lbl.setFont(font)  # type: ignore[arg-type]
    lbl.setFixedWidth(width)
    lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    lbl.setStyleSheet(f"color: {COLOR_IDLE.name()};")
    return lbl


@dataclass
class _RowLabels:
    device: QLabel
    rate: QLabel
    p50: QLabel
    jitter: QLabel
    age: QLabel

    def set_idle(self) -> None:
        """Reset the dock to its idle / no-run display."""
        css = f"color: {COLOR_IDLE.name()};"
        self.device.setStyleSheet(css)
        for lbl in (self.rate, self.p50, self.jitter, self.age):
            lbl.setText("—")
            lbl.setStyleSheet(css)

    def update(self, w: dict[str, float], *, loop_lag_warn_ms: float) -> None:
        """Refresh this widget from the latest model data."""
        polls = int(w.get("polls_emitted", 0.0))
        rate_hz = w.get("poll_rate_hz", 0.0)
        p50 = w.get("poll_period_p50_ms", 0.0)
        p99 = w.get("poll_period_p99_ms", 0.0)
        loop_lag = w.get("loop_lag_p99_ms", 0.0)
        age = w.get("last_sample_age_s", 0.0)

        # poll_period_p50 needs at least two polls to be meaningful (it
        # measures gaps). Below that, rate / period / jitter are "—" so we
        # don't flash misleading initial numbers while the first sample
        # round-trips.
        have_period = p50 > 0.0 and polls >= 2

        self.rate.setText(f"{rate_hz:.2f}" if have_period else "—")
        self.p50.setText(f"{p50:.1f}" if have_period else "—")
        self.jitter.setText(f"±{(p99 - p50):.1f}" if have_period else "—")
        self.age.setText(f"{age:.1f}" if polls > 0 else "—")

        if polls == 0:
            # No poll has landed yet — neutral (idle) coloring rather than
            # red, because the worker may simply still be warming up.
            color = COLOR_IDLE
        elif age > _AGE_FAIL_S:
            color = COLOR_FAIL
        elif age > _AGE_WARN_S or loop_lag > loop_lag_warn_ms:
            color = COLOR_WARN
        else:
            color = COLOR_OK

        css = f"color: {color.name()};"
        self.device.setStyleSheet(css)
        for lbl in (self.rate, self.p50, self.jitter, self.age):
            lbl.setStyleSheet(css)


class DiagnosticsDock(QDockWidget):
    """Dockable table of per-worker acquisition rate and timing diagnostics."""

    def __init__(
        self,
        *,
        controller: RunController,
        worker_topology: dict[str, tuple[str, ...]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Acquisition Diagnostics", parent)
        self.setObjectName("dock_diagnostics")
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )

        self._controller = controller
        self._rows: dict[str, _RowLabels] = {}

        font = monospace_font(point_size=9)
        hdr_font = monospace_font(point_size=9)

        body = QWidget()
        grid = QGridLayout(body)
        grid.setContentsMargins(6, 4, 6, 4)
        grid.setVerticalSpacing(2)
        grid.setHorizontalSpacing(8)

        for col, (hdr, width, tip) in enumerate(zip(_HEADERS, _COL_WIDTHS, _TOOLTIPS, strict=True)):
            lbl = QLabel(hdr)
            lbl.setFont(hdr_font)
            lbl.setStyleSheet(f"color: {COLOR_IDLE.name()}; font-weight: bold;")
            lbl.setToolTip(tip)
            if width is not None:
                lbl.setFixedWidth(width)
                lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(lbl, 0, col)

        for row_idx, (rid, adapter_names) in enumerate(worker_topology.items(), start=1):
            display = ", ".join(adapter_names) if adapter_names else rid

            dev_lbl = QLabel(display)
            dev_lbl.setFont(font)
            dev_lbl.setStyleSheet(f"color: {COLOR_IDLE.name()};")

            rate_lbl = _val_label(font, 82)
            p50_lbl = _val_label(font, 82)
            jitter_lbl = _val_label(font, 90)
            age_lbl = _val_label(font, 62)

            for lbl, tip in zip(
                (dev_lbl, rate_lbl, p50_lbl, jitter_lbl, age_lbl),
                _TOOLTIPS,
                strict=True,
            ):
                lbl.setToolTip(tip)

            grid.addWidget(dev_lbl, row_idx, 0)
            grid.addWidget(rate_lbl, row_idx, 1)
            grid.addWidget(p50_lbl, row_idx, 2)
            grid.addWidget(jitter_lbl, row_idx, 3)
            grid.addWidget(age_lbl, row_idx, 4)

            self._rows[rid] = _RowLabels(
                device=dev_lbl,
                rate=rate_lbl,
                p50=p50_lbl,
                jitter=jitter_lbl,
                age=age_lbl,
            )

        grid.setColumnStretch(0, 1)
        grid.setRowStretch(grid.rowCount(), 1)

        scroll = QScrollArea()
        scroll.setWidget(body)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setWidget(scroll)

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self._refresh)

    # ------------------------------------------------------------------ control

    def start(self) -> None:
        """Begin periodic updates / animation for this widget."""
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        """Stop periodic updates / animation for this widget."""
        self._timer.stop()

    # ------------------------------------------------------------------ internal

    def _refresh(self) -> None:
        conductor = self._controller.conductor
        if conductor is None:
            for row in self._rows.values():
                row.set_idle()
            return
        diag = conductor.runtime_diagnostics()
        loop_lag_warn_ms = float(
            diag.get("runtime", {}).get("loop_lag_warn_ms", _DEFAULT_LOOP_LAG_WARN_MS)
        )
        for rid, row in self._rows.items():
            w = diag.get(f"worker:{rid}")
            if w is None:
                row.set_idle()
            else:
                row.update(w, loop_lag_warn_ms=loop_lag_warn_ms)


__all__ = ["DiagnosticsDock"]
