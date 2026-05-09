"""In-process pub/sub for :class:`DeviceEmission`.

Plan §3, §7. The engine fan-out reads from per-adapter producer tasks and
publishes onto the :class:`DataBus`; the safety monitor, the (P1) UI ring
buffers, and procedure subscribers all consume from it. The durable sinks do
*not* go through here — the engine fan-out forwards to the bundle writer
directly so a slow disk never starves a UI plot.

Each subscription owns its own :class:`BoundedQueue` and declares its own
:class:`BackpressurePolicy`. The publisher's job is "drop into every
subscription's queue and move on"; per-policy semantics fire inside
:meth:`BoundedQueue.put`.

P0c ships the smallest viable surface: filter-by-adapter and filter-by-channel
predicates, with a catch-all ``subscribe_all``. Filter-by-kind (e.g. only
``DeviceEvent``) lands when a procedure needs it; the API is open enough to
add later without breaking subscribers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Final

from capa.core.backpressure import BackpressurePolicy, BoundedQueue
from capa.devices.records import (
    ChannelSample,
    DeviceEmission,
    DeviceEvent,
    DeviceSnapshot,
    SourceRecord,
)

_PredicateFn = Callable[[DeviceEmission], bool]


DEFAULT_SUBSCRIBER_CAPACITY: Final[int] = 256
"""Per-subscription buffer. Sized for capa's 3–60 Hz envelope; UI ring buffers
override this in P1 with their own decimation cadence."""


@dataclass(slots=True)
class Subscription:
    """One active subscriber, one queue.

    ``predicate`` returns ``True`` for emissions this subscriber wants to see.
    ``policy`` decides what happens when the per-subscription queue overflows.
    """

    name: str
    queue: BoundedQueue[DeviceEmission]
    predicate: _PredicateFn

    async def __aiter__(self) -> AsyncIterator[DeviceEmission]:
        while True:
            item = await self.queue.get()
            yield item


class DataBus:
    """In-process pub/sub for adapter emissions.

    Hot path is :meth:`publish`: O(N) over the active subscription list, each
    enqueue routed through the subscription's :class:`BoundedQueue`. There is
    no global lock — subscribers register and unregister via list-replacement.
    """

    __slots__ = ("_closed", "_last_values", "_subscriptions")

    def __init__(self) -> None:
        self._subscriptions: list[Subscription] = []
        self._closed = False
        # Latest scalar value seen on each channel, populated on publish so
        # subscribers (and procedure helpers like MethodExecutor) can do
        # cheap "what was the last value?" lookups without re-implementing
        # a per-channel ring. Only ChannelSample emissions populate this.
        self._last_values: dict[str, float] = {}

    # ------------------------------------------------------------------ subscribe

    def subscribe(
        self,
        name: str,
        *,
        predicate: _PredicateFn | None = None,
        capacity: int = DEFAULT_SUBSCRIBER_CAPACITY,
        policy: BackpressurePolicy = BackpressurePolicy.DROP_OLDEST,
        abort_after_s: float = 5.0,
    ) -> Subscription:
        """Register a subscription and return it.

        Default ``policy=DROP_OLDEST`` matches the UI / ring-buffer use case;
        the safety monitor passes ``BLOCK`` so it never silently misses
        evaluations.
        """
        if self._closed:
            raise RuntimeError("DataBus is closed")
        queue: BoundedQueue[DeviceEmission] = BoundedQueue(
            name=f"databus:{name}",
            capacity=capacity,
            policy=policy,
            abort_after_s=abort_after_s,
        )
        sub = Subscription(name=name, queue=queue, predicate=predicate or _always_true)
        self._subscriptions.append(sub)
        return sub

    def subscribe_all(self, name: str, **kwargs: int | BackpressurePolicy | float) -> Subscription:
        """Convenience: subscribe to every emission."""
        return self.subscribe(name, predicate=_always_true, **kwargs)  # type: ignore[arg-type]

    def subscribe_channel(
        self,
        name: str,
        *,
        channel: str,
        **kwargs: int | BackpressurePolicy | float,
    ) -> Subscription:
        """Subscribe to a single channel — receives :class:`ChannelSample`
        emissions whose :attr:`ChannelSample.channel` matches."""
        return self.subscribe(
            name,
            predicate=_channel_predicate(channel),
            **kwargs,  # type: ignore[arg-type]
        )

    def subscribe_adapter(
        self,
        name: str,
        *,
        adapter: str,
        **kwargs: int | BackpressurePolicy | float,
    ) -> Subscription:
        """Subscribe to every emission from a given adapter."""
        return self.subscribe(
            name,
            predicate=_adapter_predicate(adapter),
            **kwargs,  # type: ignore[arg-type]
        )

    def unsubscribe(self, sub: Subscription) -> None:
        """Remove ``sub`` from the active list and close its queue.

        Safe to call multiple times; a missing subscription is a no-op.
        """
        try:
            self._subscriptions.remove(sub)
        except ValueError:
            return
        sub.queue.close()

    # ------------------------------------------------------------------ publish

    async def publish(self, emission: DeviceEmission) -> None:
        """Route ``emission`` to every matching subscription.

        Per-subscription :class:`BackpressurePolicy` is honored inside
        :meth:`BoundedQueue.put`; ``BLOCK`` subscriptions can pause this
        coroutine, ``DROP_OLDEST`` ones never do, ``ABORT_RUN`` ones raise
        once their stuck-window expires (which the engine surfaces as a
        crash exit).
        """
        if self._closed:
            return
        if isinstance(emission, ChannelSample):
            with suppress(TypeError, ValueError):
                self._last_values[emission.channel] = float(emission.value)
        # Snapshot the subscriber list before iterating so a late
        # unsubscribe/close mid-publish is safe.
        for sub in list(self._subscriptions):
            if not sub.predicate(emission):
                continue
            await sub.queue.put(emission)

    def publish_nowait(self, emission: DeviceEmission) -> None:
        """Synchronous publish: matches each subscription and uses
        :meth:`BoundedQueue.put_nowait`. ``BLOCK`` subscribers that are full
        silently drop here — the caller chose to take a non-async path."""
        if self._closed:
            return
        if isinstance(emission, ChannelSample):
            with suppress(TypeError, ValueError):
                self._last_values[emission.channel] = float(emission.value)
        for sub in list(self._subscriptions):
            if not sub.predicate(emission):
                continue
            sub.queue.put_nowait(emission)

    def last_value(self, channel: str) -> float | None:
        """Return the most-recent scalar value seen on ``channel``, or
        ``None`` if no sample has been published.

        Used by :class:`~capa.experiment.executor.MethodExecutor` to discover
        the current setpoint when a ramp step is configured to ramp "from
        the current value". Cheap (single dict lookup); not a substitute for
        a real ring buffer if the caller needs history."""
        return self._last_values.get(channel)

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        """Close the bus and every active subscription queue."""
        if self._closed:
            return
        self._closed = True
        for sub in self._subscriptions:
            sub.queue.close()
        self._subscriptions.clear()
        self._last_values.clear()

    @property
    def subscription_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self._subscriptions)


# ---------------------------------------------------------------------------
# Predicate helpers
# ---------------------------------------------------------------------------


def _always_true(_: DeviceEmission) -> bool:
    return True


def _channel_predicate(channel: str) -> _PredicateFn:
    def _match(emission: DeviceEmission) -> bool:
        return isinstance(emission, ChannelSample) and emission.channel == channel

    return _match


def _adapter_predicate(adapter: str) -> _PredicateFn:
    def _match(emission: DeviceEmission) -> bool:
        match emission:
            case ChannelSample():
                # ChannelSample doesn't carry adapter; rely on source_record_id
                # prefix when an upstream wants strict adapter filtering.
                return (
                    emission.source_record_id is not None
                    and emission.source_record_id.startswith(f"{adapter}:")
                )
            case SourceRecord() | DeviceEvent() | DeviceSnapshot():
                return emission.adapter == adapter

    return _match


# Exposed so the engine can pass ``Awaitable[None]`` typed-callbacks if it ever
# wants a non-iterator subscriber API. P0c subscribers use ``async for``.
PublishFn = Callable[[DeviceEmission], Awaitable[None]]


__all__ = [
    "DEFAULT_SUBSCRIBER_CAPACITY",
    "DataBus",
    "PublishFn",
    "Subscription",
]
