""":class:`RunController` and the experiment's ``device_settings``: read on
load before READY, applied on request, refused while busy."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import anyio
import pytest

from capa.devices.records import DeviceEvent
from capa.experiment.config import ExperimentConfig
from capa.runtime.device_settings import Outcome, SettingsPlan, SettingsReport
from capa.ui.config_progress import ConfigLoadProgress, ConfigLoadState
from capa.ui.state import RunController

pytestmark = pytest.mark.anyio


@pytest.fixture
def config(configs_dir: Path) -> ExperimentConfig:
    return ExperimentConfig.load(configs_dir / "experiments" / "sim_capa_pyrolysis.yaml")


class _Recorder:
    """Every controller signal the feature touches, in emission order."""

    def __init__(self, controller: RunController) -> None:
        self.order: list[str] = []
        self.progress: list[ConfigLoadProgress] = []
        self.finished: list[ConfigLoadProgress] = []
        self.plans: list[SettingsPlan] = []
        self.reports: list[SettingsReport] = []
        self.events: list[DeviceEvent] = []
        controller.config_load_progress.connect(self.progress.append)
        controller.config_load_finished.connect(self._on_finished)
        controller.device_settings_planned.connect(self._on_planned)
        controller.device_settings_applied.connect(self.reports.append)
        controller.manual_event.connect(self.events.append)

    def _on_finished(self, progress: ConfigLoadProgress) -> None:
        self.order.append(f"finished:{progress.state.value}")
        self.finished.append(progress)

    def _on_planned(self, plan: SettingsPlan) -> None:
        self.order.append("planned")
        self.plans.append(plan)


async def _load(controller: RunController, config: ExperimentConfig, rec: _Recorder) -> None:
    controller.set_active_config(config)
    with anyio.fail_after(30):
        while not rec.finished:
            await anyio.sleep(0.01)


@pytest.fixture
async def controller(qapp: Any, tmp_path: Path) -> Any:
    ctrl = RunController(runs_root=tmp_path, configure_logging_for_bundle=False)
    yield ctrl
    await ctrl.aclose_pool()


async def test_load_reads_settings_before_ready(
    controller: RunController, config: ExperimentConfig
) -> None:
    rec = _Recorder(controller)
    await _load(controller, config, rec)

    assert rec.order == ["planned", "finished:ready"]
    assert any(p.state is ConfigLoadState.READING_SETTINGS for p in rec.progress)
    (plan,) = rec.plans
    # The simulators start at their factory defaults, so five settings differ.
    assert plan.change_count == 5
    assert controller.hardware_ready


async def test_apply_then_check_matches(
    controller: RunController, config: ExperimentConfig
) -> None:
    rec = _Recorder(controller)
    await _load(controller, config, rec)
    (plan,) = rec.plans
    selected = {(d.name, c.field) for d in plan.devices for c in d.changes}

    task = controller.apply_device_settings(plan, selected, operator_id="abr")
    assert task is not None
    assert controller.device_settings_busy
    assert controller.apply_device_settings(plan, selected, operator_id="abr") is None
    report = await task

    assert report.ok
    assert [r.outcome for r in report.results] == [Outcome.VERIFIED] * 5
    assert rec.reports == [report]
    assert len(rec.events) == 5
    assert all(e.kind.startswith("manual.set_") and e.severity == "info" for e in rec.events)

    check = controller.check_device_settings()
    assert check is not None
    assert not (await check).needs_attention


async def test_no_declared_settings_reads_nothing(
    controller: RunController, config: ExperimentConfig
) -> None:
    rec = _Recorder(controller)
    await _load(controller, config.model_copy(update={"device_settings": {}}), rec)

    assert rec.order == ["finished:ready"]
    assert not any(p.state is ConfigLoadState.READING_SETTINGS for p in rec.progress)
    assert controller.check_device_settings() is None


async def test_failed_readback_still_reaches_ready(
    controller: RunController, config: ExperimentConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_readback(_pool: object) -> Any:
        async def read(name: str) -> object:
            raise RuntimeError(f"{name} is not answering")

        return read

    monkeypatch.setattr("capa.ui.state.pool_readback", broken_readback)
    rec = _Recorder(controller)
    await _load(controller, config, rec)

    assert rec.finished[-1].state is ConfigLoadState.READY
    (plan,) = rec.plans
    assert all("not answering" in (d.error or "") for d in plan.devices)
    assert plan.needs_attention


async def test_start_is_refused_while_applying(
    controller: RunController, config: ExperimentConfig
) -> None:
    rec = _Recorder(controller)
    await _load(controller, config, rec)
    controller._settings_applying = True
    with pytest.raises(RuntimeError, match="device settings are being applied"):
        controller.start(config)
    controller._settings_applying = False


async def test_closing_the_pool_cancels_a_running_check(
    controller: RunController, config: ExperimentConfig
) -> None:
    rec = _Recorder(controller)
    await _load(controller, config, rec)
    task = controller.check_device_settings()
    assert task is not None
    await controller.aclose_pool()
    with pytest.raises(asyncio.CancelledError):
        await task
