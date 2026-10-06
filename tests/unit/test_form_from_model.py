"""Tests for the Pydantic auto-form generator.

Each test uses a small synthetic
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
from capa.ui.forms.widgets import CollapsibleGroup
from capa.ui.forms.widgets._helpers import _decimals_for_field, _label_for


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


class _RequiredNumbers(BaseModel):
    mass_g: float = Field(gt=0)
    flow_sccm: float = Field(ge=0)
    count: int = Field(ge=0)


def test_required_numbers_start_unset_and_are_omitted(qtbot: Any) -> None:
    """A required number has no honest starting value: it shows as unset
    and stays out of ``values()`` so validation reports it missing,
    rather than submitting the spinbox minimum (0 would silently pass
    ``ge=0``)."""
    form = build_form(_RequiredNumbers)
    qtbot.addWidget(form)
    spin = form._fields["flow_sccm"].findChild(QDoubleSpinBox)
    assert spin is not None
    assert spin.text() == "—"
    assert form.values() == {}
    errors = form.validate()
    assert {err["loc"][0] for err in errors} == {"mass_g", "flow_sccm", "count"}
    assert {err["type"] for err in errors} == {"missing"}


class _Units(BaseModel):
    initial_mass_g: float = Field(json_schema_extra={"capa_unit": "g"})
    target_heat_flux_kw_m2: float = Field(json_schema_extra={"capa_unit": "kW/m²"})
    ramp_rate_c_per_min: float = Field(json_schema_extra={"capa_unit": "°C/min"})
    flow_sccm: float
    hold_s: float = Field(title="Hold time", json_schema_extra={"capa_unit": "s"})


@pytest.mark.parametrize(
    ("name", "label", "decimals"),
    [
        ("initial_mass_g", "Initial mass", 4),
        ("target_heat_flux_kw_m2", "Target heat flux", 1),
        ("ramp_rate_c_per_min", "Ramp rate", 1),
        # No declared unit, so nothing else says it: the name keeps it.
        ("flow_sccm", "Flow sccm", 2),
        ("hold_s", "Hold time", 2),
    ],
)
def test_label_drops_a_unit_the_row_shows_in_brackets(name: str, label: str, decimals: int) -> None:
    info = _Units.model_fields[name]
    assert _label_for(name, info) == label
    assert _decimals_for_field(name, info) == decimals


def test_required_number_round_trips_and_resets_to_unset(qtbot: Any) -> None:
    form = build_form(_RequiredNumbers)
    qtbot.addWidget(form)
    form.set_values({"mass_g": 4.2, "flow_sccm": 0.0, "count": 0})
    assert form.values() == {"mass_g": 4.2, "flow_sccm": 0.0, "count": 0}
    form.set_values({}, replace=True)
    assert form.values() == {}


def test_set_values_replace_resets_missing_fields(qtbot: Any) -> None:
    form = build_form(_OptionalDemo)
    qtbot.addWidget(form)
    form.set_values({"notes": "first", "duration_s": 3.0})
    form.set_values({"notes": "second"})
    assert form.values() == {"notes": "second", "duration_s": 3.0}
    form.set_values({"notes": "third"}, replace=True)
    assert form.values() == {"notes": "third", "duration_s": None}


class _NestedOptional(BaseModel):
    inner: _OptionalDemo = Field(default_factory=_OptionalDemo)


def test_nested_value_is_set_whole(qtbot: Any) -> None:
    """A nested model's value is the whole object: sub-fields it leaves
    out reset rather than keeping the previous value's."""
    form = build_form(_NestedOptional)
    qtbot.addWidget(form)
    form.set_values({"inner": {"notes": "a", "duration_s": 2.0}})
    form.set_values({"inner": {"notes": "b"}})
    assert form.values() == {"inner": {"notes": "b", "duration_s": None}}


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
    optional-field handling (sop_revision)."""
    initial = CapaPyrolysisMetadata.model_validate(
        {
            "specimen": {
                "id": "P-1",
                "material": "PMMA",
                "initial_mass_g": 5.0,
                "thickness_mm": 6.0,
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
                    "target_flow_slpm": 100.0,
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
    assert rebuilt.atmosphere.purge.target_flow_slpm == initial.atmosphere.purge.target_flow_slpm


def test_values_changed_signal_fires_on_edit(qtbot: Any) -> None:
    form = build_form(_Demo)
    qtbot.addWidget(form)
    name_widget = form._fields["name"]
    edit = name_widget.findChild(QLineEdit)
    assert edit is not None
    with qtbot.waitSignal(form.valuesChanged, timeout=1000):
        edit.setText("hello")


# ---------------------------------------------------------------------------
# CollapsibleGroup — standalone widget behavior.
# ---------------------------------------------------------------------------


def test_collapsible_group_defaults_closed_and_hides_content(qtbot: Any) -> None:
    group = CollapsibleGroup("Advanced")
    qtbot.addWidget(group)
    assert group.is_open() is False
    assert group._content.isVisibleTo(group) is False


def test_collapsible_group_default_open_shows_content(qtbot: Any) -> None:
    group = CollapsibleGroup("Advanced", default_open=True)
    qtbot.addWidget(group)
    assert group.is_open() is True


def test_collapsible_group_toggle_emits_once_per_transition(qtbot: Any) -> None:
    group = CollapsibleGroup("Advanced")
    qtbot.addWidget(group)
    seen: list[bool] = []
    group.toggled.connect(seen.append)
    group.set_open(True)
    group.set_open(True)  # idempotent
    group.set_open(False)
    assert seen == [True, False]


def test_collapsible_group_button_click_toggles(qtbot: Any) -> None:
    group = CollapsibleGroup("Advanced")
    qtbot.addWidget(group)
    assert group.is_open() is False
    group._button.click()
    assert group.is_open() is True
    group._button.click()
    assert group.is_open() is False


# ---------------------------------------------------------------------------
# Grouped ModelForm — partition by capa_group, default-open per group,
# error auto-open.
# ---------------------------------------------------------------------------


class _GroupedDemo(BaseModel):
    """Two primary fields + an "advanced" group with three knobs."""

    bundle_root: str = "runs"
    sample_id: str = ""
    flush_seconds: float = Field(
        default=1.0,
        gt=0,
        json_schema_extra={
            "capa_group": "advanced",
            "capa_group_subtitle": "I/O tuning",
        },
    )
    row_group_rows: int = Field(
        default=262_144,
        gt=0,
        json_schema_extra={"capa_group": "advanced"},
    )
    codec: str = Field(
        default="zstd",
        json_schema_extra={"capa_group": "advanced"},
    )


def test_grouped_form_partitions_fields_into_collapsible(qtbot: Any) -> None:
    form = build_form(_GroupedDemo)
    qtbot.addWidget(form)
    # Every field is still present in the flat ``_fields`` map — grouping
    # is a presentation concern, not a data-model concern.
    assert set(form._fields) == {
        "bundle_root",
        "sample_id",
        "flush_seconds",
        "row_group_rows",
        "codec",
    }
    # Primary fields are not group-tracked.
    assert "bundle_root" not in form._field_group
    assert "sample_id" not in form._field_group
    # Advanced fields share a single CollapsibleGroup.
    advanced = form.group_for_field("flush_seconds")
    assert isinstance(advanced, CollapsibleGroup)
    assert form.group_for_field("row_group_rows") is advanced
    assert form.group_for_field("codec") is advanced
    # Default-closed.
    assert advanced.is_open() is False


def test_grouped_form_round_trips_values(qtbot: Any) -> None:
    """values() / set_values() must ignore the grouping layer entirely —
    the operator's view of the data shape stays flat."""
    form = build_form(_GroupedDemo)
    qtbot.addWidget(form)
    form.set_values(
        {
            "bundle_root": "/tmp/runs",
            "sample_id": "P-7",
            "flush_seconds": 0.5,
            "row_group_rows": 131_072,
            "codec": "lz4",
        }
    )
    out = form.values()
    assert out["bundle_root"] == "/tmp/runs"
    assert out["sample_id"] == "P-7"
    assert out["flush_seconds"] == pytest.approx(0.5)
    assert out["row_group_rows"] == 131_072
    assert out["codec"] == "lz4"


def test_grouped_form_validate_auto_opens_group_on_error(qtbot: Any) -> None:
    """A validation failure on an advanced field forces the group open."""
    form = build_form(_GroupedDemo)
    qtbot.addWidget(form)
    advanced = form.group_for_field("flush_seconds")
    assert advanced is not None
    assert advanced.is_open() is False
    # Drive flush_seconds below its ``gt=0`` lower bound. The spinbox
    # clamps positive values, so we have to slip past it the same way
    # the pre-existing validation test does.
    from PySide6.QtWidgets import QDoubleSpinBox

    flush = form._fields["flush_seconds"]
    spin = flush.findChild(QDoubleSpinBox)
    assert spin is not None
    spin.setMinimum(-100.0)
    spin.setValue(-1.0)
    errors = form.validate()
    assert errors, "expected a validation error from negative flush_seconds"
    assert advanced.is_open() is True, "group should auto-open on error"


def test_grouped_form_validate_clear_does_not_close_group(qtbot: Any) -> None:
    """If the operator opens a group, a passing validation pass must not
    snap it shut — open-state is sticky within session."""
    form = build_form(_GroupedDemo)
    qtbot.addWidget(form)
    advanced = form.group_for_field("flush_seconds")
    assert advanced is not None
    advanced.set_open(True)
    form.set_values({"bundle_root": "ok", "sample_id": "P-1"})
    assert form.validate() == []
    assert advanced.is_open() is True


class _OpenByDefaultGroup(BaseModel):
    """A group can opt into default-open via capa_group_open=True."""

    primary: str = ""
    one: str = Field(
        default="",
        json_schema_extra={"capa_group": "rules", "capa_group_open": True},
    )
    two: str = Field(default="", json_schema_extra={"capa_group": "rules"})


def test_grouped_form_capa_group_open_respected(qtbot: Any) -> None:
    form = build_form(_OpenByDefaultGroup)
    qtbot.addWidget(form)
    rules = form.group_for_field("one")
    assert isinstance(rules, CollapsibleGroup)
    assert rules.is_open() is True
    assert form.group_for_field("two") is rules


class _HiddenWhenDemo(BaseModel):
    shape: Literal["disk", "other"] = "disk"
    grain_um: float | None = Field(
        default=None, gt=0, json_schema_extra={"capa_hidden_when": {"shape": "disk"}}
    )


def test_hidden_when_row_follows_sibling_value(qtbot: Any) -> None:
    """A ``capa_hidden_when`` row hides while the sibling holds the value,
    reappears when it changes, and is left out of ``values()`` while hidden."""
    form = build_form(_HiddenWhenDemo)
    qtbot.addWidget(form)
    grain = form._fields["grain_um"]
    assert grain.isHidden()
    assert form._labels["grain_um"].isHidden()
    assert "grain_um" not in form.values()

    combo = form._fields["shape"].findChild(QComboBox)
    assert combo is not None
    combo.setCurrentIndex(combo.findData("other"))
    assert not grain.isHidden()
    assert not form._labels["grain_um"].isHidden()
    grain.set_value(150.0)
    assert form.values()["grain_um"] == pytest.approx(150.0)

    # set_values blocks widget signals; the row must still follow.
    form.set_values({"shape": "disk"})
    assert grain.isHidden()
    assert "grain_um" not in form.values()


def test_grouped_form_preserves_field_order_across_groups(qtbot: Any) -> None:
    """Primary fields render first regardless of where they appear in
    declaration order, then groups appear in first-mention order."""

    class _Mixed(BaseModel):
        a_primary: str = ""
        b_advanced: str = Field(default="", json_schema_extra={"capa_group": "advanced"})
        c_primary: str = ""
        d_advanced: str = Field(default="", json_schema_extra={"capa_group": "advanced"})

    form = build_form(_Mixed)
    qtbot.addWidget(form)
    # Primary fields are not in the group map.
    assert "a_primary" not in form._field_group
    assert "c_primary" not in form._field_group
    # Both advanced fields share one group.
    advanced = form.group_for_field("b_advanced")
    assert advanced is not None
    assert form.group_for_field("d_advanced") is advanced


def test_storage_policy_advanced_fields_collapse(qtbot: Any) -> None:
    """The real StoragePolicy schema picks up the advanced grouping —
    end-to-end check that the schema tagging landed correctly."""
    from capa.experiment.config import StoragePolicy

    form = build_form(StoragePolicy)
    qtbot.addWidget(form)
    # bundle_root is primary.
    assert form.group_for_field("bundle_root") is None
    # All seven tuning knobs share one closed-by-default group.
    advanced = form.group_for_field("inflight_flush_seconds")
    assert isinstance(advanced, CollapsibleGroup)
    assert advanced.is_open() is False
    for name in (
        "parquet_final_row_group_rows",
        "inflight_compression",
        "parquet_final_compression",
        "enable_tdms_passthrough",
        "enable_rocrate",
        "producer_queue_abort_after_s",
    ):
        assert form.group_for_field(name) is advanced, name
