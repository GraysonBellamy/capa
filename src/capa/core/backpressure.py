"""Named backpressure policies and a :class:`BoundedQueue` that enforces them.

every queue in the pipeline declares one of three policies, and
sinks that violate their policy are a bug, not a configuration choice. Plan
§13 gives the same rule a procedural framing — durable storage never silently
loses data; UI never blocks acquisition; safety has its own queue.

Ships the policy enum + a small queue helper; the engine task group
wires producers to fan-outs.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

import anyio

from capa.core.errors import BackpressureAbortError


class BackpressurePolicy(Enum):
    """How a queue behaves when it reaches capacity."""

    BLOCK = "block"
    """Producer waits for space, indefinitely. Use only on bounded internal
    queues with a guaranteed consumer (e.g. the writer-thread inbox).

    **Not valid on** :class:`~capa.core.databus.DataBus` **subscriptions** —
    a stuck BLOCK subscriber would freeze the fan-out and back-pressure
    every adapter. Must-not-drop databus subscribers use
    :meth:`~capa.core.databus.DataBus.subscribe_critical` (``ABORT_RUN``
    with a deadline); the databus rejects ``BLOCK`` at registration time."""

    DROP_OLDEST = "drop_oldest"
    """Ring-buffer semantics. Use when freshness > completeness (UI plots)."""

    ABORT_RUN = "abort_run"
    """Fault if the queue stays full past a timeout. Use when neither blocking
    nor dropping is acceptable (durable sinks past their grace window)."""


@dataclass(slots=True)
class QueueStats:
    """Live statistics for a :class:`BoundedQueue`. Mirrored into
    ``manifest.json``\\ ``.queue_health`` at finalize.
    """

    depth_high_water: int = 0
    dropped: int = 0
    enqueued: int = 0
    dequeued: int = 0
    block_waits: int = 0
    last_full_at_mono_ns: int | None = None


@dataclass(slots=True)
class BoundedQueue[T]:
    """A bounded async queue with a declared :class:`BackpressurePolicy`.

    Wraps an :class:`anyio.abc.ObjectStream`-style send/receive pair using a
    plain ``deque`` and an :class:`anyio.Event` so it is implementation-agnostic
    across asyncio/trio backends. Not intended for inter-thread use; producers
    and consumers run inside the same AnyIO task group.

    The :class:`BackpressurePolicy.ABORT_RUN` semantics use a stuck-window
    timer: if the queue has been at capacity continuously for ``abort_after_s``
    seconds, the next ``put()`` raises :class:`BackpressureAbort`. The timer
    resets every time space is freed.
    """

    name: str
    capacity: int
    policy: BackpressurePolicy
    abort_after_s: float = 5.0
    _items: deque[T] = field(default_factory=deque)
    _not_empty: anyio.Event = field(default_factory=anyio.Event)
    _not_full: anyio.Event = field(default_factory=anyio.Event)
    stats: QueueStats = field(default_factory=QueueStats)
    _stuck_since_mono: float | None = None
    _closed: bool = False

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise ValueError("capacity must be >= 1")
        # not_full is initially set (queue is empty -> has space)
        self._not_full.set()

    @property
    def depth(self) -> int:
        return len(self._items)

    def _record_enqueue(self) -> None:
        self.stats.enqueued += 1
        self.stats.depth_high_water = max(self.stats.depth_high_water, len(self._items))
        if len(self._items) >= self.capacity:
            self._not_full = anyio.Event()
            if self._stuck_since_mono is None:
                self._stuck_since_mono = time.monotonic()
        self._not_empty.set()

    def _record_dequeue(self) -> None:
        self.stats.dequeued += 1
        if len(self._items) < self.capacity:
            self._stuck_since_mono = None
            self._not_full.set()
        if not self._items:
            self._not_empty = anyio.Event()

    async def put(self, item: T) -> None:
        """Enqueue, applying the policy on overflow.

        For :class:`BackpressurePolicy.BLOCK`, awaits until space is free.
        For :class:`BackpressurePolicy.DROP_OLDEST`, evicts the oldest item.
        For :class:`BackpressurePolicy.ABORT_RUN`, raises
        :class:`BackpressureAbort` once the queue has been continuously full
        for :attr:`abort_after_s`.
        """
        if self._closed:
            raise RuntimeError(f"queue {self.name!r} is closed")

        if len(self._items) < self.capacity:
            self._items.append(item)
            self._record_enqueue()
            return

        match self.policy:
            case BackpressurePolicy.DROP_OLDEST:
                self._items.popleft()
                self.stats.dropped += 1
                self._items.append(item)
                self._record_enqueue()
            case BackpressurePolicy.BLOCK:
                self.stats.block_waits += 1
                while len(self._items) >= self.capacity:
                    await self._not_full.wait()
                    if self._closed:
                        raise RuntimeError(f"queue {self.name!r} closed during put")
                self._items.append(item)
                self._record_enqueue()
            case BackpressurePolicy.ABORT_RUN:
                # Check if we've been at capacity longer than abort_after_s.
                if (
                    self._stuck_since_mono is not None
                    and (time.monotonic() - self._stuck_since_mono) >= self.abort_after_s
                ):
                    raise BackpressureAbortError(
                        f"queue {self.name!r} full for >{self.abort_after_s}s "
                        f"(capacity={self.capacity}, policy={self.policy.value})"
                    )
                # Not yet stuck-window-exceeded: behave like BLOCK with a deadline.
                deadline = (self._stuck_since_mono or time.monotonic()) + self.abort_after_s
                while len(self._items) >= self.capacity:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise BackpressureAbortError(
                            f"queue {self.name!r} full for >{self.abort_after_s}s"
                        )
                    with anyio.move_on_after(remaining):
                        await self._not_full.wait()
                self._items.append(item)
                self._record_enqueue()

    def put_nowait(self, item: T) -> bool:
        """Non-blocking put. Returns ``True`` on success.

        Useful inside synchronous callbacks. ``DROP_OLDEST`` always succeeds;
        ``BLOCK`` and ``ABORT_RUN`` return ``False`` on a full queue rather
        than raising. Aborting on a stuck queue is a job for ``put()``.
        """
        if self._closed:
            return False
        if len(self._items) < self.capacity:
            self._items.append(item)
            self._record_enqueue()
            return True
        if self.policy is BackpressurePolicy.DROP_OLDEST:
            self._items.popleft()
            self.stats.dropped += 1
            self._items.append(item)
            self._record_enqueue()
            return True
        return False

    async def get(self) -> T:
        """Dequeue, awaiting if empty."""
        while not self._items:
            if self._closed:
                raise RuntimeError(f"queue {self.name!r} is closed")
            await self._not_empty.wait()
        item = self._items.popleft()
        self._record_dequeue()
        return item

    def get_nowait(self) -> T | None:
        if not self._items:
            return None
        item = self._items.popleft()
        self._record_dequeue()
        return item

    def close(self) -> None:
        """Mark closed; pending awaiters will raise on wakeup."""
        self._closed = True
        self._not_empty.set()
        self._not_full.set()


__all__ = [
    "BackpressurePolicy",
    "BoundedQueue",
    "QueueStats",
]
