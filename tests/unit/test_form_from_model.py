"""Tests for the Pydantic auto-form generator.

Plan §10.5 / P3 follow-up item 2. Each test uses a small synthetic
model so the test surface stays tight; the round-trip test exercises
the real :class:`CapaPyrolysisMetadata` to ensure the generator handles
the recursive case the production callers will throw at it."""

from __future__ import annotations

from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field
from PySide6.QtWidgets import QCheckBox, QComboBox, QDoubleSpinBox, QLineEdit, QSpinBox

from capa.experiment.profiles.capa_pyrolysis import CapaPyrolysisMetadata
from capa.ui.forms import build_form


class _Demo(BaseModel):
    name: str = ""
    count: int = Field(default=0, ge=0, le=100)


class _Constrained(BaseModel):
    flux: float = Field(default=50.0, gt=0, le=100.0)


class _Choice(BaseModel):
    mode: Literal["inert", "oxidative"] = "inert"


class _OptionalDemo(BaseModel):
    notes: str | None = None
    duration_s: float | None = Field(default=None, ge=0)


class _Inner(BaseModel):
    name: str = ""


class _Outer(BaseModel):
    inner: _Inner = Field(default_factory=_Inner)


class _ListDemo(BaseModel):
    channels: tuple[str, ...] = ()


def test_str_field_renders_lineedit(qtbot: Any) -> None:
    form = build_form(_Demo)
    qtbot.addWidget(form)
    name_widget = form._fields["name"]
    assert name_widget.findChild(QLineEdit) is not None


def test_int_field_honors_ge_le(qtbot: Any) -> None:
    form = build_form(_Demo)
    qtbot.addWidget(form)
    spin = form._fields["count"].findChild(QSpinBox)
    assert spin is not None
    assert spin.minimum() == 0
    assert spin.maximum() == 100


def test_float_field_honors_gt(qtbot: Any) -> None:
    form = build_form(_Constrained)
    qtbot.addWidget(form)
    spin = form._fields["flux"].findChild(QDoubleSpinBox)
    assert spin is not None
    # Strict-greater approximated by an epsilon nudge above 0.
    assert spin.minimum() > 0
    assert spin.maximum() == pytest.approx(100.0)


def test_literal_field_renders_combobox_with_choices(qtbot: Any) -> None:
    form = build_form(_Choice)
    qtbot.addWidget(form)
    combo = form._fields["mode"].findChild(QComboBox)
    assert combo is not None
    items = [combo.itemText(i) for i in range(combo.count())]
    assert items == ["inert", "oxidative"]


def test_optional_field_uses_set_checkbox(qtbot: Any) -> None:
    form = build_form(_OptionalDemo)
    qtbot.addWidget(form)
    notes = form._fields["notes"]
    # Optional wraps the inner widget with a "Set" checkbox; unchecked = None.
    assert notes.value() is None
    checkboxes = notes.findChildren(QCheckBox)
    assert len(checkboxes) == 1


def test_nested_basemodel_renders_inline_form(qtbot: Any) -> None:
    """The nested ``_Inner`` model should render as a nested form whose
    fields are also accessible — set_values / values must round-trip
    through the nested layer."""
    form = build_form(_Outer)
    qtbot.addWidget(form)
    form.set_values({"inner": {"name": "round-trip"}})
    out = form.values()
    assert out["inner"] == {"name": "round-trip"}


def test_str_tuple_round_trips(qtbot: Any) -> None:
    form = build_form(_ListDemo)
    qtbot.addWidget(form)
    form.set_values({"channels": ("heater.pv", "balance.mass")})
    out = form.values()
    assert tuple(out["channels"]) == ("heater.pv", "balance.mass")


def test_validate_clean_returns_empty(qtbot: Any) -> None:
    form = build_form(_Demo)
    qtbot.addWidget(form)
    form.set_values({"name": "ok", "count": 5})
    assert form.validate() == []


def test_validate_surfaces_error_on_offending_widget(qtbot: Any) -> None:
    """An out-of-range int should land on the count widget. We tighten
    the spinbox max via the constraint mapping, so to actually trip
    Pydantic we have to bypass the spinbox and pass the form a value
    that's-out-of-range for the model but in-range for the widget."""

    class _LooseWidget(BaseModel):
        # The field constrains 0..100 in pydantic; set_values bypasses
        # the spinbox limits and just calls setValue(..) which clamps.
        # So we exercise the validate() path by passing a model whose
        # default is *invalid* under stricter constraints.
        model_config = ConfigDict(extra="forbid")
        count: int = Field(default=0)
        cap: int = Field(default=0, le=10)

    form = build_form(_LooseWidget)
    qtbot.addWidget(form)
    # Reach into the spinbox and bypass its clamp by setting maximum first.
    cap_widget = form._fields["cap"]
    cap_spin = cap_widget.findChild(QSpinBox)
    assert cap_spin is not None
    cap_spin.setMaximum(1000)
    cap_spin.setValue(50)
    errors = form.validate()
    assert errors, "expected at least one validation error"
    # The offending widget got its error styled.
    assert "Less than" in (cap_widget.toolTip() or "") or cap_widget.toolTip()


def test_round_trip_capa_pyrolysis_metadata(qtbot: Any) -> None:
    """End-to-end smoke test against the real CAPA profile metadata
    schema. Populates from a known instance, reads back via
    ``values()``, and reconstructs the model — the reconstructed
    instance must equal the input.

    This exercises the recursive nested-model path
    (CapaSpecimen, HeaterProgram, Atmosphere → PurgeGas) and the
    optional-field handling (sop_revision, analyzer)."""
    initial = CapaPyrolysisMetadata.model_validate(
        {
            "specimen": {
                "id": "P-1",
                "material": "PMMA",
                "initial_mass_g": 5.0,
                "form": "disk",
                "specimen_holder": "stainless steel cup",
            },
            "program": {
                "target_heat_flux_kw_m2": 50.0,
                "heater_setpoint_c": 600.0,
            },
            "atmosphere": {
                "mode": "inert",
                "purge": {
                    "species": "N2",
                    "purity": "UHP 5.0",
                    "target_flow_sccm": 100.0,
                },
            },
        }
    )

    form = build_form(CapaPyrolysisMetadata, initial=initial)
    qtbot.addWidget(form)

    # Round-trip: read values, validate against the model, compare.
    out = form.values()
    rebuilt = CapaPyrolysisMetadata.model_validate(out)
    assert rebuilt.specimen.id == initial.specimen.id
    assert rebuilt.specimen.specimen_holder == initial.specimen.specimen_holder
    assert rebuilt.program.target_heat_flux_kw_m2 == initial.program.target_heat_flux_kw_m2
    assert rebuilt.program.heater_setpoint_c == initial.program.heater_setpoint_c
    assert rebuilt.atmosphere.mode == initial.atmosphere.mode
    assert rebuilt.atmosphere.purge.species == initial.atmosphere.purge.species
    assert rebuilt.atmosphere.purge.target_flow_sccm == initial.atmosphere.purge.target_flow_sccm


def test_values_changed_signal_fires_on_edit(qtbot: Any) -> None:
    form = build_form(_Demo)
    qtbot.addWidget(form)
    name_widget = form._fields["name"]
    edit = name_widget.findChild(QLineEdit)
    assert edit is not None
    with qtbot.waitSignal(form.valuesChanged, timeout=1000):
        edit.setText("hello")
