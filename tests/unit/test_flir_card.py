"""Tests for the IR-camera manual-control card's read-back: the temperature
range, the auto-NUC interval, the radiometric parameters and the palettes.

Drives :class:`FlirCard` against the IR sim through a real
:class:`WorkerPool`, so each read-back is the sim's own
:class:`IrCameraStateSnapshot`, reached through the camera wrapper.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QMessageBox, QPushButton, QSpinBox

from capa.devices.camera.base import (
    CameraSpec,
    CameraTemperatureRange,
    IrCameraStateSnapshot,
    IrRadiometricParams,
)
from capa.devices.sim.flir_ir_sim import FlirIrSim
from capa.experiment.config import (
    CalibrationSetRef,
    ExperimentConfig,
    HardwareProfile,
    OperatorRef,
    ProcedureRef,
    SampleInfo,
)
from capa.ui.manual.cards.camera import FlirCard, _range_label
from capa.ui.state import RunController, RunUiState
from capa.ui.statusbar import OperatorIdProvider
from tests.unit.test_manual_control_cards import _close_pool_sync, _open_pool_sync, _run_async

SIM = "capa.devices.sim.flir_ir_sim"

Applied = tuple[str, dict[str, Any]]


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


def _config() -> ExperimentConfig:
    return ExperimentConfig(
        hardware=HardwareProfile(
            name="manual",
            devices=(),
            channels=(),
            cameras=(CameraSpec(name="ir_cam0", adapter=SIM, kind="ir"),),
        ),
        procedure=ProcedureRef(id="capa.builtin.free_run", config={"duration_s": 0.1}),
        calibration_set=CalibrationSetRef(name="default"),
        operator=OperatorRef(id="opA", display_name="Op A"),
        sample=SampleInfo(id="S"),
    )


def _card(qtbot: Any, controller: RunController, op_provider: OperatorIdProvider) -> FlirCard:
    cfg = _config()
    _open_pool_sync(controller, cfg)
    card = FlirCard(
        spec=cfg.hardware.cameras[0], controller=controller, operator_provider=op_provider
    )
    qtbot.addWidget(card)
    return card


def _unopened_card(
    qtbot: Any, controller: RunController, op_provider: OperatorIdProvider
) -> FlirCard:
    card = FlirCard(
        spec=_config().hardware.cameras[0], controller=controller, operator_provider=op_provider
    )
    qtbot.addWidget(card)
    return card


def _range_combo(card: FlirCard) -> QComboBox:
    combo = card._temp_range_combo
    assert combo is not None
    return combo


def _remote_combo(card: FlirCard) -> QComboBox:
    combo = card._remote_palette_combo
    assert combo is not None
    return combo


def _preview_combo(card: FlirCard) -> QComboBox:
    combo = card._preview_palette_combo
    assert combo is not None
    return combo


def _nuc_spin(card: FlirCard) -> QSpinBox:
    spin = card._auto_nuc_spin
    assert spin is not None
    return spin


def _choices(combo: QComboBox) -> list[str]:
    return [combo.itemText(i) for i in range(combo.count())]


def _pick(combo: QComboBox, text: str) -> None:
    """Pick ``text`` as the operator does, so ``activated`` fires."""
    index = combo.findText(text)
    assert index >= 0
    combo.setCurrentIndex(index)
    combo.activated.emit(index)


def _spin(card: FlirCard, field: str) -> QDoubleSpinBox:
    return card._radiometric_spins[field]


def _apply_button(card: FlirCard, field: str) -> QPushButton:
    button = card.findChild(QPushButton, f"apply_{field}")
    assert button is not None
    return button


def _capture_applies(card: FlirCard, monkeypatch: pytest.MonkeyPatch) -> list[Applied]:
    """Record each Apply instead of scheduling it; :func:`_send` runs one."""
    applied: list[Applied] = []
    monkeypatch.setattr(
        card, "_apply_field", lambda field, **dispatch: applied.append((field, dispatch))
    )
    return applied


def _send(card: FlirCard, applied: Applied) -> None:
    field, dispatch = applied
    _run_async(card._apply_field_and_read_back(field, dispatch))


def _answer(monkeypatch: pytest.MonkeyPatch, answer: QMessageBox.StandardButton) -> None:
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_k: answer)


# ---------------------------------------------------------------------------
# Temperature range
# ---------------------------------------------------------------------------


def test_the_range_list_is_empty_until_the_camera_reports(
    qtbot: Any, controller: RunController, op_provider: OperatorIdProvider
) -> None:
    card = _card(qtbot, controller, op_provider)
    assert _choices(_range_combo(card)) == []
    assert _range_combo(card).currentIndex() == -1


def test_the_ranges_are_read_from_the_camera_in_degrees(
    qtbot: Any, controller: RunController, op_provider: OperatorIdProvider
) -> None:
    card = _card(qtbot, controller, op_provider)
    _run_async(card.refresh_readback())
    assert _choices(_range_combo(card)) == ["-20 to 120 °C", "0 to 650 °C", "300 to 1200 °C"]
    assert _range_combo(card).currentIndex() == 0
    assert "Range: -20 to 120 °C" in card._subtitle_label.text()


def test_a_range_switch_names_the_range_and_reads_back(
    qtbot: Any,
    controller: RunController,
    op_provider: OperatorIdProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = _card(qtbot, controller, op_provider)
    _run_async(card.refresh_readback())
    applied = _capture_applies(card, monkeypatch)
    _pick(_range_combo(card), "300 to 1200 °C")
    card._on_apply_temperature_range()
    assert applied == [
        (
            "temperature_range_index",
            {
                "kind": "set_temperature_range",
                "payload": {"index": 2},
                "destructive": True,
                "destructive_summary": (
                    "Switch camera temperature range to 300 to 1200 °C. "
                    "Triggers a multi-second recalibration."
                ),
            },
        )
    ]
    # The camera, not the combo, decides what the card shows next.
    _range_combo(card).setCurrentIndex(0)
    _answer(monkeypatch, QMessageBox.StandardButton.Yes)
    _send(card, applied[0])
    assert _range_combo(card).currentIndex() == 2
    assert "Range: 300 to 1200 °C" in card._subtitle_label.text()
    assert card._unapplied_edits == set()


def test_a_declined_range_switch_keeps_the_pick(
    qtbot: Any,
    controller: RunController,
    op_provider: OperatorIdProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = _card(qtbot, controller, op_provider)
    _run_async(card.refresh_readback())
    applied = _capture_applies(card, monkeypatch)
    _pick(_range_combo(card), "0 to 650 °C")
    card._on_apply_temperature_range()
    _answer(monkeypatch, QMessageBox.StandardButton.No)
    _send(card, applied[0])
    assert _range_combo(card).currentText() == "0 to 650 °C"
    assert "Range: -20 to 120 °C" in card._subtitle_label.text()  # the camera's


def test_apply_before_the_ranges_are_read_sends_nothing(
    qtbot: Any,
    controller: RunController,
    op_provider: OperatorIdProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = _card(qtbot, controller, op_provider)
    applied = _capture_applies(card, monkeypatch)
    card._on_apply_temperature_range()
    assert applied == []
    assert card._status_label.text() == "ranges not read from camera yet"


def test_nothing_is_read_back_during_a_run(
    qtbot: Any, controller: RunController, op_provider: OperatorIdProvider
) -> None:
    card = _card(qtbot, controller, op_provider)
    controller._ui_state = RunUiState.RUNNING
    _run_async(card.refresh_readback())
    assert _choices(_range_combo(card)) == []
    controller._ui_state = RunUiState.IDLE


def test_an_unknown_active_range_selects_nothing(
    qtbot: Any, controller: RunController, op_provider: OperatorIdProvider
) -> None:
    card = _unopened_card(qtbot, controller, op_provider)
    card.apply_snapshot(
        IrCameraStateSnapshot(
            temperature_ranges=(CameraTemperatureRange(min_c=0.0, max_c=650.0),),
            temperature_range_index=None,
        )
    )
    assert _choices(_range_combo(card)) == ["0 to 650 °C"]
    assert _range_combo(card).currentIndex() == -1
    assert "Range:" not in card._subtitle_label.text()


def test_a_kelvin_read_back_is_labelled_in_round_degrees() -> None:
    # 253.15 K and 393.15 K convert to -19.999999999999972 and 120.00000000000003.
    kelvin = CameraTemperatureRange(min_c=253.15 - 273.15, max_c=393.15 - 273.15)
    assert _range_label(kelvin) == "-20 to 120 °C"
    assert _range_label(CameraTemperatureRange(min_c=-1e-12, max_c=650.0)) == "0 to 650 °C"


# ---------------------------------------------------------------------------
# Radiometric parameters
# ---------------------------------------------------------------------------


def test_every_radiometric_parameter_has_a_spinbox(
    qtbot: Any, controller: RunController, op_provider: OperatorIdProvider
) -> None:
    card = _card(qtbot, controller, op_provider)
    assert set(card._radiometric_spins) == set(IrRadiometricParams.model_fields)


def test_the_radiometric_fields_show_the_cameras_values(
    qtbot: Any, controller: RunController, op_provider: OperatorIdProvider
) -> None:
    card = _card(qtbot, controller, op_provider)
    _run_async(card.dispatch(kind="set_distance_m", payload={"distance_m": 0.35}))
    _run_async(card.refresh_readback())
    assert _spin(card, "distance_m").value() == pytest.approx(0.35)
    # The sim's 20 °C, not the card's 22 °C placeholder.
    assert _spin(card, "atmospheric_temp_c").value() == pytest.approx(20.0)
    assert card._unapplied_edits == set()


def test_a_read_back_keeps_an_edit_until_it_is_applied(
    qtbot: Any,
    controller: RunController,
    op_provider: OperatorIdProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = _card(qtbot, controller, op_provider)
    _run_async(card.refresh_readback())
    # The operator types two values; the camera's atm temp changes meanwhile.
    _spin(card, "distance_m").setValue(2.5)
    _spin(card, "emissivity").setValue(0.8)
    _run_async(card.dispatch(kind="set_atmospheric_temp", payload={"temperature_c": 31.0}))
    applied = _capture_applies(card, monkeypatch)

    _apply_button(card, "emissivity").click()
    assert applied == [("emissivity", {"kind": "set_emissivity", "payload": {"emissivity": 0.8}})]
    _send(card, applied[0])
    assert _spin(card, "emissivity").value() == pytest.approx(0.8)
    assert _spin(card, "atmospheric_temp_c").value() == pytest.approx(31.0)
    assert _spin(card, "distance_m").value() == pytest.approx(2.5)  # not applied yet

    _apply_button(card, "distance_m").click()
    assert applied[1] == ("distance_m", {"kind": "set_distance_m", "payload": {"distance_m": 2.5}})
    _send(card, applied[1])
    # Applied, so the next read-back shows the camera's value again.
    _run_async(card.dispatch(kind="set_distance_m", payload={"distance_m": 3.0}))
    _run_async(card.refresh_readback())
    assert _spin(card, "distance_m").value() == pytest.approx(3.0)
    assert card._unapplied_edits == set()


def test_a_camera_value_beyond_the_spinbox_limits_is_shown_as_is(
    qtbot: Any, controller: RunController, op_provider: OperatorIdProvider
) -> None:
    card = _unopened_card(qtbot, controller, op_provider)
    card.apply_snapshot(
        IrCameraStateSnapshot(
            radiometric=IrRadiometricParams(
                emissivity=0.95,
                atmospheric_temp_c=20.0,
                reflected_temp_c=650.0,  # past the spinbox's 500 °C limit
                distance_m=1.0,
                relative_humidity=0.5,
                atmospheric_transmission=1.0,
            )
        )
    )
    assert _spin(card, "reflected_temp_c").value() == 650.0
    assert card._unapplied_edits == set()


# ---------------------------------------------------------------------------
# Auto-NUC interval and palettes
# ---------------------------------------------------------------------------


def test_the_auto_nuc_interval_and_palettes_show_the_cameras_values(
    qtbot: Any, controller: RunController, op_provider: OperatorIdProvider
) -> None:
    card = _card(qtbot, controller, op_provider)
    for kind, payload in (
        ("set_auto_nuc_interval", {"seconds": 45}),
        ("set_remote_palette", {"palette": "lava"}),
        ("set_preview_palette", {"palette": "whitehot"}),
    ):
        _run_async(card.dispatch(kind=kind, payload=payload))
    _run_async(card.refresh_readback())
    assert _nuc_spin(card).value() == 45
    assert _choices(_remote_combo(card)) == list(FlirIrSim.REMOTE_PALETTES)
    assert _remote_combo(card).currentText() == "lava"
    assert _choices(_preview_combo(card)) == sorted(FlirIrSim.PREVIEW_PALETTE_PRESETS)
    assert _preview_combo(card).currentText() == "whitehot"
    assert card._unapplied_edits == set()


def test_the_palettes_are_empty_until_the_camera_reports(
    qtbot: Any,
    controller: RunController,
    op_provider: OperatorIdProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = _card(qtbot, controller, op_provider)
    assert _choices(_remote_combo(card)) == []
    assert _choices(_preview_combo(card)) == []
    applied = _capture_applies(card, monkeypatch)
    _apply_button(card, "remote_palette").click()
    _apply_button(card, "preview_palette").click()
    assert applied == []
    assert card._status_label.text() == "palettes not read from camera yet"


def test_a_palette_pick_survives_another_apply(
    qtbot: Any,
    controller: RunController,
    op_provider: OperatorIdProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = _card(qtbot, controller, op_provider)
    _run_async(card.refresh_readback())
    _pick(_remote_combo(card), "arctic")
    _nuc_spin(card).setValue(60)
    applied = _capture_applies(card, monkeypatch)

    _apply_button(card, "auto_nuc_interval_s").click()
    assert applied == [
        ("auto_nuc_interval_s", {"kind": "set_auto_nuc_interval", "payload": {"seconds": 60}})
    ]
    _send(card, applied[0])
    assert _nuc_spin(card).value() == 60
    assert _remote_combo(card).currentText() == "arctic"  # not applied yet

    _apply_button(card, "remote_palette").click()
    assert applied[1] == (
        "remote_palette",
        {"kind": "set_remote_palette", "payload": {"palette": "arctic"}},
    )
    _send(card, applied[1])
    assert _remote_combo(card).currentText() == "arctic"  # now the camera's
    assert card._unapplied_edits == set()
