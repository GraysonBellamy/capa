"""Tests for :mod:`capa.storage.writer_thread`."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from capa.devices.camera.base import FrameReceipt
from capa.devices.records import (
    ChannelSample,
    DeviceEvent,
    DeviceSnapshot,
    SourceRecord,
)
from capa.storage.writer_thread import (
    DEFAULT_CAPACITY,
    FrameItem,
    WriteEventItem,
    WriterThread,
    WriterThreadError,
)

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Fakes & helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeWriter:
    """Records every call the writer thread makes; mirrors RunBundleWriter's
    record_* / write_event surface without needing a real bundle on disk."""

    samples: list[ChannelSample] = field(default_factory=list)
    sources: list[SourceRecord] = field(default_factory=list)
    events: list[DeviceEvent] = field(default_factory=list)
    snapshots: list[DeviceSnapshot] = field(default_factory=list)
    frames: list[FrameReceipt] = field(default_factory=list)
    write_events: list[dict[str, Any]] = field(default_factory=list)
    raise_on_record_sample: bool = False
    sleep_per_call_s: float = 0.0

    def record_sample(self, sample: ChannelSample) -> None:
        if self.raise_on_record_sample:
            raise RuntimeError("simulated writer crash")
        if self.sleep_per_call_s > 0:
            time.sleep(self.sleep_per_call_s)
        self.samples.append(sample)

    def record_source(self, record: SourceRecord) -> None:
        self.sources.append(record)

    def record_event(self, event: DeviceEvent) -> None:
        self.events.append(event)

    def record_snapshot(self, snapshot: DeviceSnapshot) -> None:
        self.snapshots.append(snapshot)

    def record_frame(self, receipt: FrameReceipt) -> None:
        self.frames.append(receipt)

    def write_event(
        self,
        *,
        kind: str,
        message: str,
        severity: str,
        source: str,
        t_mono_ns: int,
        t_utc: datetime,
        metadata: dict[str, Any] | None,
    ) -> None:
        self.write_events.append(
            {
                "kind": kind,
                "message": message,
                "severity": severity,
                "source": source,
                "t_mono_ns": t_mono_ns,
                "t_utc": t_utc,
                "metadata": metadata,
            }
        )


def _sample(channel: str, t_mono_ns: int, value: float) -> ChannelSample:
    return ChannelSample(
        channel=channel,
        t_mono_ns=t_mono_ns,
        t_mono_s=t_mono_ns / 1e9,
        value=value,
        unit="V",
    )


def _source(record_id: str, t_mono_ns: int = 0) -> SourceRecord:
    return SourceRecord(
        record_id=record_id,
        adapter="test",
        device="dev",
        shape="wide_row",
        t_mono_ns=t_mono_ns,
        t_utc=datetime.now(UTC),
        row={"x": 1.0},
    )


def _event(kind: str = "test") -> DeviceEvent:
    return DeviceEvent(
        adapter="test",
        device="dev",
        t_mono_ns=0,
        t_utc=datetime.now(UTC),
        kind=kind,
        message="msg",
    )


def _snapshot() -> DeviceSnapshot:
    return DeviceSnapshot(
        adapter="test",
        device="dev",
        t_mono_ns=0,
        t_utc=datetime.now(UTC),
        health="ok",
    )


def _frame(idx: int) -> FrameReceipt:
    return FrameReceipt(
        name="cam",
        frame_idx=idx,
        t_mono_ns=idx * 1_000_000,
        t_utc=datetime.now(UTC),
    )


def _wait_for(
    predicate: Callable[[], bool], *, timeout: float = 2.0, interval: float = 0.005
) -> None:
    """Spin-wait helper for assertions against the writer thread's drain."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"predicate not satisfied within {timeout}s")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_start_then_close_clean(self) -> None:
        wt = WriterThread(FakeWriter())
        wt.start()
        assert wt.is_alive
        assert wt.close()
        assert wt.closed
        assert not getattr(wt, "is_alive")  # noqa: B009  defeat mypy property narrowing

    def test_close_without_start_is_safe(self) -> None:
        wt = WriterThread(FakeWriter())
        assert wt.close()
        assert wt.closed

    def test_close_is_idempotent(self) -> None:
        wt = WriterThread(FakeWriter())
        wt.start()
        assert wt.close()
        assert wt.close()  # second close — still True

    def test_double_start_raises(self) -> None:
        wt = WriterThread(FakeWriter())
        wt.start()
        try:
            with pytest.raises(WriterThreadError):
                wt.start()
        finally:
            wt.close()

    def test_capacity_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            WriterThread(FakeWriter(), capacity=0)

    def test_submit_before_start_raises(self) -> None:
        wt = WriterThread(FakeWriter())
        with pytest.raises(WriterThreadError):
            wt.submit_nowait(_sample("a", 0, 1.0))

    def test_submit_after_close_raises(self) -> None:
        wt = WriterThread(FakeWriter())
        wt.start()
        wt.close()
        with pytest.raises(WriterThreadError):
            wt.submit_nowait(_sample("a", 0, 1.0))


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_dispatch_all_item_kinds(self) -> None:
        fake = FakeWriter()
        wt = WriterThread(fake)
        wt.start()
        try:
            wt.submit_nowait(_sample("a", 1, 0.1))
            wt.submit_nowait(_source("rec-1"))
            wt.submit_nowait(_event("connect"))
            wt.submit_nowait(_snapshot())
            wt.submit_nowait(FrameItem(receipt=_frame(0)))
            wt.submit_nowait(
                WriteEventItem(
                    kind="k",
                    message="m",
                    severity="info",
                    source="test",
                    t_mono_ns=0,
                    t_utc=datetime.now(UTC),
                    metadata=None,
                )
            )
        finally:
            assert wt.close()
        assert [s.channel for s in fake.samples] == ["a"]
        assert [r.record_id for r in fake.sources] == ["rec-1"]
        assert [e.kind for e in fake.events] == ["connect"]
        assert len(fake.snapshots) == 1
        assert [f.frame_idx for f in fake.frames] == [0]
        assert [e["kind"] for e in fake.write_events] == ["k"]

    def test_ordering_preserved_under_load(self) -> None:
        fake = FakeWriter()
        wt = WriterThread(fake, capacity=128)
        wt.start()
        try:
            for i in range(2000):
                sample = _sample("c", i, float(i))
                if not wt.submit_nowait(sample):
                    wt.submit_blocking(sample)
        finally:
            assert wt.close()
        assert len(fake.samples) == 2000
        # Ordering must be FIFO because we have one writer thread draining
        # a single Queue.
        assert [s.t_mono_ns for s in fake.samples] == list(range(2000))


# ---------------------------------------------------------------------------
# Async submit + backpressure
# ---------------------------------------------------------------------------


class TestAsyncSubmit:
    async def test_submit_fast_path(self, anyio_backend: str) -> None:
        fake = FakeWriter()
        wt = WriterThread(fake)
        wt.start()
        try:
            await wt.submit(_sample("a", 0, 1.0))
            await wt.record_sample(_sample("a", 1, 2.0))
            await wt.record_event(_event("e"))
            _wait_for(lambda: len(fake.samples) == 2 and len(fake.events) == 1)
        finally:
            assert wt.close()
        assert wt.submit_blocked_count == 0

    async def test_submit_blocking_path_when_inbox_fills(self, anyio_backend: str) -> None:
        # Tiny capacity + a slow fake writer forces the inbox to fill and
        # the async submit to fall through to submit_blocking.
        fake = FakeWriter(sleep_per_call_s=0.01)
        wt = WriterThread(fake, capacity=2)
        wt.start()
        try:
            for i in range(20):
                await wt.submit(_sample("a", i, float(i)))
        finally:
            assert wt.close()
        assert len(fake.samples) == 20
        # At least one submit had to wait. The exact count depends on
        # scheduling, but capacity=2 + sleep_per_call=10ms guarantees
        # backpressure kicked in repeatedly.
        assert wt.submit_blocked_count > 0


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_thread_exception_surfaces_on_next_submit(self) -> None:
        fake = FakeWriter(raise_on_record_sample=True)
        wt = WriterThread(fake)
        wt.start()
        try:
            # The first sample will crash the drain thread. We must surface
            # that on the next submission.
            wt.submit_nowait(_sample("a", 0, 1.0))
            _wait_for(lambda: not wt.is_alive)
            with pytest.raises(WriterThreadError):
                wt.submit_nowait(_sample("a", 1, 2.0))
        finally:
            with pytest.raises(WriterThreadError):
                wt.close()

    def test_close_re_raises_thread_exception(self) -> None:
        fake = FakeWriter(raise_on_record_sample=True)
        wt = WriterThread(fake)
        wt.start()
        wt.submit_nowait(_sample("a", 0, 1.0))
        _wait_for(lambda: not wt.is_alive)
        with pytest.raises(WriterThreadError):
            wt.close()


# ---------------------------------------------------------------------------
# Backpressure + metrics
# ---------------------------------------------------------------------------


class TestMetrics:
    def test_depth_high_water_tracks_inbox(self) -> None:
        # Pause the drain by routing every record_sample into a sleep that
        # outpaces the submit rate; that lets the inbox fill.
        slow = FakeWriter(sleep_per_call_s=0.02)
        wt = WriterThread(slow, capacity=64)
        wt.start()
        try:
            for i in range(50):
                wt.submit_nowait(_sample("a", i, float(i)))
        finally:
            assert wt.close()
        assert wt.depth_high_water >= 2
        snap = wt.snapshot()
        assert snap["capacity"] == 64.0
        assert snap["depth_max"] == float(wt.depth_high_water)

    def test_submit_nowait_returns_false_when_full(self) -> None:
        # capacity=1 + a paused drain ensures the second put_nowait sees a
        # full inbox. Pause by blocking the thread inside record_sample.
        gate = threading.Event()
        captured: list[ChannelSample] = []

        class GatedWriter:
            def record_sample(self, sample: ChannelSample) -> None:
                gate.wait(timeout=2.0)
                captured.append(sample)

            def record_source(self, record: SourceRecord) -> None: ...
            def record_event(self, event: DeviceEvent) -> None: ...
            def record_snapshot(self, snapshot: DeviceSnapshot) -> None: ...
            def record_frame(self, receipt: FrameReceipt) -> None: ...
            def write_event(self, **kwargs: Any) -> None: ...

        wt = WriterThread(GatedWriter(), capacity=1)
        wt.start()
        try:
            # First item enters the inbox, thread picks it up but is gated.
            # Second + third put_nowait should fill the inbox and then fail.
            assert wt.submit_nowait(_sample("a", 0, 0.0)) is True
            # Allow the drain loop to pull item 0 — but it's now blocked
            # inside record_sample by the gate, so the next put will hit
            # the empty inbox first, then fill it.
            _wait_for(lambda: wt.depth == 0, timeout=0.5)
            assert wt.submit_nowait(_sample("a", 1, 1.0)) is True
            assert wt.submit_nowait(_sample("a", 2, 2.0)) is False
        finally:
            gate.set()
            assert wt.close()
        # All three samples should have been delivered eventually (only the
        # third submit_nowait failed; the harness never queued it).
        assert len(captured) == 2

    def test_last_accept_monotonic_advances_on_each_dispatch(self) -> None:
        """The saturation signal must advance for every accepted item.

        The Conductor reads this to distinguish "writer healthy but
        empty" from "writer stuck mid-flush".
        """
        wt = WriterThread(FakeWriter())
        before_start = wt.last_accept_monotonic_ns
        wt.start()
        try:
            for i in range(3):
                wt.submit_nowait(_sample("a", i, float(i)))
                # Each accept must move the timestamp strictly forward; spin
                # until the drain visibly advances past the previous reading.
                prev: int = wt.last_accept_monotonic_ns

                def _advanced(p: int = prev) -> bool:
                    return wt.last_accept_monotonic_ns > p

                _wait_for(_advanced, timeout=1.0)
        finally:
            assert wt.close()
        assert wt.last_accept_monotonic_ns > before_start

    def test_last_accept_monotonic_stalls_when_dispatch_blocks(self) -> None:
        """When the drain is wedged inside a write, the signal must stop
        advancing — that's exactly the condition the saturation monitor
        watches for."""
        gate = threading.Event()

        class GatedWriter:
            def record_sample(self, sample: ChannelSample) -> None:
                gate.wait(timeout=2.0)

            def record_source(self, record: SourceRecord) -> None: ...
            def record_event(self, event: DeviceEvent) -> None: ...
            def record_snapshot(self, snapshot: DeviceSnapshot) -> None: ...
            def record_frame(self, receipt: FrameReceipt) -> None: ...
            def write_event(self, **kwargs: Any) -> None: ...

        wt = WriterThread(GatedWriter(), capacity=4)
        wt.start()
        try:
            wt.submit_nowait(_sample("a", 0, 0.0))
            # Let the drain pop the item and enter the gated record_sample.
            _wait_for(lambda: wt.depth == 0, timeout=0.5)
            stalled_at = wt.last_accept_monotonic_ns
            # While the drain is wedged, more items pile up but no accept
            # advances. Wait a small interval and assert no progress.
            wt.submit_nowait(_sample("a", 1, 1.0))
            time.sleep(0.05)
            assert wt.last_accept_monotonic_ns == stalled_at
            assert wt.depth >= 1
        finally:
            gate.set()
            assert wt.close()

    def test_last_accept_monotonic_in_snapshot(self) -> None:
        wt = WriterThread(FakeWriter())
        wt.start()
        try:
            wt.submit_nowait(_sample("a", 0, 0.0))
            _wait_for(lambda: wt.depth == 0, timeout=0.5)
            snap = wt.snapshot()
            assert "last_accept_monotonic_ns" in snap
            assert snap["last_accept_monotonic_ns"] == float(wt.last_accept_monotonic_ns)
        finally:
            assert wt.close()


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_default_capacity_is_4096() -> None:
    assert DEFAULT_CAPACITY == 4096
