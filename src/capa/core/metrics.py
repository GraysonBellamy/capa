"""Queue-depth and writer-lag histograms for in-flight observability.

Every queue in the pipeline is instrumented; on finalize,
the per-queue histogram lands in ``manifest.json``\\ ``.queue_health`` so the
post-run "was this run healthy?" question is a one-glance check.

Two collector classes:

* :class:`QueueMetrics` — depth high-water-mark and depth percentile
  estimates over the run.
* :class:`WriterMetrics` — per-write latency (sample-to-disk lag).

Both use a small reservoir-based percentile estimator. Run lengths are
3–60 Hz × N channels × ≤ 1 hr — at most a few million events, easily
summarized with a fixed-size sample.

The :class:`MetricsRegistry` holds every collector instance and renders the
manifest block. The engine creates one per run, hands references into the
fan-out and writer task setup, and calls
:meth:`MetricsRegistry.snapshot_for_manifest` at finalize.
"""

from __future__ import annotations

import bisect
import random
import time
from dataclasses import dataclass, field
from typing import Final

RESERVOIR_SIZE: Final[int] = 1024
"""Reservoir-sample size for percentile estimates. Plenty of resolution for
queue-depth and writer-lag distributions in capa's envelope."""


@dataclass(slots=True)
class _Reservoir:
    """Algorithm R reservoir sampler. Yields stable percentile estimates with
    O(1) memory."""

    capacity: int = RESERVOIR_SIZE
    _items: list[float] = field(default_factory=list)
    _n_seen: int = 0
    _rng: random.Random = field(default_factory=lambda: random.Random(0xCA9A))

    def observe(self, value: float) -> None:
        """Record a new observation using Algorithm R for fixed-memory percentile estimation."""
        self._n_seen += 1
        if len(self._items) < self.capacity:
            self._items.append(value)
            return
        idx = self._rng.randint(0, self._n_seen - 1)
        if idx < self.capacity:
            self._items[idx] = value

    def percentile(self, p: float) -> float:
        """Return the linearly-interpolated p-th percentile (``p`` ∈ [0, 1]).

        Returns 0.0 when nothing has been observed.
        """
        if not self._items:
            return 0.0
        items = sorted(self._items)
        if p <= 0.0:
            return items[0]
        if p >= 1.0:
            return items[-1]
        rank = p * (len(items) - 1)
        lo = int(rank)
        hi = min(lo + 1, len(items) - 1)
        frac = rank - lo
        return items[lo] + (items[hi] - items[lo]) * frac

    @property
    def count(self) -> int:
        """Total number of observations seen (including those replaced via reservoir sampling)."""
        return self._n_seen


@dataclass(slots=True)
class QueueMetrics:
    """Queue-depth observations.

    The engine ticks :meth:`observe_depth` after each enqueue; lag is
    captured separately by paired calls to :meth:`mark_enqueued` /
    :meth:`mark_dequeued` referencing the same item id (``int``).
    """

    name: str
    depth_max: int = 0
    _depth_samples: _Reservoir = field(default_factory=_Reservoir)
    _enqueue_times: dict[int, float] = field(default_factory=dict)
    _lag_samples: _Reservoir = field(default_factory=_Reservoir)
    lag_s_max: float = 0.0

    def observe_depth(self, depth: int) -> None:
        """Record one queue-depth observation; tracks both reservoir samples and max."""
        self._depth_samples.observe(float(depth))
        if depth > self.depth_max:
            self.depth_max = depth

    def mark_enqueued(self, item_id: int) -> None:
        """Record monotonic time for ``item_id``. Call from the publisher."""
        self._enqueue_times[item_id] = time.monotonic()

    def mark_dequeued(self, item_id: int) -> None:
        """Compute lag for ``item_id`` and discard the start time. Items that
        were never enqueued (e.g. dropped under DROP_OLDEST) are silently
        skipped."""
        start = self._enqueue_times.pop(item_id, None)
        if start is None:
            return
        lag = time.monotonic() - start
        self._lag_samples.observe(lag)
        if lag > self.lag_s_max:
            self.lag_s_max = lag

    def snapshot(self) -> dict[str, float]:
        """Render the histogram into the shape
        :class:`~capa.storage.manifest.QueueHealthEntry` consumes."""
        return {
            "depth_p50": self._depth_samples.percentile(0.5),
            "depth_p99": self._depth_samples.percentile(0.99),
            "depth_max": float(self.depth_max),
            "lag_s_max": self.lag_s_max,
        }


@dataclass(slots=True)
class WriterMetrics:
    """Per-sink write-latency observations."""

    name: str
    _samples: _Reservoir = field(default_factory=_Reservoir)
    write_count: int = 0
    last_write_mono: float = 0.0
    write_s_max: float = 0.0

    def time_write(self) -> _WriterTimer:
        """Context manager that observes the elapsed write time."""
        return _WriterTimer(self)

    def observe_write(self, elapsed_s: float) -> None:
        """Record one write-latency observation (in seconds)."""
        self._samples.observe(elapsed_s)
        self.write_count += 1
        self.last_write_mono = time.monotonic()
        if elapsed_s > self.write_s_max:
            self.write_s_max = elapsed_s

    def snapshot(self) -> dict[str, float]:
        """Return the dict shape :attr:`BundleManifest.queue_health` accepts for this writer."""
        return {
            "write_p50_s": self._samples.percentile(0.5),
            "write_p99_s": self._samples.percentile(0.99),
            "write_s_max": self.write_s_max,
            "write_count": float(self.write_count),
        }


class _WriterTimer:
    __slots__ = ("_metrics", "_start")

    def __init__(self, metrics: WriterMetrics) -> None:
        self._metrics = metrics
        self._start: float = 0.0

    def __enter__(self) -> _WriterTimer:
        self._start = time.monotonic()
        return self

    def __exit__(self, *_: object) -> None:
        self._metrics.observe_write(time.monotonic() - self._start)


@dataclass(slots=True)
class MetricsRegistry:
    """Owns every metric collector for one run.

    :class:`~capa.runtime.session.RealRunSession` constructs one of these
    at run start, hands references into the queue/writer setup, and
    calls :meth:`snapshot_for_manifest` when finalizing the bundle.
    """

    queues: dict[str, QueueMetrics] = field(default_factory=dict)
    writers: dict[str, WriterMetrics] = field(default_factory=dict)

    def queue(self, name: str) -> QueueMetrics:
        """Get-or-create a :class:`QueueMetrics` keyed by ``name``."""
        m = self.queues.get(name)
        if m is None:
            m = QueueMetrics(name=name)
            self.queues[name] = m
        return m

    def writer(self, name: str) -> WriterMetrics:
        """Get-or-create a :class:`WriterMetrics` keyed by ``name``."""
        m = self.writers.get(name)
        if m is None:
            m = WriterMetrics(name=name)
            self.writers[name] = m
        return m

    def snapshot_for_manifest(self) -> dict[str, dict[str, float]]:
        """Return the dict shape :attr:`BundleManifest.queue_health` accepts.

        Both queue and writer metrics flatten into the same dict keyed by
        collector name. ``QueueHealthEntry``\\ 's open ``ConfigDict`` has
        the four queue fields; writer metrics use disjoint keys
        (``write_p50_s`` etc.) to keep the manifest readable.
        """
        out: dict[str, dict[str, float]] = {}
        # Queue first so the ordering in the manifest stays alphabetical-ish.
        for name in sorted(self.queues):
            out[f"queue.{name}"] = self.queues[name].snapshot()
        for name in sorted(self.writers):
            out[f"writer.{name}"] = self.writers[name].snapshot()
        return out


# bisect is imported for tests/extensions; silence the unused-import lint when
# the algorithm only uses sorted() above.
_ = bisect


__all__ = [
    "RESERVOIR_SIZE",
    "MetricsRegistry",
    "QueueMetrics",
    "WriterMetrics",
]
