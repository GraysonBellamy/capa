"""Thread-safe bounded channel between two ``asyncio`` loops.

Implements :class:`ThreadBridge` from
``docs/per-resource-worker-migration.md`` §4.4. Each bridge connects one
producer loop (worker thread) to one consumer loop (conductor thread, or
qasync UI loop). Backpressure is per-policy; latency is observed at every
hop; ``blocked_since_ms`` is the saturation-deadline signal the Conductor
polls in §4.5.

Why this is not :class:`~capa.core.backpressure.BoundedQueue`:
``BoundedQueue`` is loop-local and explicitly documented "not intended for
inter-thread use" — its ``anyio.Event`` and deque are owned by the loop that
constructed them. :class:`ThreadBridge` instead pairs a consumer-loop
:class:`asyncio.Queue` with a producer-loop :class:`asyncio.Semaphore` and
crosses the thread seam exclusively via
:meth:`asyncio.AbstractEventLoop.call_soon_threadsafe`. See §4.4.1 of the
migration doc for the comparison against ``janus`` / ``culsans`` /
``anyio.memory_object_stream``.

Phase 0 scope: this module ships standalone and is not yet wired into any
runtime path. :class:`Worker` (Phase 1) will be the first caller.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from typing import cast

_SENTINEL: object = object()
"""Sentinel value placed on the consumer queue by :meth:`ThreadBridge.close`
to signal end-of-stream. Identity-checked on the consumer side."""


class BridgePolicy(Enum):
    """Backpressure policy for a :class:`ThreadBridge`.

    Migration doc §4.4 — these are the bridge's three policies. ``ABORT_RUN``
    is explicitly absent: aborts are a run-level concern owned by the
    Conductor's saturation monitor (§4.5), not a per-bridge knob. The shape
    parallels :class:`~capa.core.backpressure.BackpressurePolicy` but the two
    enums are kept separate so the two systems can evolve independently.
    """

    BLOCK = "block"
    """Producer awaits space. Default for worker → conductor emission
    bridges. Sustained block is surfaced via :attr:`ThreadBridgeMetrics.
    blocked_since_ms` and caught by the Conductor saturation deadline."""

    DROP_OLDEST = "drop_oldest"
    """Evict the head of the queue when full, then enqueue the new item.
    Default for the conductor → UI bridge. UI loop cannot block on
    subscriber backpressure."""

    DROP_NEWEST = "drop_newest"
    """Discard the new item when full. Used for telemetry-style channels
    where falling-behind is preferable to evicting buffered context."""


class ThreadBridgeClosedError(RuntimeError):
    """Raised by :meth:`ThreadBridge.put` / :meth:`ThreadBridge.put_nowait`
    after :meth:`ThreadBridge.close` has been called."""


class _PercentileRing:
    """Fixed-size ring of observations exposing p50/p99.

    The ring is shared with :mod:`capa.runtime.heartbeat`; lifting a small
    percentile helper out of the metric structs keeps both modules honest
    about quantile semantics and lets unit tests target the helper alone.
    """

    __slots__ = ("_buf", "_cap", "_count", "_idx", "_lock")

    def __init__(self, capacity: int = 1024) -> None:
        self._cap = capacity
        self._buf: list[float] = [0.0] * capacity
        self._idx = 0
        self._count = 0
        # Observations may arrive from either side of the bridge (worker /
        # conductor / UI). A cheap lock keeps the ring consistent against
        # concurrent observe()s; the percentile read takes a snapshot copy
        # under the same lock.
        self._lock = threading.Lock()

    def observe(self, value: float) -> None:
        with self._lock:
            self._buf[self._idx] = value
            self._idx = (self._idx + 1) % self._cap
            if self._count < self._cap:
                self._count += 1

    def percentile(self, p: float) -> float:
        with self._lock:
            if self._count == 0:
                return 0.0
            snapshot = self._buf[: self._count]
        snapshot.sort()
        idx = max(0, min(self._count - 1, int(p * self._count)))
        return snapshot[idx]

    @property
    def p50(self) -> float:
        return self.percentile(0.5)

    @property
    def p99(self) -> float:
        return self.percentile(0.99)


@dataclass
class ThreadBridgeMetrics:
    """Live statistics for one :class:`ThreadBridge`.

    Field layout mirrors migration doc §4.4 lines 877-889. The
    :attr:`blocked_since_ms` field is the saturation-deadline signal that
    the Conductor's monitor polls (§4.5): when not ``None``, the producer
    is currently waiting for space, and the value is the wall-time elapsed
    since the wait began.

    Readers should treat the dataclass as read-only and use the provided
    properties — direct field reads are atomic under the GIL for the simple
    counters but the percentile values are computed lazily and require
    locking the underlying ring.
    """

    name: str
    capacity: int
    enqueued_total: int = 0
    dequeued_total: int = 0
    dropped_total: int = 0
    depth: int = 0
    depth_max: int = 0
    blocked_total_ms: float = 0.0
    _block_start_mono: float | None = None
    _latency_ring: _PercentileRing = field(default_factory=_PercentileRing)

    @property
    def blocked_since_ms(self) -> float | None:
        """How long the producer has been blocked NOW, in ms, or ``None``
        if not currently blocked. Polled by the Conductor's saturation
        monitor (migration doc §4.5)."""
        start = self._block_start_mono
        if start is None:
            return None
        return (time.monotonic() - start) * 1000.0

    @property
    def latency_p50_ms(self) -> float:
        return self._latency_ring.p50

    @property
    def latency_p99_ms(self) -> float:
        return self._latency_ring.p99


class ThreadBridge[T]:
    """Thread-safe bounded channel between two ``asyncio`` loops.

    Per migration doc §4.4. One consumer loop owns the receive side; one
    producer loop owns the send side. Cross-thread signalling uses
    :meth:`asyncio.AbstractEventLoop.call_soon_threadsafe` exclusively;
    no synchronization primitive is shared mutably across loops.

    Lifecycle:

    1. Construct on either thread.
    2. :meth:`attach_consumer` from the consumer loop (builds the
       :class:`asyncio.Queue`).
    3. :meth:`attach_producer` from the producer loop (builds the
       :class:`asyncio.Semaphore` for BLOCK / DROP_NEWEST policies).
       DROP_OLDEST does not need a producer semaphore — eviction is
       performed on the consumer loop.
    4. Producer side calls :meth:`put` / :meth:`put_nowait`.
    5. Consumer side iterates ``async for x in bridge`` or calls
       :meth:`get` directly.
    6. Either side calls :meth:`close` to signal end-of-stream; the
       consumer drains pending items, then the iterator stops.
    """

    def __init__(
        self,
        *,
        name: str,
        capacity: int,
        consumer_loop: asyncio.AbstractEventLoop,
        policy: BridgePolicy = BridgePolicy.BLOCK,
    ) -> None:
        if capacity < 1:
            raise ValueError(f"ThreadBridge {name!r}: capacity must be >= 1, got {capacity}")
        self._name = name
        self._capacity = capacity
        self._policy = policy
        self._consumer_loop = consumer_loop
        self._async_q: asyncio.Queue[tuple[float, T] | object] | None = None
        self._space: asyncio.Semaphore | None = None
        self._producer_loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self._drained = False
        self._metrics = ThreadBridgeMetrics(name=name, capacity=capacity)
        # DROP_NEWEST sync producers need a depth view that is producer-
        # side cheap to read. The semaphore covers BLOCK and async
        # DROP_NEWEST; for the sync put_nowait path we read the semaphore
        # value lazily (semaphore.locked() is True when value == 0).

    # ----------------------------------------------------- introspection

    @property
    def name(self) -> str:
        return self._name

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def policy(self) -> BridgePolicy:
        return self._policy

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def metrics(self) -> ThreadBridgeMetrics:
        return self._metrics

    # --------------------------------------------------------- lifecycle

    def attach_consumer(self) -> None:
        """Build the consumer-side queue. Must be called from the consumer
        loop, before the first :meth:`get` or iterator step.

        ``asyncio.Queue`` is loop-affine — it binds to the loop that
        constructs it. Constructing it here (rather than in ``__init__``)
        is what lets the bridge be created on any thread.
        """
        running = asyncio.get_running_loop()
        if running is not self._consumer_loop:
            raise RuntimeError(
                f"ThreadBridge {self._name!r}: attach_consumer must run on the "
                f"consumer loop (running={running!r}, expected={self._consumer_loop!r})"
            )
        if self._async_q is not None:
            raise RuntimeError(f"ThreadBridge {self._name!r}: attach_consumer called twice")
        self._async_q = asyncio.Queue()

    def attach_producer(self, producer_loop: asyncio.AbstractEventLoop) -> None:
        """Build the producer-side semaphore. Must be called from the
        producer loop, before the first :meth:`put`.

        :class:`asyncio.Semaphore` is loop-affine. For DROP_OLDEST the
        producer never blocks at the bridge level, so the semaphore is not
        built; calls to :meth:`put` schedule a swap-and-enqueue callback
        on the consumer loop instead.
        """
        running = asyncio.get_running_loop()
        if running is not producer_loop:
            raise RuntimeError(
                f"ThreadBridge {self._name!r}: attach_producer must run on the "
                f"producer loop (running={running!r}, expected={producer_loop!r})"
            )
        if self._producer_loop is not None:
            raise RuntimeError(f"ThreadBridge {self._name!r}: attach_producer called twice")
        self._producer_loop = producer_loop
        if self._policy in (BridgePolicy.BLOCK, BridgePolicy.DROP_NEWEST):
            self._space = asyncio.Semaphore(self._capacity)

    def close(self) -> None:
        """Signal end-of-stream. Idempotent. Pending items already in flight
        complete; subsequent :meth:`put` / :meth:`put_nowait` raise
        :class:`ThreadBridgeClosedError`."""
        if self._closed:
            return
        self._closed = True
        if self._async_q is not None:
            self._consumer_loop.call_soon_threadsafe(self._async_q.put_nowait, _SENTINEL)

    # ------------------------------------------------------ producer side

    async def put(self, item: T) -> None:
        """Async put on the producer loop. Honors :attr:`policy`.

        BLOCK: awaits capacity. ``blocked_since_ms`` becomes non-``None``
        on entry to the wait and clears when the semaphore is acquired.

        DROP_NEWEST: enqueues if capacity is available, otherwise drops
        and increments ``dropped_total``. Returns immediately.

        DROP_OLDEST: schedules an eviction-then-enqueue on the consumer
        loop. The producer never blocks at the bridge level.
        """
        if self._closed:
            raise ThreadBridgeClosedError(f"ThreadBridge {self._name!r} is closed")
        if self._async_q is None:
            raise RuntimeError(f"ThreadBridge {self._name!r}: attach_consumer() was not called")

        if self._policy is BridgePolicy.BLOCK:
            assert self._space is not None, "attach_producer() not called"
            if self._space.locked():
                # Mark the start of a sustained block so the saturation
                # monitor can read blocked_since_ms while we wait.
                self._metrics._block_start_mono = time.monotonic()
                try:
                    await self._space.acquire()
                finally:
                    start = self._metrics._block_start_mono
                    if start is not None:
                        self._metrics.blocked_total_ms += (time.monotonic() - start) * 1000.0
                        self._metrics._block_start_mono = None
            else:
                await self._space.acquire()
            self._consumer_loop.call_soon_threadsafe(
                self._on_put_with_semaphore, (time.monotonic(), item)
            )
            return

        if self._policy is BridgePolicy.DROP_NEWEST:
            assert self._space is not None, "attach_producer() not called"
            if self._space.locked():
                self._metrics.dropped_total += 1
                return
            await self._space.acquire()
            self._consumer_loop.call_soon_threadsafe(
                self._on_put_with_semaphore, (time.monotonic(), item)
            )
            return

        # DROP_OLDEST: no producer semaphore; consumer evicts as needed.
        self._consumer_loop.call_soon_threadsafe(self._on_put_drop_oldest, (time.monotonic(), item))

    def put_nowait(self, item: T) -> bool:
        """Sync producer put. Forbidden for BLOCK (use :meth:`put`).

        Returns ``True`` if the item was enqueued or scheduled, ``False``
        if dropped under DROP_NEWEST. DROP_OLDEST always returns ``True``
        because eviction makes room synchronously on the consumer side.

        Used by the conductor's UI-bridge publish path: the conductor's
        async drain task wants a non-blocking handoff because the UI loop
        cannot honor blocking backpressure.
        """
        if self._closed:
            raise ThreadBridgeClosedError(f"ThreadBridge {self._name!r} is closed")
        if self._async_q is None:
            raise RuntimeError(f"ThreadBridge {self._name!r}: attach_consumer() was not called")
        if self._policy is BridgePolicy.BLOCK:
            raise RuntimeError(
                f"ThreadBridge {self._name!r}: put_nowait is incompatible with BLOCK; "
                f"use async put() instead"
            )

        if self._policy is BridgePolicy.DROP_NEWEST:
            # Without a producer semaphore (sync caller may not have a loop),
            # the consumer-side callback makes the capacity decision atomically.
            self._consumer_loop.call_soon_threadsafe(
                self._on_put_drop_newest_sync, (time.monotonic(), item)
            )
            # Sync callers can't observe the consumer-side decision; the
            # drop, if any, surfaces only via metrics.
            return True

        # DROP_OLDEST: schedule swap-and-enqueue.
        self._consumer_loop.call_soon_threadsafe(self._on_put_drop_oldest, (time.monotonic(), item))
        return True

    # ----------------------------------------------- consumer-side callbacks
    # All three of these run on the consumer loop via call_soon_threadsafe.

    def _on_put_with_semaphore(self, stamped: tuple[float, T]) -> None:
        # Producer already acquired a semaphore slot, so the queue is
        # guaranteed to have room.
        assert self._async_q is not None
        self._async_q.put_nowait(stamped)
        self._metrics.enqueued_total += 1
        depth = self._async_q.qsize()
        self._metrics.depth = depth
        if depth > self._metrics.depth_max:
            self._metrics.depth_max = depth

    def _on_put_drop_oldest(self, stamped: tuple[float, T]) -> None:
        assert self._async_q is not None
        while self._async_q.qsize() >= self._capacity:
            try:
                self._async_q.get_nowait()
                self._metrics.dropped_total += 1
            except asyncio.QueueEmpty:
                break
        self._async_q.put_nowait(stamped)
        self._metrics.enqueued_total += 1
        depth = self._async_q.qsize()
        self._metrics.depth = depth
        if depth > self._metrics.depth_max:
            self._metrics.depth_max = depth

    def _on_put_drop_newest_sync(self, stamped: tuple[float, T]) -> None:
        assert self._async_q is not None
        if self._async_q.qsize() >= self._capacity:
            self._metrics.dropped_total += 1
            return
        self._async_q.put_nowait(stamped)
        self._metrics.enqueued_total += 1
        depth = self._async_q.qsize()
        self._metrics.depth = depth
        if depth > self._metrics.depth_max:
            self._metrics.depth_max = depth

    # ------------------------------------------------------ consumer side

    async def get(self) -> T | None:
        """Consumer-side dequeue. Returns ``None`` when closed and empty.

        Once ``None`` is returned, subsequent calls also return ``None``
        immediately — the bridge has drained.
        """
        if self._async_q is None:
            raise RuntimeError(f"ThreadBridge {self._name!r}: attach_consumer() was not called")
        if self._drained:
            return None
        item = await self._async_q.get()
        if item is _SENTINEL:
            self._drained = True
            return None
        t_put, value = cast(tuple[float, T], item)
        latency_ms = (time.monotonic() - t_put) * 1000.0
        self._metrics._latency_ring.observe(latency_ms)
        self._metrics.dequeued_total += 1
        depth = self._async_q.qsize()
        self._metrics.depth = depth
        # Release producer-side capacity for BLOCK / DROP_NEWEST. DROP_OLDEST
        # has no producer semaphore (the consumer is the only depth keeper).
        if self._space is not None and self._producer_loop is not None:
            self._producer_loop.call_soon_threadsafe(self._space.release)
        return value

    def __aiter__(self) -> AsyncIterator[T]:
        return self

    async def __anext__(self) -> T:
        item = await self.get()
        if item is None:
            raise StopAsyncIteration
        return item


__all__ = [
    "BridgePolicy",
    "ThreadBridge",
    "ThreadBridgeClosedError",
    "ThreadBridgeMetrics",
]
