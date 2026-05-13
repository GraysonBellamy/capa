"""Dedicated background thread that owns all sink writes for one run.

Without this thread, every sink write happens on the asyncio/Qt event loop:
:meth:`RunBundleWriter.record_sample` lands on the channel-samples
:class:`~capa.storage.channel_samples_sink.ChannelSamplesSink` buffer, which
flushes a record batch + ``os.fsync`` every 1024 rows; on Windows that fsync
is a full ``FlushFileBuffers`` and can stall the loop for tens of
milliseconds. Multiplied across high-rate NI-DAQ block-mode unrolls, the
loop gets bursty enough to push the producer-fanout queue into
:class:`~capa.core.backpressure.BackpressurePolicy.ABORT_RUN` territory
under disk contention.

The fix is a single dedicated thread that owns every sink. The asyncio loop
hands items off via a bounded :class:`queue.Queue`; in the steady state the
hand-off is a microsecond-cost ``put_nowait``. When the inbox fills (slow
disk, antivirus stall) the loop transparently switches to a blocking
``put`` running off-loop via :func:`anyio.to_thread.run_sync`, which yields
to other tasks while waiting for space. End-to-end backpressure is
preserved: a saturated writer thread eventually pushes the producer-fanout
queue into BLOCK and from there into its ABORT_RUN window — same shape as
before, just one layer down.

Threading boundary: only the writer thread ever touches the
:class:`~capa.storage.bundle.RunBundleWriter` and its sinks while the thread
is alive. PyArrow's ``RecordBatchStreamWriter`` and ``OSFile`` are not
documented as thread-safe, and the SQLite connection — though created with
``check_same_thread=False`` — gets the same single-thread discipline.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

import anyio
import structlog

from capa.core.errors import CapaError
from capa.devices.camera.base import FrameReceipt
from capa.devices.records import (
    ChannelSample,
    DeviceEvent,
    DeviceSnapshot,
    SourceRecord,
)

if TYPE_CHECKING:
    from capa.core.metrics import WriterMetrics
    from capa.storage.bundle import RunBundleWriter


class _CloseSentinel:
    """Singleton type sent to the drain loop to signal a graceful exit."""


_CLOSE_SENTINEL: Final[_CloseSentinel] = _CloseSentinel()


@dataclass(slots=True, frozen=True)
class FrameItem:
    """Queue payload for :meth:`RunBundleWriter.record_frame`."""

    receipt: FrameReceipt


@dataclass(slots=True, frozen=True)
class WriteEventItem:
    """Queue payload for :meth:`RunBundleWriter.write_event`."""

    kind: str
    message: str
    severity: str
    source: str
    t_mono_ns: int
    t_utc: datetime
    metadata: dict[str, Any] | None


WriterItem = (
    ChannelSample | SourceRecord | DeviceEvent | DeviceSnapshot | FrameItem | WriteEventItem
)


DEFAULT_CAPACITY: Final[int] = 4096
"""Default inbox capacity. ~10× the producer-fanout queue gives headroom for
disk hiccups while keeping in-flight data bounded on crash. Pick smaller
for stricter loss windows, larger to absorb longer stalls."""

DEFAULT_CLOSE_TIMEOUT_S: Final[float] = 10.0
"""How long :meth:`WriterThread.close` waits for the drain loop to finish.
Generous so a backed-up writer can flush several sinks; logged-and-skipped
past the deadline so shutdown doesn't hang."""


class WriterThreadError(CapaError):
    """Raised when the writer thread enters an unrecoverable state."""


class WriterThread:
    """Dedicated background thread that owns all sink writes for one run.

    Lifecycle: ``start()`` → ``submit*`` × N → ``close()``. The async API
    (:meth:`submit`, :meth:`record_sample` and friends) is the entry point
    for the event loop. The synchronous ``submit_nowait`` / ``submit_blocking``
    primitives are exposed for callers that already know which path they
    want; the async helpers route through them.

    Exceptions raised inside the drain loop are captured and re-raised on
    the next ``submit`` or ``close`` so the engine never silently loses a
    dead writer thread.
    """

    __slots__ = (
        "_capacity",
        "_closed",
        "_depth_high_water",
        "_inbox",
        "_last_accept_monotonic_ns",
        "_logger",
        "_metrics",
        "_started",
        "_submit_blocked_count",
        "_thread",
        "_thread_exc",
        "_writer",
    )

    def __init__(
        self,
        writer: RunBundleWriter,
        *,
        metrics: WriterMetrics | None = None,
        logger: structlog.stdlib.BoundLogger | None = None,
        capacity: int = DEFAULT_CAPACITY,
    ) -> None:
        if capacity < 1:
            raise ValueError("WriterThread capacity must be >= 1")
        self._writer = writer
        self._metrics = metrics
        self._logger = (
            logger if logger is not None else structlog.get_logger("capa.storage.writer_thread")
        )
        self._capacity = capacity
        self._inbox: queue.Queue[WriterItem | _CloseSentinel] = queue.Queue(maxsize=capacity)
        self._thread: threading.Thread | None = None
        self._started = False
        self._closed = False
        self._thread_exc: BaseException | None = None
        self._submit_blocked_count = 0
        self._depth_high_water = 0
        # Monotonic-ns timestamp of the most recent successful inbox-pop by
        # the drain thread. The Conductor saturation monitor reads this to
        # detect a stalled writer: if `depth > 0` but `last_accept_monotonic`
        # hasn't advanced within `saturation_deadline_s`, the writer has
        # stopped accepting work (migration doc §4.5). Initialized to the
        # start-of-process monotonic time so a freshly-started, empty inbox
        # doesn't immediately read as "stalled". Updated without locks; a
        # single 64-bit int store is atomic under the GIL on CPython and the
        # reader treats the value as advisory.
        self._last_accept_monotonic_ns = time.monotonic_ns()

    # ------------------------------------------------------------------ props

    @property
    def depth(self) -> int:
        """Current inbox depth (approximate; the drain loop may be popping)."""
        return self._inbox.qsize()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def submit_blocked_count(self) -> int:
        """Number of times :meth:`submit` had to wait for inbox space."""
        return self._submit_blocked_count

    @property
    def depth_high_water(self) -> int:
        """High-water mark for inbox depth across the run."""
        return self._depth_high_water

    @property
    def last_accept_monotonic_ns(self) -> int:
        """Monotonic-ns timestamp of the most recent successful inbox pop.

        Read by the Conductor saturation monitor (migration doc §4.5) to
        detect a stalled writer: ``depth > 0`` combined with
        ``last_accept_monotonic_ns`` not advancing for
        ``saturation_deadline_s`` is the canonical writer-stall signal.

        Initialized to process-start so an empty, just-started inbox never
        appears stalled. Advanced inside the drain loop after each
        :meth:`_dispatch` returns successfully (i.e. only counts items the
        writer actually processed, not the close sentinel).
        """
        return self._last_accept_monotonic_ns

    def snapshot(self) -> dict[str, float]:
        """Render an inbox-health entry in the shape
        :class:`~capa.storage.manifest.QueueHealthEntry` consumes, so the
        engine can plug this into :class:`MetricsRegistry` at finalize.
        """
        return {
            "depth_max": float(self._depth_high_water),
            "depth_p50": 0.0,
            "depth_p99": 0.0,
            "lag_s_max": 0.0,
            "capacity": float(self._capacity),
            "submit_blocked_count": float(self._submit_blocked_count),
            "last_accept_monotonic_ns": float(self._last_accept_monotonic_ns),
        }

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def started(self) -> bool:
        return self._started

    @property
    def closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Spawn the drain thread. Idempotent only in the failure direction —
        a second call after a real start raises."""
        if self._started:
            raise WriterThreadError("WriterThread.start() called twice")
        self._started = True
        self._thread = threading.Thread(target=self._drain_loop, name="capa-writer", daemon=False)
        self._thread.start()

    def close(self, *, timeout: float = DEFAULT_CLOSE_TIMEOUT_S) -> bool:
        """Send the close sentinel, wait for drain, join.

        Returns ``True`` on clean drain. Returns ``False`` if the join
        timed out — the caller should log and proceed; partial data is
        already durable thanks to per-flush ``fsync``. Re-raises any
        exception captured inside the drain loop so the engine sees the
        cause.
        """
        if not self._started or self._thread is None:
            self._closed = True
            return True
        if self._closed:
            return True
        self._closed = True
        try:
            # Use a finite timeout matching the join — a full inbox at
            # close time is unusual (the engine drains producers first)
            # but we don't want to hang here either.
            self._inbox.put(_CLOSE_SENTINEL, timeout=max(timeout, 1.0))
        except queue.Full:
            self._logger.error("writer_thread.close.sentinel_enqueue_failed")
            return False
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            self._logger.error(
                "writer_thread.close.join_timeout",
                timeout_s=timeout,
                inbox_depth=self._inbox.qsize(),
            )
            return False
        if self._thread_exc is not None:
            raise WriterThreadError(
                f"writer thread died: {type(self._thread_exc).__name__}: {self._thread_exc}"
            ) from self._thread_exc
        return True

    # ------------------------------------------------------------------ submit

    def submit_nowait(self, item: WriterItem) -> bool:
        """Non-blocking submit. Returns ``True`` on success, ``False`` if
        the inbox is full. Raises if the writer thread has died."""
        self._raise_if_thread_dead()
        if self._closed:
            raise WriterThreadError("submit_nowait() after close()")
        if not self._started:
            raise WriterThreadError("submit_nowait() before start()")
        try:
            self._inbox.put_nowait(item)
        except queue.Full:
            return False
        depth = self._inbox.qsize()
        if depth > self._depth_high_water:
            self._depth_high_water = depth
        return True

    def submit_blocking(self, item: WriterItem) -> None:
        """Blocking submit. Call from a worker thread (e.g. via
        :func:`anyio.to_thread.run_sync`) — never from the event loop
        thread directly, since this can park indefinitely on disk stalls.
        """
        self._raise_if_thread_dead()
        if self._closed:
            raise WriterThreadError("submit_blocking() after close()")
        if not self._started:
            raise WriterThreadError("submit_blocking() before start()")
        self._submit_blocked_count += 1
        self._inbox.put(item)
        depth = self._inbox.qsize()
        if depth > self._depth_high_water:
            self._depth_high_water = depth

    async def submit(self, item: WriterItem) -> None:
        """Async submit with end-to-end backpressure.

        Tries the non-blocking fast path first; falls back to a blocking
        put running on a worker thread, which yields to the event loop
        while waiting for inbox space. The caller's task pauses; sibling
        tasks (UI, producers up to the producer-fanout queue cap) keep
        running.
        """
        if self.submit_nowait(item):
            return
        await anyio.to_thread.run_sync(self.submit_blocking, item)

    # ------------------------------------------------------------------ async record helpers

    async def record_sample(self, sample: ChannelSample) -> None:
        await self.submit(sample)

    async def record_source(self, record: SourceRecord) -> None:
        await self.submit(record)

    async def record_event(self, event: DeviceEvent) -> None:
        await self.submit(event)

    async def record_snapshot(self, snapshot: DeviceSnapshot) -> None:
        await self.submit(snapshot)

    async def record_frame(self, receipt: FrameReceipt) -> None:
        await self.submit(FrameItem(receipt=receipt))

    async def write_event(
        self,
        *,
        kind: str,
        message: str,
        severity: str = "info",
        source: str = "engine",
        t_mono_ns: int,
        t_utc: datetime,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self.submit(
            WriteEventItem(
                kind=kind,
                message=message,
                severity=severity,
                source=source,
                t_mono_ns=t_mono_ns,
                t_utc=t_utc,
                metadata=metadata,
            )
        )

    # ------------------------------------------------------------------ internals

    def _raise_if_thread_dead(self) -> None:
        if self._thread_exc is not None:
            raise WriterThreadError(
                f"writer thread died: {type(self._thread_exc).__name__}: {self._thread_exc}"
            ) from self._thread_exc

    def _drain_loop(self) -> None:
        try:
            while True:
                item = self._inbox.get()
                if isinstance(item, _CloseSentinel):
                    return
                start = time.monotonic()
                self._dispatch(item)
                # Advance the saturation signal AFTER dispatch returns so a
                # mid-flush stall reads as "not accepting" rather than
                # "accepted then crashed". Single int store is atomic under
                # the GIL; under PEP 703 free-threaded Python the reader
                # tolerates a slightly-stale read (advisory signal).
                self._last_accept_monotonic_ns = time.monotonic_ns()
                if self._metrics is not None:
                    self._metrics.observe_write(time.monotonic() - start)
        except BaseException as exc:  # surface to the engine via _thread_exc
            self._thread_exc = exc
            self._logger.error(
                "writer_thread.crashed",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def _dispatch(self, item: WriterItem) -> None:
        match item:
            case ChannelSample():
                self._writer.record_sample(item)
            case SourceRecord():
                self._writer.record_source(item)
            case DeviceEvent():
                self._writer.record_event(item)
            case DeviceSnapshot():
                self._writer.record_snapshot(item)
            case FrameItem(receipt=receipt):
                self._writer.record_frame(receipt)
            case WriteEventItem():
                self._writer.write_event(
                    kind=item.kind,
                    message=item.message,
                    severity=item.severity,
                    source=item.source,
                    t_mono_ns=item.t_mono_ns,
                    t_utc=item.t_utc,
                    metadata=item.metadata,
                )


__all__ = [
    "DEFAULT_CAPACITY",
    "DEFAULT_CLOSE_TIMEOUT_S",
    "FrameItem",
    "WriteEventItem",
    "WriterItem",
    "WriterThread",
    "WriterThreadError",
]
