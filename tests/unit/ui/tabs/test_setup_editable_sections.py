"""Editable setup-section tests: forms, validation debounce, and Problems navigation.

Covers:

* Each editable section (Experiment, Storage, Procedure, Safety)
  surfaces the current draft on bind and applies edits back into
  ``document.experiment_payload``.
* Edits flip the dirty flag and the source-label suffix.
* The debounce timer runs the validation pipeline.
* Forcing a problem onto the Problems panel routes activation to the
  outline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from capa.config.problems import ConfigProblem
from capa.ui.tabs.setup import SetupTab
from capa.ui.tabs.setup_sections.experiment import ExperimentSection
from capa.ui.tabs.setup_sections.safety import SafetySection
from capa.ui.tabs.setup_sections.storage import StorageSection

REPO_ROOT = Path(__file__).resolve().parents[4]
SIM_CAPA_EXP = REPO_ROOT / "configs" / "experiments" / "sim_capa_pyrolysis.yaml"


# ---------------------------------------------------------------------------
# Section bind / refresh.
# ---------------------------------------------------------------------------


def test_experiment_section_reads_operator_from_fixture(qtbot: Any) -> None:
    tab = SetupTab()
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    section = tab._sections["experiment"]
    assert isinstance(section, ExperimentSection)
    values = section._form.values()
    assert values["operator"]["id"] == "abr"
    # SampleInfo passes through.
    assert values["sample"]["id"] == "SIM-CAPA-001"


def test_storage_section_reads_storage_payload(qtbot: Any) -> None:
    tab = SetupTab()
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    section = tab._sections["storage"]
    assert isinstance(section, StorageSection)
    values = section._form.values()
    # The fixture omits ``storage:`` so defaults apply.
    assert values["bundle_root"] == "runs"


def test_safety_section_reads_rules(qtbot: Any) -> None:
    tab = SetupTab()
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    section = tab._sections["safety"]
    assert isinstance(section, SafetySection)
    # The fixture omits ``safety:`` — default empty rules tuple.
    assert section._model.rowCount() == 0
    assert section._default_abort_edit.text() == "safe_shutdown"


# ---------------------------------------------------------------------------
# Edits flow through to experiment_payload + flip dirty.
# ---------------------------------------------------------------------------


def test_storage_edit_propagates_to_document(qtbot: Any) -> None:
    tab = SetupTab()
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    section = tab._sections["storage"]
    assert isinstance(section, StorageSection)

    # Drive a form field directly — the form's valuesChanged signal
    # routes through the SetupTab edit slot.
    bundle_widget = section._form._fields["bundle_root"]
    bundle_widget.set_value("/tmp/scratch_bundle")
    section._form.valuesChanged.emit()

    payload = tab.draft.document.experiment_payload
    assert payload.get("storage", {}).get("bundle_root") == "/tmp/scratch_bundle"
    assert "storage" in tab.draft.dirty_sections
    # Source label now has the dirty marker.
    assert "●" in tab._source_label.text()


def test_safety_add_rule_flows_through(qtbot: Any) -> None:
    tab = SetupTab()
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    section = tab._sections["safety"]
    assert isinstance(section, SafetySection)

    section._on_add()  # adds a default rule

    payload = tab.draft.document.experiment_payload
    rules = payload.get("safety", {}).get("rules", [])
    assert len(rules) == 1
    assert rules[0]["id"] == "new_rule"
    assert "safety" in tab.draft.dirty_sections


def test_experiment_edit_round_trips_through_save(qtbot: Any, tmp_path: Path) -> None:
    """Edit operator id, save to scratch, reload, assert change persists."""
    import shutil

    work_exp = tmp_path / SIM_CAPA_EXP.name
    work_hw = tmp_path / "sim_capa.toml"
    work_method = tmp_path / "sim_capa_pyrolysis.method.toml"
    shutil.copy(SIM_CAPA_EXP, work_exp)
    shutil.copy(REPO_ROOT / "configs" / "hardware" / "sim_capa.toml", work_hw)
    shutil.copy(
        REPO_ROOT / "configs" / "methods" / "sim_capa_pyrolysis.method.toml",
        work_method,
    )
    original = work_exp.read_text(encoding="utf-8")
    work_exp.write_text(
        original.replace("../hardware/sim_capa.toml", "sim_capa.toml").replace(
            "../methods/sim_capa_pyrolysis.method.toml",
            "sim_capa_pyrolysis.method.toml",
        ),
        encoding="utf-8",
        newline="\n",
    )

    tab = SetupTab()
    qtbot.addWidget(tab)
    tab.load_path(work_exp)
    section = tab._sections["experiment"]
    assert isinstance(section, ExperimentSection)

    # Edit operator id via the underlying form.
    operator_widget = section._form._fields["operator"]
    operator_widget.set_value({"id": "edited_op", "display_name": "Edited"})
    section._form.valuesChanged.emit()

    tab._on_save()

    # Reload through ExperimentConfig.load — checks the round-trip end
    # to end. The operator change should be reflected in the YAML.
    from capa.experiment.config import ExperimentConfig

    reloaded = ExperimentConfig.load(work_exp)
    assert reloaded.operator.id == "edited_op"


# ---------------------------------------------------------------------------
# Debounce + validation.
# ---------------------------------------------------------------------------


def test_validate_runs_after_debounce(qtbot: Any) -> None:
    tab = SetupTab()
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    # The fixture is valid out of the box. Corrupt the experiment
    # payload behind the form's back so the next debounce run surfaces
    # a problem we can observe. (The spinbox would otherwise clamp a
    # negative value to its constraint minimum.)
    tab.draft.document.experiment_payload["storage"] = {"inflight_flush_seconds": -1.0}
    # Mark dirty + start the timer the same way an edit would. We
    # bypass _apply_payload because we already mutated the dict
    # directly — kicking _on_section_edited would just overwrite it
    # with the form's clamped value.
    tab._draft.mark_dirty("storage")
    tab._validate_timer.start()
    qtbot.waitUntil(
        lambda: any(p.severity == "error" for p in tab.draft.problems),
        timeout=2000,
    )
    assert any(p.severity == "error" for p in tab.draft.problems)


def test_validate_button_runs_pipeline_synchronously(qtbot: Any) -> None:
    tab = SetupTab()
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    # Calling the slot directly bypasses the modal.
    tab.draft.validate()
    # The sim CAPA fixture is valid out of the box — zero errors.
    errs = [p for p in tab.draft.problems if p.severity == "error"]
    assert errs == [], f"expected no errors, got {[p.message for p in errs]}"


# ---------------------------------------------------------------------------
# Problems panel navigation.
# ---------------------------------------------------------------------------


def test_problem_activation_selects_outline_section(qtbot: Any) -> None:
    tab = SetupTab()
    qtbot.addWidget(tab)
    problem = ConfigProblem(
        severity="error",
        code="test.dummy",
        message="needs fixing",
        section="procedure",
        path=(),
    )
    tab._problems.set_problems([problem])
    tab._problems.problemActivated.emit(problem)
    item = tab._outline.currentItem()
    assert item is not None
    # Qt UserRole = 0x100
    assert item.data(0, 0x100) == "procedure"
    assert tab._stack.currentWidget() is tab._section_panes["procedure"]
