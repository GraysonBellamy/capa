"""Calibration plot popup."""

from __future__ import annotations

from typing import Any

from capa.channels.calibration import (
    Identity,
    LinearTwoPoint,
    Lookup,
    Polynomial,
)
from capa.ui.tabs.setup_calibration_plot import (
    CalibrationPlotDialog,
    _curve_for,
    _raw_range_for,
)


def test_curve_for_identity_returns_diagonal() -> None:
    cal = Identity(input_unit="degC", output_unit="degC")
    sample = _curve_for(cal)
    assert sample is not None
    raws, values = sample
    assert len(raws) == len(values)
    assert raws[0] == values[0]
    assert raws[-1] == values[-1]


def test_curve_for_linear_two_point_spans_references() -> None:
    cal = LinearTwoPoint(
        input_unit="V",
        output_unit="degC",
        ref_low_raw=0.0,
        ref_low_value=0.0,
        ref_high_raw=5.0,
        ref_high_value=1000.0,
    )
    sample = _curve_for(cal)
    assert sample is not None
    raws, values = sample
    # Range is padded 10% beyond the refs.
    assert raws[0] < 0.0
    assert raws[-1] > 5.0
    # At ref_low_raw the curve should be ~0; at ref_high_raw ~1000.
    # Find indices that bracket the references.
    assert any(abs(v) < 50.0 for v in values)
    assert any(abs(v - 1000.0) < 50.0 for v in values)


def test_curve_for_polynomial() -> None:
    cal = Polynomial(
        input_unit="V",
        output_unit="degC",
        coefficients=(0.0, 100.0),  # y = 100 * x
    )
    sample = _curve_for(cal)
    assert sample is not None
    raws, values = sample
    # y = 100 * x at any raw.
    for r, v in zip(raws, values, strict=False):
        assert abs(v - 100.0 * r) < 1e-9


def test_curve_for_lookup_spans_table_range() -> None:
    cal = Lookup(
        input_unit="V",
        output_unit="degC",
        table=((0.0, 0.0), (1.0, 100.0), (2.0, 200.0)),
    )
    raw_min, raw_max = _raw_range_for(cal)
    assert raw_min == 0.0
    assert raw_max == 2.0


def test_dialog_for_identity_constructs(qtbot: Any) -> None:
    cal = Identity(input_unit="degC", output_unit="degC")
    dialog = CalibrationPlotDialog(
        channel_name="heater.pv",
        calibration=cal,
    )
    qtbot.addWidget(dialog)
    assert dialog.windowTitle() == "Calibration — heater.pv"


def test_show_for_channel_builds_dialog_from_dict(qtbot: Any) -> None:
    channel = {
        "name": "TC_top_1",
        "calibration": {
            "kind": "linear_two_point",
            "input_unit": "V",
            "output_unit": "degC",
            "ref_low_raw": 0.0,
            "ref_low_value": 0.0,
            "ref_high_raw": 5.0,
            "ref_high_value": 1000.0,
        },
    }
    dialog = CalibrationPlotDialog.show_for_channel(channel=channel, parent=None)
    assert dialog is not None
    qtbot.addWidget(dialog)
    assert "TC_top_1" in dialog.windowTitle()


def test_show_for_channel_returns_none_when_no_calibration() -> None:
    channel = {"name": "free_channel"}
    dialog = CalibrationPlotDialog.show_for_channel(channel=channel, parent=None)
    assert dialog is None


def test_show_for_channel_returns_none_on_malformed_calibration() -> None:
    channel = {
        "name": "bad",
        "calibration": {"kind": "no_such_variant"},
    }
    dialog = CalibrationPlotDialog.show_for_channel(channel=channel, parent=None)
    assert dialog is None
