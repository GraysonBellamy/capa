"""Channels section tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from capa.channels.spec import ChannelKind
from capa.config import ConfigDocument
from capa.config.binding_policy import (
    KIND_TO_PREFERRED_BINDINGS,
    filter_bindings_for_family,
    ordered_bindings_for_kind,
)
from capa.devices.registry import ensure_adapters_loaded
from capa.ui.tabs.setup_sections.channels import (
    ChannelsSection,
    _channel_from_template,
)
from capa.ui.tabs.setup_state import SetupDraft

REPO_ROOT = Path(__file__).resolve().parents[4]
SIM_CAPA_EXP = REPO_ROOT / "configs" / "experiments" / "sim_capa_pyrolysis.yaml"


# ---------------------------------------------------------------------------
# Binding-policy pure functions.
# ---------------------------------------------------------------------------


def test_ordered_bindings_for_thermocouple_prefers_nidaq() -> None:
    ordered = ordered_bindings_for_kind(ChannelKind.THERMOCOUPLE)
    assert ordered[0] == "nidaq_reading_field"
    # Every variant is still present.
    assert set(ordered) >= set(KIND_TO_PREFERRED_BINDINGS[ChannelKind.THERMOCOUPLE])


def test_ordered_bindings_falls_back_on_unknown_kind() -> None:
    ordered = ordered_bindings_for_kind(None)
    assert ordered[0] == "watlow_parameter"


def test_filter_bindings_keeps_derived() -> None:
    ordered = ordered_bindings_for_kind(ChannelKind.THERMOCOUPLE)
    filtered = filter_bindings_for_family(ordered, ("nidaq_reading_field",))
    assert "nidaq_reading_field" in filtered
    # ``derived`` is always preserved at the end.
    assert filtered[-1] == "derived"


def test_filter_bindings_with_none_supported_passes_through() -> None:
    ordered = ordered_bindings_for_kind(ChannelKind.MASS)
    assert filter_bindings_for_family(ordered, None) == ordered


# ---------------------------------------------------------------------------
# Channel template → dict helper.
# ---------------------------------------------------------------------------


def test_channel_from_template_writes_capa_group_in_metadata() -> None:
    ensure_adapters_loaded()
    from capa.devices._templates import WATLOW_HEATER_PV

    record = _channel_from_template(WATLOW_HEATER_PV, existing_names=[], device_name="heater")
    assert record["kind"] == "process_var"
    assert record["unit"] == "degC"
    assert record["plot_group"] == "temperatures"
    assert record["metadata"]["capa_group"] == "heater_pv"
    assert record["source"]["source"] == "watlow_parameter"
    assert record["source"]["device"] == "heater"


# ---------------------------------------------------------------------------
# Section behaviour against the sim_capa fixture.
# ---------------------------------------------------------------------------


def _make_section(qtbot: Any) -> tuple[ChannelsSection, SetupDraft]:
    document = ConfigDocument.load(SIM_CAPA_EXP)
    draft = SetupDraft(document=document)
    section = ChannelsSection()
    qtbot.addWidget(section)
    section.set_draft(draft)
    return section, draft


def test_channels_section_lists_channels_from_hardware_payload(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    names = [c["name"] for c in section._model.channels()]
    # Six channels in sim_capa.toml.
    assert names == [
        "heater.pv",
        "heater.setpoint",
        "purge.flow",
        "TC_sample_top",
        "TC_sample_mid",
        "balance.mass",
    ]


def test_channels_section_payload_under_channels_key(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    payload = section.payload()
    assert "channels" in payload
    assert isinstance(payload["channels"], list)
    assert len(payload["channels"]) == 6


def test_channels_section_select_populates_detail_widgets(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    section._table.selectRow(0)  # heater.pv
    assert section._name_edit.text() == "heater.pv"
    assert section._kind_combo.currentData() == "process_var"
    assert section._unit_edit.text() == "degC"
    assert section._capa_group_edit.text() == "heater_pv"
    # Source editor reflects the bound device + variant.
    assert section._source_editor._device_combo.currentData() == "heater"
    assert section._source_editor._variant_combo.currentData() == "watlow_parameter"


def test_channels_section_rename_round_trips(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    section._table.selectRow(0)
    section._name_edit.setText("heater.pv_renamed")
    # Manually drive the slot to avoid relying on Qt event delivery in the
    # headless test loop.
    section._on_name_changed("heater.pv_renamed")
    channels = section._model.channels()
    assert channels[0]["name"] == "heater.pv_renamed"


def test_channels_section_change_kind_reorders_binding_combo(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    # Add a blank channel and flip its kind to thermocouple — the
    # binding combo should put nidaq_reading_field first.
    section._on_add_blank()
    last_row = section._model.rowCount() - 1
    section._table.selectRow(last_row)
    tc_idx = section._kind_combo.findData(ChannelKind.THERMOCOUPLE.value)
    section._kind_combo.setCurrentIndex(tc_idx)
    section._on_kind_changed(tc_idx)
    # Empty / new channel has no device pinned, so no family filter
    # applies — the kind preference still steers the order.
    assert section._source_editor._variant_combo.itemData(0) == "nidaq_reading_field"


def test_channels_section_filter_variants_by_device_family(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    section._table.selectRow(0)  # heater.pv → heater device (Watlow sim)
    # The Watlow sim descriptor only supports ``watlow_parameter``.
    combo = section._source_editor._variant_combo
    # Some sim adapter families may not declare supported sources, but
    # the watlow_sim does; verify watlow_parameter is the only entry
    # (or at least the first one).
    items = [combo.itemData(i) for i in range(combo.count())]
    assert "watlow_parameter" in items
    assert items[0] == "watlow_parameter"


def test_channels_section_add_from_template_creates_row(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    from capa.devices._templates import SARTORIUS_MASS

    section._on_add_from_template(SARTORIUS_MASS)
    rows = section._model.channels()
    new = rows[-1]
    assert new["kind"] == "mass"
    assert new["source"]["source"] == "sartorius_reading"
    # Should auto-bind to the existing sartorius_sim device "balance".
    assert new["source"]["device"] == "balance"


def test_channels_section_remove_drops_row(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    section._table.selectRow(0)
    section._on_remove()
    rows = section._model.channels()
    assert len(rows) == 5
    assert rows[0]["name"] == "heater.setpoint"


def test_channels_section_capa_group_writes_metadata(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    section._table.selectRow(2)  # purge.flow
    # Clear the capa_group then set a new one.
    section._on_capa_group_changed("")
    chan = section._model.channels()[2]
    assert "metadata" not in chan or "capa_group" not in chan.get("metadata", {})
    section._on_capa_group_changed("purge_gas_flow")
    chan = section._model.channels()[2]
    assert chan["metadata"]["capa_group"] == "purge_gas_flow"


def test_channels_section_round_trips_save_after_edit(qtbot: Any, tmp_path: Path) -> None:
    """Edit a channel, save the document to a temp dir, reload, diff."""
    document = ConfigDocument.load(SIM_CAPA_EXP)
    draft = SetupDraft(document=document)
    section = ChannelsSection()
    qtbot.addWidget(section)
    section.set_draft(draft)

    section._table.selectRow(0)
    section._on_unit_changed("degF")
    # Push the section's payload into the document.
    draft.document.hardware_payload.update(section.payload())

    # Save to a fresh location.
    new_exp = tmp_path / "sim_capa_pyrolysis.yaml"
    new_hw = tmp_path / "hardware" / "sim_capa.toml"
    from capa.config import SourceLayout

    draft.document.save_as(
        SourceLayout(
            experiment_path=new_exp,
            experiment_format="yaml",
            hardware_path=new_hw,
            hardware_format="toml",
            hardware_mode="external",
            method_path=None,
            method_format=None,
            method_mode="none",
        )
    )
    reloaded = ConfigDocument.load(new_exp)
    assert reloaded.hardware_payload["channels"][0]["unit"] == "degF"
