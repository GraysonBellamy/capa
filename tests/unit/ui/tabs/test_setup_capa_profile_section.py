"""CAPA Profile section tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from capa.config import ConfigDocument
from capa.config.capa_profile import (
    CAPA_OPTIONAL_GROUPS,
    CAPA_REQUIRED_GROUPS,
    current_capa_mappings,
)
from capa.ui.tabs.setup_sections.capa_profile import CapaProfileSection
from capa.ui.tabs.setup_state import SetupDraft

REPO_ROOT = Path(__file__).resolve().parents[4]
SIM_CAPA_EXP = REPO_ROOT / "configs" / "experiments" / "sim_capa_pyrolysis.yaml"


# ---------------------------------------------------------------------------
# Shared helper.
# ---------------------------------------------------------------------------


def test_current_capa_mappings_extracts_groups() -> None:
    channels = [
        {"name": "heater.pv", "metadata": {"capa_group": "heater_pv"}},
        {"name": "TC_top_1", "metadata": {"capa_group": "sample_temperature"}},
        {"name": "TC_top_2", "metadata": {"capa_group": "sample_temperature"}},
        {"name": "noise", "metadata": {}},
    ]
    mappings = current_capa_mappings(channels)
    assert mappings == {
        "heater_pv": ["heater.pv"],
        "sample_temperature": ["TC_top_1", "TC_top_2"],
    }


def test_required_groups_present_for_pyrolysis() -> None:
    assert {
        "heater_setpoint",
        "heater_pv",
        "sample_temperature",
        "purge_gas_flow",
        "mass",
    } == set(CAPA_REQUIRED_GROUPS)
    assert "reactor_pressure" not in CAPA_OPTIONAL_GROUPS
    assert "mass" not in CAPA_OPTIONAL_GROUPS


# ---------------------------------------------------------------------------
# Section behaviour.
# ---------------------------------------------------------------------------


def _make_section(qtbot: Any) -> tuple[CapaProfileSection, SetupDraft]:
    document = ConfigDocument.load(SIM_CAPA_EXP)
    draft = SetupDraft(document=document)
    section = CapaProfileSection()
    qtbot.addWidget(section)
    section.set_draft(draft)
    return section, draft


def test_capa_profile_renders_mapping_rows(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    groups = [row.group for row in section._mapping_rows]
    # Required + optional groups present, required first.
    assert groups[: len(CAPA_REQUIRED_GROUPS)] == list(CAPA_REQUIRED_GROUPS)


def test_capa_profile_chip_states_reflect_existing_mappings(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    chips = {row.group: row.chip.text() for row in section._mapping_rows}
    # sim_capa.toml maps all required groups → ✓.
    for group in CAPA_REQUIRED_GROUPS:
        assert chips[group] == "✓", f"{group} should be ✓ in sim_capa.toml"


def test_capa_profile_payload_includes_channels_and_profile(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    payload = section.payload()
    assert "channels" in payload
    assert "domain_profile" in payload
    profile = payload["domain_profile"]
    assert isinstance(profile, dict)
    assert "metadata" in profile
    metadata = profile["metadata"]
    assert "specimen" in metadata
    assert "heater_program" in metadata
    assert "atmosphere" in metadata


def test_capa_profile_change_mapping_updates_channel_metadata(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    # Find the heater_pv mapping combo; change it from heater.pv to
    # heater.setpoint (silly choice for the kind, but the test verifies
    # only metadata routing — the validator handles kind sanity).
    for row in section._mapping_rows:
        if row.group != "heater_pv":
            continue
        # The combo has been populated by allowed kinds (process_var);
        # only heater.pv is a process_var. Let's verify the combo
        # contains only that channel + the (none) sentinel.
        items = [row.combo.itemData(i) for i in range(row.combo.count())]
        assert items == ["", "heater.pv"]
        # Clear the mapping.
        row.combo.setCurrentIndex(0)
        section._on_mapping_changed("heater_pv")
        break

    channels = section._compose_channels_with_mappings()
    # heater.pv channel should have lost its capa_group.
    target = next(c for c in channels if c["name"] == "heater.pv")
    assert "capa_group" not in (target.get("metadata") or {})


def test_capa_profile_specimen_pane_round_trips(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    section._specimen_form.set_values(
        {
            "id": "pmma_disk_S073-001",
            "material": "PMMA",
            "form": "disk",
            "mass_g": 25.0,
            "thickness_mm": 10.0,
            "specimen_holder": "ceramic_ring_25mm",
        }
    )
    payload = section.payload()
    metadata = payload["domain_profile"]["metadata"]
    assert metadata["specimen"]["id"] == "pmma_disk_S073-001"
    assert metadata["specimen"]["material"] == "PMMA"
    assert metadata["specimen"]["mass_g"] == 25.0


def test_capa_profile_compose_preserves_unmanaged_capa_group(qtbot: Any) -> None:
    """A channel mapped to a non-required, non-optional ``capa_group``
    (a plugin's custom group) should keep its metadata across compose."""
    section, draft = _make_section(qtbot)
    channels = list(draft.document.hardware_payload["channels"])
    channels.append(
        {
            "name": "exotic",
            "kind": "process_var",
            "unit": "Pa",
            "source": {"source": "watlow_parameter", "device": "heater", "parameter": "x"},
            "metadata": {"capa_group": "exotic_plugin_group"},
        }
    )
    draft.document.hardware_payload["channels"] = channels
    section.refresh()
    composed = section._compose_channels_with_mappings()
    exotic = next(c for c in composed if c["name"] == "exotic")
    assert exotic["metadata"]["capa_group"] == "exotic_plugin_group"
