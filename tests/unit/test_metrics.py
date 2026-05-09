"""Tests for :mod:`capa.core.metrics`."""

from __future__ import annotations

import time

from capa.core.metrics import (
    MetricsRegistry,
    QueueMetrics,
    WriterMetrics,
    _Reservoir,
)


def test_reservoir_records_first_n_then_random_replaces() -> None:
    r = _Reservoir(capacity=4)
    for i in range(10):
        r.observe(float(i))
    assert r.count == 10
    # 4 out of 10 should be retained.
    assert len(r._items) == 4
    # Percentiles are bounded by observed range.
    p50 = r.percentile(0.5)
    assert 0.0 <= p50 <= 9.0


def test_queue_metrics_track_high_water_and_lag() -> None:
    q = QueueMetrics(name="x")
    q.observe_depth(1)
    q.observe_depth(5)
    q.observe_depth(2)
    assert q.depth_max == 5
    snap = q.snapshot()
    assert snap["depth_max"] == 5.0
    assert snap["depth_p99"] >= snap["depth_p50"]


def test_queue_metrics_lag_round_trip() -> None:
    q = QueueMetrics(name="x")
    q.mark_enqueued(1)
    time.sleep(0.005)
    q.mark_dequeued(1)
    snap = q.snapshot()
    assert snap["lag_s_max"] >= 0.005
    # Unknown id is silently ignored.
    q.mark_dequeued(999)


def test_writer_metrics_time_write_context_manager() -> None:
    w = WriterMetrics(name="bundle")
    with w.time_write():
        time.sleep(0.002)
    snap = w.snapshot()
    assert w.write_count == 1
    assert snap["write_s_max"] >= 0.002


def test_metrics_registry_snapshot_keys() -> None:
    reg = MetricsRegistry()
    q = reg.queue("fanout")
    q.observe_depth(3)
    w = reg.writer("bundle")
    w.observe_write(0.01)
    snap = reg.snapshot_for_manifest()
    assert "queue.fanout" in snap
    assert "writer.bundle" in snap
    assert snap["queue.fanout"]["depth_max"] == 3.0
    assert snap["writer.bundle"]["write_count"] == 1.0


def test_metrics_registry_get_or_create_is_stable() -> None:
    reg = MetricsRegistry()
    a = reg.queue("x")
    b = reg.queue("x")
    assert a is b
