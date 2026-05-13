"""Per-channel decimating ring buffer for live UI consumption.

Plan §4 / §7. The acquisition fan-out writes to durable sinks at the native
sample rate; a parallel UI consumer maintains a small in-memory ring per
channel for plot repaint at ~10 Hz. This module is the ring.

Two pieces:

* :class:`ChannelRingBuffer` — fixed-capacity ``(t_mono_ns, value)`` ring with
  per-channel decimation. ``DROP_OLDEST`` semantics on overflow.
* :class:`RingBufferRegistry` — name-keyed pool of buffers. The UI registers
  one buffer per channel it cares about, then feeds every
  :class:`~capa.devices.records.ChannelSample` it sees through
  :meth:`RingBufferRegistry.push`.

Storage shape: two parallel ``int64`` / ``float64`` numpy arrays. Snapshots
return contiguous numpy slices (PyQtGraph's preferred input). Boolean and
integer values collapse to ``float`` — plots only deal in floats.

Concurrency: single-producer / single-consumer. The fan-out → ring pump runs
on one task; the UI pump reads via :meth:`snapshot` on another. Operations
are *not* atomic across producer and consumer — a concurrent ``snapshot``
during a ``push`` may see one element fewer than the producer has just
written. That's harmless: the missed sample appears on the next repaint
tick. There is no lock on the hot path.

Decimation policy: a sample is dropped if its ``t_mono_ns`` is within
``1 / decimate_to_hz`` of the last *kept* sample. This produces a stable
plot line shape (vs. random skip-N decimation) and matches the operator's
intuition that "decimate_to_hz=10" means "no more than ten points per
second visible."

Drop counters are exposed for the status bar (plan §10.4 "Dropped UI
samples (rolling 10 s)").
"""

from __future__ import annotations

from typing import Final

import numpy as np

from capa.devices.records import ChannelSample

DEFAULT_CAPACITY: Final[int] = 6000
"""Floor for per-channel ring depth. Used when an explicit capacity is
passed to :class:`ChannelRingBuffer` and as a minimum for the
rate-derived capacity computed by :meth:`RingBufferRegistry.register`."""

DEFAULT_HISTORY_S: Final[float] = 600.0
"""Default history window targeted by :meth:`RingBufferRegistry.register`.
The capacity allocated for a channel is sized so the ring can hold this
many seconds of samples at the channel's ``decimate_to_hz`` without
rolling over — so a 50 Hz channel ends up with a ~30 000-sample ring
(10 min), not the 120 s a fixed 6000-sample buffer would give. Ten
minutes is long enough to scroll back through a typical CAPA run
without paging the durable sink."""


class ChannelRingBuffer:
    """Fixed-capacity ring buffer for one channel's recent samples.

    Single-producer / single-consumer. The producer is the UI's
    DataBus-pump task; the consumer is the 10 Hz plot repaint timer.

    The buffer is *lossy by design* in two ways:

    * Decimation skips samples that arrive faster than ``decimate_to_hz``.
      The skipped count is exposed as :attr:`dropped_decimation`.
    * Overflow drops the oldest kept sample once :attr:`capacity` is full,
      tracked as :attr:`dropped_overflow`.

    Both are normal: durable storage already has the full-rate stream; the
    ring is a viewport, not an archive.
    """

    __slots__ = (
        "_cap",
        "_dropped_decimation",
        "_dropped_overflow",
        "_head",
        "_last_kept_t",
        "_min_dt_ns",
        "_size",
        "_t",
        "_total_kept",
        "_v",
    )

    def __init__(self, *, capacity: int = DEFAULT_CAPACITY, decimate_to_hz: float = 10.0) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        if decimate_to_hz < 0:
            raise ValueError(f"decimate_to_hz must be >= 0, got {decimate_to_hz}")
        self._cap = capacity
        self._t = np.zeros(capacity, dtype=np.int64)
        self._v = np.zeros(capacity, dtype=np.float64)
        self._size = 0
        self._head = 0
        self._min_dt_ns = int(1e9 / decimate_to_hz) if decimate_to_hz > 0 else 0
        self._last_kept_t: int | None = None
        self._dropped_decimation = 0
        self._dropped_overflow = 0
        self._total_kept = 0

    # ------------------------------------------------------------------ properties

    @property
    def capacity(self) -> int:
        return self._cap

    @property
    def size(self) -> int:
        """Number of samples currently in the buffer."""
        return self._size

    @property
    def dropped_decimation(self) -> int:
        return self._dropped_decimation

    @property
    def dropped_overflow(self) -> int:
        return self._dropped_overflow

    @property
    def total_dropped(self) -> int:
        return self._dropped_decimation + self._dropped_overflow

    @property
    def total_kept(self) -> int:
        """Monotonic count of samples accepted into the ring since
        construction. Used by the plot pane to skip ``setData`` calls for
        curves that have not received new samples between repaint ticks.
        Never reset by :meth:`clear` — wraparound is not a concern at
        plausible run lengths (1 kHz × 10 yr ≪ int64)."""
        return self._total_kept

    # ------------------------------------------------------------------ producer

    def push(self, sample: ChannelSample) -> bool:
        """Insert ``sample`` (subject to decimation). Returns ``True`` if
        the sample was kept, ``False`` if decimated."""
        if (
            self._min_dt_ns > 0
            and self._last_kept_t is not None
            and sample.t_mono_ns - self._last_kept_t < self._min_dt_ns
        ):
            self._dropped_decimation += 1
            return False
        try:
            v = float(sample.value)  # bool/int/float → float
        except (TypeError, ValueError):
            return False
        self._push_raw(sample.t_mono_ns, v)
        return True

    def push_raw(self, t_mono_ns: int, value: float) -> bool:
        """Lower-level push that skips :class:`ChannelSample` construction.
        Useful for tests and for synthetic feeds. Decimation still applies."""
        if (
            self._min_dt_ns > 0
            and self._last_kept_t is not None
            and t_mono_ns - self._last_kept_t < self._min_dt_ns
        ):
            self._dropped_decimation += 1
            return False
        self._push_raw(t_mono_ns, value)
        return True

    def _push_raw(self, t_mono_ns: int, value: float) -> None:
        if self._size < self._cap:
            idx = (self._head + self._size) % self._cap
            self._size += 1
        else:
            idx = self._head
            self._head = (self._head + 1) % self._cap
            self._dropped_overflow += 1
        self._t[idx] = t_mono_ns
        self._v[idx] = value
        self._last_kept_t = t_mono_ns
        self._total_kept += 1

    # ------------------------------------------------------------------ consumer

    def snapshot(self) -> tuple[np.ndarray, np.ndarray]:
        """Return ``(t_mono_ns, value)`` arrays in chronological order.

        Always returns fresh copies so the consumer can hand the arrays to
        ``pyqtgraph.PlotDataItem.setData`` without worrying about producer
        mutation. Empty buffer returns two zero-length arrays.
        """
        if self._size == 0:
            return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64)
        end = self._head + self._size
        if end <= self._cap:
            return self._t[self._head : end].copy(), self._v[self._head : end].copy()
        first_n = self._cap - self._head
        t = np.empty(self._size, dtype=np.int64)
        v = np.empty(self._size, dtype=np.float64)
        t[:first_n] = self._t[self._head :]
        v[:first_n] = self._v[self._head :]
        t[first_n:] = self._t[: self._size - first_n]
        v[first_n:] = self._v[: self._size - first_n]
        return t, v

    def latest(self) -> tuple[int, float] | None:
        """Newest sample, or ``None`` if empty. Used by the numerics dock."""
        if self._size == 0:
            return None
        idx = (self._head + self._size - 1) % self._cap
        return int(self._t[idx]), float(self._v[idx])

    def clear(self) -> None:
        """Reset to empty. Drop counters are *not* reset — the status bar
        treats them as run-cumulative."""
        self._size = 0
        self._head = 0
        self._last_kept_t = None


class RingBufferRegistry:
    """Name-keyed pool of :class:`ChannelRingBuffer` instances.

    The UI builds one of these per run: each registered channel name gets
    its own buffer. The pump task feeds every observed
    :class:`ChannelSample` through :meth:`push`; samples for unregistered
    channels are silently ignored (the UI is allowed to be selective).
    """

    __slots__ = ("_buffers",)

    def __init__(self) -> None:
        self._buffers: dict[str, ChannelRingBuffer] = {}

    def register(
        self,
        channel: str,
        *,
        capacity: int | None = None,
        decimate_to_hz: float = 10.0,
        history_s: float = DEFAULT_HISTORY_S,
    ) -> ChannelRingBuffer:
        """Create-or-replace the buffer for ``channel``.

        When ``capacity`` is ``None`` (the default) the ring is sized to
        hold ``history_s`` seconds at ``decimate_to_hz``, floored at
        :data:`DEFAULT_CAPACITY`. This is what keeps a 50 Hz Sartorius
        balance from overflowing two minutes into a run with the previous
        fixed 6000-sample default: at ``decimate_to_hz=60`` it now gets a
        36 000-sample ring (10 min) instead. Pass ``capacity`` explicitly
        only when a test or caller needs an exact size.
        """
        if capacity is None:
            rate_capacity = int(decimate_to_hz * history_s) if decimate_to_hz > 0 else 0
            capacity = max(DEFAULT_CAPACITY, rate_capacity)
        buf = ChannelRingBuffer(capacity=capacity, decimate_to_hz=decimate_to_hz)
        self._buffers[channel] = buf
        return buf

    def get(self, channel: str) -> ChannelRingBuffer | None:
        return self._buffers.get(channel)

    def push(self, sample: ChannelSample) -> bool:
        """Route ``sample`` to its channel's buffer if registered. Returns
        ``True`` if a buffer accepted it, ``False`` otherwise (unregistered
        channel or decimated)."""
        buf = self._buffers.get(sample.channel)
        if buf is None:
            return False
        return buf.push(sample)

    def channels(self) -> tuple[str, ...]:
        return tuple(self._buffers.keys())

    def total_dropped(self) -> int:
        """Sum of dropped samples across every registered buffer. Used by
        the status bar's "Dropped UI samples" readout (plan §10.4)."""
        return sum(b.total_dropped for b in self._buffers.values())

    def total_overflow(self) -> int:
        """Total ring rollovers across every buffer — samples evicted
        because the ring was at capacity when a new push arrived.

        **Not a UI-distress signal.** Plot snapshots are non-draining
        copies, so a ring left running long enough will *always* roll
        over once it has accumulated ``capacity`` samples — that's the
        defining behavior of a ring buffer, not consumer slowness. For
        actual backpressure use the conductor's saturation deadline,
        loop-lag percentile, and worker-bridge depth metrics."""
        return sum(b.dropped_overflow for b in self._buffers.values())

    def total_decimated(self) -> int:
        """Decimation-only drops across every buffer. By-design shedding
        of samples that arrive faster than ``decimate_to_hz``; reported
        separately so the status bar can distinguish 'plot decimation
        working' from 'UI is overwhelmed'."""
        return sum(b.dropped_decimation for b in self._buffers.values())

    def per_channel_drops(self) -> dict[str, tuple[int, int]]:
        """Return ``{channel: (overflow, decimated)}`` for every registered
        buffer — used by the status bar to attribute drops to specific
        producers in the pill tooltip."""
        return {
            name: (buf.dropped_overflow, buf.dropped_decimation)
            for name, buf in self._buffers.items()
        }

    def clear_all(self) -> None:
        for buf in self._buffers.values():
            buf.clear()


__all__ = [
    "DEFAULT_CAPACITY",
    "DEFAULT_HISTORY_S",
    "ChannelRingBuffer",
    "RingBufferRegistry",
]
