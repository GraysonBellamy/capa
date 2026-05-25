"""Shared helpers for sim adapters.

Every sim adapter:

* takes a list of :class:`ChannelSpec` filtered to its declared device,
* takes a mapping ``{source_field: SignalFn}`` for the underlying synthetic
  signals,
* drives a tick cadence based on ``RunClock`` so emitted ``t_mono_ns`` values
  are real monotonic offsets,
* applies the channel calibration directly via the shared
  :func:`~capa.devices._helpers.build_channel_sample` helper.

Each adapter writes its own ``stream()`` because the per-tick payload differs
(one wide row vs. many long rows vs. a balance row vs. a block). The helpers
here cover the bits that *are* shared: the two-call timing synthesis, a UTC
``now``, re-exports of the calibration / channel-routing helpers, and
re-exports of the command authorization gate so sims and real adapters use
the same accept/reject logic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from capa.core.clock import RunClock
from capa.devices._helpers import (
    build_channel_sample,
    channels_for_device,
    make_accepted_result,
    make_record_id,
    reject_unless_authorized,
)


def synth_timing(
    clock: RunClock,
    *,
    poll_latency_s: float = 0.002,
) -> tuple[int, datetime, datetime, datetime, datetime, float]:
    """Return ``(monotonic_ns_at_midpoint, requested_at, received_at,
    midpoint_at, t_utc_midpoint, latency_s)`` consistent with what the real
    libraries record.

    The two-call convention every library uses is
    ``time.monotonic_ns()`` + ``datetime.now(UTC)`` at the read boundary; we
    synthesize a small deterministic latency so tests can pin behavior.
    """
    requested_at = clock.started_utc + timedelta(seconds=clock.t_mono())
    received_at = requested_at + timedelta(seconds=poll_latency_s)
    midpoint_at = requested_at + timedelta(seconds=poll_latency_s / 2.0)
    midpoint_offset_ns = int(((midpoint_at - clock.started_utc).total_seconds()) * 1e9)
    return (
        midpoint_offset_ns,
        requested_at,
        received_at,
        midpoint_at,
        midpoint_at,
        poll_latency_s,
    )


def now_utc() -> datetime:
    """Current UTC wall-clock time. Overridable seam for deterministic sim tests."""
    return datetime.now(UTC)


__all__ = [
    "build_channel_sample",
    "channels_for_device",
    "make_accepted_result",
    "make_record_id",
    "now_utc",
    "reject_unless_authorized",
    "synth_timing",
]
