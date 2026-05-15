"""Unit tests for :mod:`capa.devices._helpers`.

Covers the shared adapter scaffolding lifted out of the per-adapter modules:
the authorization gate, the last-sample tracker, and the silence-state view.
"""

from __future__ import annotations

from capa.core.clock import RunClock
from capa.devices._helpers import (
    LastSampleTracker,
    WatchdogState,
    make_accepted_result,
    make_not_open_result,
    make_unauthorized_result,
    reject_unless_authorized,
)
from capa.devices.adapter import DeviceCommand


def _cmd(*, authorization_id: str | None = None, confirmed_by: str | None = None) -> DeviceCommand:
    return DeviceCommand(
        kind="set_setpoint",
        target=None,
        payload={"value": 1.0},
        issued_by="alice",
        authorization_id=authorization_id,
        confirmed_by=confirmed_by,
    )


class TestAuthorizationGate:
    def test_no_auth_no_confirm_rejected(self) -> None:
        clock = RunClock.now()
        cmd = _cmd()
        result = reject_unless_authorized(cmd, adapter_id="x", device_name="dev1", clock=clock)
        assert result is not None
        assert result.accepted is False
        assert "unauthorized" in result.detail.lower()

    def test_authorization_id_lets_command_through(self) -> None:
        clock = RunClock.now()
        cmd = _cmd(authorization_id="run-1")
        assert (
            reject_unless_authorized(cmd, adapter_id="x", device_name="dev1", clock=clock) is None
        )

    def test_confirmed_by_lets_command_through(self) -> None:
        clock = RunClock.now()
        cmd = _cmd(confirmed_by="alice")
        assert (
            reject_unless_authorized(cmd, adapter_id="x", device_name="dev1", clock=clock) is None
        )


class TestResultBuilders:
    def test_unauthorized_carries_clock_timestamp(self) -> None:
        clock = RunClock.now()
        r = make_unauthorized_result(adapter_id="x", device_name="d", clock=clock)
        assert r.accepted is False
        assert r.t_mono_ns >= 0

    def test_not_open_carries_clock_timestamp(self) -> None:
        clock = RunClock.now()
        r = make_not_open_result(adapter_id="x", device_name="d", clock=clock)
        assert r.accepted is False
        assert "not open" in r.detail

    def test_accepted_round_trips_detail(self) -> None:
        clock = RunClock.now()
        r = make_accepted_result(detail="set_setpoint=42", clock=clock)
        assert r.accepted is True
        assert r.detail == "set_setpoint=42"


class TestLastSampleTracker:
    def test_initial_age_is_none(self) -> None:
        t = LastSampleTracker()
        assert t.last_t_mono_ns is None
        assert t.age_ns(now_t_mono_ns=10**18) is None

    def test_mark_and_age(self) -> None:
        t = LastSampleTracker()
        t.mark(1_000_000_000)
        assert t.age_ns(now_t_mono_ns=2_000_000_000) == 1_000_000_000

    def test_reset_clears(self) -> None:
        t = LastSampleTracker()
        t.mark(123)
        t.reset()
        assert t.last_t_mono_ns is None


class TestWatchdogState:
    def test_no_marks_means_not_silent(self) -> None:
        s = WatchdogState(device="d", last_t_mono_ns=None, expected_period_ns=10_000_000)
        assert not s.is_silent(now_t_mono_ns=10**18)

    def test_within_window_not_silent(self) -> None:
        last = 1_000_000_000
        s = WatchdogState(device="d", last_t_mono_ns=last, expected_period_ns=100_000_000)
        # 1 period elapsed — well within 2x slack
        assert not s.is_silent(now_t_mono_ns=last + 100_000_000)

    def test_past_slack_silent(self) -> None:
        last = 1_000_000_000
        s = WatchdogState(device="d", last_t_mono_ns=last, expected_period_ns=100_000_000)
        # 5 periods elapsed → silent
        assert s.is_silent(now_t_mono_ns=last + 500_000_000)

    def test_custom_slack(self) -> None:
        last = 0
        s = WatchdogState(device="d", last_t_mono_ns=last, expected_period_ns=1_000)
        # tighter slack of 1.0 → silent at 2× period
        assert s.is_silent(now_t_mono_ns=2_000, slack=1.0)
        # default 2× slack → not silent at 2×
        assert not s.is_silent(now_t_mono_ns=2_000, slack=2.0)

    def test_lifecycle_state_open_suppresses_silence(self) -> None:
        """An adapter that has been stopped (lifecycle state ``"open"``,
        not ``"running"``) is *expected* to be silent — clean shutdown
        must not trip ``device_silent``."""
        last = 1_000_000_000
        s = WatchdogState(
            device="d",
            last_t_mono_ns=last,
            expected_period_ns=100_000_000,
            lifecycle_state="open",
        )
        # 5 periods elapsed → would normally be silent, but adapter is stopped.
        assert not s.is_silent(now_t_mono_ns=last + 500_000_000)

    def test_lifecycle_state_closed_suppresses_silence(self) -> None:
        last = 1_000_000_000
        s = WatchdogState(
            device="d",
            last_t_mono_ns=last,
            expected_period_ns=100_000_000,
            lifecycle_state="closed",
        )
        assert not s.is_silent(now_t_mono_ns=last + 500_000_000)

    def test_lifecycle_state_running_does_not_suppress(self) -> None:
        """Running adapter past its window IS silent, as before."""
        last = 1_000_000_000
        s = WatchdogState(
            device="d",
            last_t_mono_ns=last,
            expected_period_ns=100_000_000,
            lifecycle_state="running",
        )
        assert s.is_silent(now_t_mono_ns=last + 500_000_000)

    def test_lifecycle_state_none_preserves_legacy_behaviour(self) -> None:
        """Existing call sites that don't pass ``lifecycle_state`` keep their
        previous semantics — silent strictly by elapsed-time math."""
        last = 1_000_000_000
        s = WatchdogState(device="d", last_t_mono_ns=last, expected_period_ns=100_000_000)
        assert s.lifecycle_state is None
        assert s.is_silent(now_t_mono_ns=last + 500_000_000)
