""":class:`DeviceSettingsDialog` — rows, selection, the apply flow, skip, and
start mode. Driven against a fake controller so each state is reachable."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QMessageBox, QPushButton

from capa.devices.settings import SettingChange, SettingIssue
from capa.runtime.device_settings import (
    ChangeResult,
    DevicePlan,
    Outcome,
    SettingsPlan,
    SettingsReport,
)
from capa.ui.device_settings_dialog import DeviceSettingsDialog
from capa.ui.statusbar import OperatorIdProvider

pytestmark = pytest.mark.anyio

_GAS = SettingChange(
    field="gas",
    label="Gas",
    current="Air",
    desired="N2",
    kind="set_gas",
    payload={"gas": "N2", "save": False},
)
_RANGE = SettingChange(
    field="temperature_range",
    label="Temperature range",
    current=None,
    desired="0 to 650 °C",
    kind="set_temperature_range",
    payload={"index": 1},
    note="The camera recalibrates for a few seconds.",
)
PLAN = SettingsPlan(
    devices=(
        DevicePlan(name="purge_mfc", changes=(_GAS,)),
        DevicePlan(
            name="ir_cam0",
            changes=(_RANGE,),
            issues=(SettingIssue("emissivity", "Emissivity", "no radiometric parameters"),),
        ),
        DevicePlan(name="balance", error="no read-back within 15 s"),
    )
)


class _FakeController:
    def __init__(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self.loop = loop
        self.apply_calls: list[tuple[set[tuple[str, str]], str]] = []
        self.on_progress: Any = None
        self.future: asyncio.Future[SettingsReport] | None = None
        self.skipped: list[SettingsPlan] = []
        self.refuse = False

    def apply_device_settings(
        self, plan: SettingsPlan, selected: Any, *, operator_id: str, on_progress: Any
    ) -> Any:
        if self.refuse:
            return None
        assert self.loop is not None
        self.apply_calls.append((set(selected), operator_id))
        self.on_progress = on_progress
        self.future = self.loop.create_future()
        return self.future

    def record_device_settings_skipped(self, plan: SettingsPlan) -> None:
        self.skipped.append(plan)


@pytest.fixture(autouse=True)
def _no_nested_event_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dialog must only ever be opened with ``open()``."""

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("nested event loop")

    monkeypatch.setattr(QDialog, "exec", _boom)
    monkeypatch.setattr(QMessageBox, "question", _boom)


def _dialog(
    qtbot: Any, controller: _FakeController, *, mode: str = "load", operator: str = "abr"
) -> DeviceSettingsDialog:
    dialog = DeviceSettingsDialog(
        plan=PLAN,
        controller=controller,  # type: ignore[arg-type]
        operator_provider=OperatorIdProvider(operator),
        mode=mode,  # type: ignore[arg-type]
    )
    qtbot.addWidget(dialog)
    return dialog


def _button(dialog: DeviceSettingsDialog, text: str) -> QPushButton:
    return next(b for b in dialog.findChildren(QPushButton) if b.text() == text)


def _texts(dialog: DeviceSettingsDialog, row: int) -> list[str]:
    table = dialog._table
    return [table.item(row, col).text() for col in range(table.columnCount())]


def test_lists_changes_issues_and_errors(qtbot: Any) -> None:
    dialog = _dialog(qtbot, _FakeController())
    rows = [_texts(dialog, row)[1:] for row in range(dialog._table.rowCount())]
    assert rows == [
        ["purge_mfc", "Gas", "Air", "N2", ""],
        ["ir_cam0", "Temperature range", "unknown", "0 to 650 °C", _RANGE.note],
        ["ir_cam0", "Emissivity", "", "", "no radiometric parameters"],
        ["balance", "—", "", "", "no read-back within 15 s"],
    ]
    assert dialog.selected() == {("purge_mfc", "gas"), ("ir_cam0", "temperature_range")}
    # Issue and error rows can't be ticked.
    for row in (2, 3):
        assert not dialog._table.item(row, 0).flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert "2 device settings differ" in dialog._header.text()


def test_unticking_everything_disables_apply(qtbot: Any) -> None:
    dialog = _dialog(qtbot, _FakeController())
    for row in (0, 1):
        dialog._table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
    assert not _button(dialog, "Apply selected").isEnabled()


async def test_apply_sends_the_ticked_rows_and_shows_outcomes(qtbot: Any) -> None:
    controller = _FakeController(asyncio.get_running_loop())
    dialog = _dialog(qtbot, controller)
    dialog._table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
    _button(dialog, "Apply selected").click()

    assert controller.apply_calls == [({("purge_mfc", "gas")}, "abr")]
    assert dialog.applying
    assert not any(b.isEnabled() for b in dialog._buttons.buttons())
    dialog.reject()  # Esc does nothing mid-apply
    assert controller.skipped == []

    controller.on_progress(ChangeResult("purge_mfc", _GAS, Outcome.VERIFIED, "ok"))
    assert _texts(dialog, 0)[5] == "✓ applied"
    assert controller.future is not None
    controller.future.set_result(SettingsReport(results=(), plan_after=SettingsPlan()))
    await asyncio.sleep(0)

    assert not dialog.applying
    close = _button(dialog, "Close")
    assert close.isEnabled()
    close.click()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert controller.skipped == []


async def test_failed_change_is_shown_with_its_reason(qtbot: Any) -> None:
    controller = _FakeController(asyncio.get_running_loop())
    dialog = _dialog(qtbot, controller)
    _button(dialog, "Apply selected").click()
    controller.on_progress(ChangeResult("ir_cam0", _RANGE, Outcome.REFUSED, "recording"))
    assert _texts(dialog, 1)[5] == "✗ refused: recording"


def test_skip_is_recorded(qtbot: Any) -> None:
    controller = _FakeController()
    dialog = _dialog(qtbot, controller)
    _button(dialog, "Skip").click()
    assert controller.skipped == [PLAN]
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_no_operator_blocks_apply(qtbot: Any) -> None:
    controller = _FakeController()
    dialog = _dialog(qtbot, controller, operator="")
    _button(dialog, "Apply selected").click()
    assert controller.apply_calls == []
    assert "operator" in dialog._status.text()


def test_controller_refusal_is_shown(qtbot: Any) -> None:
    controller = _FakeController()
    controller.refuse = True
    dialog = _dialog(qtbot, controller)
    _button(dialog, "Apply selected").click()
    assert "Can't apply right now" in dialog._status.text()
    assert not dialog.applying


def test_start_mode_start_anyway(qtbot: Any) -> None:
    controller = _FakeController()
    dialog = _dialog(qtbot, controller, mode="start")
    started: list[bool] = []
    dialog.startRequested.connect(lambda: started.append(True))
    assert "Apply before the run starts?" in dialog._header.text()
    _button(dialog, "Start anyway").click()
    assert started == [True]
    assert controller.skipped == []


def test_start_mode_cancel_does_not_start(qtbot: Any) -> None:
    dialog = _dialog(qtbot, _FakeController(), mode="start")
    started: list[bool] = []
    dialog.startRequested.connect(lambda: started.append(True))
    _button(dialog, "Cancel").click()
    assert started == []


async def test_start_mode_offers_start_after_apply(qtbot: Any) -> None:
    controller = _FakeController(asyncio.get_running_loop())
    dialog = _dialog(qtbot, controller, mode="start")
    started: list[bool] = []
    dialog.startRequested.connect(lambda: started.append(True))
    _button(dialog, "Apply selected").click()
    assert controller.future is not None
    controller.future.set_result(SettingsReport(results=(), plan_after=SettingsPlan()))
    await asyncio.sleep(0)
    _button(dialog, "Start run").click()
    assert started == [True]
