""":class:`RunClock` — single monotonic timebase per run.

Captures one ``time.monotonic_ns`` and one ``datetime.now(UTC)`` at run start,
so that:

* every :class:`~capa.devices.records.ChannelSample` carries a monotonic offset
  (``t_mono_s``) joinable across devices, and
* any monotonic offset can be converted back to wall-clock for human review or
  cross-correlation with externally-stamped events.

"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True, slots=True)
class RunClock:
    """Anchor pair: ``time.monotonic_ns`` and wall-clock UTC at run start."""

    started_mono_ns: int
    started_utc: datetime

    @classmethod
    def now(cls) -> RunClock:
        """Capture the anchor pair at "now"."""
        return cls(
            started_mono_ns=time.monotonic_ns(),
            started_utc=datetime.now(UTC),
        )

    def t_mono(self) -> float:
        """Monotonic seconds since run start.

        Floating-point seconds are convenient for in-memory bookkeeping; the
        canonical persisted column is ``t_mono_ns`` (int64).
        """
        return (time.monotonic_ns() - self.started_mono_ns) / 1e9

    def t_mono_ns(self) -> int:
        """Monotonic nanoseconds since run start (int64-safe for hour-long runs)."""
        return time.monotonic_ns() - self.started_mono_ns

    def to_wall(self, t_mono_s: float) -> datetime:
        """Convert a monotonic offset (seconds) back to UTC wall-clock."""
        return self.started_utc + timedelta(seconds=t_mono_s)

    def to_wall_ns(self, t_mono_ns: int) -> datetime:
        """Convert a monotonic offset (nanoseconds) back to UTC wall-clock."""
        return self.started_utc + timedelta(microseconds=t_mono_ns / 1000.0)


__all__ = ["RunClock"]
