"""Multi-pane PyQtGraph plot widget.

Plan §10.3. One :class:`pg.PlotWidget` per ``plot_group`` declared in the
channel registry, populated automatically from the active config. Repaint
runs on a 100 ms ``QTimer`` (≈10 Hz, plan §7.2). Drag-to-reassign panes is
P6 polish.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QSplitter, QVBoxLayout, QWidget

from capa.channels.spec import ChannelSpec
from capa.core.ringbuffer import RingBufferRegistry

REPAINT_INTERVAL_MS: Final[int] = 100
"""Plan §7.2: UI bridge drains ring buffers at 10 Hz."""

# A small palette cycled across lines within a single pane. Distinct enough
# to be readable on a high-density panel; not so saturated that the eye
# tires over a long run.
_LINE_COLORS = (
    "#5a8def",
    "#e07b5f",
    "#5fb27a",
    "#c965d8",
    "#d8a25f",
    "#5fc6d8",
    "#d85f9c",
    "#9bd85f",
)


class PlotPane(QWidget):
    """Container that owns one sub-plot per ``plot_group``.

    Channels with no ``plot_group`` declared land in a single ``"misc"``
    pane so they remain visible. Time axis is seconds since run start
    (``t_mono_ns / 1e9``).
    """

    def __init__(
        self,
        *,
        registry: RingBufferRegistry,
        channels: Iterable[ChannelSpec],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._registry: RingBufferRegistry = registry

        # Group channels by plot_group; keep declaration order.
        groups: dict[str, list[ChannelSpec]] = {}
        for ch in channels:
            key = ch.plot_group or "misc"
            groups.setdefault(key, []).append(ch)

        self._curves: dict[str, pg.PlotDataItem] = {}
        self._unit_per_group: dict[str, str] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._splitter = QSplitter(Qt.Orientation.Vertical, self)
        layout.addWidget(self._splitter)

        pg.setConfigOptions(antialias=True, useOpenGL=False)
        for group_name, group_channels in groups.items():
            plot = pg.PlotWidget()
            plot.setBackground("w")
            plot.showGrid(x=True, y=True, alpha=0.3)
            plot.setLabel("bottom", "t (s)")
            unit = group_channels[0].derived_unit or group_channels[0].unit
            self._unit_per_group[group_name] = unit
            plot.setLabel("left", f"{group_name} [{unit}]")
            plot.addLegend(offset=(8, 8))
            for idx, ch in enumerate(group_channels):
                color = _LINE_COLORS[idx % len(_LINE_COLORS)]
                pen = pg.mkPen(color=color, width=1.5)
                # skipFiniteCheck speeds up setData for hot paths; values are
                # already finite floats from the ring buffer.
                curve = plot.plot(
                    [],
                    [],
                    pen=pen,
                    name=ch.name,
                    skipFiniteCheck=True,
                )
                self._curves[ch.name] = curve
            self._splitter.addWidget(plot)

        self._timer = QTimer(self)
        self._timer.setInterval(REPAINT_INTERVAL_MS)
        self._timer.timeout.connect(self._refresh)

    # ------------------------------------------------------------------ control

    def start(self) -> None:
        """Begin the 10 Hz refresh timer. Idempotent."""
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        """Stop the refresh timer. Curves retain their last data."""
        self._timer.stop()

    def set_registry(self, registry: RingBufferRegistry) -> None:
        """Swap the buffer source — used between runs."""
        self._registry = registry

    def clear(self) -> None:
        for curve in self._curves.values():
            curve.setData([], [])

    # ------------------------------------------------------------------ internal

    def _refresh(self) -> None:
        for channel, curve in self._curves.items():
            buf = self._registry.get(channel)
            if buf is None:
                continue
            t_ns, v = buf.snapshot()
            if t_ns.size == 0:
                continue
            # Convert t_mono_ns → seconds-since-run-start for display.
            curve.setData(t_ns.astype("float64") / 1e9, v)


__all__ = ["REPAINT_INTERVAL_MS", "PlotPane"]
