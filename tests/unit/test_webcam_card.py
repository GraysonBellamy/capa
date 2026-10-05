"""Tests for the webcam manual-control card's read-back: each UVC control's
range, value and auto mode come from the camera, and rows for controls the
camera lacks are greyed out.

There is no webcam simulator, so the card talks to a fake
:class:`ManualClient` that answers read-backs and commands the way a camera
behind :class:`WebcamAdapter` would.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QPushButton

from capa.devices.adapter import CommandResult, DeviceCommand
from capa.devices.camera.base import CameraSpec, WebcamControlState, WebcamStateSnapshot
from capa.ui.manual.cards.webcam import WebcamCard
from capa.ui.state import RunController, RunUiState
from capa.ui.statusbar import OperatorIdProvider
from tests.unit.test_manual_control_cards import _run_async

Applied = tuple[str, dict[str, Any]]


class _FakeCamera:
    """A camera's controls as the fake client serves them."""

    def __init__(self, controls: dict[str, WebcamControlState]) -> None:
        self.controls = controls
        self.unavailable: str | None = None
        self.commands: list[tuple[str, dict[str, Any]]] = []

    def snapshot(self) -> WebcamStateSnapshot:
        return WebcamStateSnapshot(controls=dict(self.controls), unavailable=self.unavailable)

    def command(self, cmd: DeviceCommand) -> CommandResult:
        self.commands.append((cmd.kind, dict(cmd.payload)))
        if cmd.kind.startswith("set_auto_"):
            name = cmd.kind.removeprefix("set_auto_")
            update: dict[str, Any] = {"auto": bool(cmd.payload["enable"])}
        else:
            name = cmd.kind.removeprefix("set_")
            # A value puts the control in manual mode, as duvc-ctl does.
            update = {"value": int(cmd.payload["value"])}
            if self.controls[name].auto is not None:
                update["auto"] = False
        self.controls[name] = self.controls[name].model_copy(update=update)
        return CommandResult(accepted=True, detail=cmd.kind, t_mono_ns=0, t_utc=datetime.now(UTC))


class _FakeClient:
    def __init__(self, camera: _FakeCamera) -> None:
        self._camera = camera

    def camera(self, name: str) -> object:
        return self._camera

    async def device_readback(self, name: str) -> WebcamStateSnapshot:
        return self._camera.snapshot()

    async def dispatch(self, name: str, cmd: DeviceCommand) -> CommandResult:
        return self._camera.command(cmd)


def _c930e() -> _FakeCamera:
    return _FakeCamera(
        {
            "zoom": WebcamControlState(value=100, minimum=100, maximum=500, step=1),
            "pan": WebcamControlState(value=0, minimum=-36000, maximum=36000, step=3600),
            "tilt": WebcamControlState(value=0, minimum=-36000, maximum=36000, step=3600),
            "exposure": WebcamControlState(value=-5, auto=True, minimum=-11, maximum=-2, step=1),
            "focus": WebcamControlState(value=0, auto=False, minimum=0, maximum=250, step=5),
            "brightness": WebcamControlState(value=128, minimum=0, maximum=255, step=1),
        }
    )


@pytest.fixture
def controller(tmp_path: Path) -> RunController:
    return RunController(runs_root=tmp_path)


@pytest.fixture
def op_provider() -> OperatorIdProvider:
    return OperatorIdProvider(initial="opA")


@pytest.fixture
def camera() -> _FakeCamera:
    return _c930e()


@pytest.fixture
def card(
    qtbot: Any, controller: RunController, op_provider: OperatorIdProvider, camera: _FakeCamera
) -> WebcamCard:
    controller._manual_client = _FakeClient(camera)  # type: ignore[assignment]
    controller._hardware_ready = True
    spec = CameraSpec(name="visible_cam0", adapter="capa.devices.camera.webcam", kind="visible")
    widget = WebcamCard(spec=spec, controller=controller, operator_provider=op_provider)
    qtbot.addWidget(widget)
    return widget


def _apply_button(card: WebcamCard, field: str) -> QPushButton:
    button = card.findChild(QPushButton, f"apply_{field}")
    assert button is not None
    return button


def _capture_applies(card: WebcamCard, monkeypatch: pytest.MonkeyPatch) -> list[Applied]:
    applied: list[Applied] = []
    monkeypatch.setattr(
        card, "_apply_field", lambda field, **dispatch: applied.append((field, dispatch))
    )
    return applied


def _send(card: WebcamCard, applied: Applied) -> None:
    field, dispatch = applied
    _run_async(card._apply_field_and_read_back(field, dispatch))


def test_a_read_back_shows_each_controls_range_step_and_value(card: WebcamCard) -> None:
    _run_async(card.refresh_readback())
    zoom = card._value_spins["zoom"]
    assert (zoom.minimum(), zoom.maximum(), zoom.value()) == (100, 500, 100)
    tilt = card._value_spins["tilt"]
    assert (tilt.minimum(), tilt.maximum(), tilt.singleStep()) == (-36000, 36000, 3600)
    assert card._value_spins["exposure"].value() == -5
    assert card._unapplied_edits == set()


def test_the_auto_checkboxes_take_the_cameras_modes(card: WebcamCard) -> None:
    _run_async(card.refresh_readback())
    assert card._auto_checks["auto_exposure"].isChecked()
    assert not card._auto_checks["auto_focus"].isChecked()
    # The camera has no white balance control: left as built.
    assert card._auto_checks["auto_white_balance"].isChecked()


def test_rows_for_controls_the_camera_lacks_are_greyed_out(
    card: WebcamCard, controller: RunController
) -> None:
    _run_async(card.refresh_readback())
    assert not card._value_spins["digital_zoom"].isEnabled()
    assert not _apply_button(card, "white_balance").isEnabled()
    assert not _apply_button(card, "auto_white_balance").isEnabled()
    assert card._value_spins["zoom"].isEnabled()
    # A state change re-enables the card's rows, but not those.
    card._on_engine_state(RunUiState.IDLE)
    assert not card._value_spins["digital_zoom"].isEnabled()
    assert _apply_button(card, "zoom").isEnabled()
    # Nor is the resolution row affected.
    assert card._resolution_combo is not None and card._resolution_combo.isEnabled()


def test_unavailable_controls_grey_every_row_and_say_why(
    card: WebcamCard, camera: _FakeCamera
) -> None:
    camera.controls = {}
    camera.unavailable = "camera controls are only available on Windows"
    _run_async(card.refresh_readback())
    assert not any(spin.isEnabled() for spin in card._value_spins.values())
    assert not any(check.isEnabled() for check in card._auto_checks.values())
    assert "Controls: camera controls are only available on Windows" in (
        card._subtitle_label.text()
    )


def test_apply_sends_the_value_and_reads_back(
    card: WebcamCard, camera: _FakeCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_async(card.refresh_readback())
    applied = _capture_applies(card, monkeypatch)
    card._value_spins["exposure"].setValue(-7)
    _apply_button(card, "exposure").click()
    assert applied == [("exposure", {"kind": "set_exposure", "payload": {"value": -7}})]
    _send(card, applied[0])
    assert camera.commands == [("set_exposure", {"value": -7})]
    # The value took the camera out of auto exposure; the card shows it.
    assert not card._auto_checks["auto_exposure"].isChecked()
    assert card._unapplied_edits == set()


def test_apply_sends_the_auto_toggle(
    card: WebcamCard, camera: _FakeCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_async(card.refresh_readback())
    applied = _capture_applies(card, monkeypatch)
    card._auto_checks["auto_focus"].setChecked(True)
    _apply_button(card, "auto_focus").click()
    assert applied == [("auto_focus", {"kind": "set_auto_focus", "payload": {"enable": True}})]
    _send(card, applied[0])
    assert camera.controls["focus"].auto is True
    assert card._auto_checks["auto_focus"].isChecked()


def test_a_read_back_keeps_an_edit_until_it_is_applied(
    card: WebcamCard, camera: _FakeCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_async(card.refresh_readback())
    card._value_spins["tilt"].setValue(3600)
    applied = _capture_applies(card, monkeypatch)
    card._value_spins["zoom"].setValue(265)
    _apply_button(card, "zoom").click()
    _send(card, applied[0])
    assert card._value_spins["zoom"].value() == 265
    assert card._value_spins["tilt"].value() == 3600  # not applied yet
    assert card._unapplied_edits == {"tilt"}


def test_a_camera_value_outside_its_range_is_shown_as_is(card: WebcamCard) -> None:
    card.apply_snapshot(
        WebcamStateSnapshot(
            controls={"gain": WebcamControlState(value=300, minimum=0, maximum=255, step=1)}
        )
    )
    assert card._value_spins["gain"].value() == 300


def test_nothing_is_read_back_during_a_run(card: WebcamCard, controller: RunController) -> None:
    controller._ui_state = RunUiState.RUNNING
    _run_async(card.refresh_readback())
    assert card._value_spins["zoom"].value() == 0
    controller._ui_state = RunUiState.IDLE
