"""Unit tests for :mod:`capa.runtime.metrics`.

The metric struct holds counter integers, percentile rings, and a state
enum. There are no behavioural surprises — these tests pin the API contract
so a later refactor (e.g. swapping the ring implementation) doesn't silently
break the manifest schema.
"""

from __future__ import annotations

import time

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
        assert m.polls_emitted == 0
        assert m.disconnects == 0
        assert m.bridge_out is None
        assert m.poll_rate_hz == 0.0
        assert m.last_sample_age_s == 0.0

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


class TestPollMetrics:
    """The operator-facing per-poll surface that the diagnostics dock reads.

    These tests pin the contract that fixes the "thousands of Hz" bug:
    rate is computed from poll periods (one observation per SourceRecord),
    not from tick durations (one per emission). For a 4 Hz device with 5
    channel bindings, ``poll_rate_hz`` must report ~4 Hz, not ~30 Hz.
    """

    def test_counter_increments_per_observation(self) -> None:
        m = WorkerMetrics(resource_id="r", adapter_names=("a",))
        m.observe_poll_emitted(t_mono_s=10.0)
        m.observe_poll_emitted(t_mono_s=10.25)
        m.observe_poll_emitted(t_mono_s=10.50)
        assert m.polls_emitted == 3

    def test_first_observation_seeds_without_period(self) -> None:
        # No period can be measured from a single observation — only the
        # 2nd+ observation feeds the ring.
        m = WorkerMetrics(resource_id="r", adapter_names=("a",))
        m.observe_poll_emitted(t_mono_s=10.0)
        assert m.polls_emitted == 1
        assert m.poll_period_p50_ms == 0.0
        assert m.poll_rate_hz == 0.0

    def test_period_p50_matches_configured_rate(self) -> None:
        # 4 Hz cadence → 250 ms between polls → rate ~ 4 Hz.
        m = WorkerMetrics(resource_id="r", adapter_names=("a",))
        t = 10.0
        for _ in range(20):
            m.observe_poll_emitted(t_mono_s=t)
            t += 0.25  # 250 ms
        assert 200.0 <= m.poll_period_p50_ms <= 300.0
        assert 3.0 <= m.poll_rate_hz <= 5.0

    def test_rate_unaffected_by_emission_fanout(self) -> None:
        # The diagnostics bug was: tick_duration is observed per emission,
        # so 1 SourceRecord + 5 ChannelSamples per poll produced a p50 of
        # microseconds and a "rate" in the tens of thousands. The
        # observe_poll_emitted path must be immune to that.
        m = WorkerMetrics(resource_id="r", adapter_names=("a",))
        t = 10.0
        for _ in range(10):
            # Simulated emission burst at every poll: 6 tick observations
            # (5 sub-millisecond, 1 large) but only ONE poll observation.
            for sub_ms in (0.05, 0.05, 0.05, 0.05, 0.05):
                m.observe_tick_duration(sub_ms)
            m.observe_tick_duration(250.0)  # gap to next poll
            m.observe_poll_emitted(t_mono_s=t)
            t += 0.25
        # tick-duration p50 reflects burst microseconds — that's correct
        # for the saturation budget metric but wrong for operator rate.
        assert m.tick_duration_p50_ms < 1.0
        # poll-rate is the operator-facing metric and must match cadence.
        assert 3.0 <= m.poll_rate_hz <= 5.0


class TestLastSampleAge:
    def test_zero_before_first_poll(self) -> None:
        m = WorkerMetrics(resource_id="r", adapter_names=("a",))
        assert m.last_sample_age_s == 0.0

    def test_grows_with_wallclock_after_poll(self) -> None:
        m = WorkerMetrics(resource_id="r", adapter_names=("a",))
        m.observe_poll_emitted(t_mono_s=time.monotonic())
        # Same-tick read: age is some small positive number close to zero.
        age0 = m.last_sample_age_s
        assert 0.0 <= age0 < 0.5
        # Simulate an old stamp — age must report the gap.
        m.observe_poll_emitted(t_mono_s=time.monotonic() - 3.0)
        age1 = m.last_sample_age_s
        assert 2.5 <= age1 <= 3.5


class TestDisarmResult:
    """The enum the conductor reads to decide whether a run is degraded."""

    def test_distinct_values(self) -> None:
        assert DisarmResult.OK is not DisarmResult.FORCED
        assert DisarmResult.FORCED is not DisarmResult.LEAKED

    @pytest.mark.parametrize("value", ["ok", "forced", "leaked"])
    def test_value_round_trip(self, value: str) -> None:
        assert DisarmResult(value).value == value
