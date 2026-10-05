"""A manual zero or span driven through :class:`capa.devices.fuji.FujiAdapter`.

Every run is against fujilib's simulated analyzer, whose front panel follows
the bench unit: the keys the adapter's calibration task sends really move it
through channel selection, the wait step and the calibration. The steadiness
rule is shortened so the gas settles in a fraction of a second.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import fujilib
import pytest
from fujilib import Analyzer
from fujilib.devices.session import SessionState
from fujilib.devices.steadiness import SteadinessRule
from fujilib.testing import DEFAULT_ZPA_BANK, FaultKind, MockAnalyzer, mock_transport

from capa.devices.adapter import Capability, CommandResult, DeviceCommand
from capa.devices.fuji import (
    SETTINGS_MAX_AGE_S,
    FujiAdapter,
    FujiChannelSettings,
    FujiRange,
    FujiStateSnapshot,
    _calibration_result,
    _RefusedError,
)
from capa.devices.fuji_calibration import IDLE, CalibrationStatus
from capa.devices.records import ChannelSample, DeviceEvent, SourceRecord
from capa.runtime.shutdown import WorkerShutdownConfig
from tests._adapter_helpers import make_start_ctx
from tests.unit.test_fuji_adapter import _gas_channels

pytestmark = pytest.mark.anyio

CHANNEL_MAP = {"CH1": "co2", "CH2": "co", "CH3": "o2"}
FC06 = 6
KEY_REGISTER = 0x07D0
ESC, ENT, ZERO, SPAN, DOWN = 0x10, 0x20, 0x40, 0x80, 0x08
RULE = SteadinessRule(window_s=0.15, response_factor=0, timeout_s=3)


def _bench() -> MockAnalyzer:
    """The bench analyzer, its panel offering channels 1-3, a calibration taking 0.05 s."""
    return MockAnalyzer(
        dataclasses.replace(DEFAULT_ZPA_BANK, panel_channels=(1, 2, 3), manual_calibration_s=0.05)
    )


@asynccontextmanager
async def _adapter_on(mock: MockAnalyzer) -> AsyncIterator[FujiAdapter]:
    async with mock_transport(mock) as (transport, _line):

        async def factory() -> Analyzer:
            return await fujilib.open_device(transport, channel_map=CHANNEL_MAP, timeout=0.25)

        adapter = FujiAdapter(
            name="analyzer",
            port="mock://zp",
            channel_map=CHANNEL_MAP,
            rate_hz=5.0,
            snapshot_period_s=1e6,
            analyzer_factory=factory,
        )
        adapter._steadiness_rule = RULE
        adapter._calibration_interval_s = 0.02
        adapter.configure_channels(_gas_channels())
        await adapter.open()
        mock.clear()
        try:
            yield adapter
        finally:
            await adapter.close()


def _keys(mock: MockAnalyzer) -> list[int]:
    """The keys written to 42001 that acted on the panel."""
    return [key for key, _ in mock.remote_keys]


async def _begin(adapter: FujiAdapter, kind: str = "zero", **gas: Any) -> CommandResult:
    gas = gas or {"gas_value": 0.0}
    return await adapter.begin_calibration("CH3", kind, issued_by="op", confirmed_by="op", **gas)


async def _until(adapter: FujiAdapter, done: Any, *, timeout_s: float = 5.0) -> CalibrationStatus:
    """Poll the adapter's calibration status until ``done(status)``."""
    loop = asyncio.get_running_loop()
    give_up_at = loop.time() + timeout_s
    while True:
        status = adapter.calibration
        if done(status):
            return status
        assert loop.time() < give_up_at, f"still {status}"
        await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# A run
# ---------------------------------------------------------------------------


def test_the_adapter_declares_gas_calibration() -> None:
    adapter = FujiAdapter(name="a", port="COM8", channel_map=CHANNEL_MAP)
    assert Capability.HAS_GAS_CALIBRATION in adapter.capabilities
    assert adapter.calibration is IDLE


async def test_a_zero_from_begin_to_commit() -> None:
    mock = _bench()
    async with _adapter_on(mock) as adapter:
        begun = await _begin(adapter, gas_value=0.0, gas_label="N2, cylinder 1234")
        assert begun.accepted, begun.detail
        assert begun.detail == (
            "calibration_begin: zero of O2: O2 0–21 vol% against 0 vol%; on the wait step"
        )
        waiting = adapter.calibration
        assert (waiting.state, waiting.channel, waiting.kind) == ("waiting", "CH3", "zero")
        assert mock.register("display.calibration_step") != (0,)

        # The operator opens the zero-gas valve; the task watches the reading settle.
        mock.flow("CH3", 0.0, tau_s=0.02)
        steady = await _until(adapter, lambda s: s.steady)
        assert steady.readings == {"CH3": 0.0}
        assert steady.reasons

        committed = await adapter.commit_calibration(issued_by="op", confirmed_by="op")
        ended = adapter.calibration
    assert committed.accepted, committed.detail
    assert committed.detail == "zero of O2: completed"
    assert (ended.state, ended.outcome, ended.clean, ended.error) == (
        "ended",
        "completed",
        True,
        None,
    )
    # ZERO, the cursor down to CH3, ENT to select, ENT to calibrate: nothing else.
    assert _keys(mock) == [ZERO, DOWN, ENT, ENT]
    assert mock.register("display.calibration_step") == (0,)
    record = ended.record
    assert record is not None
    assert record["format"] == "fujilib-calibration/1"
    assert (record["source"], record["kind"], record["outcome"]) == ("remote", "zero", "completed")
    assert record["operator"] == "op"
    assert record["named_gas"] == {
        "CH3": {"value": 0.0, "unit": None, "label": "N2, cylinder 1234"}
    }
    assert record["calibrating_key_sent"] is True


async def test_cancel_leaves_the_wait_step_without_calibrating() -> None:
    mock = _bench()
    async with _adapter_on(mock) as adapter:
        assert (await _begin(adapter)).accepted
        cancelled = await adapter.cancel_calibration(issued_by="op", authorization_id="run-1")
        ended = adapter.calibration
        again = await adapter.cancel_calibration(issued_by="op", confirmed_by="op")
    assert cancelled.accepted, cancelled.detail
    assert cancelled.detail == "zero of O2: cancelled"
    assert (ended.state, ended.outcome, ended.clean) == ("ended", "cancelled", True)
    assert _keys(mock) == [ZERO, DOWN, ENT, ESC]
    assert ended.record is not None
    assert ended.record["calibrating_key_sent"] is False
    assert not again.accepted
    assert "no calibration is under way" in again.detail


# ---------------------------------------------------------------------------
# What is refused
# ---------------------------------------------------------------------------


async def test_begin_and_commit_need_a_person() -> None:
    mock = _bench()
    by_run = {"issued_by": "op", "authorization_id": "run-1"}
    async with _adapter_on(mock) as adapter:
        begin = await adapter.command(
            DeviceCommand(
                kind="calibration_begin",
                payload={"channel": "CH3", "kind": "zero", "gas_value": 0.0},
                **by_run,
            )
        )
        assert mock.transactions() == []  # nothing was read or sent
        assert (await _begin(adapter)).accepted
        mock.flow("CH3", 0.0, tau_s=0.02)
        _ = await _until(adapter, lambda s: s.steady)
        commit = await adapter.command(DeviceCommand(kind="calibration_commit", **by_run))
        assert adapter.calibration.state == "waiting"
        _ = await adapter.cancel_calibration(issued_by="op", confirmed_by="op")
    assert not begin.accepted
    assert "begun by a person" in begin.detail
    assert not commit.accepted
    assert "person's confirmation" in commit.detail
    assert _keys(mock) == [ZERO, DOWN, ENT, ESC]  # the calibrating ENT never went out


async def test_commit_is_refused_while_the_gas_is_not_steady() -> None:
    mock = _bench()
    async with _adapter_on(mock) as adapter:
        assert (await _begin(adapter)).accepted
        # The inlet still carries air: CH3 reads 20 vol%, nowhere near the zero gas.
        _ = await _until(adapter, lambda s: s.steady is not None)
        refused = await adapter.commit_calibration(issued_by="op", confirmed_by="op")
        after = adapter.calibration
        _ = await adapter.cancel_calibration(issued_by="op", confirmed_by="op")
    assert not refused.accepted
    assert "not steady" in refused.detail
    assert after.state == "waiting"  # the run goes on; the operator can wait or cancel
    assert ENT not in _keys(mock)[3:]


async def test_one_calibration_at_a_time() -> None:
    mock = _bench()
    async with _adapter_on(mock) as adapter:
        assert (await _begin(adapter)).accepted
        second = await _begin(adapter)
        _ = await adapter.cancel_calibration(issued_by="op", confirmed_by="op")
        third = await _begin(adapter)
        _ = await adapter.cancel_calibration(issued_by="op", confirmed_by="op")
    assert not second.accepted
    assert "already under way" in second.detail
    assert third.accepted


@pytest.mark.parametrize(
    ("gas", "match"),
    [
        ({"gas_value": 5.0, "gas_unit": "vol%"}, "refused"),  # not the zero-gas setting
        ({"gas_value": 5.0}, "needs its unit"),
        ({"gas_value": "plenty"}, "finite number"),
    ],
)
async def test_a_gas_that_does_not_fit_is_refused_and_the_panel_left_alone(
    gas: dict[str, Any], match: str
) -> None:
    mock = _bench()
    async with _adapter_on(mock) as adapter:
        result = await _begin(adapter, **gas)
        status = adapter.calibration
    assert not result.accepted
    assert match in result.detail
    assert _keys(mock) == []
    assert status.state in ("idle", "ended")
    assert mock.register("display.calibration_step") == (0,)


async def test_commit_and_cancel_without_a_run_are_refused() -> None:
    async with _adapter_on(_bench()) as adapter:
        commit = await adapter.commit_calibration(issued_by="op", confirmed_by="op")
        cancel = await adapter.cancel_calibration(issued_by="op", confirmed_by="op")
    assert not commit.accepted
    assert not cancel.accepted
    assert "no calibration is under way" in commit.detail


# ---------------------------------------------------------------------------
# Ends that nobody asked for
# ---------------------------------------------------------------------------


async def test_close_mid_run_returns_the_panel_to_measurement() -> None:
    mock = _bench()
    async with _adapter_on(mock) as adapter:
        assert (await _begin(adapter)).accepted
        await adapter.close()
        ended = adapter.calibration
    assert ended.state == "ended"
    assert ended.clean is True
    assert ended.error is not None
    assert "adapter closed" in ended.error
    assert _keys(mock) == [ZERO, DOWN, ENT, ESC]
    assert mock.register("display.calibration_step") == (0,)


async def test_close_keeps_inside_the_workers_grace_when_the_analyzer_is_silent() -> None:
    mock = _bench()
    async with _adapter_on(mock) as adapter:
        analyzer = adapter._analyzer
        assert analyzer is not None
        assert (await _begin(adapter)).accepted
        # The analyzer stops answering: the cleanup cannot return the panel.
        _ = mock.inject(FaultKind.DROP, times=None)
        loop = asyncio.get_running_loop()
        started = loop.time()
        # The worker gives an adapter this long to close, then cancels it.
        await asyncio.wait_for(
            adapter.close(), timeout=WorkerShutdownConfig().adapter_close_grace_s
        )
        took = loop.time() - started
        ended = adapter.calibration
    assert took < WorkerShutdownConfig().adapter_close_grace_s
    assert analyzer.session.state is SessionState.CLOSED  # the port was still released
    assert ended.state == "ended"
    assert ended.clean is False
    assert ended.error is not None
    assert "adapter closed" in ended.error


async def test_a_close_that_is_cut_short_still_releases_the_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = _bench()
    async with _adapter_on(mock) as adapter:
        analyzer = adapter._analyzer
        assert analyzer is not None
        assert (await _begin(adapter)).accepted
        run = adapter._calibration
        assert run is not None

        async def never() -> None:
            await asyncio.sleep(60)

        monkeypatch.setattr(run, "close", never)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(adapter.close(), timeout=0.2)
        assert analyzer.session.state is SessionState.CLOSED
        monkeypatch.undo()
        await run.close()


async def test_a_run_starting_cancels_a_calibration_left_on_its_wait_step() -> None:
    mock = _bench()
    async with _adapter_on(mock) as adapter:
        assert (await _begin(adapter)).accepted
        await adapter.start(make_start_ctx())
        ended = adapter.calibration
        queued = list(adapter._events)
    assert (ended.state, ended.outcome, ended.clean) == ("ended", "cancelled", True)
    assert ended.error == "cancelled because a run started"
    assert _keys(mock) == [ZERO, DOWN, ENT, ESC]
    # Its end is the run's first event.
    assert [(e.kind, e.severity, e.metadata["outcome"]) for e in queued] == [
        ("calibration", "info", "cancelled")
    ]
    assert "cancelled because a run started" in queued[0].message


async def test_a_task_that_dies_is_reported_and_not_taken_for_the_wait_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = _bench()
    async with _adapter_on(mock) as adapter:
        analyzer = adapter._analyzer
        assert analyzer is not None

        def broken(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            raise RuntimeError("boom")

        monkeypatch.setattr(analyzer, "manual_calibration", broken)
        result = await _begin(adapter)
        status = adapter.calibration
    assert not result.accepted
    assert "ended before its wait step" in result.detail
    assert (status.state, status.error) == ("ended", "RuntimeError: boom")
    assert _keys(mock) == []


def test_a_commit_whose_run_was_cancelled_under_it_is_not_accepted() -> None:
    cancelled = CalibrationStatus(
        state="ended", channel="CH3", kind="zero", outcome="cancelled", clean=True
    )
    assert _calibration_result(cancelled) == "zero of CH3: cancelled"
    with pytest.raises(_RefusedError, match="nothing was calibrated"):
        _ = _calibration_result(cancelled, committing=True)
    completed = dataclasses.replace(cancelled, outcome="completed")
    assert _calibration_result(completed, committing=True) == "zero of CH3: completed"


async def test_a_run_left_alone_cancels_itself() -> None:
    mock = _bench()
    async with _adapter_on(mock) as adapter:
        adapter._calibration_timeout_s = 0.3
        assert (await _begin(adapter)).accepted
        ended = await _until(adapter, lambda s: s.state == "ended")
    assert ended.outcome == "cancelled"
    assert ended.clean is True
    assert ended.error is not None
    assert "no decision within 0.3 s" in ended.error
    assert _keys(mock) == [ZERO, DOWN, ENT, ESC]


async def test_an_operator_leaving_the_wait_step_at_the_panel_ends_the_run() -> None:
    mock = _bench()
    async with _adapter_on(mock) as adapter:
        assert (await _begin(adapter)).accepted
        mock.press(ESC)  # someone at the analyzer presses ESC
        ended = await _until(adapter, lambda s: s.state == "ended")
        commit = await adapter.commit_calibration(issued_by="op", confirmed_by="op")
    assert ended.error is not None
    assert ended.clean is True
    assert not commit.accepted


# ---------------------------------------------------------------------------
# With a recording running
# ---------------------------------------------------------------------------


async def test_a_calibration_during_a_stream_is_one_event_and_marks_the_rows() -> None:
    mock = _bench()
    emissions: list[Any] = []
    async with _adapter_on(mock) as adapter:
        await adapter.start(make_start_ctx())

        async def consume() -> None:
            async for emission in adapter.stream():
                emissions.append(emission)

        stream = asyncio.get_running_loop().create_task(consume())
        try:
            assert (await _begin(adapter)).accepted
            mock.flow("CH3", 0.0, tau_s=0.02)
            _ = await _until(adapter, lambda s: s.steady)
            # Let the recording see the wait step before the calibration ends it.
            await asyncio.sleep(0.5)
            committed = await adapter.commit_calibration(issued_by="op", confirmed_by="op")
            await asyncio.sleep(0.5)
        finally:
            await adapter.stop()
            await asyncio.wait_for(stream, timeout=5)
    assert committed.accepted, committed.detail
    records = [e for e in emissions if isinstance(e, SourceRecord)]
    assert all(r.row["error_type"] is None for r in records)  # the port was shared cleanly
    statuses = {
        e.status for e in emissions if isinstance(e, ChannelSample) and e.channel == "gas.o2"
    }
    assert "calibrating" in statuses
    calibrations = [e for e in emissions if isinstance(e, DeviceEvent) and e.kind == "calibration"]
    # The run reports itself; the stream's own watch of the panel does not report it again.
    assert len(calibrations) == 1
    event = calibrations[0]
    assert (event.metadata["source"], event.metadata["outcome"]) == ("remote", "completed")
    assert event.metadata["record"]["format"] == "fujilib-calibration/1"
    assert event.severity == "info"


# ---------------------------------------------------------------------------
# The card's read-back
# ---------------------------------------------------------------------------


async def test_read_state_snapshot() -> None:
    mock = _bench()
    closed = FujiAdapter(name="analyzer", port="COM8", channel_map=CHANNEL_MAP)
    assert await closed.read_state_snapshot() is None
    async with _adapter_on(mock) as adapter:
        idle = await adapter.read_state_snapshot()
        assert (await _begin(adapter)).accepted
        waiting = await adapter.read_state_snapshot()
        _ = await adapter.cancel_calibration(issued_by="op", confirmed_by="op")
    assert isinstance(idle, FujiStateSnapshot)
    assert idle.calibration is IDLE
    assert [(r.channel, r.gas, r.unit, r.state) for r in idle.readings] == [
        ("CH1", "co2", "vol%", "ok"),
        ("CH2", "co", "vol%", "ok"),
        ("CH3", "o2", "vol%", "ok"),
    ]
    assert idle.channels == (
        FujiChannelSettings(
            channel="CH1",
            gas="co2",
            name="CO2",
            ranges=(FujiRange(1, "vol%", 10.0, 0.0, 0.2),),
            current_range=1,
            range_method="manual",
            response_time_s=15,
        ),
        FujiChannelSettings(
            channel="CH2",
            gas="co",
            name="CO",
            ranges=(FujiRange(1, "vol%", 1.0, 0.0, 0.02),),
            current_range=1,
            range_method="manual",
            response_time_s=15,
        ),
        FujiChannelSettings(
            channel="CH3",
            gas="o2",
            name="O2",
            ranges=(FujiRange(1, "vol%", 21.0, 0.0, 20.95), FujiRange(2, "vol%", 25.0, 0.0, 20.01)),
            current_range=1,
            range_method="manual",
            response_time_s=15,
        ),
    )
    assert [r.name for r in idle.channels[2].ranges] == ["0–21 vol%", "0–25 vol%"]
    assert (idle.output_hold, idle.hold_mode) == (False, "last_value")
    assert waiting is not None
    assert waiting.calibration.state == "waiting"
    assert waiting.calibration.channel_name == "O2"
    assert {r.channel: r.state for r in waiting.readings}["CH3"] == "calibrating"


async def test_old_settings_are_read_again(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = _bench()
    async with _adapter_on(mock) as adapter:
        # Changed at the front panel: the cached settings still say 15 s.
        mock.set_register("response_time.o2", 20)
        fresh = await adapter.read_state_snapshot()
        reads: list[str] = []
        analyzer = adapter._analyzer
        assert analyzer is not None
        read_metadata = analyzer.read_metadata

        async def counted(*args: Any, **kwargs: Any) -> Any:
            reads.append("metadata")
            return await read_metadata(*args, **kwargs)

        monkeypatch.setattr(analyzer, "read_metadata", counted)
        still = await adapter.read_state_snapshot()
        adapter._settings_read_at -= SETTINGS_MAX_AGE_S + 1
        old = await adapter.read_state_snapshot()
    assert fresh is not None and still is not None and old is not None
    assert fresh.channels[2].response_time_s == 15
    assert still.channels[2].response_time_s == 15
    assert reads == ["metadata"]
    assert old.channels[2].response_time_s == 20


async def test_the_read_back_during_a_calibration_costs_no_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = _bench()
    async with _adapter_on(mock) as adapter:
        analyzer = adapter._analyzer
        assert analyzer is not None
        assert (await _begin(adapter)).accepted
        _ = await _until(adapter, lambda s: s.steady is not None)

        async def polled(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            raise AssertionError("the read-back polled the analyzer")

        monkeypatch.setattr(analyzer, "poll", polled)
        # The calibration's own reads of the wait step are what is shown.
        snap = await adapter.read_state_snapshot()
        _ = await adapter.cancel_calibration(issued_by="op", confirmed_by="op")
    assert snap is not None
    assert {r.channel: r.state for r in snap.readings}["CH3"] == "calibrating"


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


async def test_the_plan_is_read_without_touching_the_panel() -> None:
    mock = _bench()
    async with _adapter_on(mock) as adapter:
        plan = await adapter.command(
            DeviceCommand(
                kind="calibration_plan",
                payload={"channel": "CH3", "kind": "span"},
                issued_by="op",
                authorization_id="run-1",
            )
        )
        bad = await adapter.command(
            DeviceCommand(
                kind="calibration_plan",
                payload={"channel": "CH3", "kind": "sideways"},
                issued_by="op",
                confirmed_by="op",
            )
        )
        writes = [t for t in mock.transactions() if t[0] in (FC06, 16)]
    assert plan.accepted, plan.detail
    assert plan.detail.startswith("calibration_plan: span of O2: O2 0–21 vol% against ")
    assert not bad.accepted
    assert writes == []
    assert adapter.calibration is IDLE


async def test_read_state_snapshot_survives_a_failed_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _adapter_on(_bench()) as adapter:
        analyzer = adapter._analyzer
        assert analyzer is not None

        async def dead(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            raise fujilib.FujiModbusTimeoutError("no reply")

        monkeypatch.setattr(analyzer, "poll", dead)
        snap = await adapter.read_state_snapshot()
    assert snap is not None
    assert snap.readings == ()
    # The settings as last read are still shown.
    assert {c.name: c.response_time_s for c in snap.channels} == {"CO2": 15, "CO": 15, "O2": 15}
