"""Per-loop heartbeat for observability.

Every per-resource worker loop, the conductor loop, and the UI loop run
one :func:`heartbeat_task` in the background. The task re-targets every
``period_s`` seconds and observes the actual wake-up time vs the target;
the difference is loop lag.

``loop_lag.p99 > 50 ms`` is the smoke alarm. The manifest's
``diagnostics.runtime`` block records the p99 per thread; the UI status
bar flashes when any loop exceeds threshold.

The :class:`LoopLagMetric` shares the same percentile-ring implementation
as :class:`~capa.runtime.bridge.ThreadBridgeMetrics` (lifted from there)
so quantile semantics stay consistent across the runtime.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import anyio

from capa.runtime.bridge import _PercentileRing


@dataclass
class LoopLagMetric:
    """Observations of how late an event loop is waking up vs its target.

    A loop running cooperatively should wake from a 50 ms sleep within
    a few ms; sustained lag above ~50 ms means the loop has a CPU-bound
    or I/O-blocking task starving it. The bounded-latency goal translates
    directly to ``p99_ms < 50`` for every loop.

    All readers should treat fields as read-only and use the percentile
    properties — direct ``_ring`` access is private.
    """

    name: str
    """Human label for telemetry: ``"worker-heater"``, ``"conductor"``,
    ``"ui"``."""

    samples_total: int = 0
    """Number of heartbeat observations recorded. The percentile values
    are well-defined only once this is at least a handful (`>= 16`)."""

    max_lag_ms: float = 0.0
    """Largest single lag observed since start. Useful for smoke tests
    that want to confirm a deliberately-injected stall was caught."""

    _ring: _PercentileRing = field(default_factory=_PercentileRing)

    def observe(self, lag_ms: float) -> None:
        self._ring.observe(lag_ms)
        self.samples_total += 1
        if lag_ms > self.max_lag_ms:
            self.max_lag_ms = lag_ms

    @property
    def p50_ms(self) -> float:
        return self._ring.p50

    @property
    def p99_ms(self) -> float:
        return self._ring.p99


async def heartbeat_task(
    metric: LoopLagMetric,
    stop_event: anyio.Event,
    *,
    period_s: float = 0.05,
) -> None:
    """Background task that observes loop lag at a fixed cadence.

    Sleeps from a moving target rather than from the current time, so a
    one-off stall shows up as lag on the *next* wake-up, not as drift in
    subsequent samples.

    Exits when ``stop_event`` is set. The task does not handle
    :class:`asyncio.CancelledError` specially — cancellation through the
    enclosing task group is the standard shutdown path.

    Args:
        metric: shared metric struct; lag values are appended to its ring.
        stop_event: an :class:`anyio.Event` constructed on the same loop
            this task will run on; setting it from any thread terminates
            the heartbeat.
        period_s: heartbeat cadence. Default 50 ms ≈ 20 Hz, matching the
            doc's worked example.
    """
    if period_s <= 0:
        raise ValueError(f"heartbeat period_s must be > 0, got {period_s}")
    loop = asyncio.get_running_loop()
    target = loop.time()
    while not stop_event.is_set():
        target += period_s
        now = loop.time()
        delay = target - now
        if delay > 0:
            await asyncio.sleep(delay)
        actual = loop.time()
        lag_ms = max(0.0, (actual - target) * 1000.0)
        metric.observe(lag_ms)


__all__ = [
    "LoopLagMetric",
    "heartbeat_task",
]
