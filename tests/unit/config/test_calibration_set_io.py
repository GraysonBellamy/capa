"""Calibration set IO + diff helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from capa.channels.calibration import Calibration, CalibrationSet, Identity
from capa.config.calibration_set_io import (
    apply_diff_selection,
    build_set_from_channels,
    diff_set_against_channels,
    load_calibration_set,
    save_calibration_set,
)


def _identity(unit: str = "degC") -> dict[str, Any]:
    return {"kind": "identity", "input_unit": unit, "output_unit": unit}


def _linear(input_unit: str = "V", output_unit: str = "degC") -> dict[str, Any]:
    return {
        "kind": "linear_two_point",
        "input_unit": input_unit,
        "output_unit": output_unit,
        "ref_low_raw": 0.0,
        "ref_low_value": 0.0,
        "ref_high_raw": 5.0,
        "ref_high_value": 1000.0,
    }


def _build_calibration(payload: dict[str, Any]) -> Calibration:
    return TypeAdapter(Calibration).validate_python(payload)


# ---------------------------------------------------------------------------
# Diff classifier.
# ---------------------------------------------------------------------------


def test_diff_override_identity_is_recommended() -> None:
    channels = [{"name": "TC_top_1", "calibration": _identity("V")}]
    set_curves = {"TC_top_1": _build_calibration(_linear())}
    entries = diff_set_against_channels(set_curves=set_curves, channels=channels)
    assert len(entries) == 1
    e = entries[0]
    assert e.kind == "override_identity"
    assert e.actionable is True
    assert e.recommended is True


def test_diff_override_existing_is_not_recommended() -> None:
    """Replacing an existing non-Identity curve is destructive — the
    dialog must leave the checkbox unticked by default."""
    channels = [
        {
            "name": "TC_top_1",
            "calibration": _linear(input_unit="V", output_unit="degC"),
        }
    ]
    different_linear = dict(_linear())
    different_linear["ref_high_value"] = 1100.0
    set_curves = {"TC_top_1": _build_calibration(different_linear)}
    entries = diff_set_against_channels(set_curves=set_curves, channels=channels)
    assert len(entries) == 1
    e = entries[0]
    assert e.kind == "override_existing"
    assert e.actionable is True
    assert e.recommended is False


def test_diff_matches_is_not_actionable() -> None:
    channels = [{"name": "TC_top_1", "calibration": _linear()}]
    set_curves = {"TC_top_1": _build_calibration(_linear())}
    entries = diff_set_against_channels(set_curves=set_curves, channels=channels)
    assert len(entries) == 1
    assert entries[0].kind == "matches"
    assert entries[0].actionable is False


def test_diff_surfaces_set_only_and_channel_only() -> None:
    channels = [
        {"name": "TC_top_1", "calibration": _identity("V")},
        {"name": "purge_flow", "calibration": _identity("sccm")},
    ]
    set_curves = {
        "TC_top_1": _build_calibration(_linear()),
        "TC_top_2": _build_calibration(_linear()),  # set only
    }
    entries = diff_set_against_channels(set_curves=set_curves, channels=channels)
    by_kind = {e.kind: e for e in entries}
    assert "override_identity" in by_kind
    assert by_kind["override_identity"].channel_name == "TC_top_1"
    assert "set_only" in by_kind
    assert by_kind["set_only"].channel_name == "TC_top_2"
    assert "channel_only" in by_kind
    assert by_kind["channel_only"].channel_name == "purge_flow"


def test_diff_ordering_actionable_first() -> None:
    """Actionable rows (override_*) appear before informational rows."""
    channels = [
        {"name": "matched", "calibration": _linear()},
        {"name": "identity_ch", "calibration": _identity("V")},
        {"name": "existing_ch", "calibration": _linear(output_unit="degC")},
    ]
    different = dict(_linear())
    different["ref_high_value"] = 2000.0
    set_curves = {
        "matched": _build_calibration(_linear()),
        "identity_ch": _build_calibration(_linear()),
        "existing_ch": _build_calibration(different),
    }
    entries = diff_set_against_channels(set_curves=set_curves, channels=channels)
    kinds = [e.kind for e in entries]
    # override_identity < override_existing < matches
    assert kinds.index("override_identity") < kinds.index("override_existing")
    assert kinds.index("override_existing") < kinds.index("matches")


# ---------------------------------------------------------------------------
# apply_diff_selection.
# ---------------------------------------------------------------------------


def test_apply_diff_selection_mutates_only_selected_channels() -> None:
    channels = [
        {"name": "TC_top_1", "calibration": _identity("V")},
        {"name": "TC_top_2", "calibration": _identity("V")},
        {"name": "TC_top_3", "calibration": _identity("V")},
    ]
    new_cal = _linear()
    set_curves = {
        name: _build_calibration(new_cal) for name in ("TC_top_1", "TC_top_2", "TC_top_3")
    }
    entries = diff_set_against_channels(set_curves=set_curves, channels=channels)
    changed = apply_diff_selection(
        channels=channels,
        entries=entries,
        selected_names={"TC_top_1", "TC_top_3"},
    )
    assert changed == 2
    # TC_top_2 still Identity; the other two received the linear curve.
    assert channels[0]["calibration"]["kind"] == "linear_two_point"
    assert channels[1]["calibration"]["kind"] == "identity"
    assert channels[2]["calibration"]["kind"] == "linear_two_point"


def test_apply_diff_selection_skips_set_only_rows() -> None:
    """A row in the set with no matching channel can't be applied."""
    channels: list[dict[str, Any]] = []
    set_curves = {"ghost": _build_calibration(_linear())}
    entries = diff_set_against_channels(set_curves=set_curves, channels=channels)
    changed = apply_diff_selection(
        channels=channels,
        entries=entries,
        selected_names={"ghost"},
    )
    assert changed == 0
    assert channels == []


# ---------------------------------------------------------------------------
# IO round-trip.
# ---------------------------------------------------------------------------


def test_build_set_from_channels_skips_malformed_calibrations() -> None:
    channels = [
        {"name": "ok", "calibration": _identity("degC")},
        {"name": "bad", "calibration": {"kind": "no_such_variant"}},
        {"name": "no_cal"},
    ]
    cs = build_set_from_channels(name="test", revision="1", channels=channels)
    assert set(cs.curves.keys()) == {"ok"}


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    cs = CalibrationSet(
        name="thermocouples",
        revision="3",
        curves={
            "TC_top_1": Identity(input_unit="V", output_unit="V"),
        },
    )
    path = tmp_path / "set.toml"
    save_calibration_set(path, cs)
    assert path.exists()
    loaded = load_calibration_set(path)
    assert loaded.name == cs.name
    assert loaded.revision == cs.revision
    assert set(loaded.curves.keys()) == set(cs.curves.keys())


@pytest.mark.parametrize("name", ["with spaces", "a.b.c", "normal"])
def test_save_handles_assorted_channel_names(name: str, tmp_path: Path) -> None:
    cs = CalibrationSet(
        name="ts",
        revision="1",
        curves={name: Identity(input_unit="degC", output_unit="degC")},
    )
    path = tmp_path / "cal.toml"
    save_calibration_set(path, cs)
    loaded = load_calibration_set(path)
    assert name in loaded.curves
