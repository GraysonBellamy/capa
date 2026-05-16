"""Overview CAPA mappings panel tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QFormLayout, QLabel

from capa.config import ConfigDocument
from capa.ui.tabs.setup_sections.overview import OverviewSection
from capa.ui.tabs.setup_state import SetupDraft

REPO_ROOT = Path(__file__).resolve().parents[4]
SIM_CAPA_EXP = REPO_ROOT / "configs" / "experiments" / "sim_capa_pyrolysis.yaml"


def _make_section(qtbot: Any) -> tuple[OverviewSection, SetupDraft]:
    document = ConfigDocument.load(SIM_CAPA_EXP)
    draft = SetupDraft(document=document)
    section = OverviewSection()
    qtbot.addWidget(section)
    section.set_draft(draft)
    return section, draft


def _form_field_texts(section: OverviewSection) -> list[str]:
    texts: list[str] = []
    for row in range(section._capa_form.rowCount()):
        item = section._capa_form.itemAt(row, QFormLayout.ItemRole.FieldRole)
        if item is None:
            continue
        widget = item.widget()
        if isinstance(widget, QLabel):
            texts.append(widget.text())
    return texts


def test_overview_renders_capa_mapping_rows_for_pyrolysis(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    field_texts = _form_field_texts(section)
    # Required groups all have owners in sim_capa.toml.
    assert any("heater.pv" in t for t in field_texts)
    assert any("heater.setpoint" in t for t in field_texts)
    assert any("purge.flow" in t for t in field_texts)
    assert any("TC_sample_top" in t for t in field_texts)


def test_overview_shows_placeholder_for_non_capa_profile(qtbot: Any) -> None:
    document = ConfigDocument.load(SIM_CAPA_EXP)
    # Strip the profile so we're back to "no CAPA profile loaded".
    document.experiment_payload.pop("domain_profile", None)
    draft = SetupDraft(document=document)
    section = OverviewSection()
    qtbot.addWidget(section)
    section.set_draft(draft)
    field_texts = _form_field_texts(section)
    assert any("no CAPA profile" in t for t in field_texts)


def test_overview_flags_missing_required_group(qtbot: Any) -> None:
    section, draft = _make_section(qtbot)
    # Drop the heater_pv assignment.
    channels = list(draft.document.hardware_payload["channels"])
    for i, ch in enumerate(channels):
        if (ch.get("metadata") or {}).get("capa_group") == "heater_pv":
            new_meta = dict(ch.get("metadata") or {})
            new_meta.pop("capa_group", None)
            new = dict(ch)
            if new_meta:
                new["metadata"] = new_meta
            else:
                new.pop("metadata", None)
            channels[i] = new
    draft.document.hardware_payload["channels"] = channels
    section.refresh()
    field_texts = _form_field_texts(section)
    # The heater_pv row carries the ✗ marker now.
    assert any("✗" in t and "no channel mapped" in t for t in field_texts)
