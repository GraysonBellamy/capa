"""SetupTab tests — outline tree, hardware payload routing, Devices section.

Covers the cross-cutting prep (outline children, payload routing) and the
Devices section. Channels / Cameras / Hardware glance / CAPA Profile tests
land alongside their respective sections.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest  # noqa: F401 — qtbot fixture is implicit

from capa.config import ConfigDocument
from capa.ui.tabs.setup import _HARDWARE_PAYLOAD_KEYS, SetupTab
from capa.ui.tabs.setup_outline import ALL_SECTIONS, SetupOutline
from capa.ui.tabs.setup_sections.devices import DevicesSection
from capa.ui.tabs.setup_state import SetupDraft

REPO_ROOT = Path(__file__).resolve().parents[4]
SIM_CAPA_EXP = REPO_ROOT / "configs" / "experiments" / "sim_capa_pyrolysis.yaml"


# ---------------------------------------------------------------------------
# E0 — outline tree and payload routing.
# ---------------------------------------------------------------------------


def test_outline_has_hardware_subtree_and_capa_profile(qtbot: Any) -> None:
    outline = SetupOutline()
    qtbot.addWidget(outline)
    ids = {entry.section_id for entry in ALL_SECTIONS}
    # Hardware parent + three children + CAPA Profile sibling.
    assert {"hardware", "devices", "channels", "cameras", "capa_profile"} <= ids


def test_outline_rolls_up_child_dirty_to_hardware_parent(qtbot: Any) -> None:
    outline = SetupOutline()
    qtbot.addWidget(outline)
    outline.set_markers(dirty_sections={"channels"}, problems=[])
    # Hardware parent label gets the dirty marker when only a child is dirty.
    hw_item = outline._items["hardware"]
    assert "●" in hw_item.text(0)


def test_outline_rolls_up_child_error_to_hardware_parent(qtbot: Any) -> None:
    outline = SetupOutline()
    qtbot.addWidget(outline)
    from capa.config.problems import ConfigProblem

    outline.set_markers(
        dirty_sections=set(),
        problems=[
            ConfigProblem(
                severity="error",
                code="x.test",
                message="test",
                section="devices",
            )
        ],
    )
    hw_item = outline._items["hardware"]
    assert "✗" in hw_item.text(0)


def test_hardware_payload_keys_are_routed_correctly() -> None:
    # The router is a flat key check; this test pins the contract so a
    # future regression doesn't silently inline channels into the
    # experiment payload.
    assert frozenset({"devices", "channels", "cameras"}) == _HARDWARE_PAYLOAD_KEYS


def test_apply_payload_routes_devices_to_hardware_payload(qtbot: Any) -> None:
    tab = SetupTab()
    qtbot.addWidget(tab)
    tab._apply_payload("devices", {"devices": [{"name": "x", "adapter": "y"}]})
    assert tab._draft.document.hardware_payload["devices"][0]["name"] == "x"
    # Experiment payload stays untouched.
    assert "devices" not in tab._draft.document.experiment_payload


def test_apply_payload_splits_capa_profile_payload(qtbot: Any) -> None:
    """The CAPA Profile section emits a multi-key payload — channels go
    to hardware, domain_profile to experiment. The router does the split."""
    tab = SetupTab()
    qtbot.addWidget(tab)
    tab._apply_payload(
        "capa_profile",
        {
            "channels": [{"name": "c1"}],
            "domain_profile": {"id": "capa.profiles.capa_pyrolysis"},
        },
    )
    assert tab._draft.document.hardware_payload["channels"][0]["name"] == "c1"
    assert (
        tab._draft.document.experiment_payload["domain_profile"]["id"]
        == "capa.profiles.capa_pyrolysis"
    )


# ---------------------------------------------------------------------------
# E1 — Devices section.
# ---------------------------------------------------------------------------


def test_devices_section_lists_devices_from_hardware_payload(qtbot: Any) -> None:
    document = ConfigDocument.load(SIM_CAPA_EXP)
    draft = SetupDraft(document=document)
    section = DevicesSection()
    qtbot.addWidget(section)
    section.set_draft(draft)
    devices = section._model.devices()
    # The sim_capa fixture has 4 devices: heater, purge_mfc, balance, cdaq1.
    assert [d["name"] for d in devices] == [
        "heater",
        "purge_mfc",
        "balance",
        "cdaq1",
    ]


def test_devices_section_emits_payload_under_devices_key(qtbot: Any) -> None:
    document = ConfigDocument.load(SIM_CAPA_EXP)
    draft = SetupDraft(document=document)
    section = DevicesSection()
    qtbot.addWidget(section)
    section.set_draft(draft)
    payload = section.payload()
    assert "devices" in payload
    assert isinstance(payload["devices"], list)
    assert len(payload["devices"]) == 4


def test_devices_section_add_device_appends_row(qtbot: Any) -> None:
    document = ConfigDocument.load(SIM_CAPA_EXP)
    draft = SetupDraft(document=document)
    section = DevicesSection()
    qtbot.addWidget(section)
    section.set_draft(draft)

    from capa.devices.registry import ADAPTERS

    descriptor = ADAPTERS["capa.devices.sim.watlow_sim"]
    section._on_add_device(descriptor)
    devices = section._model.devices()
    assert len(devices) == 5
    # New row carries the descriptor's default_params.
    assert devices[-1]["adapter"] == descriptor.id
    assert devices[-1]["params"] == descriptor.default_params


def test_devices_section_remove_drops_selected_row(qtbot: Any) -> None:
    document = ConfigDocument.load(SIM_CAPA_EXP)
    draft = SetupDraft(document=document)
    section = DevicesSection()
    qtbot.addWidget(section)
    section.set_draft(draft)
    section._table.selectRow(0)
    section._on_remove()
    devices = section._model.devices()
    assert len(devices) == 3
    # Original device order preserved sans the removed first row.
    assert devices[0]["name"] == "purge_mfc"


def test_devices_section_handshake_signal_fires(qtbot: Any) -> None:
    document = ConfigDocument.load(SIM_CAPA_EXP)
    draft = SetupDraft(document=document)
    section = DevicesSection()
    qtbot.addWidget(section)
    section.set_draft(draft)
    received: list[str] = []
    section.handshakeRequested.connect(received.append)
    section._table.selectRow(0)  # heater
    section._on_test_connection()
    assert received == ["heater"]


def test_devices_section_round_trips_through_setup_tab(qtbot: Any) -> None:
    """Edit a device name via the section, assert it lands in
    ``hardware_payload`` via the SetupTab's routing helper."""
    tab = SetupTab()
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    devices_section = tab._sections["devices"]
    assert isinstance(devices_section, DevicesSection)
    devices_section._table.selectRow(0)
    devices_section._name_edit.setText("heater_renamed")
    # The section emits valuesChanged synchronously on text change.
    devices_section._on_name_changed("heater_renamed")
    tab._on_section_edited("devices")
    hw = tab._draft.document.hardware_payload
    assert hw["devices"][0]["name"] == "heater_renamed"
    # Setup tab marks the section dirty.
    assert "devices" in tab._draft.dirty_sections
