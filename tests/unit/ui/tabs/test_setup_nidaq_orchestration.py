"""Tests for SetupTab's NI-DAQ cross-section orchestration.

SetupTab handles two coupled mutations that NIDAQChannelsField surfaces
but cannot perform itself:

1. Create one capa channel per unbound NI input — driven from the
   widget's "Create capa channels…" button.
2. Remove an NI input row that's referenced by a capa channel — with
   a Yes / No / Cancel prompt that decides whether to remove the
   referencing channels too.

Both flows must route through :meth:`SetupTab._apply_payload` so dirty
state, validation, and undo stay coherent across the two sections.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from PySide6.QtWidgets import QMessageBox

from capa.config import ConfigDocument
from capa.ui.tabs.setup import SetupTab
from capa.ui.tabs.setup_state import SetupDraft

REPO_ROOT = Path(__file__).resolve().parents[4]
NIDAQ_REAL_EXP = REPO_ROOT / "configs" / "experiments" / "nidaq_real_freerun.yaml"


def _make_tab_with_draft(qtbot: Any, exp_path: Path = NIDAQ_REAL_EXP) -> SetupTab:
    document = ConfigDocument.load(exp_path)
    tab = SetupTab()
    qtbot.addWidget(tab)
    tab._draft = SetupDraft(document=document)
    return tab


# ---------------------------------------------------------------------------
# _collect_bound_nidaq_triples
# ---------------------------------------------------------------------------


def test_collect_bound_triples_returns_every_nidaq_binding(qtbot: Any) -> None:
    tab = _make_tab_with_draft(qtbot)
    bound = tab._collect_bound_nidaq_triples()
    # nidaq_real.toml has two TC channels bound to cdaq1.default_task.
    assert ("cdaq1", "default_task", "TC_top_1") in bound
    assert ("cdaq1", "default_task", "TC_top_2") in bound


def test_collect_bound_triples_returns_empty_for_no_nidaq_bindings(qtbot: Any) -> None:
    tab = SetupTab()
    qtbot.addWidget(tab)
    # Empty draft, no channels → empty set.
    assert tab._collect_bound_nidaq_triples() == set()


# ---------------------------------------------------------------------------
# _on_create_capa_channels_for_unbound
# ---------------------------------------------------------------------------


def test_create_capa_channels_for_unbound_adds_rows_with_correct_units(qtbot: Any) -> None:
    """Add a fresh NI input that no capa channel references, then run the
    "create capa channels" flow — exactly one new ``[[channels]]`` row
    should appear with ``unit="degC"`` (the NI row's declared unit).
    """
    tab = _make_tab_with_draft(qtbot)
    # Add a third NI channel that no capa channel binds to.
    devices = tab._draft.document.hardware_payload["devices"]
    devices[0] = dict(devices[0])
    devices[0]["params"] = dict(devices[0]["params"])
    devices[0]["params"]["channels"] = [
        *devices[0]["params"]["channels"],
        {
            "kind": "thermocouple",
            "physical_channel": "cDAQ1Mod1/ai2",
            "name": "TC_top_3",
            "thermocouple_type": "K",
            "min_val": 0.0,
            "max_val": 1000.0,
            "cjc_source": "BUILT_IN",
            "units": "DEG_C",
            "adc_timing_mode": "HIGH_RESOLUTION",
            "auto_zero_mode": "ONCE",
        },
    ]
    before = len(tab._draft.document.hardware_payload["channels"])
    tab._on_create_capa_channels_for_unbound(
        None,
        [
            {
                "field_name": "TC_top_3",
                "kind": "thermocouple",
                "units": "DEG_C",
                "physical_channel": "cDAQ1Mod1/ai2",
            }
        ],
    )
    after = tab._draft.document.hardware_payload["channels"]
    assert len(after) == before + 1
    new_row = after[-1]
    assert new_row["kind"] == "tc"
    assert new_row["unit"] == "degC"
    assert new_row["source"]["device"] == "cdaq1"
    assert new_row["source"]["task"] == "default_task"
    assert new_row["source"]["field"] == "TC_top_3"


def test_create_capa_channels_for_unbound_skips_already_bound_fields(qtbot: Any) -> None:
    """The widget may emit an unbound list that became stale before the
    orchestrator fires — the orchestrator must idempotently skip any
    NI channel that's already bound by the time it runs.
    """
    tab = _make_tab_with_draft(qtbot)
    before = len(tab._draft.document.hardware_payload["channels"])
    # TC_top_1 is already bound in the fixture.
    tab._on_create_capa_channels_for_unbound(
        None,
        [{"field_name": "TC_top_1", "kind": "thermocouple", "units": "DEG_C"}],
    )
    after = len(tab._draft.document.hardware_payload["channels"])
    assert after == before


def test_create_capa_channels_for_unbound_uses_full_join_key(qtbot: Any) -> None:
    """Same NI field name on another device/task is a distinct input and
    should not be skipped because cdaq1.default_task is already bound.
    """
    tab = _make_tab_with_draft(qtbot)
    devices = tab._draft.document.hardware_payload["devices"]
    devices.append(
        {
            "name": "cdaq2",
            "adapter": "capa.devices.nidaq",
            "params": {
                "task_name": "other_task",
                "rate_hz": 10.0,
                "channels": [
                    {
                        "kind": "thermocouple",
                        "physical_channel": "cDAQ2Mod1/ai0",
                        "name": "TC_top_1",
                        "thermocouple_type": "K",
                        "min_val": 0.0,
                        "max_val": 1000.0,
                        "units": "DEG_C",
                    }
                ],
            },
        }
    )
    before = len(tab._draft.document.hardware_payload["channels"])
    tab._on_create_capa_channels_for_unbound(
        None,
        [
            {
                "device_name": "cdaq2",
                "task_name": "other_task",
                "field_name": "TC_top_1",
                "kind": "thermocouple",
                "units": "DEG_C",
                "physical_channel": "cDAQ2Mod1/ai0",
            }
        ],
    )
    after = tab._draft.document.hardware_payload["channels"]
    assert len(after) == before + 1
    assert after[-1]["source"]["device"] == "cdaq2"
    assert after[-1]["source"]["task"] == "other_task"


def test_create_capa_channels_for_unbound_no_op_on_empty(qtbot: Any) -> None:
    tab = _make_tab_with_draft(qtbot)
    before = list(tab._draft.document.hardware_payload["channels"])
    tab._on_create_capa_channels_for_unbound(None, [])
    after = tab._draft.document.hardware_payload["channels"]
    assert after == before


# ---------------------------------------------------------------------------
# _on_delete_nidaq_with_bindings
# ---------------------------------------------------------------------------


def test_delete_with_bindings_yes_removes_both_sections(qtbot: Any) -> None:
    """Operator clicks Yes — both the NI row and the bound capa channels
    are removed in a single coherent state transition.
    """
    tab = _make_tab_with_draft(qtbot)
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
        tab._on_delete_nidaq_with_bindings(
            None, "cdaq1", "default_task", "TC_top_1", ["TC_sample_top"]
        )
    ni_channels = tab._draft.document.hardware_payload["devices"][0]["params"]["channels"]
    capa_channels = tab._draft.document.hardware_payload["channels"]
    assert all(c.get("name") != "TC_top_1" for c in ni_channels)
    assert all(c.get("name") != "TC_sample_top" for c in capa_channels)


def test_delete_with_bindings_yes_computes_bound_names_when_widget_omits_them(qtbot: Any) -> None:
    """The real widget path may hand over an empty name list; SetupTab must
    compute the referencing capa channel names before applying the Yes path.
    """
    tab = _make_tab_with_draft(qtbot)
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
        tab._on_delete_nidaq_with_bindings(None, "cdaq1", "default_task", "TC_top_1", [])
    capa_channels = tab._draft.document.hardware_payload["channels"]
    assert all(c.get("name") != "TC_sample_top" for c in capa_channels)


def test_delete_with_bindings_no_keeps_capa_channel(qtbot: Any) -> None:
    """No → drop just the NI row; let Layer-2 flag the broken binding."""
    tab = _make_tab_with_draft(qtbot)
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
        tab._on_delete_nidaq_with_bindings(
            None, "cdaq1", "default_task", "TC_top_1", ["TC_sample_top"]
        )
    ni_channels = tab._draft.document.hardware_payload["devices"][0]["params"]["channels"]
    capa_channels = tab._draft.document.hardware_payload["channels"]
    assert all(c.get("name") != "TC_top_1" for c in ni_channels)
    assert any(c.get("name") == "TC_sample_top" for c in capa_channels)


def test_delete_with_bindings_cancel_is_noop(qtbot: Any) -> None:
    tab = _make_tab_with_draft(qtbot)
    before_ni = list(tab._draft.document.hardware_payload["devices"][0]["params"]["channels"])
    before_capa = list(tab._draft.document.hardware_payload["channels"])
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel):
        tab._on_delete_nidaq_with_bindings(
            None, "cdaq1", "default_task", "TC_top_1", ["TC_sample_top"]
        )
    assert tab._draft.document.hardware_payload["devices"][0]["params"]["channels"] == before_ni
    assert tab._draft.document.hardware_payload["channels"] == before_capa
