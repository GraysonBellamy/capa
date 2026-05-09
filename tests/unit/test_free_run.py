"""Tests for :class:`capa.experiment.procedures.builtin.free_run.FreeRun`."""

from __future__ import annotations

from datetime import UTC, datetime

import anyio
import pytest
import structlog

from capa.channels.registry import ChannelRegistry
from capa.core.clock import RunClock
from capa.core.databus import DataBus
from capa.experiment.authorization import Authorization
from capa.experiment.config import (
    CalibrationSetRef,
    ExperimentConfig,
    HardwareProfile,
    OperatorRef,
    ProcedureRef,
    SampleInfo,
)
from capa.experiment.procedures.base import ProcedureContext, ProcedureError
from capa.experiment.procedures.builtin.free_run import FreeRun, FreeRunConfig


class _FakeWriter:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def write_event(self, **kwargs):
        self.events.append(kwargs)


def _ctx(stop: anyio.Event, *, with_method: bool = False) -> ProcedureContext:
    method = (
        {
            "name": "test-method",
            "steps": [{"kind": "wait", "duration_s": 1.0}],
        }
        if with_method
        else None
    )
    config = ExperimentConfig(
        hardware=HardwareProfile(name="empty", devices=(), channels=()),
        method=method,
        procedure=ProcedureRef(id="capa.builtin.free_run"),
        calibration_set=CalibrationSetRef(name="default"),
        operator=OperatorRef(id="abr"),
        sample=SampleInfo(id="S-1"),
    )
    instruments = ChannelRegistry.from_specs(list(config.hardware.channels))
    instruments.freeze()
    return ProcedureContext(
        clock=RunClock(started_mono_ns=0, started_utc=datetime.now(UTC)),
        config=config,
        bundle_writer=_FakeWriter(),  # type: ignore[arg-type]
        databus=DataBus(),
        logger=structlog.get_logger("test"),
        external_stop=stop,
        instruments=instruments,
        adapters={},
        authorization=Authorization(operator_id="abr", run_id="test"),
    )


def test_config_validates_duration_field() -> None:
    cfg = FreeRunConfig.model_validate({"duration_s": 0.25})
    assert cfg.duration_s == 0.25
    cfg2 = FreeRunConfig.model_validate({})
    assert cfg2.duration_s is None
    with pytest.raises(Exception):
        FreeRunConfig.model_validate({"duration_s": -1.0})


@pytest.mark.anyio
async def test_preflight_rejects_method() -> None:
    proc = FreeRun.from_config({"duration_s": 0.0})
    stop = anyio.Event()
    with pytest.raises(ProcedureError):
        await proc.preflight(_ctx(stop, with_method=True))


@pytest.mark.anyio
async def test_run_with_zero_duration_emits_two_events() -> None:
    proc = FreeRun.from_config({"duration_s": 0.0})
    stop = anyio.Event()
    ctx = _ctx(stop)
    await proc.run(ctx)
    assert isinstance(ctx.bundle_writer, _FakeWriter)
    kinds = [e["kind"] for e in ctx.bundle_writer.events]
    assert kinds == ["free_run.started", "free_run.ended"]
    assert ctx.bundle_writer.events[-1]["metadata"]["stopped_by"] == "zero_duration"


@pytest.mark.anyio
async def test_run_external_stop_fires() -> None:
    proc = FreeRun.from_config({})  # no duration
    stop = anyio.Event()
    ctx = _ctx(stop)

    async with anyio.create_task_group() as tg:

        async def stop_soon() -> None:
            await anyio.sleep(0.05)
            stop.set()

        tg.start_soon(stop_soon)
        await proc.run(ctx)

    assert isinstance(ctx.bundle_writer, _FakeWriter)
    assert ctx.bundle_writer.events[-1]["metadata"]["stopped_by"] == "external_stop"


@pytest.mark.anyio
async def test_run_duration_elapsed_path() -> None:
    proc = FreeRun.from_config({"duration_s": 0.05})
    stop = anyio.Event()
    ctx = _ctx(stop)
    await proc.run(ctx)
    assert isinstance(ctx.bundle_writer, _FakeWriter)
    assert ctx.bundle_writer.events[-1]["metadata"]["stopped_by"] == "duration_elapsed"
