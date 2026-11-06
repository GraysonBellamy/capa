"""Live views show a pressure in Pa as kPa, scaling at display time only."""

from __future__ import annotations

from typing import Any

import pytest

from capa.channels.calibration import Identity
from capa.channels.spec import Bmp58xReading, ChannelKind, ChannelSpec
from capa.core.ringbuffer import RingBufferRegistry
from capa.ui.display_units import display_unit
from capa.ui.docks.numerics import NumericsDock
from capa.ui.plots.pane import PlotPane


def _pressure() -> ChannelSpec:
    return ChannelSpec(
        name="ambient.pressure",
        kind=ChannelKind.PRESSURE,
        source=Bmp58xReading(device="baro", field="pressure"),
        unit="Pa",
        derived_unit="Pa",
        calibration=Identity(input_unit="Pa", output_unit="Pa"),
        plot_group="ambient_pressure",
    )


@pytest.mark.parametrize("unit", ["Pa", "pascal"])
def test_pa_is_shown_as_kpa(unit: str) -> None:
    shown, scale = display_unit(unit)
    assert shown == "kPa"
    assert scale == pytest.approx(1e-3)


@pytest.mark.parametrize("unit", ["kPa", "degC", "percent", "kW/m**2"])
def test_other_units_are_shown_as_declared(unit: str) -> None:
    assert display_unit(unit) == (unit, 1.0)


def test_numerics_dock_reads_a_pa_channel_in_kpa(qtbot: Any) -> None:
    registry = RingBufferRegistry()
    registry.register("ambient.pressure").push_raw(0, 100_812.0)
    dock = NumericsDock(registry=registry, channels=[_pressure()])
    qtbot.addWidget(dock)
    dock._refresh()
    tile = dock._tiles["ambient.pressure"]
    assert tile._value_label.text().strip() == "100.81"
    assert tile._unit_label.text() == "kPa"


def test_plot_pane_draws_a_pa_channel_in_kpa(qtbot: Any) -> None:
    registry = RingBufferRegistry()
    buf = registry.register("ambient.pressure")
    pane = PlotPane(registry=registry, channels=[_pressure()])
    qtbot.addWidget(pane)
    pane.show()
    qtbot.waitExposed(pane)
    buf.push_raw(0, 100_812.0)
    pane._refresh()
    _, y = pane._curves["ambient.pressure"].getData()
    assert y is not None
    assert y[-1] == pytest.approx(100.812)
    assert pane._unit_per_group["ambient_pressure"] == "kPa"
    # The ring buffer — what gets recorded — stays in Pa.
    latest = buf.latest()
    assert latest is not None
    assert latest[1] == 100_812.0
