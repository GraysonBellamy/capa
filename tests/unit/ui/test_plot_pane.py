"""Which channels the Run tab's plot pane draws."""

from __future__ import annotations

from typing import Any

from capa.channels.calibration import Identity
from capa.channels.spec import ChannelKind, ChannelSpec, WatlowParameter
from capa.core.ringbuffer import RingBufferRegistry
from capa.ui.plots.pane import PlotPane


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
