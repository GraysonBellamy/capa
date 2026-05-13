"""Tests for :mod:`capa.runtime.saturation` — :class:`SaturationMonitor`.

Two signal sources, one deadline knob, one callback. Tests inject a fake
clock so deadlines fire deterministically — wall-clock timing tests would
either be slow or flaky.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from capa.runtime.saturation import (
    SaturationEvent,
    SaturationMonitor,
    WriterSaturationSource,
)

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeWriterSignal:
    """Stand-in for the writer-thread's saturation properties."""

    last_accept_monotonic_ns: int = 0
    depth: int = 0


@dataclass
class FakeBridgeMetrics:
    """Single read-only field on the metrics object the monitor cares about."""

    blocked_since_ms: float | None = None


@dataclass
class FakeBridge:
    """Stand-in for :class:`ThreadBridge` — only exposes ``metrics``."""

    metrics: FakeBridgeMetrics = field(default_factory=FakeBridgeMetrics)


class _Clock:
    """Manually-advanced monotonic clock."""

    def __init__(self) -> None:
        self.now_ns = 0

    def __call__(self) -> int:
        return self.now_ns

    def advance(self, seconds: float) -> None:
        self.now_ns += int(seconds * 1e9)


@dataclass
class _RecordingCallback:
    events: list[SaturationEvent] = field(default_factory=list)

    async def __call__(self, ev: SaturationEvent) -> None:
        self.events.append(ev)


# ---------------------------------------------------------------------------
# Sanity / no-trip behaviour
# ---------------------------------------------------------------------------


class TestNoTrip:
    async def test_stop_event_returns_cleanly(self) -> None:
        """Setting the stop event must retire the monitor; the callback
        must NOT fire."""
        cb = _RecordingCallback()
        stop = asyncio.Event()
        mon = SaturationMonitor(
            bridges={},
            writer=FakeWriterSignal(depth=0),
            on_saturated=cb,
            deadline_s=5.0,
            poll_period_s=0.01,
            stop_event=stop,
        )

        async def _retire() -> None:
            await asyncio.sleep(0.03)
            stop.set()

        await asyncio.gather(mon.run(), _retire())
        assert not mon.fired
        assert cb.events == []

    async def test_empty_writer_inbox_never_trips(self) -> None:
        """``depth == 0`` means the writer is healthy regardless of
        ``last_accept_monotonic_ns``."""
        cb = _RecordingCallback()
        stop = asyncio.Event()
        clk = _Clock()
        # Writer last accepted "long ago" but the inbox is empty.
        writer = FakeWriterSignal(last_accept_monotonic_ns=0, depth=0)
        clk.advance(60.0)
        mon = SaturationMonitor(
            bridges={},
            writer=writer,
            on_saturated=cb,
            deadline_s=1.0,
            poll_period_s=0.01,
            stop_event=stop,
            clock_monotonic_ns=clk,
        )

        async def _retire() -> None:
            await asyncio.sleep(0.05)
            stop.set()

        await asyncio.gather(mon.run(), _retire())
        assert not mon.fired

    async def test_bridge_not_blocked_never_trips(self) -> None:
        cb = _RecordingCallback()
        stop = asyncio.Event()
        bridge = FakeBridge(metrics=FakeBridgeMetrics(blocked_since_ms=None))
        mon = SaturationMonitor(
            bridges={"r1": bridge},  # type: ignore[dict-item]
            writer=None,
            on_saturated=cb,
            deadline_s=1.0,
            poll_period_s=0.01,
            stop_event=stop,
        )

        async def _retire() -> None:
            await asyncio.sleep(0.05)
            stop.set()

        await asyncio.gather(mon.run(), _retire())
        assert not mon.fired


# ---------------------------------------------------------------------------
# Trip on bridge block
# ---------------------------------------------------------------------------


class TestBridgeBlocked:
    async def test_sustained_bridge_block_trips(self) -> None:
        """A bridge whose producer has been blocked for longer than the
        deadline must trip the monitor exactly once with the correct
        reason tag."""
        cb = _RecordingCallback()
        bridge = FakeBridge(metrics=FakeBridgeMetrics(blocked_since_ms=1500.0))
        mon = SaturationMonitor(
            bridges={"serial:COM6": bridge},  # type: ignore[dict-item]
            writer=None,
            on_saturated=cb,
            deadline_s=1.0,  # 1500ms > 1.0s
            poll_period_s=0.01,
        )
        await mon.run()
        assert mon.fired
        assert len(cb.events) == 1
        ev = cb.events[0]
        assert ev.reason == "worker_serial:COM6_outbound_saturated"
        assert ev.details["resource_id"] == "serial:COM6"
        assert ev.details["blocked_s"] == pytest.approx(1.5)

    async def test_block_under_deadline_does_not_trip(self) -> None:
        cb = _RecordingCallback()
        stop = asyncio.Event()
        bridge = FakeBridge(metrics=FakeBridgeMetrics(blocked_since_ms=500.0))
        mon = SaturationMonitor(
            bridges={"r": bridge},  # type: ignore[dict-item]
            writer=None,
            on_saturated=cb,
            deadline_s=1.0,
            poll_period_s=0.01,
            stop_event=stop,
        )

        async def _retire() -> None:
            await asyncio.sleep(0.05)
            stop.set()

        await asyncio.gather(mon.run(), _retire())
        assert not mon.fired

    async def test_fires_once_only(self) -> None:
        """Once tripped, the run loop returns; subsequent ticks don't
        re-fire the callback."""
        cb = _RecordingCallback()
        bridge = FakeBridge(metrics=FakeBridgeMetrics(blocked_since_ms=99999.0))
        mon = SaturationMonitor(
            bridges={"r": bridge},  # type: ignore[dict-item]
            writer=None,
            on_saturated=cb,
            deadline_s=1.0,
            poll_period_s=0.01,
        )
        await mon.run()
        assert len(cb.events) == 1


# ---------------------------------------------------------------------------
# Trip on writer stall
# ---------------------------------------------------------------------------


class TestWriterStall:
    async def test_writer_with_nonzero_depth_and_no_progress_trips(self) -> None:
        cb = _RecordingCallback()
        clk = _Clock()
        writer = FakeWriterSignal(last_accept_monotonic_ns=0, depth=5)
        # Advance the clock past the deadline.
        clk.advance(2.0)
        mon = SaturationMonitor(
            bridges={},
            writer=writer,
            on_saturated=cb,
            deadline_s=1.0,
            poll_period_s=0.005,
            clock_monotonic_ns=clk,
        )
        await mon.run()
        assert mon.fired
        ev = cb.events[0]
        assert ev.reason == "writer_inbox_stalled"
        assert ev.details["depth"] == 5
        # 2.0s passed since accept "happened at 0".
        assert float(ev.details["since_last_accept_s"]) >= 1.0

    async def test_writer_progresses_does_not_trip(self) -> None:
        """If the writer keeps advancing ``last_accept_monotonic_ns`` on
        each tick, the monitor must NOT trip even with a steady-state
        non-empty inbox."""
        cb = _RecordingCallback()
        stop = asyncio.Event()
        clk = _Clock()
        writer = FakeWriterSignal(last_accept_monotonic_ns=0, depth=3)
        mon = SaturationMonitor(
            bridges={},
            writer=writer,
            on_saturated=cb,
            deadline_s=1.0,
            poll_period_s=0.01,
            stop_event=stop,
            clock_monotonic_ns=clk,
        )

        async def _drive() -> None:
            # Advance both the clock AND the writer's accept stamp in
            # lock-step. The writer never appears stalled.
            for _ in range(20):
                await asyncio.sleep(0.005)
                clk.advance(0.1)
                writer.last_accept_monotonic_ns = clk.now_ns
            stop.set()

        await asyncio.gather(mon.run(), _drive())
        assert not mon.fired


# ---------------------------------------------------------------------------
# Both signals present — bridge tripping wins if first
# ---------------------------------------------------------------------------


class TestBothSignals:
    async def test_bridge_trip_takes_precedence_when_first(self) -> None:
        cb = _RecordingCallback()
        bridge = FakeBridge(metrics=FakeBridgeMetrics(blocked_since_ms=2000.0))
        writer = FakeWriterSignal(last_accept_monotonic_ns=0, depth=0)
        mon = SaturationMonitor(
            bridges={"r": bridge},  # type: ignore[dict-item]
            writer=writer,
            on_saturated=cb,
            deadline_s=1.0,
            poll_period_s=0.005,
        )
        await mon.run()
        assert mon.fired
        assert "outbound_saturated" in cb.events[0].reason


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_rejects_nonpositive_deadline(self) -> None:
        async def _cb(_: SaturationEvent) -> None: ...

        with pytest.raises(ValueError, match="deadline_s"):
            SaturationMonitor(
                bridges={},
                writer=None,
                on_saturated=_cb,
                deadline_s=0.0,
            )

    def test_rejects_nonpositive_poll_period(self) -> None:
        async def _cb(_: SaturationEvent) -> None: ...

        with pytest.raises(ValueError, match="poll_period_s"):
            SaturationMonitor(
                bridges={},
                writer=None,
                on_saturated=_cb,
                deadline_s=1.0,
                poll_period_s=0.0,
            )

    def test_writer_protocol_compatibility_check(self) -> None:
        """A WriterThread (which has both attrs) satisfies the protocol."""
        from capa.storage.writer_thread import WriterThread

        fake_writer: Any = type("W", (), {"record_sample": lambda *_: None})()
        wt = WriterThread(fake_writer)
        assert isinstance(wt, WriterSaturationSource)
