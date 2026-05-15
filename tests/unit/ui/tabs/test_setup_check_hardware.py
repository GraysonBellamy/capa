"""Slice F4 — Check Hardware button (plan §4.2, §5.9 layer 5)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from PySide6.QtCore import QObject, Signal

from capa.config.problems import ConfigProblem
from capa.ui.state import RunUiState
from capa.ui.tabs.setup import SetupTab, _BannerState

REPO_ROOT = Path(__file__).resolve().parents[4]
SIM_CAPA_EXP = REPO_ROOT / "configs" / "experiments" / "sim_capa_pyrolysis.yaml"


class _ControllerStub(QObject):
    state_changed = Signal(object)
    config_load_finished = Signal(object)
    hardware_ready_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.state = RunUiState.IDLE
        self.is_active = False
        self.hardware_ready = False


# ---------------------------------------------------------------------------
# Button gating
# ---------------------------------------------------------------------------


def test_check_button_disabled_when_draft_has_errors(qtbot: Any) -> None:
    controller = _ControllerStub()
    tab = SetupTab(controller=controller)  # type: ignore[arg-type]
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    assert tab._action_check.isEnabled()
    # Break the schema; Check button greys out.
    tab._draft.document.hardware_payload.pop("name", None)
    tab._draft.validate()
    tab._refresh_apply_enabled()
    assert not tab._action_check.isEnabled()


def test_check_button_enabled_with_valid_draft(qtbot: Any) -> None:
    controller = _ControllerStub()
    tab = SetupTab(controller=controller)  # type: ignore[arg-type]
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    assert tab._action_check.isEnabled()


def test_check_button_disabled_during_active_run(qtbot: Any) -> None:
    controller = _ControllerStub()
    tab = SetupTab(controller=controller)  # type: ignore[arg-type]
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    controller.is_active = True
    controller.state_changed.emit(RunUiState.RUNNING)
    assert not tab._action_check.isEnabled()


def test_check_button_stays_enabled_after_live_handshake_error(qtbot: Any) -> None:
    """A failed Layer-5 handshake must not disable Check Hardware —
    retrying after fixing a cable / power-cycling a controller is the
    whole point of the button.
    """
    controller = _ControllerStub()
    tab = SetupTab(controller=controller)  # type: ignore[arg-type]
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    assert tab._action_check.isEnabled()
    live_fail = [
        ConfigProblem(
            severity="error",
            code="live.handshake_failed",
            message="heater: serial port not responding",
            section="devices",
            path=("devices", "heater"),
        )
    ]
    tab._begin_check()
    tab._finish_check(live_fail)
    assert tab._action_check.isEnabled()


def test_check_button_disabled_when_hardware_ready_and_synced(qtbot: Any) -> None:
    """Once a config has been applied (pool open) and the draft matches
    the applied state, Check Hardware would conflict with the pool's
    open ports — fresh handshake tries to re-open the same serial port
    and reports every connected device as a failure. The button is
    disabled instead, with a tooltip explaining why.
    """
    controller = _ControllerStub()
    tab = SetupTab(controller=controller)  # type: ignore[arg-type]
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    assert tab._action_check.isEnabled()

    # Simulate "apply succeeded": pool is open, draft matches applied.
    controller.hardware_ready = True
    controller.hardware_ready_changed.emit(True)
    assert tab._draft.unapplied is False
    assert not tab._action_check.isEnabled()
    assert "connected and verified" in tab._action_check.toolTip()


def test_check_button_re_enables_after_draft_edit_post_apply(qtbot: Any) -> None:
    """Editing the draft after apply re-enables Check Hardware so the
    operator can verify the new config before applying again. Fresh
    handshake against an edited (typically different) port doesn't
    conflict with the open pool.
    """
    controller = _ControllerStub()
    tab = SetupTab(controller=controller)  # type: ignore[arg-type]
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    controller.hardware_ready = True
    controller.hardware_ready_changed.emit(True)
    assert not tab._action_check.isEnabled()

    # Operator edits the draft — unapplied flips True.
    tab._draft.unapplied = True
    tab._refresh_apply_enabled()
    assert tab._action_check.isEnabled()


def test_check_button_disabled_while_check_in_flight(qtbot: Any) -> None:
    controller = _ControllerStub()
    tab = SetupTab(controller=controller)  # type: ignore[arg-type]
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    assert tab._action_check.isEnabled()
    tab._begin_check()
    assert not tab._action_check.isEnabled()
    # And the apply button is also locked out during the bus check.
    assert not tab._action_apply.isEnabled()
    tab._finish_check([])
    assert tab._action_check.isEnabled()


# ---------------------------------------------------------------------------
# Banner state machine
# ---------------------------------------------------------------------------


def test_check_banner_state(qtbot: Any) -> None:
    controller = _ControllerStub()
    tab = SetupTab(controller=controller)  # type: ignore[arg-type]
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    assert tab._banner_state is _BannerState.HIDDEN
    tab._begin_check()
    assert tab._banner_state is _BannerState.CHECKING
    tab._finish_check([])
    # No live problems → banner returns to HIDDEN (loaded draft is
    # applied/clean).
    assert tab._banner_state is _BannerState.HIDDEN


def test_check_finish_merges_live_problems(qtbot: Any) -> None:
    controller = _ControllerStub()
    tab = SetupTab(controller=controller)  # type: ignore[arg-type]
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    fake = [
        ConfigProblem(
            severity="info",
            code="live.handshake_ok",
            message="heater: PM3R1CA",
            section="devices",
            path=("devices", "heater"),
        )
    ]
    tab._begin_check()
    tab._finish_check(fake)
    codes = [p.code for p in tab._draft.problems]
    assert "live.handshake_ok" in codes


# ---------------------------------------------------------------------------
# Refusal paths
# ---------------------------------------------------------------------------


def test_check_refused_during_active_run(qtbot: Any) -> None:
    controller = _ControllerStub()
    tab = SetupTab(controller=controller)  # type: ignore[arg-type]
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    controller.is_active = True
    controller.state_changed.emit(RunUiState.RUNNING)
    with patch("capa.ui.tabs.setup.QMessageBox.information") as info:
        tab._on_check_hardware()
    assert info.call_count == 1
    assert not tab._check_in_flight


def test_check_refused_with_errors_in_draft(qtbot: Any) -> None:
    controller = _ControllerStub()
    tab = SetupTab(controller=controller)  # type: ignore[arg-type]
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    # Inject an error: drop the required hardware ``name``.
    tab._draft.document.hardware_payload.pop("name", None)
    tab._draft.validate()
    with patch("capa.ui.tabs.setup.QMessageBox.warning") as warn:
        tab._on_check_hardware()
    assert warn.call_count == 1
    assert not tab._check_in_flight


def test_check_without_running_loop_surfaces_info(qtbot: Any) -> None:
    """Outside a qasync loop, the slot opens a dialog rather than
    silently doing nothing — keeps the test path observable."""
    controller = _ControllerStub()
    tab = SetupTab(controller=controller)  # type: ignore[arg-type]
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    with patch("capa.ui.tabs.setup.QMessageBox.information") as info:
        tab._on_check_hardware()
    assert info.call_count == 1
    assert not tab._check_in_flight
