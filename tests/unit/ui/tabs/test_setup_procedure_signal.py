""":class:`SetupTab` — ``procedureChanged`` signal flow.

The signal is the trigger that feeds :class:`MainWindow`'s Method-tab
gate. Two pieces of behaviour to lock in:

* It fires with the new procedure id when the draft is reloaded or the
  Procedure section's combo changes.
* It is suppressed for identical-id refreshes — the section's
  ``valuesChanged`` fires on every config-form keystroke, but the
  gate only needs to react when the id itself moves.
"""

from __future__ import annotations

from typing import Any

from capa.ui.tabs.setup import SetupTab


def _set_procedure_id(tab: SetupTab, procedure_id: str) -> None:
    """Drive the draft's procedure id and replay the section-edit slot.

    Bypasses the actual QComboBox so the test doesn't depend on the
    ProcedureSection's discovery / combo-population side effects.
    Refreshes the section after the draft mutation so the combo's view
    matches before ``_on_section_edited`` reads it back — otherwise
    the slot's payload-merge step would clobber our draft with the
    stale combo contents.
    """
    tab._draft.document.experiment_payload["procedure"] = {"id": procedure_id, "config": {}}
    tab._refresh_section("procedure")
    tab._on_section_edited("procedure")


def test_procedure_changed_fires_when_id_changes(qtbot: Any) -> None:
    tab = SetupTab()
    qtbot.addWidget(tab)

    received: list[str] = []
    tab.procedureChanged.connect(received.append)

    _set_procedure_id(tab, "capa.builtin.heat_flux_tune")
    _set_procedure_id(tab, "capa.builtin.recipe_runner")

    assert received == ["capa.builtin.heat_flux_tune", "capa.builtin.recipe_runner"]


def test_procedure_changed_suppressed_for_same_id(qtbot: Any) -> None:
    """Successive edits to procedure config (not id) must not re-fire.

    Otherwise every keystroke in the procedure config form would
    re-run MainWindow's gate handler — visible churn on the tab strip
    for no reason.
    """
    tab = SetupTab()
    qtbot.addWidget(tab)

    received: list[str] = []
    tab.procedureChanged.connect(received.append)

    _set_procedure_id(tab, "capa.builtin.heat_flux_tune")
    # Simulate a config-form edit: same id, different config.
    tab._draft.document.experiment_payload["procedure"] = {
        "id": "capa.builtin.heat_flux_tune",
        "config": {"t_set_max_c": 850.0},
    }
    tab._on_section_edited("procedure")

    assert received == ["capa.builtin.heat_flux_tune"]


def test_current_procedure_id_reads_draft(qtbot: Any) -> None:
    tab = SetupTab()
    qtbot.addWidget(tab)

    assert tab.current_procedure_id() == ""

    tab._draft.document.experiment_payload["procedure"] = {
        "id": "capa.builtin.free_run",
        "config": {},
    }
    assert tab.current_procedure_id() == "capa.builtin.free_run"
