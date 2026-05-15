"""Tests for ``_DiscriminatedUnionField`` (plan §5.8)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field
from PySide6.QtWidgets import QComboBox

from capa.channels.calibration import (
    Calibration,
    Identity,
    LinearTwoPoint,
)
from capa.ui.forms import build_form
from capa.ui.forms.widgets import (
    _DiscriminatedUnionField,
    _is_discriminated_union,
)

# ---------------------------------------------------------------------------
# Small synthetic union to isolate the widget's behaviour from any
# domain-model evolution.
# ---------------------------------------------------------------------------


class _VariantA(BaseModel):
    kind: Literal["a"] = "a"
    common_field: str = "shared"
    a_only: int = 1


class _VariantB(BaseModel):
    kind: Literal["b"] = "b"
    common_field: str = "shared"
    b_only: float = 0.0


_TestUnion = Annotated[_VariantA | _VariantB, Field(discriminator="kind")]


class _Holder(BaseModel):
    """Wrapper model so build_form can render the union as a field."""

    value: _TestUnion = Field(default_factory=_VariantA)


# ---------------------------------------------------------------------------
# Detection.
# ---------------------------------------------------------------------------


def test_detects_discriminated_union() -> None:
    assert _is_discriminated_union(_TestUnion) is True


def test_rejects_plain_union() -> None:
    plain: Any = int | str
    assert _is_discriminated_union(plain) is False


def test_rejects_optional_scalar() -> None:
    assert _is_discriminated_union(int | None) is False


# ---------------------------------------------------------------------------
# Dispatcher routes to the widget.
# ---------------------------------------------------------------------------


def test_dispatcher_picks_discriminated_union_widget(qtbot: Any) -> None:
    form = build_form(_Holder)
    qtbot.addWidget(form)
    widget = form._fields["value"]
    assert isinstance(widget, _DiscriminatedUnionField)


# ---------------------------------------------------------------------------
# Variant switching preserves overlapping fields.
# ---------------------------------------------------------------------------


def test_cross_variant_value_preservation(qtbot: Any) -> None:
    form = build_form(_Holder)
    qtbot.addWidget(form)
    widget: _DiscriminatedUnionField = form._fields["value"]
    # Initial variant is A; set common_field via the subform.
    widget._current_form.set_values({"common_field": "operator-typed"})
    # Flip variant to B.
    combo: QComboBox = widget._combo
    for i in range(combo.count()):
        if combo.itemData(i) == "b":
            combo.setCurrentIndex(i)
            break
    # After flip, common_field should carry over; b_only should be its
    # default.
    values = widget.value()
    assert values["kind"] == "b"
    assert values["common_field"] == "operator-typed"


def test_non_overlapping_fields_drop_silently(qtbot: Any) -> None:
    """Switching variant must not raise; missing-on-other-variant
    fields are silently dropped from the buffer view."""
    form = build_form(_Holder)
    qtbot.addWidget(form)
    widget: _DiscriminatedUnionField = form._fields["value"]
    widget._current_form.set_values({"a_only": 42, "common_field": "x"})
    # Now flip to variant B which has no a_only field.
    for i in range(widget._combo.count()):
        if widget._combo.itemData(i) == "b":
            widget._combo.setCurrentIndex(i)
            break
    values = widget.value()
    # a_only is silently absent (B has no such field).
    assert "a_only" not in values
    # common_field preserved.
    assert values["common_field"] == "x"


# ---------------------------------------------------------------------------
# Round-trip with the real Calibration union.
# ---------------------------------------------------------------------------


class _ChannelLike(BaseModel):
    """Holder model: mirrors the discriminator pattern from ChannelSpec."""

    calibration: Calibration = Field(default_factory=Identity)


def test_calibration_identity_round_trip(qtbot: Any) -> None:
    form = build_form(_ChannelLike)
    qtbot.addWidget(form)
    widget: _DiscriminatedUnionField = form._fields["calibration"]
    instance = Identity(input_unit="degC", output_unit="degC")
    widget.set_value(instance)
    out = widget.value()
    assert out["kind"] == "identity"
    assert out["input_unit"] == "degC"
    # Validate back to ensure the dict is a legal Calibration.
    reparsed = _ChannelLike.model_validate({"calibration": out})
    assert isinstance(reparsed.calibration, Identity)


def test_calibration_linear_two_point_round_trip(qtbot: Any) -> None:
    form = build_form(_ChannelLike)
    qtbot.addWidget(form)
    widget: _DiscriminatedUnionField = form._fields["calibration"]
    instance = LinearTwoPoint(
        input_unit="V",
        output_unit="degC",
        ref_low_raw=0.0,
        ref_low_value=0.0,
        ref_high_raw=5.0,
        ref_high_value=1000.0,
    )
    widget.set_value(instance)
    out = widget.value()
    assert out["kind"] == "linear_two_point"
    assert out["ref_high_raw"] == 5.0
    reparsed = _ChannelLike.model_validate({"calibration": out})
    assert isinstance(reparsed.calibration, LinearTwoPoint)


def test_calibration_variant_switch_preserves_units(qtbot: Any) -> None:
    """Flipping Identity → LinearTwoPoint preserves ``input_unit``."""
    form = build_form(_ChannelLike)
    qtbot.addWidget(form)
    widget: _DiscriminatedUnionField = form._fields["calibration"]
    widget.set_value(Identity(input_unit="degC", output_unit="degC"))
    # Flip to linear_two_point.
    for i in range(widget._combo.count()):
        if widget._combo.itemData(i) == "linear_two_point":
            widget._combo.setCurrentIndex(i)
            break
    out = widget.value()
    assert out["kind"] == "linear_two_point"
    assert out["input_unit"] == "degC"
    assert out["output_unit"] == "degC"


# ---------------------------------------------------------------------------
# Discriminator hidden inside the subform.
# ---------------------------------------------------------------------------


def test_discriminator_field_not_rendered_in_subform(qtbot: Any) -> None:
    form = build_form(_Holder)
    qtbot.addWidget(form)
    widget: _DiscriminatedUnionField = form._fields["value"]
    # The subform must not have a widget for "kind" — the variant combo
    # above owns that responsibility.
    assert "kind" not in widget._current_form._fields
    # Common fields are still there.
    assert "common_field" in widget._current_form._fields
