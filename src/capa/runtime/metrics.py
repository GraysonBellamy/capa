""":class:`WorkerMetrics`, :class:`DisarmResult` — per-worker telemetry surface.

Migration doc §5.5 lines 1438-1453 defines the per-worker metrics block that
the bundle's ``diagnostics.runtime`` manifest entry consumes (and that the UI
status bar reads for the per-loop lag badge).

Why a dataclass on top of free-standing fields:

1. The manifest writer serializes one struct per worker into one TOML/JSON
   table — easier to keep field order stable when there's one type.
2. The percentile-tracking fields ([loop_lag, tick_duration, bridge_out
   latency]) all read through the shared
   :class:`~capa.runtime.bridge._PercentileRing`; the dataclass owns those
   rings so the worker code can drop observations into them without each
   adding bespoke struct fields.
3. Tests assert against a typed surface — ``metrics.commands_total == 1``
   is more legible than ``metrics["commands_total"] == 1``.

Phase 1 scope: struct definitions plus a minimal :meth:`observe_*` API.
The Conductor consumes these in Phase 2 to assemble the runtime diagnostics
block; the UI status-bar binding lands in Phase 4.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from capa.runtime.bridge import ThreadBridgeMetrics, _PercentileRing
from capa.runtime.heartbeat import LoopLagMetric
from capa.runtime.lifecycle import WorkerState


class DisarmResult(Enum):
    """Outcome of one :meth:`Worker.disarm` call.

    Migration doc §4.1 line 581 and §3.8 Phase A/B.

    The distinction matters at the run level: a single ``FORCED`` from any
    worker marks the run as degraded in the bundle manifest. Multiple
    workers can FORCE on the same disarm — the conductor's disarm-all
    aggregates them.
    """

    OK = "ok"
    """``adapter.stop()`` completed inside ``grace_s``; outbound bridge
    drained cleanly; worker returned to IDLE."""

    FORCED = "forced"
    """Grace expired with at least one adapter still running. The worker
    attempted a hard-stop (``loop.stop()`` + ``thread.join(timeout)``). The
    thread may or may not have joined — :attr:`thread_joined` carries that
    distinction. Either way the worker's per-run context is cleared and the
    pool is told the run is over."""

    LEAKED = "leaked"
    """Grace expired AND the hard-stop's ``thread.join(timeout)`` also
    expired. The thread persists as a daemon for the process lifetime; the
    bundle records ``worker_thread_leaked`` with a stack from
    ``sys._current_frames()`` (migration doc §3.8 Phase B line 442). Pool
    drops the worker from its map; the resource is unusable for the rest of
    the process's life — operator must restart capa to recover."""


@dataclass(slots=True)
class WorkerMetrics:
    """Live observability for one :class:`~capa.runtime.worker.Worker`.

    Migration doc §5.5 lines 1438-1453. Fields are read by:

    * Conductor's manifest-writer at run finalize.
    * UI status bar (Phase 4) for the per-loop lag badge.
    * Saturation monitor (Phase 2) which reads :attr:`bridge_out` and
      :attr:`commands_inflight`.

    Mutators are intentionally narrow — each observation routes through a
    method on this struct rather than callers reaching in to fiddle fields,
    so a future "metrics get exported every 1 s" hook has exactly one place
    to plug into.

    Reads from any thread are atomic (single int / float / enum); the
    percentile rings are internally locked. No external lock is required.
    """

    resource_id: str
    """Matches ``Worker.resource_id``."""

    adapter_names: tuple[str, ...]
    """Names of every adapter hosted by this worker, in construction order.
    A worker with one adapter has a singleton tuple here. Migration doc
    §4.12 line 1294."""

    state: WorkerState = WorkerState.CLOSED
    """Current worker state; updated by :class:`Worker` at every transition.
    Atomic reads under the GIL; no lock required for readers."""

    # ---- counters --------------------------------------------------------

    commands_total: int = 0
    """Number of :meth:`Worker.dispatch` calls that completed (success or
    failure) since worker start. Counted on completion, not on submission,
    so an in-flight command isn't double-counted in :attr:`commands_inflight`."""

    commands_inflight: int = 0
    """Number of :meth:`Worker.dispatch` calls that have been accepted by
    the worker loop but have not yet completed. Bounded in practice by the
    adapter's own per-port lock; an inflight count > 1 means concurrent
    callers, not pipelined commands."""

    commands_failed: int = 0
    """Subset of :attr:`commands_total`: the dispatch's ``adapter.command``
    raised. Caller cancellations are NOT counted here — under the
    cancellation shield (migration doc §4.2), the worker-side coroutine
    runs to completion; only its caller-facing future is cancelled. A
    cancelled-caller-but-successful-adapter command is counted as a normal
    success in :attr:`commands_total`."""

    samples_emitted: int = 0
    """Number of ``adapter.stream()`` items the worker put on its outbound
    bridge — every emission, including the channel-sample fanout. Read by
    the per-worker watchdog (Phase 2) to detect stream silence —
    ``samples_emitted`` failing to advance for ``2 / rate_hz`` is the silence
    trigger (§5.3). For "polls per second" use :attr:`polls_emitted`."""

    samples_late: int = 0
    """Subset of :attr:`samples_emitted`: producer side observed > 1 tick
    of clock lag at stamp time. The exact "late" threshold is adapter-
    specific; the worker exposes the metric, the adapter decides what to
    record."""

    polls_emitted: int = 0
    """Number of ``SourceRecord`` emissions — i.e. actual polls / samples,
    not the per-poll fanout. Every device adapter yields 1 ``SourceRecord``
    plus N ``ChannelSample``s plus the occasional ``DeviceSnapshot`` per
    poll; :attr:`samples_emitted` counts all of those, while this counter
    counts only the underlying poll cadence. The diagnostics dock divides
    this by elapsed time to display the operator-facing acquisition rate."""

    disconnects: int = 0
    """Number of mid-run adapter reconnects (when the adapter declares
    :attr:`Capability.SUPPORTS_AUTO_RECONNECT` and recovers silently).
    Diagnostic only; non-supporting adapters bubble the error and trigger
    the worker's degraded path instead."""

    # ---- bridges ---------------------------------------------------------

    bridge_out: ThreadBridgeMetrics | None = None
    """Outbound (worker → conductor) :class:`~capa.runtime.bridge.ThreadBridge`
    metrics. ``None`` while the worker is below SAMPLING (no bridge yet)."""

    # ---- percentile rings ------------------------------------------------

    loop_lag: LoopLagMetric = field(init=False)
    """Per-worker loop lag (heartbeat metric). Wired to the worker's own
    :func:`~capa.runtime.heartbeat.heartbeat_task` at thread start."""

    tick_duration_ms: _PercentileRing = field(default_factory=_PercentileRing)
    """One observation per ``adapter.stream()`` yield: time spent in the
    worker between consecutive emissions. p50/p99 of this is what §10 line
    1962 budgets to < 18 ms for the Sartorius @ 50 Hz. Includes the
    microsecond gaps inside a single poll's emission burst — this is the
    bridge-latency budget metric, NOT the operator-facing poll period.
    Use :attr:`poll_period_ms` for the latter."""

    poll_period_ms: _PercentileRing = field(default_factory=_PercentileRing)
    """One observation per ``SourceRecord`` emission: time between
    consecutive polls. p50 of this is the inverse of the actual sample
    rate — what the diagnostics dock reports as "Rate (Hz)" and what
    operators compare against the configured ``rate_hz``."""

    _last_poll_mono_s: float | None = field(default=None, init=False)
    """``time.monotonic()`` of the most recent :class:`SourceRecord`
    emission, or ``None`` before the first poll has landed. Used by the
    :attr:`last_sample_age_s` property to compute age on read, and by
    :meth:`observe_poll_emitted` to compute :attr:`poll_period_ms`."""

    def __post_init__(self) -> None:
        self.loop_lag = LoopLagMetric(name=f"worker-{self.resource_id}")

    # ---- mutators --------------------------------------------------------

    def observe_command_accepted(self) -> None:
        """Called by the worker loop when ``dispatch`` enters the shielded
        ``adapter.command`` call."""
        self.commands_inflight += 1

    def observe_command_completed(self, *, failed: bool) -> None:
        """Called by the worker loop when the shielded coroutine exits
        (success or exception). The caller-side cancellation path does NOT
        call this — only the worker-side coroutine does."""
        self.commands_inflight -= 1
        self.commands_total += 1
        if failed:
            self.commands_failed += 1

    def observe_sample_emitted(self, *, late: bool = False) -> None:
        self.samples_emitted += 1
        if late:
            self.samples_late += 1

    def observe_tick_duration(self, dt_ms: float) -> None:
        self.tick_duration_ms.observe(dt_ms)

    def observe_poll_emitted(self, *, t_mono_s: float) -> None:
        """Called once per :class:`SourceRecord` emission (i.e. once per
        actual poll/sample, not per per-poll fanout emission). Updates the
        poll counter, the poll-period percentile ring, and the age stamp.

        The first observation has no prior poll to subtract from, so it
        seeds :attr:`_last_poll_mono_s` without recording a period.
        Subsequent observations record ``(t_mono_s - prev) * 1000`` as one
        :attr:`poll_period_ms` sample.
        """
        prev = self._last_poll_mono_s
        if prev is not None:
            self.poll_period_ms.observe((t_mono_s - prev) * 1000.0)
        self._last_poll_mono_s = t_mono_s
        self.polls_emitted += 1

    def observe_disconnect(self) -> None:
        self.disconnects += 1

    # ---- read-only helpers -----------------------------------------------

    @property
    def tick_duration_p50_ms(self) -> float:
        return self.tick_duration_ms.p50

    @property
    def tick_duration_p99_ms(self) -> float:
        return self.tick_duration_ms.p99

    @property
    def poll_period_p50_ms(self) -> float:
        return self.poll_period_ms.p50

    @property
    def poll_period_p99_ms(self) -> float:
        return self.poll_period_ms.p99

    @property
    def poll_rate_hz(self) -> float:
        """Inverse of :attr:`poll_period_p50_ms`. ``0.0`` before the
        second poll has landed (i.e. before any period has been measured).
        """
        p50 = self.poll_period_ms.p50
        return 1000.0 / p50 if p50 > 0.0 else 0.0

    @property
    def last_sample_age_s(self) -> float:
        """Wall-clock seconds since the most recent poll (``SourceRecord``
        emission). ``0.0`` before the first poll has landed — callers that
        want to distinguish "never polled" from "just polled" should check
        :attr:`polls_emitted` first.
        """
        prev = self._last_poll_mono_s
        if prev is None:
            return 0.0
        return time.monotonic() - prev

    @property
    def loop_lag_ms_p99(self) -> float:
        return self.loop_lag.p99_ms


__all__ = [
    "DisarmResult",
    "WorkerMetrics",
]
