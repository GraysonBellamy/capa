"""Unit tests for :mod:`capa.runtime.metrics`.

The metric struct holds counter integers, percentile rings, and a state
enum. There are no behavioural surprises — these tests pin the API contract
so a later refactor (e.g. swapping the ring implementation) doesn't silently
break the manifest schema.
"""

from __future__ import annotations

import pytest

from capa.runtime.lifecycle import WorkerState
from capa.runtime.metrics import DisarmResult, WorkerMetrics


class TestWorkerMetricsInit:
    def test_defaults(self) -> None:
        m = WorkerMetrics(resource_id="serial:COMTEST", adapter_names=("a",))
        assert m.resource_id == "serial:COMTEST"
        assert m.adapter_names == ("a",)
        assert m.state is WorkerState.CLOSED
        assert m.commands_total == 0
        assert m.commands_inflight == 0
        assert m.commands_failed == 0
        assert m.samples_emitted == 0
        assert m.samples_late == 0
        assert m.disconnects == 0
        assert m.bridge_out is None

    def test_loop_lag_is_per_resource(self) -> None:
        m1 = WorkerMetrics(resource_id="serial:A", adapter_names=())
        m2 = WorkerMetrics(resource_id="serial:B", adapter_names=())
        assert m1.loop_lag.name == "worker-serial:A"
        assert m2.loop_lag.name == "worker-serial:B"
        # Each metric gets its own ring instance — observations don't bleed.
        m1.loop_lag.observe(99.0)
        assert m2.loop_lag.samples_total == 0


class TestCommandCounters:
    """The accepted/completed/failed accounting from migration doc §5.5."""

    def test_accepted_then_completed_clean(self) -> None:
        m = WorkerMetrics(resource_id="r", adapter_names=("a",))
        m.observe_command_accepted()
        assert m.commands_inflight == 1
        assert m.commands_total == 0  # not counted until completion
        m.observe_command_completed(failed=False)
        assert m.commands_inflight == 0
        assert m.commands_total == 1
        assert m.commands_failed == 0

    def test_accepted_then_completed_failed(self) -> None:
        m = WorkerMetrics(resource_id="r", adapter_names=("a",))
        m.observe_command_accepted()
        m.observe_command_completed(failed=True)
        assert m.commands_total == 1
        assert m.commands_failed == 1

    def test_concurrent_inflight(self) -> None:
        m = WorkerMetrics(resource_id="r", adapter_names=("a",))
        m.observe_command_accepted()
        m.observe_command_accepted()
        assert m.commands_inflight == 2
        m.observe_command_completed(failed=False)
        assert m.commands_inflight == 1
        m.observe_command_completed(failed=True)
        assert m.commands_inflight == 0
        assert m.commands_total == 2
        assert m.commands_failed == 1


class TestSampleCounters:
    def test_default_emit_not_late(self) -> None:
        m = WorkerMetrics(resource_id="r", adapter_names=("a",))
        m.observe_sample_emitted()
        assert m.samples_emitted == 1
        assert m.samples_late == 0

    def test_late_increments_both(self) -> None:
        m = WorkerMetrics(resource_id="r", adapter_names=("a",))
        m.observe_sample_emitted(late=True)
        assert m.samples_emitted == 1
        assert m.samples_late == 1

    def test_disconnect_counter(self) -> None:
        m = WorkerMetrics(resource_id="r", adapter_names=("a",))
        m.observe_disconnect()
        m.observe_disconnect()
        assert m.disconnects == 2


class TestTickDurationPercentiles:
    def test_p50_p99_increase_with_observations(self) -> None:
        m = WorkerMetrics(resource_id="r", adapter_names=("a",))
        for v in range(1, 101):
            m.observe_tick_duration(float(v))
        # Bridge's _PercentileRing uses nearest-rank-ish lookup; p50 lands
        # around 50, p99 around 99-100. Exact value matters less than the
        # relative order.
        assert 40.0 <= m.tick_duration_p50_ms <= 60.0
        assert 90.0 <= m.tick_duration_p99_ms <= 100.0
        assert m.tick_duration_p99_ms >= m.tick_duration_p50_ms

    def test_empty_returns_zero(self) -> None:
        m = WorkerMetrics(resource_id="r", adapter_names=("a",))
        assert m.tick_duration_p50_ms == 0.0
        assert m.tick_duration_p99_ms == 0.0


class TestDisarmResult:
    """The enum the conductor reads to decide whether a run is degraded."""

    def test_distinct_values(self) -> None:
        assert DisarmResult.OK is not DisarmResult.FORCED
        assert DisarmResult.FORCED is not DisarmResult.LEAKED

    @pytest.mark.parametrize("value", ["ok", "forced", "leaked"])
    def test_value_round_trip(self, value: str) -> None:
        assert DisarmResult(value).value == value
