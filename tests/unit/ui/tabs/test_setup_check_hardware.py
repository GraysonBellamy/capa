"""Verify connection button tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from PySide6.QtCore import QObject, Signal

from capa.config.problems import ConfigProblem
from capa.ui.state import RunUiState
from capa.ui.tabs.setup import SetupTab
from capa.ui.tabs.setup_connection_strip import ConnectionState

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
    """A failed Layer-5 handshake must not disable Verify connection —
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


def test_check_button_disabled_when_hardware_ready(qtbot: Any) -> None:
    """Once a config has been applied (pool open), Verify connection
    would conflict with the pool's open ports — fresh handshake tries
    to re-open the same serial port and reports every connected device
    as a failure. The button stays disabled for the whole time the
    pool is open, with a tooltip pointing the operator at apply /
    disconnect.
    """
    controller = _ControllerStub()
    tab = SetupTab(controller=controller)  # type: ignore[arg-type]
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    assert tab._action_check.isEnabled()

    # Simulate "apply succeeded": pool is open.
    controller.hardware_ready = True
    controller.hardware_ready_changed.emit(True)
    assert not tab._action_check.isEnabled()
    assert "disconnect" in tab._action_check.toolTip()


def test_check_button_stays_disabled_after_draft_edit_post_apply(qtbot: Any) -> None:
    """Editing the draft after apply must NOT re-enable Verify connection.
    The pool still holds the original ports, so any handshake against an
    unchanged port would still collide. The operator's path to re-verify
    is to apply the new config (or disconnect first).
    """
    controller = _ControllerStub()
    tab = SetupTab(controller=controller)  # type: ignore[arg-type]
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    controller.hardware_ready = True
    controller.hardware_ready_changed.emit(True)
    assert not tab._action_check.isEnabled()

    # Operator edits the draft — unapplied flips True, but the button
    # stays disabled because the pool is still open.
    tab._draft.unapplied = True
    tab._refresh_apply_enabled()
    assert not tab._action_check.isEnabled()


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
# Connection strip state machine
# ---------------------------------------------------------------------------


def test_check_connection_strip_state(qtbot: Any) -> None:
    controller = _ControllerStub()
    tab = SetupTab(controller=controller)  # type: ignore[arg-type]
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    # Loaded clean draft + no hardware ready stub → strip sits in UNAPPLIED
    # (the controller stub doesn't expose a live pool).
    assert tab._connection_strip.state in (ConnectionState.UNAPPLIED, ConnectionState.CONNECTED)
    tab._begin_check()
    assert getattr(tab._connection_strip, "state") is ConnectionState.CHECKING  # noqa: B009
    tab._finish_check([])
    # Returns to whatever the underlying inputs say (no in-flight verify).
    assert tab._connection_strip.state in (ConnectionState.UNAPPLIED, ConnectionState.CONNECTED)


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
