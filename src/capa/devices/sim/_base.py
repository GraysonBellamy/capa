"""Shared base for sim adapters.

Every sim adapter:

* takes a list of :class:`ChannelSpec` filtered to its declared device,
* takes a mapping ``{source_field: SignalFn}`` for the underlying synthetic
  signals,
* drives a tick cadence based on ``RunClock`` so emitted ``t_mono_ns`` values
  are real monotonic offsets,
* applies the channel calibration directly via the shared
  :func:`~capa.devices._helpers.build_channel_sample` helper.

The base does not implement ``stream()`` itself because every adapter has a
slightly different per-tick payload (one wide row vs. many long rows vs. a
balance row vs. a block).

Calibration / channel-routing helpers live in
:mod:`capa.devices._helpers` so the real adapters (P0d+) can reuse them
without importing out of :mod:`capa.devices.sim`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from capa.core.clock import RunClock
from capa.core.errors import AdapterError
from capa.devices._helpers import (
    build_channel_sample,
    channels_for_device,
    make_record_id,
)
from capa.devices.adapter import AdapterLifecycle
from capa.devices.sim._signals import SignalFn


@dataclass(slots=True)
class SimContext:
    """Per-adapter sim state."""

    name: str
    clock: RunClock
    tick_period_s: float
    lifecycle: AdapterLifecycle = field(default_factory=AdapterLifecycle)
    record_seq: int = 0
    next_tick_mono: float = 0.0


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


def evaluate_signal(signals: dict[str, SignalFn], key: str, t_s: float) -> float:
    """Look up ``key`` in ``signals`` and call it at ``t_s``."""
    try:
        fn = signals[key]
    except KeyError as exc:
        raise AdapterError(
            f"sim adapter has no signal generator for {key!r}; "
            f"declare one in the adapter constructor"
        ) from exc
    return float(fn(t_s))


def now_utc() -> datetime:
    return datetime.now(UTC)


def perf_sleep_until(target_mono_s: float) -> None:
    """Tight-busy-wait helper for tests that want sub-millisecond ticks
    without bringing in anyio.sleep. Not used in the regular stream path
    (which uses ``anyio.sleep``)."""
    while time.monotonic() < target_mono_s:
        pass


__all__ = [
    "SimContext",
    "build_channel_sample",
    "channels_for_device",
    "evaluate_signal",
    "make_record_id",
    "now_utc",
    "perf_sleep_until",
    "synth_timing",
]
