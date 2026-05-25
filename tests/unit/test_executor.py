"""Tests for :class:`capa.experiment.executor.MethodExecutor`."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import anyio
import pytest
import structlog

from capa.channels.calibration import Identity
from capa.channels.registry import ChannelRegistry
from capa.channels.spec import ChannelKind, ChannelSpec, WatlowParameter
from capa.core.clock import RunClock
from capa.core.databus import DataBus
from capa.devices.adapter import CommandResult, DeviceCommand
from capa.devices.records import ChannelSample
from capa.experiment.authorization import Authorization
from capa.experiment.config import (
    CalibrationSetRef,
    DeviceConfig,
    ExperimentConfig,
    HardwareProfile,
    OperatorRef,
    ProcedureRef,
    SampleInfo,
)
from capa.experiment.executor import (
    DEFAULT_RAMP_TICK_HZ,
    MethodExecutor,
    MethodExecutorError,
)
from capa.experiment.method import (
    ChannelRef,
    EndCondition,
    HoldStep,
    Method,
    PromptStep,
    RampStep,
    SafeShutdownStep,
    SetpointStep,
    WaitStep,
)
from capa.experiment.procedures.base import ProcedureContext
from capa.runtime.dispatch import AdapterDispatcher


class _FakeWriter:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def write_event(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


class _FakeAdapter:
    """Records every command issued. Returns ``accepted=True`` by default."""

    def __init__(self, name: str = "heater", *, accept: bool = True) -> None:
        self.name = name
        self.accept = accept
        self.commands: list[DeviceCommand] = []

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        self.commands.append(cmd)
        return CommandResult(
            accepted=self.accept,
            detail="ok" if self.accept else "rejected",
            t_mono_ns=0,
            t_utc=datetime.now(UTC),
        )


def _heater_setpoint_channel() -> ChannelSpec:
    return ChannelSpec(
        name="heater.setpoint",
        kind=ChannelKind.SETPOINT,
        unit="degC",
        derived_unit="degC",
        source=WatlowParameter(device="heater", parameter="setpoint"),
        calibration=Identity(input_unit="degC", output_unit="degC"),
    )


def _make_ctx(
    *,
    adapter: _FakeAdapter | None = None,
) -> tuple[ProcedureContext, _FakeWriter, _FakeAdapter, anyio.Event]:
    adapter = adapter or _FakeAdapter()
    channels = (_heater_setpoint_channel(),)
    config = ExperimentConfig(
        hardware=HardwareProfile(
            name="exec-test",
            devices=(DeviceConfig(name="heater", adapter="capa.devices.sim.watlow_sim"),),
            channels=channels,
        ),
        method=None,
        procedure=ProcedureRef(id="capa.builtin.recipe_runner"),
        calibration_set=CalibrationSetRef(name="default"),
        operator=OperatorRef(id="abr"),
        sample=SampleInfo(id="S-EX"),
    )
    instruments = ChannelRegistry.from_specs(list(channels))
    instruments.freeze()
    writer = _FakeWriter()
    stop = anyio.Event()
    adapters_map: dict[str, Any] = {"heater": adapter}
    ctx = ProcedureContext(
        clock=RunClock(started_mono_ns=0, started_utc=datetime.now(UTC)),
        config=config,
        bundle_writer=writer,  # type: ignore[arg-type]
        databus=DataBus(),
        logger=structlog.get_logger("test"),
        external_stop=stop,
        instruments=instruments,
        adapters=adapters_map,
        dispatcher=AdapterDispatcher(adapters_map),
        authorization=Authorization(operator_id="abr", run_id="r1"),
        metadata={},
    )
    return ctx, writer, adapter, stop


@pytest.mark.anyio
async def test_setpoint_step_issues_one_authorized_command() -> None:
    ctx, writer, adapter, _ = _make_ctx()
    method = Method(
        name="m",
        steps=(SetpointStep(target=ChannelRef(name="heater.setpoint"), value=400.0),),
    )
    await MethodExecutor(ctx=ctx).run_to_completion(method)
    assert len(adapter.commands) == 1
    cmd = adapter.commands[0]
    assert cmd.kind == "set_setpoint"
    assert cmd.payload["value"] == 400.0
    assert cmd.issued_by == "abr"
    assert cmd.authorization_id == ctx.authorization.authorization_id
    kinds = [e["kind"] for e in writer.events]
    assert "method.step.entered" in kinds
    assert "method.step.exited" in kinds
    assert "method.command.issued" in kinds


@pytest.mark.anyio
async def test_hold_step_with_duration_returns_after_sleep() -> None:
    ctx, _writer, adapter, _ = _make_ctx()
    method = Method(
        name="m",
        steps=(
            HoldStep(
                target=ChannelRef(name="heater.setpoint"),
                value=350.0,
                duration_s=0.05,
            ),
        ),
    )
    await MethodExecutor(ctx=ctx).run_to_completion(method)
    assert adapter.commands[0].payload["value"] == 350.0


@pytest.mark.anyio
async def test_wait_step_end_condition_triggers_on_databus_sample() -> None:
    ctx, _writer, _adapter, _stop = _make_ctx()
    method = Method(
        name="m",
        steps=(
            WaitStep(
                end_condition=EndCondition(channel="heater.pv", op=">", value=99.0),
                timeout_s=2.0,
            ),
        ),
    )

    async def publish_sample() -> None:
        await anyio.sleep(0.05)
        await ctx.databus.publish(
            ChannelSample(
                channel="heater.pv",
                t_mono_s=0.05,
                t_mono_ns=int(5e7),
                value=100.0,
                unit="degC",
            )
        )

    async with anyio.create_task_group() as tg:
        tg.start_soon(publish_sample)
        await MethodExecutor(ctx=ctx).run_to_completion(method)


@pytest.mark.anyio
async def test_wait_step_timeout_with_abort_raises() -> None:
    ctx, _, _, _ = _make_ctx()
    method = Method(
        name="m",
        steps=(
            WaitStep(
                end_condition=EndCondition(channel="missing", op=">", value=1.0),
                timeout_s=0.05,
                on_timeout="abort",
            ),
        ),
    )
    with pytest.raises(MethodExecutorError, match="timed out"):
        await MethodExecutor(ctx=ctx).run_to_completion(method)


@pytest.mark.anyio
async def test_external_stop_aborts_between_steps() -> None:
    ctx, _writer, adapter, stop = _make_ctx()
    method = Method(
        name="m",
        steps=(
            SetpointStep(target=ChannelRef(name="heater.setpoint"), value=100.0),
            HoldStep(
                target=ChannelRef(name="heater.setpoint"),
                value=200.0,
                duration_s=10.0,
            ),
            SetpointStep(target=ChannelRef(name="heater.setpoint"), value=300.0),
        ),
    )
    stop.set()  # set before run starts
    await MethodExecutor(ctx=ctx).run_to_completion(method)
    # No commands should be issued — stop is checked at the top of each step.
    assert adapter.commands == []


@pytest.mark.anyio
async def test_ramp_with_duration_emits_multiple_setpoints() -> None:
    ctx, _, adapter, _ = _make_ctx()
    method = Method(
        name="m",
        steps=(
            RampStep(
                target=ChannelRef(name="heater.setpoint"),
                start_value=100.0,
                end_value=200.0,
                duration_s=0.3,
            ),
        ),
    )
    await MethodExecutor(ctx=ctx).run_to_completion(method)
    # 0.3 s @ 10 Hz tick = 3 ticks (give or take 1 for rounding); each
    # tick issues one setpoint command.
    assert 2 <= len(adapter.commands) <= int(0.3 * DEFAULT_RAMP_TICK_HZ) + 2
    final = adapter.commands[-1].payload["value"]
    assert final == pytest.approx(200.0)


@pytest.mark.anyio
async def test_safe_shutdown_drives_each_target() -> None:
    ctx, _, adapter, _ = _make_ctx()
    method = Method(
        name="m",
        steps=(
            SafeShutdownStep(
                cool_target={"heater.setpoint": 50.0},
                duration_s=0.0,
            ),
        ),
    )
    await MethodExecutor(ctx=ctx).run_to_completion(method)
    assert adapter.commands[-1].payload["value"] == 50.0


@pytest.mark.anyio
async def test_unknown_channel_in_setpoint_raises() -> None:
    ctx, _, _, _ = _make_ctx()
    method = Method(
        name="m",
        steps=(SetpointStep(target=ChannelRef(name="nope"), value=1.0),),
    )
    with pytest.raises(MethodExecutorError, match="not in registry"):
        await MethodExecutor(ctx=ctx).run_to_completion(method)


@pytest.mark.anyio
async def test_prompt_step_auto_acknowledge_completes() -> None:
    ctx, writer, _, _ = _make_ctx()
    method = Method(
        name="m",
        steps=(PromptStep(message="ignite"),),
    )
    executor = MethodExecutor(ctx=ctx, auto_acknowledge_prompts=True)
    await executor.run_to_completion(method)
    kinds = [e["kind"] for e in writer.events]
    assert "method.prompt.shown" in kinds
    assert "method.prompt.acknowledged" in kinds


@pytest.mark.anyio
async def test_prompt_step_confirmed_via_metadata_flag() -> None:
    ctx, writer, _, _ = _make_ctx()
    method = Method(
        name="m",
        steps=(PromptStep(message="ignite", timeout_s=2.0),),
    )

    async def confirm_after_delay() -> None:
        await anyio.sleep(0.05)
        ctx.metadata["_prompt_confirmed"] = True  # type: ignore[index]

    async with anyio.create_task_group() as tg:
        tg.start_soon(confirm_after_delay)
        await MethodExecutor(ctx=ctx).run_to_completion(method)
    kinds = [e["kind"] for e in writer.events]
    assert "method.prompt.shown" in kinds
    assert "method.prompt.acknowledged" in kinds
    ack = next(e for e in writer.events if e["kind"] == "method.prompt.acknowledged")
    assert ack["metadata"] == {"by": "operator"}
    # No timeout / unanswered event
    assert "method.prompt.unanswered" not in kinds


@pytest.mark.anyio
async def test_advance_until_runs_partial_then_resumes() -> None:
    ctx, _, adapter, _ = _make_ctx()
    method = Method(
        name="m",
        steps=(
            SetpointStep(target=ChannelRef(name="heater.setpoint"), value=10.0),
            SetpointStep(target=ChannelRef(name="heater.setpoint"), value=20.0),
            SetpointStep(target=ChannelRef(name="heater.setpoint"), value=30.0),
        ),
    )
    executor = MethodExecutor(ctx=ctx)
    await executor.advance_until(method, step_id=2)
    assert [c.payload["value"] for c in adapter.commands] == [10.0, 20.0]
    await executor.advance_until(method, step_id=3)
    assert [c.payload["value"] for c in adapter.commands] == [10.0, 20.0, 30.0]
