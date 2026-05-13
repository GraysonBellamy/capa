""":class:`SaturationMonitor` — end-to-end durable-output deadline.

Migration doc §4.5. Per-channel backpressure policies catch individual
queues filling up, but they don't catch the macro condition that matters
most for hardware safety: *the durable side has stopped accepting work*.
A wedged writer thread or a writer-side `fsync` stall makes every worker
outbound bridge block in unison. From any single bridge's perspective
that's the normal BLOCK case; the run-level fact that nothing is being
written goes unnoticed.

The monitor watches two signals:

1. **Per-bridge ``blocked_since_ms``** — how long has any outbound bridge's
   producer been parked waiting for space?
2. **Writer-thread ``last_accept_monotonic_ns`` vs ``depth``** — is the
   writer's inbox non-empty AND not advancing?

If either condition holds for ``deadline_s``, the run is sealed as
``crashed_but_sealed`` via the supplied callback. Hardware does not stay
in an inconsistent state because the conductor's normal disarm still runs
each adapter's ``stop()`` (which calls safe-shutdown for state-bearing
devices).

The monitor is intentionally **read-only**. It signals; it does not stop
anything itself. Pulling the run apart is the conductor's job: the monitor
calls the ``on_saturated`` callback the conductor passes in.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol, runtime_checkable

import structlog

from capa.runtime.bridge import ThreadBridge

_logger = structlog.get_logger("capa.runtime.saturation")


DEFAULT_SATURATION_DEADLINE_S: Final[float] = 10.0
"""How long any single saturation signal may stay tripped before the
monitor escalates. Conservative — captures genuinely wedged disks /
hung adapters while ignoring 10s-of-ms hiccups."""

DEFAULT_POLL_PERIOD_S: Final[float] = 1.0
"""How often the monitor wakes to recheck. Tunable up for very long
deadlines; the doc's recommendation is ``deadline_s / 10`` clamped to
``[1.0, 5.0]``."""


@runtime_checkable
class WriterSaturationSource(Protocol):
    """Read-only view of the writer thread's saturation signals.

    Implemented by :class:`~capa.storage.writer_thread.WriterThread` natively
    (``last_accept_monotonic_ns`` and ``depth`` properties) and by tests via
    a lightweight stub. Decouples the monitor from the concrete writer class.
    """

    @property
    def last_accept_monotonic_ns(self) -> int: ...

    @property
    def depth(self) -> int: ...


@dataclass(frozen=True, slots=True)
class SaturationEvent:
    """Why the monitor tripped.

    Carried back to the conductor via the ``on_saturated`` callback so it
    can record a structured ``saturation_deadline`` event into the bundle.
    """

    reason: str
    """Stable string tag (``"writer_inbox_stalled"`` /
    ``"worker_<rid>_outbound_saturated"``) — written to the bundle event."""

    details: Mapping[str, float | int | str]
    """Structured details for the event metadata: stuck time, depth,
    resource_id, etc."""


OnSaturatedCallback = Callable[[SaturationEvent], Awaitable[None]]


class SaturationMonitor:
    """End-to-end output deadline monitor.

    Construction is cheap; :meth:`run` is the long-lived coroutine. Stops
    when ``stop_event`` fires or when the first trip escalates (the monitor
    only fires once per run — the conductor handles shutdown thereafter).

    :param bridges: ``resource_id`` → outbound :class:`ThreadBridge`. The
        monitor reads ``bridge.metrics.blocked_since_ms`` on each tick; the
        bridges themselves are immutable through the monitor's lens.
    :param writer: Source of the writer-thread saturation signal. Pass
        ``None`` for tests that only want to exercise the bridge path.
    :param on_saturated: Callback invoked exactly once when a deadline
        trips. Awaited by the monitor — make it cheap; the conductor's
        completion-event-set should be the last meaningful action.
    :param deadline_s: How long a signal must stay tripped before escalating.
    :param poll_period_s: Recheck cadence.
    :param stop_event: Set this to retire the monitor cooperatively (e.g.
        when the run completes normally without saturation).
    :param clock_monotonic_ns: Time source (injection for tests; defaults
        to :func:`time.monotonic_ns`).
    """

    __slots__ = (
        "_bridges",
        "_clock",
        "_deadline_ns",
        "_fired",
        "_on_saturated",
        "_poll_period_s",
        "_stop_event",
        "_writer",
    )

    def __init__(
        self,
        *,
        bridges: Mapping[str, ThreadBridge[Any]],
        writer: WriterSaturationSource | None,
        on_saturated: OnSaturatedCallback,
        deadline_s: float = DEFAULT_SATURATION_DEADLINE_S,
        poll_period_s: float = DEFAULT_POLL_PERIOD_S,
        stop_event: asyncio.Event | None = None,
        clock_monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if deadline_s <= 0:
            raise ValueError("deadline_s must be positive")
        if poll_period_s <= 0:
            raise ValueError("poll_period_s must be positive")
        self._bridges = dict(bridges)
        self._writer = writer
        self._on_saturated = on_saturated
        self._deadline_ns = int(deadline_s * 1e9)
        self._poll_period_s = poll_period_s
        self._stop_event = stop_event
        self._clock = clock_monotonic_ns
        self._fired = False

    @property
    def fired(self) -> bool:
        """``True`` if a deadline has been escalated. The monitor fires
        at most once per run; after that the coroutine exits."""
        return self._fired

    async def run(self) -> None:
        """Long-lived poll loop. Returns when:

        * the stop event fires, OR
        * a deadline trips and the callback returns.

        Exceptions raised by ``on_saturated`` propagate out so the
        conductor's task group can decide what to do (typically: log and
        let normal shutdown take over).
        """
        # Snapshot the writer's accept-time at entry so a pre-existing
        # backlog isn't immediately read as "stalled". The first
        # deadline-honest tick is `entry + deadline_s` from now.
        last_accept_baseline = self._writer.last_accept_monotonic_ns if self._writer else 0
        _logger.debug(
            "saturation_monitor.start",
            deadline_s=self._deadline_ns / 1e9,
            poll_period_s=self._poll_period_s,
            bridge_count=len(self._bridges),
            has_writer=self._writer is not None,
        )
        while not self._fired and not self._stop_set():
            try:
                await asyncio.wait_for(self._wait_for_stop(), timeout=self._poll_period_s)
                # stop_event fired
                return
            except TimeoutError:
                pass  # one poll tick elapsed; recheck

            event = self._check(last_accept_baseline)
            if event is not None:
                self._fired = True
                _logger.error(
                    "saturation_monitor.deadline_exceeded",
                    reason=event.reason,
                    **{k: v for k, v in event.details.items() if isinstance(v, (int, float, str))},
                )
                await self._on_saturated(event)
                return

            if self._writer is not None:
                # Refresh the baseline so a writer that's healthily draining
                # an item every tick doesn't accidentally trip — only a
                # writer where `depth > 0` AND accept hasn't advanced beyond
                # the previous tick's baseline counts as stalled.
                current = self._writer.last_accept_monotonic_ns
                if current > last_accept_baseline:
                    last_accept_baseline = current

    def _stop_set(self) -> bool:
        return self._stop_event is not None and self._stop_event.is_set()

    async def _wait_for_stop(self) -> None:
        if self._stop_event is None:
            # No stop event configured — sleep the full tick. asyncio.sleep
            # is honoured by asyncio.wait_for so the timeout still bounds
            # the outer wait.
            await asyncio.sleep(self._poll_period_s * 2)
            return
        await self._stop_event.wait()

    def _check(self, writer_baseline_ns: int) -> SaturationEvent | None:
        """Evaluate every signal once. Returns the first trip or ``None``."""

        # 1. Per-bridge blocked-since check.
        for rid, bridge in self._bridges.items():
            blocked_ms = bridge.metrics.blocked_since_ms
            if blocked_ms is not None and (blocked_ms * 1e6) > self._deadline_ns:
                return SaturationEvent(
                    reason=f"worker_{rid}_outbound_saturated",
                    details={
                        "resource_id": rid,
                        "blocked_s": blocked_ms / 1000.0,
                        "deadline_s": self._deadline_ns / 1e9,
                    },
                )

        # 2. Writer-inbox stall check.
        if self._writer is not None:
            depth = self._writer.depth
            last_accept = self._writer.last_accept_monotonic_ns
            now = self._clock()
            # Stalled if: depth has items AND accept stamp has not advanced
            # past the baseline for >= deadline_s.
            if depth > 0 and (now - last_accept) > self._deadline_ns:
                return SaturationEvent(
                    reason="writer_inbox_stalled",
                    details={
                        "depth": depth,
                        "since_last_accept_s": (now - last_accept) / 1e9,
                        "deadline_s": self._deadline_ns / 1e9,
                    },
                )
            # Secondary trip path: depth > 0 AND last_accept hasn't moved
            # past the entry baseline at all (covers a writer that wedged
            # before the monitor even saw one successful tick).
            if (
                depth > 0
                and last_accept <= writer_baseline_ns
                and (now - writer_baseline_ns) > self._deadline_ns
            ):
                return SaturationEvent(
                    reason="writer_inbox_stalled",
                    details={
                        "depth": depth,
                        "since_last_accept_s": (now - writer_baseline_ns) / 1e9,
                        "deadline_s": self._deadline_ns / 1e9,
                    },
                )

        return None


__all__ = [
    "DEFAULT_POLL_PERIOD_S",
    "DEFAULT_SATURATION_DEADLINE_S",
    "OnSaturatedCallback",
    "SaturationEvent",
    "SaturationMonitor",
    "WriterSaturationSource",
]
