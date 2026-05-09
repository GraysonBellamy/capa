from __future__ import annotations

import time
from datetime import UTC, datetime

from capa.core.clock import RunClock


class TestRunClock:
    def test_t_mono_starts_near_zero(self) -> None:
        clock = RunClock.now()
        # Allow the test runner a small slop window.
        assert clock.t_mono() < 1.0
        assert clock.t_mono_ns() < 1_000_000_000

    def test_t_mono_is_monotonic(self) -> None:
        clock = RunClock.now()
        a = clock.t_mono()
        time.sleep(0.001)
        b = clock.t_mono()
        assert b >= a

    def test_to_wall_at_zero_is_started_utc(self) -> None:
        clock = RunClock.now()
        assert clock.to_wall(0.0) == clock.started_utc

    def test_to_wall_advances(self) -> None:
        anchor = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)
        clock = RunClock(started_mono_ns=0, started_utc=anchor)
        assert clock.to_wall(60.0).hour == 12
        assert clock.to_wall(60.0).minute == 1

    def test_ns_int64_preserves_precision(self) -> None:
        anchor = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)
        clock = RunClock(started_mono_ns=0, started_utc=anchor)
        # 1 hour in ns
        one_hour_ns = 3_600 * 1_000_000_000
        assert clock.to_wall_ns(one_hour_ns).hour == 13
