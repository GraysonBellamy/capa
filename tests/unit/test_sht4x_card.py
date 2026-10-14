"""Manual-control card for the SHT4x, driven against the simulated adapter.

The sim lives at ``capa.devices.sim.sht4x_sim``, so the card's import-path
fingerprint matches it and the pool hosts a real command target with the
precision and heater state the card reads back.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QLabel, QMessageBox, QPushButton

from capa.experiment.config import (
    DeviceConfig,
    ExperimentConfig,
    HardwareProfile,
    OperatorRef,
    ProcedureRef,
    SampleInfo,
)
from capa.ui.docks.manual_control import ManualControlDock
from capa.ui.manual.cards.sht4x import Sht4xCard, is_sht4x_device
from capa.ui.state import RunController
from capa.ui.statusbar import OperatorIdProvider

SIM_SHT4X = "capa.devices.sim.sht4x_sim"


def _make_config(*devices: DeviceConfig) -> ExperimentConfig:
    return ExperimentConfig(
        hardware=HardwareProfile(name="manual", devices=devices, channels=()),
        procedure=ProcedureRef(id="capa.builtin.free_run", config={"duration_s": 0.1}),
        operator=OperatorRef(id="opA", display_name="Op A"),
        sample=SampleInfo(id="S"),
    )


def _sim_config(name: str = "ambient") -> ExperimentConfig:
    return _make_config(DeviceConfig(name=name, adapter=SIM_SHT4X, params={}))


@pytest.fixture
def controller(tmp_path: Path) -> RunController:
    return RunController(runs_root=tmp_path)


@pytest.fixture
def op_provider() -> OperatorIdProvider:
    return OperatorIdProvider(initial="opA")


@pytest.fixture(autouse=True)
def _pool_closed_after(controller: RunController) -> Iterator[None]:
    yield
    _close_pool_sync(controller)


def _run_async(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _open_pool_sync(controller: RunController, cfg: ExperimentConfig) -> None:
    from capa.runtime.dispatch import ManualClient
    from capa.runtime.pool import WorkerPool

    pool = WorkerPool.from_config(cfg)
    controller._active_config = cfg
    controller._worker_pool = pool
    controller._manual_client = ManualClient(
        pool=pool, conductor_provider=lambda: controller._conductor
    )
    _run_async(pool.open())


def _close_pool_sync(controller: RunController) -> None:
    pool = controller._worker_pool
    if pool is not None:
        _run_async(pool.close())
    controller._worker_pool = None
    controller._manual_client = None
    controller._active_config = None


def _adapter_for(controller: RunController, name: str) -> Any:
    pool = controller._worker_pool
    assert pool is not None
    return pool.worker_for(name).adapters[name]


def _section_titles(card: Any) -> list[str]:
    layout = card._sections_layout
    titles = []
    for i in range(layout.count()):
        widget = layout.itemAt(i).widget()
        if isinstance(widget, QLabel):
            titles.append(widget.text().strip("─ "))
    return titles


def _button(card: Any, text: str) -> QPushButton:
    return next(b for b in card.findChildren(QPushButton) if b.text() == text)


def test_fingerprint_matches_real_and_sim_adapters() -> None:
    assert is_sht4x_device(DeviceConfig(name="a", adapter="capa.devices.sht4x"))
    assert is_sht4x_device(DeviceConfig(name="a", adapter=SIM_SHT4X))
    assert not is_sht4x_device(DeviceConfig(name="a", adapter="capa.devices.sartorius"))


class TestSht4xCard:
    def test_renders_measurement_heater_and_maintenance(
        self, qtbot: Any, controller: RunController, op_provider: OperatorIdProvider
    ) -> None:
        cfg = _sim_config()
        _open_pool_sync(controller, cfg)
        card = Sht4xCard(
            spec=cfg.hardware.devices[0], controller=controller, operator_provider=op_provider
        )
        qtbot.addWidget(card)
        assert _section_titles(card) == ["Measurement", "Heater", "Maintenance"]
        texts = [b.text() for b in card.findChildren(QPushButton)]
        assert {"Apply", "Pulse heater…", "Soft reset"} <= set(texts)

    def test_apply_precision_dispatches_and_reads_back(
        self, qtbot: Any, controller: RunController, op_provider: OperatorIdProvider
    ) -> None:
        cfg = _sim_config()
        _open_pool_sync(controller, cfg)
        card = Sht4xCard(
            spec=cfg.hardware.devices[0], controller=controller, operator_provider=op_provider
        )
        qtbot.addWidget(card)
        combo = card._precision_combo
        assert combo is not None
        assert combo.currentIndex() == -1  # nothing claimed until the device reports
        _run_async(card.refresh_readback())
        assert combo.currentText() == "high"
        assert "Sensor: 0x53494d00" in card._subtitle_label.text()

        combo.setCurrentText("low")
        result = _run_async(card.dispatch(kind="set_precision", payload={"precision": "low"}))
        assert result is not None and result.accepted
        adapter = _adapter_for(controller, "ambient")
        assert adapter.precision == "low"
        _run_async(card.refresh_readback())
        assert combo.currentText() == "low"

    def test_heater_pulse_is_destructive_and_confirmable(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = _sim_config()
        _open_pool_sync(controller, cfg)
        card = Sht4xCard(
            spec=cfg.hardware.devices[0], controller=controller, operator_provider=op_provider
        )
        qtbot.addWidget(card)
        adapter = _adapter_for(controller, "ambient")

        monkeypatch.setattr(QMessageBox, "question", lambda *a, **kw: QMessageBox.StandardButton.No)
        refused = _run_async(
            card.dispatch(
                kind="heater_pulse",
                payload={"power_mw": 20, "duration_s": 0.1},
                destructive=True,
                destructive_summary="pulse",
            )
        )
        assert refused is None
        assert _run_async(adapter.read_state_snapshot()).heater_pulses == 0

        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **kw: QMessageBox.StandardButton.Yes
        )
        result = _run_async(
            card.dispatch(
                kind="heater_pulse",
                payload={"power_mw": 20, "duration_s": 0.1},
                destructive=True,
                destructive_summary="pulse",
            )
        )
        assert result is not None and result.accepted
        assert "end_of_pulse" in result.detail
        _run_async(card.refresh_readback())
        assert "Heater pulses: 1" in card._subtitle_label.text()

    def test_empty_operator_id_blocks_dispatch(
        self, qtbot: Any, controller: RunController, op_provider: OperatorIdProvider
    ) -> None:
        cfg = _sim_config()
        _open_pool_sync(controller, cfg)
        card = Sht4xCard(
            spec=cfg.hardware.devices[0],
            controller=controller,
            operator_provider=OperatorIdProvider(initial=""),
        )
        qtbot.addWidget(card)
        assert _run_async(card.dispatch(kind="soft_reset")) is None
        assert "operator id required" in card._status_label.text()


class TestDockRoutesSht4x:
    def test_dock_builds_an_sht4x_card(
        self, qtbot: Any, controller: RunController, op_provider: OperatorIdProvider
    ) -> None:
        from PySide6.QtWidgets import QMainWindow

        window = QMainWindow()
        qtbot.addWidget(window)
        dock = ManualControlDock(
            controller=controller, operator_provider=op_provider, parent=window
        )
        cfg = _sim_config("ambient")
        _open_pool_sync(controller, cfg)
        dock.load_config(cfg)
        cards = dock.findChildren(Sht4xCard)
        assert [c.device_name for c in cards] == ["ambient"]
