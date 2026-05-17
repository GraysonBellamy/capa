"""NI-DAQ-aware channels-section behaviours.

Step 4 of the NI-DAQ UX work wires the channels section to
:mod:`capa.devices.nidaq_join` for three things:

1. The "reads from" combo (NI variant) sources its field choices from
   declared NI channels instead of free-text.
2. The Add menu surfaces one entry per declared NI channel, with units
   derived from the NI row.
3. The table paints a row's "reads from" cell red when its NI binding
   doesn't resolve against any declared NI channel.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt

from capa.config import ConfigDocument
from capa.ui.tabs.setup_sections.channels import (
    ChannelsSection,
    _nidaq_unit_kind_calibration,
    _SourceBindingEditor,
)
from capa.ui.tabs.setup_state import SetupDraft

REPO_ROOT = Path(__file__).resolve().parents[4]
NIDAQ_REAL_EXP = REPO_ROOT / "configs" / "experiments" / "nidaq_real_freerun.yaml"


def _make_section(
    qtbot: Any, exp_path: Path = NIDAQ_REAL_EXP
) -> tuple[ChannelsSection, SetupDraft]:
    document = ConfigDocument.load(exp_path)
    draft = SetupDraft(document=document)
    section = ChannelsSection()
    qtbot.addWidget(section)
    section.set_draft(draft)
    return section, draft


# ---------------------------------------------------------------------------
# Unit translation helper.
# ---------------------------------------------------------------------------


def test_nidaq_thermocouple_degc_yields_degc_calibration() -> None:
    """The latent ``K`` vs ``degC`` mismatch that the generic template
    used to ship: a thermocouple declared as DEG_C on the NI side must
    produce a capa channel with ``unit="degC"`` and identity-degC
    calibration so the schema check passes.
    """
    unit, kind, cal = _nidaq_unit_kind_calibration("thermocouple", "DEG_C")
    assert unit == "degC"
    assert kind == "tc"
    assert cal == {"kind": "identity", "input_unit": "degC", "output_unit": "degC"}


def test_nidaq_thermocouple_k_yields_k_calibration() -> None:
    """Kelvin TC is rare in CAPA but the helper must still pick the right unit."""
    unit, _kind, cal = _nidaq_unit_kind_calibration("thermocouple", "K")
    assert unit == "K"
    assert cal is not None
    assert cal["input_unit"] == "K"


def test_nidaq_unknown_kind_returns_blank_unit_no_calibration() -> None:
    unit, kind, cal = _nidaq_unit_kind_calibration("digital_input", None)
    assert unit == ""
    assert kind == "analog_in"
    assert cal is None


# ---------------------------------------------------------------------------
# Live join validation — table paints unresolved bindings red.
# ---------------------------------------------------------------------------


def test_correct_binding_does_not_flag_any_row(qtbot: Any) -> None:
    section, _draft = _make_section(qtbot)
    issues = section._model._row_issues
    assert issues == {}


def test_typoed_field_paints_the_offending_row(qtbot: Any) -> None:
    """A binding pointing at a field that isn't declared on the NI device
    surfaces as a non-empty row issue, with the available fields listed.
    """
    section, draft = _make_section(qtbot)
    # Mutate the first channel's binding to a typo.
    channels = list(draft.document.hardware_payload["channels"])
    channels[0] = dict(channels[0])
    channels[0]["source"] = dict(channels[0]["source"])
    channels[0]["source"]["field"] = "TC_top_X"
    draft.document.hardware_payload["channels"] = channels
    section.refresh()
    issues = section._model._row_issues
    assert 0 in issues
    assert "TC_top_X" in issues[0]
    assert "TC_top_1" in issues[0]  # available field is named


def test_empty_nidaq_task_paints_bound_rows(qtbot: Any) -> None:
    section, draft = _make_section(qtbot)
    devices = list(draft.document.hardware_payload["devices"])
    devices[0] = dict(devices[0])
    devices[0]["params"] = dict(devices[0]["params"])
    devices[0]["params"]["channels"] = []
    draft.document.hardware_payload["devices"] = devices
    section.refresh()
    issues = section._model._row_issues
    assert 0 in issues
    assert "No NI fields" in issues[0]


def test_row_issue_surfaces_via_model_data_roles(qtbot: Any) -> None:
    """Background + tooltip roles return a non-None value on the
    ``binding`` column for an unresolved-binding row.
    """
    section, draft = _make_section(qtbot)
    channels = list(draft.document.hardware_payload["channels"])
    channels[0] = dict(channels[0])
    channels[0]["source"] = dict(channels[0]["source"])
    channels[0]["source"]["field"] = "DoesNotExist"
    draft.document.hardware_payload["channels"] = channels
    section.refresh()
    model = section._model
    # COLUMN_KEYS = ("name", "kind", "device", "binding", "unit") — index 3.
    binding_index = model.index(0, 3)
    assert model.data(binding_index, Qt.ItemDataRole.BackgroundRole) is not None
    tooltip = model.data(binding_index, Qt.ItemDataRole.ToolTipRole)
    assert isinstance(tooltip, str)
    assert "DoesNotExist" in tooltip


def test_clean_binding_returns_no_background(qtbot: Any) -> None:
    section, _draft = _make_section(qtbot)
    model = section._model
    binding_index = model.index(0, 3)
    assert model.data(binding_index, Qt.ItemDataRole.BackgroundRole) is None


# ---------------------------------------------------------------------------
# NI-aware Add menu entries.
# ---------------------------------------------------------------------------


def test_add_menu_includes_declared_nidaq_entries(qtbot: Any) -> None:
    """Real-NI fixture declares two TC inputs; the Add menu surfaces one
    entry per declared NI channel.
    """
    section, _draft = _make_section(qtbot)
    labels = [a.text() for a in section._add_menu.actions() if a.text()]
    nidaq_entries = [label for label in labels if "cdaq1.TC_top" in label]
    assert len(nidaq_entries) == 2
    assert any("TC_top_1" in label for label in nidaq_entries)
    assert any("TC_top_2" in label for label in nidaq_entries)


def test_add_from_declared_nidaq_uses_declared_units(qtbot: Any) -> None:
    """Clicking an NI-aware Add entry on a DEG_C thermocouple must produce
    a capa channel with ``unit="degC"`` — the bug §2.4 in the evaluation
    doc was that the generic template hard-coded ``K``.
    """
    section, _draft = _make_section(qtbot)
    from capa.devices.nidaq_join import declared_channels_from_payload

    declared = declared_channels_from_payload(_draft.document.hardware_payload)
    target = next(d for d in declared if d.field_name == "TC_top_1")
    before = len(section._model.channels())
    section._on_add_from_declared_nidaq(target)
    after = section._model.channels()
    assert len(after) == before + 1
    new_row = after[-1]
    assert new_row["kind"] == "tc"
    assert new_row["unit"] == "degC"
    assert new_row["derived_unit"] == "degC"
    assert new_row["calibration"]["input_unit"] == "degC"
    assert new_row["source"]["source"] == "nidaq_reading_field"
    assert new_row["source"]["device"] == "cdaq1"
    assert new_row["source"]["field"] == "TC_top_1"


# ---------------------------------------------------------------------------
# Variant-field combo populated from declared NI channels.
# ---------------------------------------------------------------------------


def test_binding_editor_renders_combo_for_nidaq_field_when_declared_provided(qtbot: Any) -> None:
    """When the section feeds declared NI channels into the editor, the
    ``field`` widget for nidaq_reading_field becomes a QComboBox seeded
    with declared field names.
    """
    from PySide6.QtWidgets import QComboBox

    editor = _SourceBindingEditor()
    qtbot.addWidget(editor)
    devices = [
        {"name": "cdaq1", "adapter": "capa.devices.nidaq", "params": {"task_name": "ai_task"}}
    ]
    declared = (
        ("cdaq1", "ai_task", "TC_a"),
        ("cdaq1", "ai_task", "TC_b"),
    )
    editor.set_context(devices=devices, kind="tc", nidaq_declared=declared)
    editor.set_value(
        {
            "source": "nidaq_reading_field",
            "device": "cdaq1",
            "task": "ai_task",
            "field": "TC_a",
        }
    )
    field_widget = editor._variant_fields["field"]
    assert isinstance(field_widget, QComboBox)
    assert field_widget.isEditable()
    choices = [field_widget.itemText(i) for i in range(field_widget.count())]
    assert "TC_a" in choices
    assert "TC_b" in choices


def test_binding_editor_falls_back_to_lineedit_when_no_declared(qtbot: Any) -> None:
    """Backwards compat: an editor with no declared NI inventory still
    renders the legacy free-text QLineEdit. Operators editing on a
    machine without NI hardware shouldn't be blocked.
    """
    from PySide6.QtWidgets import QLineEdit

    editor = _SourceBindingEditor()
    qtbot.addWidget(editor)
    devices = [
        {"name": "cdaq1", "adapter": "capa.devices.nidaq", "params": {"task_name": "ai_task"}}
    ]
    editor.set_context(devices=devices, kind="tc", nidaq_declared=())
    editor.set_value(
        {
            "source": "nidaq_reading_field",
            "device": "cdaq1",
            "task": "ai_task",
            "field": "TC_a",
        }
    )
    field_widget = editor._variant_fields["field"]
    assert isinstance(field_widget, QLineEdit)
    assert field_widget.text() == "TC_a"
