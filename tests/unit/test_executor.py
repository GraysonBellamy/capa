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
from capa.experiment import executor as executor_module
from capa.experiment.authorization import Authorization
from capa.experiment.config import (
    DeviceConfig,
    ExperimentConfig,
    HardwareProfile,
    OperatorRef,
    ProcedureRef,
    SampleInfo,
)
from capa.experiment.executor import (
    DEFAULT_RAMP_TICK_HZ,
    PROMPT_TICK_KIND,
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
from capa.experiment.procedures.base import OperatorCommand, ProcedureContext
from capa.runtime.dispatch import AdapterDispatcher
from capa.runtime.emissions import ProcedureTick


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
        operator=OperatorRef(id="abr", display_name="Test Operator"),
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


class _FakeSink:
    """Collects the ticks a prompt publishes for the UI."""

    def __init__(self) -> None:
        self.ticks: list[ProcedureTick] = []

    def publish(self, tick: ProcedureTick) -> None:
        self.ticks.append(tick)

    def states(self) -> list[tuple[object, object]]:
        return [(t.payload["state"], t.payload["prompt_id"]) for t in self.ticks]


def _attach_ui(ctx: ProcedureContext) -> tuple[_FakeSink, Any]:
    """Wire a UI sink and an operator-command stream into ``ctx``."""
    sink = _FakeSink()
    send, recv = anyio.create_memory_object_stream[OperatorCommand](max_buffer_size=8)
    ctx.ui_sink = sink
    ctx.operator_commands = recv
    return sink, send


async def _open_prompt_id(sink: _FakeSink) -> str:
    """Wait for the first ``open`` tick and return its prompt id."""
    with anyio.fail_after(2.0):
        while not sink.ticks:
            await anyio.sleep(0.005)
    prompt_id = sink.ticks[0].payload["prompt_id"]
    assert isinstance(prompt_id, str)
    return prompt_id


@pytest.mark.anyio
async def test_prompt_step_confirmed_by_operator_command() -> None:
    ctx, writer, _, _ = _make_ctx()
    sink, send = _attach_ui(ctx)
    method = Method(name="m", steps=(PromptStep(title="Ignite", message="ignite"),))

    async def confirm() -> None:
        prompt_id = await _open_prompt_id(sink)
        await send.send(OperatorCommand(kind="confirm_prompt", metadata={"prompt_id": prompt_id}))

    async with anyio.create_task_group() as tg:
        tg.start_soon(confirm)
        await MethodExecutor(ctx=ctx).run_to_completion(method)

    kinds = [e["kind"] for e in writer.events]
    assert "method.prompt.unanswered" not in kinds
    ack = next(e for e in writer.events if e["kind"] == "method.prompt.acknowledged")
    prompt_id = sink.ticks[0].payload["prompt_id"]
    assert ack["metadata"] == {"by": "operator", "prompt_id": prompt_id}
    first = sink.ticks[0].payload
    assert first["kind"] == PROMPT_TICK_KIND
    assert (first["title"], first["message"]) == ("Ignite", "ignite")
    assert (first["step_index"], first["step_count"]) == (0, 1)
    assert sink.ticks[0].procedure_id == "capa.builtin.recipe_runner"
    assert sink.states()[-1] == ("closed", prompt_id)


@pytest.mark.anyio
async def test_prompt_step_drops_confirm_for_another_prompt() -> None:
    """A stale confirm (e.g. a double-click on the previous prompt) must
    not release the prompt that is waiting now."""
    ctx, writer, _, _ = _make_ctx()
    sink, send = _attach_ui(ctx)
    method = Method(name="m", steps=(PromptStep(message="slide to B"),))
    finished = anyio.Event()

    async def confirm() -> None:
        prompt_id = await _open_prompt_id(sink)
        await send.send(OperatorCommand(kind="confirm_prompt", metadata={"prompt_id": "stale"}))
        await send.send(OperatorCommand(kind="pause"))
        await anyio.sleep(0.1)
        assert not finished.is_set()
        await send.send(OperatorCommand(kind="confirm_prompt", metadata={"prompt_id": prompt_id}))

    async def run() -> None:
        await MethodExecutor(ctx=ctx).run_to_completion(method)
        finished.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(confirm)
        tg.start_soon(run)
    assert "method.prompt.acknowledged" in [e["kind"] for e in writer.events]


@pytest.mark.anyio
async def test_prompt_step_republishes_open_tick_while_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(executor_module, "PROMPT_REPUBLISH_S", 0.02)
    ctx, _writer, _, _ = _make_ctx()
    sink, send = _attach_ui(ctx)
    method = Method(name="m", steps=(PromptStep(message="wait"),))

    async def confirm_later() -> None:
        prompt_id = await _open_prompt_id(sink)
        await anyio.sleep(0.15)
        await send.send(OperatorCommand(kind="confirm_prompt", metadata={"prompt_id": prompt_id}))

    async with anyio.create_task_group() as tg:
        tg.start_soon(confirm_later)
        await MethodExecutor(ctx=ctx).run_to_completion(method)
    states = sink.states()
    assert len({prompt_id for _, prompt_id in states}) == 1
    assert [s for s, _ in states].count("open") >= 3
    assert states[-1][0] == "closed"


@pytest.mark.anyio
async def test_prompt_step_without_timeout_waits_when_ui_attached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a UI attached an unset timeout_s means no deadline: the
    headless default must not apply."""
    monkeypatch.setattr(executor_module, "PROMPT_HEADLESS_DEFAULT_TIMEOUT_S", 0.05)
    ctx, writer, _, _ = _make_ctx()
    sink, send = _attach_ui(ctx)
    method = Method(name="m", steps=(PromptStep(message="slow operator"),))

    async def confirm_late() -> None:
        prompt_id = await _open_prompt_id(sink)
        await anyio.sleep(0.2)
        await send.send(OperatorCommand(kind="confirm_prompt", metadata={"prompt_id": prompt_id}))

    async with anyio.create_task_group() as tg:
        tg.start_soon(confirm_late)
        await MethodExecutor(ctx=ctx).run_to_completion(method)
    assert "method.prompt.acknowledged" in [e["kind"] for e in writer.events]


@pytest.mark.anyio
async def test_prompt_step_headless_default_timeout_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(executor_module, "PROMPT_HEADLESS_DEFAULT_TIMEOUT_S", 0.05)
    ctx, writer, _, _ = _make_ctx()
    method = Method(name="m", steps=(PromptStep(message="nobody here"),))
    with pytest.raises(MethodExecutorError, match="timed out"):
        await MethodExecutor(ctx=ctx).run_to_completion(method)
    unanswered = next(e for e in writer.events if e["kind"] == "method.prompt.unanswered")
    assert unanswered["metadata"]["reason"] == "timeout"
    assert unanswered["severity"] == "warning"


@pytest.mark.anyio
async def test_prompt_step_external_stop_returns_and_closes_dialog() -> None:
    ctx, writer, _, stop = _make_ctx()
    sink, _send = _attach_ui(ctx)
    method = Method(name="m", steps=(PromptStep(message="stop me"),))

    async def stop_later() -> None:
        await _open_prompt_id(sink)
        stop.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(stop_later)
        await MethodExecutor(ctx=ctx).run_to_completion(method)
    unanswered = next(e for e in writer.events if e["kind"] == "method.prompt.unanswered")
    assert unanswered["metadata"]["reason"] == "external_stop"
    assert sink.states()[-1][0] == "closed"


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
