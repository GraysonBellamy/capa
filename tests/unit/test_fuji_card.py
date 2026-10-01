"""Tests for the gas-analyzer manual-control card.

Drives :class:`FujiCard` against the simulated analyzer through a real
:class:`WorkerPool`, so each dispatch crosses to the worker's loop and each
read-back is the adapter's own :class:`FujiStateSnapshot`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QMessageBox, QPushButton

from capa.devices.fuji import FujiReadback, FujiStateSnapshot
from capa.devices.fuji_calibration import CalibrationStatus
from capa.experiment.config import DeviceConfig
from capa.ui.docks.manual_control import ManualControlDock
from capa.ui.manual.cards.fuji import (
    CALIBRATION_BEGIN_NOTE,
    FujiCard,
    _calibration_text,
    is_fuji_device,
)
from capa.ui.state import RunController, RunUiState
from capa.ui.statusbar import OperatorIdProvider
from capa.ui.theme import COLOR_FAIL, COLOR_IDLE, COLOR_OK, COLOR_WARN
from tests.unit.test_manual_control_cards import (
    _adapter_for,
    _close_pool_sync,
    _make_config,
    _open_pool_sync,
    _run_async,
)

SIM = "capa.devices.sim.fuji_sim"


@pytest.fixture
def controller(tmp_path: Path) -> RunController:
    return RunController(runs_root=tmp_path)


@pytest.fixture
def op_provider() -> OperatorIdProvider:
    return OperatorIdProvider(initial="opA")


@pytest.fixture(autouse=True)
def _pool_closed_after(controller: RunController) -> Iterator[None]:
    """Close the worker pool even when a test fails: an open pool's worker
    thread would keep the test process alive."""
    yield
    _close_pool_sync(controller)


def _device(**params: Any) -> DeviceConfig:
    return DeviceConfig(name="analyzer", adapter=SIM, params={"settle_s": 0.0, **params})


def _card(
    qtbot: Any,
    controller: RunController,
    op_provider: OperatorIdProvider,
    tmp_path: Path,
    **params: Any,
) -> FujiCard:
    cfg = _make_config((_device(**params),))
    _open_pool_sync(controller, cfg)
    card = FujiCard(
        spec=cfg.hardware.devices[0],
        controller=controller,
        operator_provider=op_provider,
        calibration_dir=tmp_path / "calibrations",
    )
    qtbot.addWidget(card)
    return card


def _say(monkeypatch: pytest.MonkeyPatch, answer: QMessageBox.StandardButton) -> list[str]:
    """Answer every confirmation dialog with ``answer``; the texts asked."""
    asked: list[str] = []

    def question(_parent: Any, _title: str, text: str, *_args: Any, **_kwargs: Any) -> Any:
        asked.append(text)
        return answer

    monkeypatch.setattr(QMessageBox, "question", question)
    return asked


def test_fingerprint() -> None:
    assert is_fuji_device(DeviceConfig(name="a", adapter="capa.devices.fuji", params={}))
    assert is_fuji_device(_device())
    assert not is_fuji_device(DeviceConfig(name="b", adapter="capa.devices.watlow", params={}))


def test_the_dock_gives_the_analyzer_its_card(
    qtbot: Any, controller: RunController, op_provider: OperatorIdProvider
) -> None:
    cfg = _make_config((_device(),))
    _open_pool_sync(controller, cfg)
    dock = ManualControlDock(controller=controller, operator_provider=op_provider)
    qtbot.addWidget(dock)
    dock.load_config(cfg)
    assert isinstance(dock._cards_by_name["analyzer"], FujiCard)
    _close_pool_sync(controller)


def test_the_card_offers_the_mapped_channels(
    qtbot: Any, controller: RunController, op_provider: OperatorIdProvider, tmp_path: Path
) -> None:
    card = _card(qtbot, controller, op_provider, tmp_path, channel_map={"CH1": "co2", "CH4": "o2"})
    choices = [card._cal_channel.itemText(i) for i in range(card._cal_channel.count())]
    assert choices == ["CH1", "CH4"]
    texts = [b.text() for b in card.findChildren(QPushButton)]
    assert "Begin…" in texts
    assert "Return to measurement" in texts
    assert "Hold to calibrate" in texts
    _close_pool_sync(controller)


def test_a_setting_is_dispatched_with_the_operator(
    qtbot: Any, controller: RunController, op_provider: OperatorIdProvider, tmp_path: Path
) -> None:
    card = _card(qtbot, controller, op_provider, tmp_path)
    result = _run_async(
        card.dispatch(kind="set_response_time", payload={"target": "o2", "seconds": 5})
    )
    assert result is not None
    assert result.accepted
    _run_async(card.refresh_readback())
    assert card._settings["response_time_o2_s"] == 5
    _close_pool_sync(controller)


def test_a_calibration_gas_is_confirmed_first(
    qtbot: Any,
    controller: RunController,
    op_provider: OperatorIdProvider,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = _card(qtbot, controller, op_provider, tmp_path)
    payload = {"channel": "CH3", "range": 1, "kind": "span", "value": 20.0, "unit": "vol%"}
    kwargs: dict[str, Any] = {
        "kind": "set_calibration_gas",
        "payload": payload,
        "destructive": True,
        "destructive_summary": "Set the span gas",
    }
    asked = _say(monkeypatch, QMessageBox.StandardButton.No)
    assert _run_async(card.dispatch(**kwargs)) is None
    assert len(asked) == 1
    sim = _adapter_for(controller, "analyzer")
    assert sim._settings["ch3_range1_span_gas"] != 20.0

    _ = _say(monkeypatch, QMessageBox.StandardButton.Yes)
    result = _run_async(card.dispatch(**kwargs))
    assert result is not None
    assert result.accepted
    assert sim._settings["ch3_range1_span_gas"] == 20.0
    _close_pool_sync(controller)


def test_a_zero_from_the_card_and_its_saved_record(
    qtbot: Any,
    controller: RunController,
    op_provider: OperatorIdProvider,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = _card(qtbot, controller, op_provider, tmp_path)
    card.show()
    assert card._commit_button is not None
    assert card._cancel_button is not None
    assert not card._commit_button.isEnabled()

    card._cal_channel.setCurrentText("CH3")
    card._cal_kind.setCurrentText("zero")
    card._cal_value.setValue(0.0)
    card._cal_label.setText("N2, cylinder 1234")
    asked = _say(monkeypatch, QMessageBox.StandardButton.Yes)
    begun = _run_async(
        card.dispatch(
            kind="calibration_begin",
            payload={"channel": "CH3", "kind": "zero", "gas_value": 0.0, "gas_label": "N2"},
            destructive=True,
            destructive_summary="Begin a zero of CH3",
        )
    )
    assert begun is not None
    assert begun.accepted
    assert len(asked) == 1

    # The read-back shows the wait step, steady: only now can the key be held.
    _run_async(card.refresh_readback())
    assert card._calibration.state == "waiting"
    assert card._commit_button.isEnabled()
    assert card._cancel_button.isEnabled()
    assert card._calibration_label is not None
    assert "STEADY" in card._calibration_label.text()
    assert "[calibrating]" in card._subtitle_label.text()

    committed = _run_async(card.dispatch(kind="calibration_commit"))
    assert committed is not None
    assert committed.accepted
    _run_async(card.refresh_readback())
    assert card._calibration.outcome == "completed"
    assert not card._commit_button.isEnabled()
    assert not card._cancel_button.isEnabled()

    saved = card.last_saved_record
    assert saved is not None
    assert saved.parent == tmp_path / "calibrations"
    assert saved.name.startswith("analyzer_CH3_zero_")
    record = json.loads(saved.read_text(encoding="utf-8"))
    assert record["format"] == "fujilib-calibration/1"
    assert (record["kind"], record["outcome"]) == ("zero", "completed")
    # A later read-back of the same ended run does not write a second file.
    _run_async(card.refresh_readback())
    assert len(list(saved.parent.iterdir())) == 1
    _close_pool_sync(controller)


def test_the_begin_button_asks_and_names_the_gas(
    qtbot: Any,
    controller: RunController,
    op_provider: OperatorIdProvider,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = _card(qtbot, controller, op_provider, tmp_path)
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(card, "schedule_calibration", lambda **kwargs: seen.append(kwargs))
    card._cal_channel.setCurrentText("CH3")
    card._cal_kind.setCurrentText("span")
    card._cal_value.setValue(20.95)
    card._cal_label.setText("air")
    card._on_begin()
    assert seen[0]["kind"] == "calibration_begin"
    assert seen[0]["destructive"] is True
    assert seen[0]["payload"] == {
        "channel": "CH3",
        "kind": "span",
        "gas_value": 20.95,
        "gas_unit": "vol%",
        "gas_label": "air",
    }
    assert "20.95 vol% (air)" in seen[0]["destructive_summary"]
    assert "Nothing is calibrated until you hold Calibrate" in seen[0]["destructive_note"]
    # A zero gas of 0 goes without a unit.
    card._cal_value.setValue(0.0)
    card._on_begin()
    assert seen[1]["payload"]["gas_unit"] is None
    _close_pool_sync(controller)


def test_the_confirmation_says_what_the_operation_does(
    qtbot: Any,
    controller: RunController,
    op_provider: OperatorIdProvider,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = _card(qtbot, controller, op_provider, tmp_path)
    asked = _say(monkeypatch, QMessageBox.StandardButton.No)
    declined = _run_async(
        card.dispatch(
            kind="calibration_begin",
            payload={"channel": "CH3", "kind": "zero", "gas_value": 0.0},
            destructive=True,
            destructive_summary="Begin a zero of CH3",
            destructive_note=CALIBRATION_BEGIN_NOTE,
        )
    )
    assert declined is None
    assert "Begin a zero of CH3" in asked[0]
    assert "calibration keys" in asked[0]
    # Beginning a calibration writes nothing that persists; the dialog does not say it does.
    assert "EEPROM" not in asked[0]
    # Without a note of its own, a destructive write keeps the usual warning.
    _ = _run_async(card.dispatch(kind="set_output_hold", destructive=True))
    assert "EEPROM" in asked[1]
    _close_pool_sync(controller)


def test_the_plan_button_reads_what_the_calibration_would_reach(
    qtbot: Any, controller: RunController, op_provider: OperatorIdProvider, tmp_path: Path
) -> None:
    card = _card(qtbot, controller, op_provider, tmp_path)
    assert "Plan" in [b.text() for b in card.findChildren(QPushButton)]
    card._cal_channel.setCurrentText("CH3")
    card._cal_kind.setCurrentText("span")
    result = _run_async(
        card.dispatch(kind="calibration_plan", payload={"channel": "CH3", "kind": "span"})
    )
    assert result is not None
    assert result.accepted
    assert result.detail == "calibration_plan: span of CH3: CH3 range 1 against 20.95 vol%"
    assert "span of CH3" in card._status_label.text()
    # Reading the plan begins nothing.
    assert _adapter_for(controller, "analyzer").calibration.state == "idle"
    _close_pool_sync(controller)


def test_a_calibration_command_reads_back_at_once(
    qtbot: Any,
    controller: RunController,
    op_provider: OperatorIdProvider,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = _card(qtbot, controller, op_provider, tmp_path)
    _ = _say(monkeypatch, QMessageBox.StandardButton.Yes)
    _run_async(
        card._dispatch_and_read_back(
            {
                "kind": "calibration_begin",
                "payload": {"channel": "CH3", "kind": "zero", "gas_value": 0.0},
                "destructive": True,
            }
        )
    )
    # No timer tick was needed for the buttons to follow the run.
    assert card._calibration.state == "waiting"
    assert card._cancel_button is not None
    assert card._cancel_button.isEnabled()
    assert card._calibration_label is not None
    assert "zero of CH3: CH3 range 1 against 0 vol%" in card._calibration_label.text()
    _run_async(card._dispatch_and_read_back({"kind": "calibration_cancel"}))
    assert card._calibration.outcome == "cancelled"
    assert card.last_saved_record is not None
    _close_pool_sync(controller)


def test_a_card_built_again_does_not_save_the_record_twice(
    qtbot: Any,
    controller: RunController,
    op_provider: OperatorIdProvider,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = _card(qtbot, controller, op_provider, tmp_path)
    _ = _say(monkeypatch, QMessageBox.StandardButton.Yes)
    begin = {
        "kind": "calibration_begin",
        "payload": {"channel": "CH3", "kind": "zero", "gas_value": 0.0},
        "destructive": True,
    }
    _run_async(card._dispatch_and_read_back(begin))
    _run_async(card._dispatch_and_read_back({"kind": "calibration_cancel"}))
    saved = card.last_saved_record
    assert saved is not None
    # The dock builds its cards again when a config is loaded; the adapter
    # still holds the run that ended.
    again = FujiCard(
        spec=card._spec,
        controller=controller,
        operator_provider=op_provider,
        calibration_dir=tmp_path / "calibrations",
    )
    qtbot.addWidget(again)
    _run_async(again.refresh_readback())
    assert again.last_saved_record == saved
    assert len(list(saved.parent.iterdir())) == 1
    _close_pool_sync(controller)


def test_a_calibration_that_never_began_leaves_no_record(
    qtbot: Any, controller: RunController, op_provider: OperatorIdProvider, tmp_path: Path
) -> None:
    card = _card(qtbot, controller, op_provider, tmp_path)
    # Refused before the first key: nothing happened at the analyzer.
    refused = CalibrationStatus(
        state="ended",
        channel="CH3",
        kind="zero",
        error="the gas named is not the calibration-gas setting",
        record={"format": "fujilib-calibration/1", "outcome": None, "keys": []},
    )
    card.apply_snapshot(FujiStateSnapshot(calibration=refused))
    assert card.last_saved_record is None
    assert not (tmp_path / "calibrations").exists()
    # A run that sent a key and then stopped is kept, whatever its outcome.
    stopped = CalibrationStatus(
        state="ended",
        channel="CH3",
        kind="zero",
        error="ENT was not taken",
        record={
            "format": "fujilib-calibration/1",
            "outcome": None,
            "keys": [{"key": "zero"}],
            "run_ended_at": "2026-10-01T15:33:39+00:00",
        },
    )
    card.apply_snapshot(FujiStateSnapshot(calibration=stopped))
    assert card.last_saved_record is not None
    assert card.last_saved_record.name == "analyzer_CH3_zero_20261001T153339Z.json"
    _close_pool_sync(controller)


def test_nothing_is_read_back_during_a_run(
    qtbot: Any, controller: RunController, op_provider: OperatorIdProvider, tmp_path: Path
) -> None:
    card = _card(qtbot, controller, op_provider, tmp_path)
    before = card._subtitle_label.text()
    controller._ui_state = RunUiState.RUNNING
    _run_async(card.refresh_readback())
    card._on_timer()
    assert card._subtitle_label.text() == before
    controller._ui_state = RunUiState.IDLE
    _close_pool_sync(controller)


def test_a_snapshot_without_readings_says_so(
    qtbot: Any, controller: RunController, op_provider: OperatorIdProvider, tmp_path: Path
) -> None:
    card = _card(qtbot, controller, op_provider, tmp_path)
    card.apply_snapshot(FujiStateSnapshot())
    assert "did not answer" in card._subtitle_label.text()
    card.apply_snapshot(
        FujiStateSnapshot(readings=(FujiReadback("CH3", "o2", None, "vol%", "unknown"),))
    )
    assert card._subtitle_label.text() == "O2 — vol% [unknown]"
    _close_pool_sync(controller)


def test_a_record_that_cannot_be_saved_is_reported(
    qtbot: Any, controller: RunController, op_provider: OperatorIdProvider, tmp_path: Path
) -> None:
    card = _card(qtbot, controller, op_provider, tmp_path)
    blocker = tmp_path / "calibrations"
    blocker.write_text("a file where the directory should be", encoding="utf-8")
    ended = CalibrationStatus(
        state="ended",
        channel="CH3",
        kind="zero",
        outcome="completed",
        clean=True,
        record={"format": "fujilib-calibration/1", "run_ended_at": "2026-10-01T00:00:00+00:00"},
    )
    card.apply_snapshot(FujiStateSnapshot(calibration=ended))
    assert card.last_saved_record is None
    assert "not saved" in card._status_label.text()
    _close_pool_sync(controller)


@pytest.mark.parametrize(
    ("status", "fragment", "color"),
    [
        (CalibrationStatus(), "no calibration", COLOR_IDLE),
        (
            CalibrationStatus(state="waiting", channel="CH3", kind="zero", steady=True),
            "STEADY",
            COLOR_OK,
        ),
        (
            CalibrationStatus(
                state="waiting", channel="CH3", kind="zero", steady=False, reasons=("CH3: moving",)
            ),
            "CH3: moving",
            COLOR_WARN,
        ),
        (
            CalibrationStatus(state="calibrating", channel="CH3", kind="zero"),
            "calibrating",
            COLOR_WARN,
        ),
        (
            CalibrationStatus(
                state="ended", channel="CH3", kind="span", outcome="completed", clean=True
            ),
            "ended: completed",
            COLOR_OK,
        ),
        (
            CalibrationStatus(
                state="ended", channel="CH3", kind="span", outcome="cancelled", clean=True
            ),
            "ended: cancelled",
            COLOR_IDLE,
        ),
        (
            CalibrationStatus(
                state="ended", channel="CH3", kind="span", outcome="failed", error="error 5"
            ),
            "error 5",
            COLOR_FAIL,
        ),
        (
            CalibrationStatus(state="ended", channel="CH3", kind="span", clean=False),
            "not left clean",
            COLOR_FAIL,
        ),
    ],
)
def test_the_calibration_readout(status: CalibrationStatus, fragment: str, color: Any) -> None:
    text, shown = _calibration_text(status)
    assert fragment in text
    assert shown == color
