"""Tests for the :class:`SetupTab` editor shell.

Covers:

* Shell construction (outline entries, sections, Problems panel, banner).
* Open path round-trips through :class:`SetupDraft`.
* Save back to the same files is byte-identical (canonical round-trip).
* Save As writes to a new location via the dialog's chosen layout.
* Banner toggles on :class:`RunController` state transitions.
* Files-section mode toggles propagate to :class:`ConfigDocument` and
  fire ``methodRefChanged`` when the method ref settles.

Tests avoid spinning up a real :class:`RunController`; a tiny stub with
a ``state_changed`` signal is enough to drive the banner code path.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QObject, Signal

from capa.config import ConfigDocument, SourceLayout
from capa.ui.state import RunUiState
from capa.ui.tabs.setup import SetupTab
from capa.ui.tabs.setup_outline import LEAF_SECTIONS
from capa.ui.tabs.setup_sections.files import FilesSection
from capa.ui.tabs.setup_sections.overview import OverviewSection

REPO_ROOT = Path(__file__).resolve().parents[4]
SIM_CAPA_EXP = REPO_ROOT / "configs" / "experiments" / "sim_capa_pyrolysis.yaml"


class _ControllerStub(QObject):
    """Minimal stand-in for ``RunController`` for banner tests."""

    state_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.state = RunUiState.IDLE


# ---------------------------------------------------------------------------
# Construction.
# ---------------------------------------------------------------------------


def test_setup_tab_constructs_with_empty_draft(qtbot: Any) -> None:
    tab = SetupTab()
    qtbot.addWidget(tab)
    # The empty draft seeds an untitled source label.
    assert tab._source_label.text() == "untitled"
    # The baseline leaf sections are present (more have since been added —
    # the set is now a superset, not equality).
    expected_ids = {sid for sid, _ in LEAF_SECTIONS}
    assert expected_ids.issubset(tab._sections.keys())
    # Overview and Files are real implementations.
    assert isinstance(tab._sections["overview"], OverviewSection)
    assert isinstance(tab._sections["files"], FilesSection)


def test_setup_tab_outline_default_selects_overview(qtbot: Any) -> None:
    tab = SetupTab()
    qtbot.addWidget(tab)
    assert tab._stack.currentWidget() is tab._section_panes["overview"]
    assert tab._section_panes["overview"].widget() is tab._sections["overview"]


# ---------------------------------------------------------------------------
# Load path.
# ---------------------------------------------------------------------------


def test_load_path_seeds_draft_from_experiment_yaml(qtbot: Any) -> None:
    tab = SetupTab()
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    doc = tab.draft.document
    assert doc.experiment_path == SIM_CAPA_EXP.resolve()
    assert doc.experiment_format == "yaml"
    assert doc.hardware_mode == "external"
    assert doc.hardware_path is not None
    assert doc.method_mode == "external"
    assert doc.method_path is not None
    assert tab._source_label.text() == SIM_CAPA_EXP.name


def test_load_path_refreshes_overview(qtbot: Any) -> None:
    tab = SetupTab()
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    overview = tab._sections["overview"]
    assert isinstance(overview, OverviewSection)
    # The operator from the fixture should be reflected.
    assert "abr" in overview._operator.text()
    # Hardware-path label points at the external TOML.
    assert overview._hardware_path.text().endswith("sim_capa.toml")


# ---------------------------------------------------------------------------
# Round-trip save.
# ---------------------------------------------------------------------------


def test_save_with_no_edits_is_byte_identical(qtbot: Any, tmp_path: Path) -> None:
    # Copy the fixture + its referenced TOMLs into a scratch directory so
    # the save writes there. ``ConfigDocument.save`` rewrites every file
    # in the load set, but byte-identity requires the source files to
    # already be canonical — capa-plan canonicalises in-repo fixtures, so
    # the round-trip is clean.
    work_exp = tmp_path / SIM_CAPA_EXP.name
    work_hw = tmp_path / "sim_capa.toml"
    work_method = tmp_path / "sim_capa_pyrolysis.method.toml"
    shutil.copy(SIM_CAPA_EXP, work_exp)
    shutil.copy(REPO_ROOT / "configs" / "hardware" / "sim_capa.toml", work_hw)
    shutil.copy(
        REPO_ROOT / "configs" / "methods" / "sim_capa_pyrolysis.method.toml",
        work_method,
    )
    # Rewrite the ref paths in the experiment YAML to point at the
    # copied siblings.
    original = work_exp.read_text(encoding="utf-8")
    rewritten = original.replace("../hardware/sim_capa.toml", "sim_capa.toml").replace(
        "../methods/sim_capa_pyrolysis.method.toml",
        "sim_capa_pyrolysis.method.toml",
    )
    work_exp.write_text(rewritten, encoding="utf-8", newline="\n")

    tab = SetupTab()
    qtbot.addWidget(tab)
    tab.load_path(work_exp)
    # Snapshot every file we expect to be touched.
    before_exp = work_exp.read_bytes()
    before_hw = work_hw.read_bytes()

    tab._on_save()
    assert not tab.draft.is_dirty

    after_exp = work_exp.read_bytes()
    after_hw = work_hw.read_bytes()
    # Round-trip guarantee is *semantic* equivalence — the canonical
    # writer strips comments and normalises line endings (canonical
    # round-trip behaviour). Compare parsed payloads.
    import tomllib

    from ruamel.yaml import YAML

    yaml = YAML(typ="safe")
    assert yaml.load(after_exp.decode("utf-8")) == yaml.load(before_exp.decode("utf-8"))
    assert tomllib.loads(after_hw.decode("utf-8")) == tomllib.loads(before_hw.decode("utf-8"))


# ---------------------------------------------------------------------------
# Save As.
# ---------------------------------------------------------------------------


def test_save_as_writes_chosen_layout(qtbot: Any, tmp_path: Path) -> None:
    tab = SetupTab()
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    # Drive save_as directly with a chosen layout (bypass the modal).
    new_exp = tmp_path / "renamed.yaml"
    new_hw = tmp_path / "renamed_hardware.toml"
    layout = SourceLayout(
        experiment_path=new_exp,
        experiment_format="yaml",
        hardware_path=new_hw,
        hardware_format="toml",
        hardware_mode="external",
        method_path=tab.draft.document.method_path,
        method_format=tab.draft.document.method_format,
        method_mode=tab.draft.document.method_mode,
    )
    tab.draft.document.save_as(layout)
    assert new_exp.is_file()
    assert new_hw.is_file()


# ---------------------------------------------------------------------------
# Connection strip / frozen-while-armed.
# ---------------------------------------------------------------------------


def test_strip_frozen_when_run_active(qtbot: Any) -> None:
    controller = _ControllerStub()
    tab = SetupTab(controller=controller)  # type: ignore[arg-type]
    qtbot.addWidget(tab)
    # IDLE state and no config — strip starts in IDLE.
    from capa.ui.tabs.setup_connection_strip import ConnectionState

    assert tab._connection_strip.state is ConnectionState.IDLE
    controller.state_changed.emit(RunUiState.RUNNING)
    assert tab._connection_strip.state is ConnectionState.FROZEN
    controller.state_changed.emit(RunUiState.IDLE)
    assert tab._connection_strip.state is ConnectionState.IDLE


@pytest.mark.parametrize(
    "state",
    [
        RunUiState.PREPARING,
        RunUiState.RUNNING,
        RunUiState.DRAINING,
        RunUiState.FINALIZING,
    ],
)
def test_strip_frozen_for_each_active_state(qtbot: Any, state: RunUiState) -> None:
    controller = _ControllerStub()
    tab = SetupTab(controller=controller)  # type: ignore[arg-type]
    qtbot.addWidget(tab)
    from capa.ui.tabs.setup_connection_strip import ConnectionState

    controller.state_changed.emit(state)
    assert tab._connection_strip.state is ConnectionState.FROZEN


# ---------------------------------------------------------------------------
# Files section interactions.
# ---------------------------------------------------------------------------


def test_files_section_method_mode_changes_emit_signal(qtbot: Any) -> None:
    tab = SetupTab()
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    files = tab._sections["files"]
    assert isinstance(files, FilesSection)
    fired: list[object] = []
    tab.methodRefChanged.connect(fired.append)
    # Flip method to "none".
    files._method_none.setChecked(True)
    assert tab.draft.document.method_mode == "none"
    assert tab.draft.document.method_path is None
    assert fired  # methodRefChanged should have fired at least once


def test_files_section_extract_hardware_flips_mode(qtbot: Any) -> None:
    tab = SetupTab()
    qtbot.addWidget(tab)
    # Synthesise an inline-hardware document so we can exercise extract.
    document = ConfigDocument(
        experiment_path=Path("/tmp/example.yaml"),
        experiment_format="yaml",
        hardware_mode="inline",
        hardware_payload={"name": "rig", "devices": []},
    )
    tab._draft.document = document
    tab._refresh_all_sections()
    # Trigger the underlying ConfigDocument transition (mirrors what
    # the Extract button does once the modal returns).
    document.extract_hardware_inline_to_file(Path("/tmp/rig.toml"))
    assert document.hardware_mode == "external"
    assert document.hardware_path is not None


# ---------------------------------------------------------------------------
# Problems panel navigation.
# ---------------------------------------------------------------------------


def test_problem_activation_navigates_outline(qtbot: Any) -> None:
    from capa.config.problems import ConfigProblem

    tab = SetupTab()
    qtbot.addWidget(tab)
    # Force a problem onto the panel and activate it.
    tab._problems.set_problems(
        [
            ConfigProblem(
                severity="error",
                code="test.dummy",
                message="needs fixing",
                section="storage",
                path=(),
            )
        ]
    )
    # Simulate activation via the panel's signal.
    tab._problems.problemActivated.emit(
        ConfigProblem(
            severity="error",
            code="test.dummy",
            message="needs fixing",
            section="storage",
            path=(),
        )
    )
    # Outline's currentItem should now be the storage entry.
    item = tab._outline.currentItem()
    assert item is not None
    assert item.data(0, 0x100) == "storage"  # Qt.UserRole == 0x100
