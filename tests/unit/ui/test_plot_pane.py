"""The Run tab's plot pane: which channels it draws, how it decimates them,
and that it stops repainting once its data stops changing."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QObject

from capa.channels.calibration import Identity
from capa.channels.spec import ChannelKind, ChannelSpec, WatlowParameter
from capa.core.ringbuffer import RingBufferRegistry
from capa.ui.plots.pane import MAX_CURVE_POINTS, PlotPane, peak_decimate


def _channel(name: str, *, plot_group: str | None, plot: bool = True) -> ChannelSpec:
    return ChannelSpec(
        name=name,
        kind=ChannelKind.PROCESS_VAR,
        source=WatlowParameter(device="heater", parameter="process_value", instance=1),
        unit="degC",
        derived_unit="degC",
        calibration=Identity(input_unit="degC", output_unit="degC"),
        plot_group=plot_group,
        plot=plot,
    )


def _pane(qtbot: Any, channels: list[ChannelSpec]) -> PlotPane:
    pane = PlotPane(registry=RingBufferRegistry(), channels=channels)
    qtbot.addWidget(pane)
    return pane


def test_every_channel_has_a_curve_by_default(qtbot: Any) -> None:
    pane = _pane(
        qtbot,
        [
            _channel("a", plot_group="temperatures"),
            _channel("b", plot_group="temperatures"),
            _channel("c", plot_group=None),
        ],
    )
    assert set(pane._curves) == {"a", "b", "c"}
    # One sub-plot per group; a channel without a group goes to "misc".
    assert list(pane._unit_per_group) == ["temperatures", "misc"]


def test_a_channel_that_opts_out_has_no_curve(qtbot: Any) -> None:
    pane = _pane(
        qtbot,
        [
            _channel("a", plot_group="temperatures"),
            _channel("a_valid", plot_group="temperatures", plot=False),
        ],
    )
    assert set(pane._curves) == {"a"}
    assert list(pane._unit_per_group) == ["temperatures"]


def test_a_group_with_nothing_to_plot_has_no_sub_plot(qtbot: Any) -> None:
    pane = _pane(
        qtbot,
        [
            _channel("a", plot_group="temperatures"),
            _channel("flag", plot_group="flags", plot=False),
            _channel("state", plot_group=None, plot=False),
        ],
    )
    assert list(pane._unit_per_group) == ["temperatures"]
    assert len(pane._plots) == 1


def test_a_pane_with_nothing_to_plot_is_empty_and_still_refreshes(qtbot: Any) -> None:
    pane = _pane(qtbot, [_channel("flag", plot_group=None, plot=False)])
    assert pane._curves == {}
    assert pane._plots == []
    pane._refresh()


def test_peak_decimate_leaves_a_short_trace_alone() -> None:
    x = np.arange(100.0)
    y = np.sin(x)
    out_x, out_y = peak_decimate(x, y, max_points=100)
    assert out_x is x
    assert out_y is y


def test_peak_decimate_keeps_both_ends_and_every_extreme() -> None:
    rng = np.random.default_rng(0)
    x = np.arange(36_001.0) / 60.0
    y = rng.normal(0.0, 1.0, x.size)
    y[12_345] = 50.0  # one-sample spike
    y[30_000] = -50.0
    out_x, out_y = peak_decimate(x, y, max_points=1000)
    assert out_x.size <= 1000 + 2 * (x.size // 500)
    assert (out_x[0], out_x[-1]) == (x[0], x[-1])
    assert np.all(np.diff(out_x) > 0)
    assert out_y.max() == 50.0
    assert out_y.min() == -50.0
    # Every point is a real sample, not a bucket average.
    assert np.array_equal(out_y, y[np.searchsorted(x, out_x)])


def test_a_deep_ring_reaches_its_curve_decimated_end_to_end(qtbot: Any) -> None:
    registry = RingBufferRegistry()
    buf = registry.register("a", decimate_to_hz=60.0)
    pane = PlotPane(registry=registry, channels=[_channel("a", plot_group="temperatures")])
    qtbot.addWidget(pane)
    pane.show()
    qtbot.waitExposed(pane)
    for i in range(buf.capacity):
        buf.push_raw(i * 1_000_000_000 // 60, float(i % 100))
    pane._refresh()
    x, _ = pane._curves["a"].getData()
    assert x is not None
    assert x.size <= MAX_CURVE_POINTS + buf.capacity // (MAX_CURVE_POINTS // 2)
    assert x[0] == 0.0
    assert x[-1] == pytest.approx((buf.capacity - 1) / 60, abs=1e-6)


class _PaintCounter(QObject):
    """Counts paint events delivered to the widget it filters."""

    def __init__(self) -> None:
        super().__init__()
        self.count = 0

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        """Qt event filter — see :class:`PySide6.QtCore.QObject`."""
        if event.type() == QEvent.Type.Paint:
            self.count += 1
        return False


def test_a_plot_goes_idle_once_its_data_stops_changing(qtbot: Any) -> None:
    # Issue #26. pyqtgraph's auto-downsampler picks its factor from the x
    # range, and x-autorange picks the range from the downsampled data, whose
    # extent shrinks as the factor steps up. At the sample count where the
    # factor steps from 1 to 2 the two fed each other forever: the plot
    # repainted continuously with no new data and froze the window. Grow a
    # 1 Hz trace through that count and check the plot falls quiet each time.
    registry = RingBufferRegistry()
    registry.register("a", decimate_to_hz=10.0)
    pane = PlotPane(registry=registry, channels=[_channel("a", plot_group="temperatures")])
    qtbot.addWidget(pane)
    pane.resize(500, 300)
    pane.show()
    qtbot.waitExposed(pane)
    plot = pane._plots[0]
    vb = plot.getViewBox()
    counter = _PaintCounter()
    plot.viewport().installEventFilter(counter)
    buf = registry.get("a")
    assert buf is not None

    def falls_quiet() -> bool:
        # A healthy plot paints twice (data, then autorange) and stops.
        for _ in range(4):
            counter.count = 0
            qtbot.wait(25)
            if counter.count == 0:
                return True
        return False

    # pyqtgraph aims for 5 samples per pixel and pads the autorange on both sides.
    boundary = int(10 * vb.width() / (1 + 2 * vb.suggestPadding(0)))
    for i in range(boundary - 25):
        buf.push_raw(i * 1_000_000_000, 300.0 + 0.1 * (i % 7))
    for n in range(boundary - 25, boundary + 25):
        buf.push_raw(n * 1_000_000_000, 300.0 + 0.1 * (n % 7))
        pane._refresh()
        assert falls_quiet(), f"still repainting with no new data at {n + 1} samples"
