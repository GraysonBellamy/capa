"""Multi-pane PyQtGraph plot widget.

One :class:`pg.PlotWidget` per ``plot_group`` declared in the
channel registry, populated automatically from the active config. Repaint
runs on a 200 ms ``QTimer`` (5 Hz) — fast enough for operator viewing of
slow-trending temperatures and flows, slow enough to leave the UI thread
with deep headroom. Ring buffers still carry full-rate (un-decimated)
data so each repaint draws the most recent samples and the peak-mode
downsampler preserves transient spikes between repaints.

Performance optimizations that remain in place regardless of repaint rate:

* ``setDownsampling(auto=True, mode="peak")`` — pyqtgraph collapses N
  points to ~pixel-width using min/max per bucket, so paint cost scales
  with plot width, not buffer depth.
* ``setClipToView(True)`` — skip rendering off-screen segments.
* ``skipFiniteCheck=True`` + ``connect="all"`` on curves — values are
  already finite floats from the ring buffer, so we both skip the
  pre-paint finite sweep and use the fastest ``arrayToQPath`` path.
* ``antialias=False`` + ``width=1`` pens — pyqtgraph#533 documents that
  Qt falls off a performance cliff for any pen width > 1.0 (geometric
  stroke outlining instead of the fast 1-pixel raster path) regardless
  of ``cosmetic=True``. Combined with antialias off, line drawing stays
  on Qt's hot path.
* Per-curve dirty check against :attr:`ChannelRingBuffer.total_kept` —
  curves whose buffer has not received new samples since the last tick
  skip ``setData`` entirely. Low-rate channels (Watlow, Sartorius at
  1 Hz) cost nothing on most repaints.

Note on OpenGL: pyqtgraph's ``useOpenGL=True`` is the *old* GraphicsView-
on-QOpenGLWidget path, not a shader pipeline. On Windows specifically,
multiple reports indicate it can *hurt* performance (pyqtgraph#2227).
The downsampling + clip-to-view combo is what actually delivers headroom;
OpenGL is left disabled until evidence on the real rig says otherwise.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QLabel,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from capa.channels.spec import ChannelSpec
from capa.core.ringbuffer import RingBufferRegistry

REPAINT_INTERVAL_MS: Final[int] = 200
"""5 Hz plot repaint cadence. Ring buffers still accept samples at their
full producer rate (``decimate_to_hz`` default 10 Hz); each repaint draws
the most recent buffer contents through the peak downsampler so transients
that occur between repaints still show up in the trace. Slower than the
producer rate by design — the operator does not need 10 Hz visual refresh
for a 1 Hz temperature trace, and halving the repaint rate doubles the UI
thread's headroom for free."""

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
        self._channels: list[ChannelSpec] = list(channels)

        # Group channels by plot_group; keep declaration order.
        groups: dict[str, list[ChannelSpec]] = {}
        for ch in self._channels:
            key = ch.plot_group or "misc"
            groups.setdefault(key, []).append(ch)

        self._curves: dict[str, pg.PlotDataItem] = {}
        self._unit_per_group: dict[str, str] = {}
        # Last-seen ``total_kept`` per channel — used in ``_refresh`` to
        # skip ``setData`` for curves whose ring buffer has not advanced
        # since the previous repaint tick. ``-1`` forces a first paint.
        self._last_kept: dict[str, int] = {}
        # Has the first sample arrived? Used to drop the "Press Start"
        # overlay the moment real data starts drawing.
        self._has_data: bool = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._splitter = QSplitter(Qt.Orientation.Vertical, self)
        layout.addWidget(self._splitter, 1)

        pg.setConfigOptions(antialias=False, useOpenGL=False)
        # Shared tick font — pyqtgraph defaults to the system font at
        # whatever the theme picks, which on Windows leaves tick labels
        # large enough to clip against the axis line on dense plots.
        tick_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
        tick_font.setPointSize(8)
        # Style for the axis title labels. White-space ``color`` keeps
        # the label readable on the white plot background.
        label_style = {"color": "#344054", "font-size": "9pt"}
        self._plots: list[pg.PlotWidget] = []
        self._overlays: list[QLabel] = []
        for group_name, group_channels in groups.items():
            plot = pg.PlotWidget()
            plot.setBackground("w")
            plot.showGrid(x=True, y=True, alpha=0.3)
            plot.setLabel("bottom", "t (s)", **label_style)
            unit = group_channels[0].derived_unit or group_channels[0].unit
            self._unit_per_group[group_name] = unit
            plot.setLabel("left", f"{group_name} [{unit}]", **label_style)
            # Reserve fixed axis-area sizes so long tick labels (large
            # temperatures, scientific-notation flows) don't clip
            # against the plot edge and the axis title text always has
            # room. Without these, pyqtgraph's auto-sizing can leave the
            # leftmost tick label half-cut on first paint.
            left_axis = plot.getAxis("left")
            bottom_axis = plot.getAxis("bottom")
            left_axis.setWidth(64)
            bottom_axis.setHeight(32)
            left_axis.setStyle(tickFont=tick_font)
            bottom_axis.setStyle(tickFont=tick_font)
            # Disable pyqtgraph's autoSIPrefix. The unit is already
            # embedded in the label text rather than passed via
            # ``setLabel(units=…)``, so pyqtgraph's labelString() treats
            # ``labelUnits`` as empty and, once the data range triggers
            # a non-unity scale, appends ``(x0.001)`` to the label —
            # which overflows the fixed axis reservations the moment a
            # run starts. Tick values stay in the channel's declared
            # unit instead of being silently rescaled.
            left_axis.enableAutoSIPrefix(False)
            bottom_axis.enableAutoSIPrefix(False)
            plot.addLegend(offset=(8, 8))
            # Centered "Press Start" overlay. Hidden the moment the
            # first sample arrives in :meth:`_refresh`. The overlay is
            # a child of the plot's viewport-equivalent (the
            # ``PlotWidget`` itself) so resize tracking is automatic.
            overlay = QLabel(
                "Press Start to begin recording.\nLive values appear in the Numerics dock.", plot
            )
            overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            font = QFont()
            font.setPointSize(11)
            overlay.setFont(font)
            overlay.setStyleSheet(
                "color: #98a2b3; background: rgba(255,255,255,0.6); "
                "padding: 12px 18px; border-radius: 8px;"
            )
            overlay.adjustSize()
            self._overlays.append(overlay)
            self._plots.append(plot)
            # autoDownsample + peak method: pyqtgraph picks the downsample
            # factor from the curve's pixel width vs point count, then
            # plots min/max per bucket so transient peaks survive. Lets
            # the ring buffer carry full-rate (un-decimated) data without
            # forcing the GPU to draw N points into N/k pixels.
            plot.setDownsampling(auto=True, mode="peak")
            plot.setClipToView(True)
            for idx, ch in enumerate(group_channels):
                color = _LINE_COLORS[idx % len(_LINE_COLORS)]
                # width=1 is load-bearing: pyqtgraph#533 documents Qt's
                # 1-pixel raster fast path vs. the geometric stroke path
                # used for any width > 1.0 — the cliff is ~100× on dense
                # traces and is not bypassed by ``cosmetic=True``.
                pen = pg.mkPen(color=color, width=1)
                # ``connect="all"`` + ``skipFiniteCheck=True``: values
                # from the ring buffer are already finite floats, so
                # both the finite sweep and the segment-break check can
                # be skipped (PlotDataItem docs, pyqtgraph 0.13 release
                # notes on ``arrayToQPath`` speedup).
                curve = plot.plot(
                    [],
                    [],
                    pen=pen,
                    name=ch.name,
                    skipFiniteCheck=True,
                    connect="all",
                )
                self._curves[ch.name] = curve
                self._last_kept[ch.name] = -1
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
        """Swap the buffer source — used between runs. Resets the
        per-curve dirty tracker so the first refresh against the new
        registry repaints every curve."""
        self._registry = registry
        for name in self._last_kept:
            self._last_kept[name] = -1
        self._has_data = False
        for overlay in self._overlays:
            overlay.show()
        self._reposition_overlays()

    def clear(self) -> None:
        """Clear the widget's contents in place."""
        for name, curve in self._curves.items():
            curve.setData([], [])
            self._last_kept[name] = -1

    # ------------------------------------------------------------------ internal

    def _refresh(self) -> None:
        new_data = False
        for channel, curve in self._curves.items():
            buf = self._registry.get(channel)
            if buf is None:
                continue
            kept = buf.total_kept
            if kept == self._last_kept[channel]:
                # No new samples since the previous tick — nothing to
                # redraw. ``pg.PlotDataItem`` keeps its last data so the
                # curve stays visible.
                continue
            t_ns, v = buf.snapshot()
            if t_ns.size == 0:
                continue
            # Convert t_mono_ns → seconds-since-run-start for display.
            curve.setData(t_ns.astype("float64") / 1e9, v)
            self._last_kept[channel] = kept
            new_data = True
        if new_data and not self._has_data:
            self._has_data = True
            for overlay in self._overlays:
                overlay.hide()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        """Qt event handler — see :class:`PySide6.QtWidgets.QWidget`."""
        super().resizeEvent(event)
        self._reposition_overlays()

    def _reposition_overlays(self) -> None:
        for plot, overlay in zip(self._plots, self._overlays, strict=False):
            overlay.adjustSize()
            x = (plot.width() - overlay.width()) // 2
            y = (plot.height() - overlay.height()) // 2
            overlay.move(max(x, 0), max(y, 0))

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        """Qt event handler — see :class:`PySide6.QtWidgets.QWidget`."""
        super().showEvent(event)
        self._reposition_overlays()


__all__ = ["REPAINT_INTERVAL_MS", "PlotPane"]
