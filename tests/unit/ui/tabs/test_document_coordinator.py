"""Tests for :class:`DocumentCoordinator` Setup ↔ Method sync.

Covers:

* Loading a setup with an external method ref populates MethodTab.
* Loading a setup with no method clears MethodTab.
* Setting the method ref to a different file via the Files section
  reloads MethodTab.
* Setting the method mode to "none" clears MethodTab.
* The coordinator's re-entry guard prevents an infinite ping-pong:
  Setup.methodRefChanged → MethodTab.load → MethodTab.methodChanged →
  Setup refresh does not produce a new Setup.methodRefChanged emit.
* MethodTab.methodChanged updates the Setup draft's method_payload.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from capa.experiment.method import HoldStep, Method
from capa.ui.document_coordinator import DocumentCoordinator
from capa.ui.tabs.method import MethodTab
from capa.ui.tabs.setup import SetupTab

REPO_ROOT = Path(__file__).resolve().parents[4]
SIM_CAPA_EXP = REPO_ROOT / "configs" / "experiments" / "sim_capa_pyrolysis.yaml"


def _make_pair(qtbot: Any) -> tuple[SetupTab, MethodTab, DocumentCoordinator]:
    setup = SetupTab()
    method = MethodTab()
    qtbot.addWidget(setup)
    qtbot.addWidget(method)
    coordinator = DocumentCoordinator(setup_tab=setup, method_tab=method)
    return setup, method, coordinator


# ---------------------------------------------------------------------------
# Initial sync on load.
# ---------------------------------------------------------------------------


def test_load_setup_with_external_method_populates_method_tab(
    qtbot: Any,
) -> None:
    setup, method_tab, _coord = _make_pair(qtbot)
    setup.load_path(SIM_CAPA_EXP)
    assert method_tab.has_method()
    name = method_tab.current_method_name()
    # The sim fixture's method is non-empty and has a name.
    assert name and name != "untitled"


def test_load_setup_with_no_method_clears_method_tab(qtbot: Any, tmp_path: Path) -> None:
    setup, method_tab, _coord = _make_pair(qtbot)
    # Seed MethodTab with something so we can observe clearing.
    method_tab.load_method(
        Method(
            name="seeded",
            steps=(HoldStep(target={"name": "heater.setpoint"}, value=25.0, duration_s=10.0),),
        )
    )
    assert method_tab.has_method()

    # Build a free-run experiment YAML.
    yaml_path = tmp_path / "free.yaml"
    hw_path = tmp_path / "rig.toml"
    hw_path.write_text(
        'name = "rig"\ndevices = []\nchannels = []\ncameras = []\n',
        encoding="utf-8",
        newline="\n",
    )
    yaml_path.write_text(
        f"hardware: {hw_path.name}\n"
        "procedure:\n  id: capa.builtin.recipe_runner\n  config: {}\n"
        "calibration_set:\n  name: default\n"
        "operator:\n  id: tester\n"
        "sample:\n  id: ZZ-1\n",
        encoding="utf-8",
        newline="\n",
    )
    setup.load_path(yaml_path)
    # No method on the loaded fixture — coordinator clears.
    assert not method_tab.has_method()


# ---------------------------------------------------------------------------
# Setup → Method propagation on Files-section toggle.
# ---------------------------------------------------------------------------


def test_method_mode_none_clears_method_tab(qtbot: Any) -> None:
    setup, method_tab, _coord = _make_pair(qtbot)
    setup.load_path(SIM_CAPA_EXP)
    assert method_tab.has_method()
    files = setup._sections["files"]
    # FilesSection: flip to "none".
    files._method_none.setChecked(True)  # type: ignore[attr-defined]
    assert not method_tab.has_method()


def test_method_path_change_loads_new_method(qtbot: Any, tmp_path: Path) -> None:
    setup, method_tab, _coord = _make_pair(qtbot)
    setup.load_path(SIM_CAPA_EXP)
    first_name = method_tab.current_method_name()

    # Build a second, distinct method file and swap to it.
    other = tmp_path / "other.method.toml"
    other.write_text(
        'name = "other_method"\n'
        'description = ""\n'
        "[[steps]]\n"
        'kind = "hold"\n'
        'target = { name = "heater.setpoint" }\n'
        "value = 25.0\n"
        "duration_s = 5.0\n",
        encoding="utf-8",
        newline="\n",
    )
    setup.draft.document.method_path = other.resolve()
    setup.draft.document.method_mode = "external"
    setup.draft.document.method_format = "toml"
    # Now drive the coordinator slot directly — Files section
    # normally emits methodRefChanged after such an edit.
    setup.methodRefChanged.emit(other.resolve())
    assert method_tab.has_method()
    assert method_tab.current_method_name() == "other_method"
    assert method_tab.current_method_name() != first_name


# ---------------------------------------------------------------------------
# Re-entry guard.
# ---------------------------------------------------------------------------


def test_initial_sync_does_not_loop(qtbot: Any) -> None:
    """Loading a draft must not produce a feedback storm.

    MethodTab.load → MethodTab.methodChanged → Setup refresh → ... If
    the guard is missing, MethodTab.load would re-enter via
    methodChanged and produce extra dirty marks. We assert by counting
    Setup.methodRefChanged emissions during a single load_path.
    """
    setup, _method_tab, _coord = _make_pair(qtbot)
    fired: list[object] = []
    setup.methodRefChanged.connect(fired.append)
    setup.load_path(SIM_CAPA_EXP)
    # Coordinator's initial sync should not fire Setup.methodRefChanged
    # (that signal only fires from FilesSection toggles).
    assert fired == []
    # Draft should not be dirty as a side-effect of the initial sync.
    assert not setup.draft.is_dirty


# ---------------------------------------------------------------------------
# Method → Setup propagation.
# ---------------------------------------------------------------------------


def test_method_tab_edit_updates_setup_payload(qtbot: Any, tmp_path: Path) -> None:
    setup, method_tab, _coord = _make_pair(qtbot)
    # Copy the fixture into scratch so we don't pollute the repo.
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
    setup.load_path(work_exp)
    # Load a different method into MethodTab — coordinator updates
    # the Setup draft's method_payload.
    new_method = Method(
        name="overridden",
        steps=(
            HoldStep(
                target={"name": "heater.setpoint"},
                value=42.0,
                duration_s=7.0,
            ),
        ),
    )
    method_tab.load_method(new_method, path=work_method)
    assert setup.draft.document.method_payload is not None
    assert setup.draft.document.method_payload.get("name") == "overridden"
    # Files section is marked dirty because the payload changed.
    assert "files" in setup.draft.dirty_sections
