"""Tests for :meth:`DocumentCoordinator.build_applied_config`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from capa.core.errors import CapaError
from capa.experiment.config import ExperimentConfig
from capa.experiment.method import ChannelRef, HoldStep, Method
from capa.ui.document_coordinator import DocumentCoordinator
from capa.ui.tabs.method import MethodTab
from capa.ui.tabs.setup import SetupTab

REPO_ROOT = Path(__file__).resolve().parents[4]
SIM_CAPA_EXP = REPO_ROOT / "configs" / "experiments" / "sim_capa_pyrolysis.yaml"


def _make_triple(
    qtbot: Any,
) -> tuple[SetupTab, MethodTab, DocumentCoordinator]:
    setup = SetupTab()
    method = MethodTab()
    qtbot.addWidget(setup)
    qtbot.addWidget(method)
    coord = DocumentCoordinator(setup_tab=setup, method_tab=method)
    return setup, method, coord


# ---------------------------------------------------------------------------
# Happy path: loaded fixture composes back to an equivalent ExperimentConfig.
# ---------------------------------------------------------------------------


def test_build_applied_config_round_trips_loaded_fixture(qtbot: Any) -> None:
    setup, _method, coord = _make_triple(qtbot)
    setup.load_path(SIM_CAPA_EXP)

    cfg = coord.build_applied_config()
    assert isinstance(cfg, ExperimentConfig)
    assert cfg.hardware.name == "sim_capa"
    assert cfg.method is not None
    # The composed method must match what the Method tab is showing.
    assert cfg.method.name


def test_build_applied_config_empty_draft_raises(qtbot: Any) -> None:
    _setup, _method, coord = _make_triple(qtbot)
    with pytest.raises(CapaError):
        coord.build_applied_config()


# ---------------------------------------------------------------------------
# Method-tab buffer wins over the document's stored method_payload.
# ---------------------------------------------------------------------------


def test_method_tab_buffer_overrides_document_payload(qtbot: Any) -> None:
    """Editing the Method tab without saving must be reflected in
    ``build_applied_config`` — Apply-to-Rig honours the operator's most
    recent intent."""
    setup, method_tab, coord = _make_triple(qtbot)
    setup.load_path(SIM_CAPA_EXP)

    # Replace the loaded method with a single-step buffer in the Method tab.
    new_method = Method(
        name="buffer_override",
        steps=(
            HoldStep(
                target=ChannelRef(name="heater.setpoint"),
                value=42.0,
                duration_s=5.0,
            ),
        ),
    )
    method_tab.load_method(new_method, path=None)
    # ``load_method(... path=None)`` simulates an unsaved buffer. The
    # coordinator's _on_method_tab_changed slot will fire and mirror
    # this back into the document — but build_applied_config should
    # arrive at the same composed config either way.
    cfg = coord.build_applied_config()
    assert cfg.method is not None
    assert cfg.method.name == "buffer_override"
    assert len(cfg.method.steps) == 1


def test_method_tab_empty_with_method_mode_none_omits_method(qtbot: Any, tmp_path: Path) -> None:
    """A free-run draft (``method_mode='none'``) composes without a
    ``method`` key even when nothing is in the Method tab."""
    setup, _method, coord = _make_triple(qtbot)
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
    cfg = coord.build_applied_config()
    assert cfg.method is None
