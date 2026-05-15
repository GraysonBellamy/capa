"""Hardware glance view tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from capa.config import ConfigDocument
from capa.ui.tabs.setup import SetupTab
from capa.ui.tabs.setup_sections.hardware import HardwareGlanceSection
from capa.ui.tabs.setup_state import SetupDraft

REPO_ROOT = Path(__file__).resolve().parents[4]
SIM_CAPA_EXP = REPO_ROOT / "configs" / "experiments" / "sim_capa_pyrolysis.yaml"


def _make_section(qtbot: Any) -> tuple[HardwareGlanceSection, SetupDraft]:
    document = ConfigDocument.load(SIM_CAPA_EXP)
    draft = SetupDraft(document=document)
    section = HardwareGlanceSection()
    qtbot.addWidget(section)
    section.set_draft(draft)
    return section, draft


def test_glance_summarises_counts(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    text = section._summary.text()
    assert "4 device(s)" in text
    assert "6 channel(s)" in text
    assert "0 camera(s)" in text


def test_glance_populates_tables(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    assert section._devices_model.rowCount() == 4
    assert section._channels_model.rowCount() == 6
    assert section._cameras_model.rowCount() == 0


def test_glance_edit_button_emits_target_section(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    captured: list[str] = []
    section.editSectionRequested.connect(captured.append)
    section._on_edit_requested("channels")
    assert captured == ["channels"]


def test_setup_tab_wires_glance_to_outline(qtbot: Any) -> None:
    """Clicking Edit… on the glance view changes the outline selection."""
    tab = SetupTab()
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    glance = tab._sections["hardware"]
    assert isinstance(glance, HardwareGlanceSection)
    glance.editSectionRequested.emit("channels")
    # Outline selection changed → stack swapped to the Channels page.
    assert tab._stack.currentWidget() is tab._section_panes["channels"]
