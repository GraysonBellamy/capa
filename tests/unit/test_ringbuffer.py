"""Tests for :mod:`capa.core.ringbuffer`."""

from __future__ import annotations

import numpy as np
import pytest

from capa.core.ringbuffer import ChannelRingBuffer, RingBufferRegistry
from capa.devices.records import ChannelSample


def _sample(channel: str, t_mono_ns: int, value: float) -> ChannelSample:
    return ChannelSample(
        channel=channel,
        t_mono_ns=t_mono_ns,
        t_mono_s=t_mono_ns / 1e9,
        value=value,
        unit="V",
    )


# ---------------------------------------------------------------------------
# ChannelRingBuffer
# ---------------------------------------------------------------------------


class TestChannelRingBuffer:
    def test_capacity_validation(self) -> None:
        with pytest.raises(ValueError):
            ChannelRingBuffer(capacity=0)
        with pytest.raises(ValueError):
            ChannelRingBuffer(capacity=-5)

    def test_decimate_to_hz_validation(self) -> None:
        with pytest.raises(ValueError):
            ChannelRingBuffer(capacity=10, decimate_to_hz=-1.0)

    def test_empty_snapshot_is_zero_length(self) -> None:
        buf = ChannelRingBuffer(capacity=10, decimate_to_hz=0)
        t, v = buf.snapshot()
        assert t.shape == (0,)
        assert v.shape == (0,)
        assert t.dtype == np.int64
        assert v.dtype == np.float64

    def test_latest_empty_returns_none(self) -> None:
        buf = ChannelRingBuffer(capacity=10, decimate_to_hz=0)
        assert buf.latest() is None

    def test_push_and_snapshot_chronological(self) -> None:
        buf = ChannelRingBuffer(capacity=10, decimate_to_hz=0)
        for i in range(5):
            buf.push(_sample("ch", t_mono_ns=i, value=float(i)))
        t, v = buf.snapshot()
        assert list(t) == [0, 1, 2, 3, 4]
        assert list(v) == [0.0, 1.0, 2.0, 3.0, 4.0]
        assert buf.size == 5

    def test_overflow_drops_oldest_and_increments_counter(self) -> None:
        buf = ChannelRingBuffer(capacity=3, decimate_to_hz=0)
        for i in range(5):
            buf.push_raw(t_mono_ns=i, value=float(i))
        t, v = buf.snapshot()
        # Oldest two (i=0, i=1) dropped — newest three retained, in order.
        assert list(t) == [2, 3, 4]
        assert list(v) == [2.0, 3.0, 4.0]
        assert buf.dropped_overflow == 2
        assert buf.size == 3

    def test_wrap_around_snapshot_correct_order(self) -> None:
        # Force the head past 0 so snapshot exercises the wrap-around branch.
        buf = ChannelRingBuffer(capacity=4, decimate_to_hz=0)
        for i in range(7):
            buf.push_raw(t_mono_ns=i, value=float(i))
        t, v = buf.snapshot()
        assert list(t) == [3, 4, 5, 6]
        assert list(v) == [3.0, 4.0, 5.0, 6.0]

    def test_decimation_skips_within_window(self) -> None:
        # decimate_to_hz=10 → min_dt = 1e8 ns = 100 ms.
        buf = ChannelRingBuffer(capacity=100, decimate_to_hz=10.0)
        # 1 kHz feed for 1 s → 1000 samples in, ~10 should survive.
        for i in range(1000):
            buf.push_raw(t_mono_ns=i * 1_000_000, value=float(i))  # 1 ms apart
        # Each kept sample must be at least 1e8 ns after the previous one.
        t, _ = buf.snapshot()
        assert len(t) <= 11  # tolerate boundary inclusiveness
        diffs = np.diff(t)
        assert (diffs >= 100_000_000).all()
        assert buf.dropped_decimation > 0
        # Total in == kept + decimated + overflow (no overflow here).
        assert buf.dropped_decimation + buf.size == 1000

    def test_decimation_disabled_when_hz_zero(self) -> None:
        buf = ChannelRingBuffer(capacity=100, decimate_to_hz=0)
        for i in range(50):
            buf.push_raw(t_mono_ns=i, value=float(i))  # 1 ns apart
        assert buf.size == 50
        assert buf.dropped_decimation == 0

    def test_latest_returns_most_recent(self) -> None:
        buf = ChannelRingBuffer(capacity=4, decimate_to_hz=0)
        for i in range(7):
            buf.push_raw(t_mono_ns=i * 100, value=float(i))
        latest = buf.latest()
        assert latest == (600, 6.0)

    def test_clear_resets_size_but_not_counters(self) -> None:
        buf = ChannelRingBuffer(capacity=2, decimate_to_hz=0)
        for i in range(5):
            buf.push_raw(t_mono_ns=i, value=float(i))
        assert buf.dropped_overflow == 3
        buf.clear()
        assert buf.size == 0
        # Run-cumulative counters survive a clear so the status bar can keep
        # showing them; only a fresh buffer per run starts at zero.
        assert buf.dropped_overflow == 3

    def test_bool_value_collapses_to_float(self) -> None:
        buf = ChannelRingBuffer(capacity=4, decimate_to_hz=0)
        buf.push(_sample("ch", t_mono_ns=1, value=True))  # type: ignore[arg-type]
        buf.push(_sample("ch", t_mono_ns=2, value=False))  # type: ignore[arg-type]
        _, v = buf.snapshot()
        assert list(v) == [1.0, 0.0]

    def test_push_returns_true_for_kept_false_for_decimated(self) -> None:
        buf = ChannelRingBuffer(capacity=10, decimate_to_hz=10.0)
        assert buf.push(_sample("ch", t_mono_ns=0, value=1.0)) is True
        assert buf.push(_sample("ch", t_mono_ns=1_000_000, value=2.0)) is False  # 1 ms < 100 ms
        assert buf.push(_sample("ch", t_mono_ns=200_000_000, value=3.0)) is True


# ---------------------------------------------------------------------------
# RingBufferRegistry
# ---------------------------------------------------------------------------


class TestRingBufferRegistry:
    def test_unregistered_channel_silently_ignored(self) -> None:
        reg = RingBufferRegistry()
        assert reg.push(_sample("not_registered", t_mono_ns=1, value=1.0)) is False

    def test_register_and_route(self) -> None:
        reg = RingBufferRegistry()
        reg.register("a", capacity=4, decimate_to_hz=0)
        reg.register("b", capacity=4, decimate_to_hz=0)
        reg.push(_sample("a", t_mono_ns=1, value=10.0))
        reg.push(_sample("b", t_mono_ns=2, value=20.0))
        reg.push(_sample("a", t_mono_ns=3, value=30.0))
        a = reg.get("a")
        b = reg.get("b")
        assert a is not None and b is not None
        assert a.size == 2
        assert b.size == 1
        assert reg.channels() == ("a", "b")

    def test_total_dropped_aggregates(self) -> None:
        reg = RingBufferRegistry()
        reg.register("a", capacity=2, decimate_to_hz=0)
        reg.register("b", capacity=2, decimate_to_hz=0)
        for i in range(5):
            reg.push(_sample("a", t_mono_ns=i, value=float(i)))
            reg.push(_sample("b", t_mono_ns=i, value=float(i)))
        # Each buffer overflowed 3 times.
        assert reg.total_dropped() == 6

    def test_clear_all_resets_sizes(self) -> None:
        reg = RingBufferRegistry()
        reg.register("a", capacity=4, decimate_to_hz=0)
        for i in range(3):
            reg.push(_sample("a", t_mono_ns=i, value=float(i)))
        reg.clear_all()
        a = reg.get("a")
        assert a is not None
        assert a.size == 0
