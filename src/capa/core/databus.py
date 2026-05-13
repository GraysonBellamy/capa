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

Subscribers register through one of four buckets so :meth:`publish` does
**not** walk every active subscription:

* :meth:`subscribe_channel` — keyed by ``ChannelSample.channel``.
* :meth:`subscribe_adapter` — keyed by adapter name (matches
  ``SourceRecord.adapter`` directly and the adapter prefix on
  ``ChannelSample.source_record_id``).
* :meth:`subscribe_all` — wildcard list iterated for every emission.
* :meth:`subscribe` with a custom ``predicate`` — second-class hot-path
  bucket; the predicate runs per emission, so reach for one of the indexed
  helpers when possible.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Final

from capa.core.backpressure import BackpressurePolicy, BoundedQueue
from capa.core.errors import CapaError
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


# Bucket discriminators. Stored on each :class:`Subscription` so
# :meth:`DataBus.unsubscribe` knows where to look without walking every
# bucket.
_BUCKET_CHANNEL: Final[str] = "channel"
_BUCKET_ADAPTER: Final[str] = "adapter"
_BUCKET_WILDCARD: Final[str] = "wildcard"
_BUCKET_CUSTOM: Final[str] = "custom"


class DataBusLoopError(CapaError):
    """Raised when :meth:`DataBus.publish` / :meth:`DataBus.publish_nowait`
    is called from a different event loop than the one that owns the bus.

    Migration doc §3.10 / §3.11 invariant 7. Each :class:`DataBus` instance
    is **loop-affine**: its subscription queues are :class:`BoundedQueue`s
    bound to one loop, and mutating them from a different loop's task is
    undefined behaviour for asyncio. Phase 2's :class:`Conductor` builds one
    bus on the conductor loop; UIBridge (Phase 4) builds a separate mirror
    bus on the UI loop.
    """


@dataclass(slots=True)
class Subscription:
    """One active subscriber, one queue.

    ``predicate`` returns ``True`` for emissions this subscriber wants to see.
    ``policy`` decides what happens when the per-subscription queue overflows.
    """

    name: str
    queue: BoundedQueue[DeviceEmission]
    predicate: _PredicateFn
    # Set by :class:`DataBus` when the subscription is registered so
    # :meth:`DataBus.unsubscribe` can locate it in O(B) bucket time instead
    # of walking every subscriber. ``("wildcard", "")`` / ``("custom", "")``
    # have no key.
    _bucket: tuple[str, str] = field(default=(_BUCKET_CUSTOM, ""))

    async def __aiter__(self) -> AsyncIterator[DeviceEmission]:
        while True:
            item = await self.queue.get()
            yield item


class DataBus:
    """In-process pub/sub for adapter emissions.

    Publishing is dispatched through per-channel and per-adapter indexes so
    the publisher only visits subscribers actually interested in a given
    emission. Wildcard and custom-predicate subscribers are kept in two
    additional lists; the custom-predicate list is the only fallback path
    that walks every entry per publish.
    """

    __slots__ = (
        "_by_adapter",
        "_by_channel",
        "_closed",
        "_custom",
        "_last_values",
        "_owning_loop",
        "_wildcard",
    )

    def __init__(self) -> None:
        self._by_channel: dict[str, list[Subscription]] = {}
        self._by_adapter: dict[str, list[Subscription]] = {}
        self._wildcard: list[Subscription] = []
        self._custom: list[Subscription] = []
        self._closed = False
        # Latest scalar value seen on each channel, populated on publish so
        # subscribers (and procedure helpers like MethodExecutor) can do
        # cheap "what was the last value?" lookups without re-implementing
        # a per-channel ring. Only ChannelSample emissions populate this.
        self._last_values: dict[str, float] = {}
        # Loop the bus is pinned to (migration doc §3.10). ``None`` until the
        # first :meth:`publish` / :meth:`publish_nowait` call, at which point
        # the running loop is captured. :meth:`bind_loop` lets owners (the
        # Conductor) bind explicitly at construction time so a misconfigured
        # subscriber fails immediately rather than after first publish.
        self._owning_loop: asyncio.AbstractEventLoop | None = None

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

        Default ``policy=DROP_OLDEST`` matches the UI / ring-buffer use case.
        Subscribers that must not miss an evaluation pass
        ``policy=ABORT_RUN`` (see :meth:`subscribe_critical` for the
        intended shape).

        ``policy=BLOCK`` is rejected at the DataBus boundary: a stuck
        ``BLOCK`` subscriber would freeze the fan-out (and therefore every
        adapter behind it). Must-not-drop subscribers use
        :meth:`subscribe_critical`, which has a deadline and a bounded
        blast radius.

        When ``predicate`` is ``None``, the subscription joins the wildcard
        bucket; when ``predicate`` is supplied, it joins the custom bucket
        and the predicate runs per emission. Indexed dispatch is only
        possible through :meth:`subscribe_channel` / :meth:`subscribe_adapter`.
        """
        if self._closed:
            raise RuntimeError("DataBus is closed")
        _reject_block_policy(policy)
        queue: BoundedQueue[DeviceEmission] = BoundedQueue(
            name=f"databus:{name}",
            capacity=capacity,
            policy=policy,
            abort_after_s=abort_after_s,
        )
        if predicate is None:
            sub = Subscription(
                name=name,
                queue=queue,
                predicate=_always_true,
                _bucket=(_BUCKET_WILDCARD, ""),
            )
            self._wildcard.append(sub)
        else:
            sub = Subscription(
                name=name,
                queue=queue,
                predicate=predicate,
                _bucket=(_BUCKET_CUSTOM, ""),
            )
            self._custom.append(sub)
        return sub

    def subscribe_all(self, name: str, **kwargs: int | BackpressurePolicy | float) -> Subscription:
        """Convenience: subscribe to every emission."""
        return self.subscribe(name, predicate=None, **kwargs)  # type: ignore[arg-type]

    def subscribe_channel(
        self,
        name: str,
        *,
        channel: str,
        **kwargs: int | BackpressurePolicy | float,
    ) -> Subscription:
        """Subscribe to a single channel — receives :class:`ChannelSample`
        emissions whose :attr:`ChannelSample.channel` matches."""
        if self._closed:
            raise RuntimeError("DataBus is closed")
        policy = kwargs.get("policy", BackpressurePolicy.DROP_OLDEST)
        _reject_block_policy(policy)  # type: ignore[arg-type]
        queue: BoundedQueue[DeviceEmission] = BoundedQueue(
            name=f"databus:{name}",
            capacity=int(kwargs.get("capacity", DEFAULT_SUBSCRIBER_CAPACITY)),  # type: ignore[arg-type]
            policy=policy,  # type: ignore[arg-type]
            abort_after_s=float(kwargs.get("abort_after_s", 5.0)),  # type: ignore[arg-type]
        )
        sub = Subscription(
            name=name,
            queue=queue,
            predicate=_channel_predicate(channel),
            _bucket=(_BUCKET_CHANNEL, channel),
        )
        self._by_channel.setdefault(channel, []).append(sub)
        return sub

    def subscribe_adapter(
        self,
        name: str,
        *,
        adapter: str,
        **kwargs: int | BackpressurePolicy | float,
    ) -> Subscription:
        """Subscribe to every emission from a given adapter.

        Receives :class:`SourceRecord` / :class:`DeviceEvent` /
        :class:`DeviceSnapshot` for the matching adapter, plus
        :class:`ChannelSample` emissions whose ``source_record_id`` starts
        with ``"{adapter}:"``.
        """
        if self._closed:
            raise RuntimeError("DataBus is closed")
        policy = kwargs.get("policy", BackpressurePolicy.DROP_OLDEST)
        _reject_block_policy(policy)  # type: ignore[arg-type]
        queue: BoundedQueue[DeviceEmission] = BoundedQueue(
            name=f"databus:{name}",
            capacity=int(kwargs.get("capacity", DEFAULT_SUBSCRIBER_CAPACITY)),  # type: ignore[arg-type]
            policy=policy,  # type: ignore[arg-type]
            abort_after_s=float(kwargs.get("abort_after_s", 5.0)),  # type: ignore[arg-type]
        )
        sub = Subscription(
            name=name,
            queue=queue,
            predicate=_adapter_predicate(adapter),
            _bucket=(_BUCKET_ADAPTER, adapter),
        )
        self._by_adapter.setdefault(adapter, []).append(sub)
        return sub

    def subscribe_critical(
        self,
        name: str,
        *,
        predicate: _PredicateFn | None = None,
        capacity: int = DEFAULT_SUBSCRIBER_CAPACITY,
        abort_after_s: float = 5.0,
    ) -> Subscription:
        """Register a must-not-drop subscription with ``ABORT_RUN`` semantics.

        Use this for the safety monitor and any other consumer where a
        missed evaluation is unacceptable. The :class:`ABORT_RUN` policy
        means a stuck subscriber raises :class:`BackpressureAbortError`
        from :meth:`publish` once ``abort_after_s`` elapses — the engine
        surfaces this as a crashed-but-sealed run rather than freezing
        acquisition behind the stuck consumer. Bounded blast radius.

        Pass ``predicate=None`` for a wildcard critical subscriber, or a
        function for the custom-bucket path.
        """
        return self.subscribe(
            name,
            predicate=predicate,
            capacity=capacity,
            policy=BackpressurePolicy.ABORT_RUN,
            abort_after_s=abort_after_s,
        )

    def unsubscribe(self, sub: Subscription) -> None:
        """Remove ``sub`` from the active list and close its queue.

        Safe to call multiple times; a missing subscription is a no-op.
        """
        bucket_kind, key = sub._bucket
        match bucket_kind:
            case s if s == _BUCKET_CHANNEL:
                bucket = self._by_channel.get(key)
                if bucket is not None:
                    try:
                        bucket.remove(sub)
                    except ValueError:
                        return
                    if not bucket:
                        del self._by_channel[key]
            case s if s == _BUCKET_ADAPTER:
                bucket = self._by_adapter.get(key)
                if bucket is not None:
                    try:
                        bucket.remove(sub)
                    except ValueError:
                        return
                    if not bucket:
                        del self._by_adapter[key]
            case s if s == _BUCKET_WILDCARD:
                try:
                    self._wildcard.remove(sub)
                except ValueError:
                    return
            case _:
                try:
                    self._custom.remove(sub)
                except ValueError:
                    return
        sub.queue.close()

    # ------------------------------------------------------------------ loop affinity

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Pin the bus to ``loop`` explicitly.

        Migration doc §3.10. The :class:`Conductor` calls this once at
        startup so a misconfigured subscriber (e.g. a UI thread accidentally
        publishing into the conductor bus) fails at bind time rather than at
        first publish.

        Calling :meth:`bind_loop` with the already-bound loop is a no-op;
        binding to a *different* loop raises :class:`DataBusLoopError`.
        """
        if self._owning_loop is None:
            self._owning_loop = loop
            return
        if self._owning_loop is not loop:
            raise DataBusLoopError(
                "DataBus already bound to a different loop "
                f"(bound={self._owning_loop!r}, new={loop!r}); each bus is "
                "loop-affine (migration doc §3.10) — construct a separate "
                "DataBus on the other loop instead."
            )

    @property
    def owning_loop(self) -> asyncio.AbstractEventLoop | None:
        """Loop the bus is pinned to, or ``None`` if not yet bound.

        Bound lazily by the first :meth:`publish` / :meth:`publish_nowait`
        call, or eagerly via :meth:`bind_loop`.
        """
        return self._owning_loop

    def _check_loop_affinity(self) -> None:
        """Assert the current running loop matches ``_owning_loop``; on
        first call, capture the running loop as the owner.

        Cheap: one ``get_running_loop()`` and one ``is`` comparison per
        publish. Sub-microsecond on CPython. Worth the cost — a loop-affinity
        violation produces silent data loss or queue corruption, neither of
        which a metric will reliably surface.
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError as exc:
            # publish_nowait is sync but the underlying BoundedQueue mutates
            # an asyncio.Queue; mutating that from outside any loop violates
            # the queue's invariants even when same-thread.
            raise DataBusLoopError(
                "DataBus.publish/publish_nowait must be called from within "
                "the owning event loop; no loop is currently running."
            ) from exc
        if self._owning_loop is None:
            self._owning_loop = running
            return
        if running is not self._owning_loop:
            raise DataBusLoopError(
                "DataBus publish from wrong loop "
                f"(bound={self._owning_loop!r}, running={running!r}); each "
                "bus is loop-affine (migration doc §3.10)."
            )

    # ------------------------------------------------------------------ publish

    async def publish(self, emission: DeviceEmission) -> None:
        """Route ``emission`` to every matching subscription.

        Per-subscription :class:`BackpressurePolicy` is honored inside
        :meth:`BoundedQueue.put`; ``BLOCK`` subscriptions can pause this
        coroutine, ``DROP_OLDEST`` ones never do, ``ABORT_RUN`` ones raise
        once their stuck-window expires (which the engine surfaces as a
        crash exit).

        Loop-affinity is checked on every call; the first call captures the
        owning loop (see :meth:`bind_loop` for explicit binding).
        """
        if self._closed:
            return
        self._check_loop_affinity()
        for sub in self._iter_targets(emission):
            await sub.queue.put(emission)

    def publish_nowait(self, emission: DeviceEmission) -> None:
        """Synchronous publish: matches each subscription and uses
        :meth:`BoundedQueue.put_nowait`. ``BLOCK`` subscribers that are full
        silently drop here — the caller chose to take a non-async path.

        Loop-affinity is checked on every call. The call must be made from a
        synchronous context running on the owning loop's thread (e.g. from
        inside a coroutine that hasn't yielded since the running loop was
        established); calling from a non-loop thread raises.
        """
        if self._closed:
            return
        self._check_loop_affinity()
        for sub in self._iter_targets(emission):
            sub.queue.put_nowait(emission)

    def _iter_targets(self, emission: DeviceEmission) -> list[Subscription]:
        """Build the (already-filtered) subscriber list for ``emission``.

        Indexed buckets (channel / adapter) are exact-match — no per-emission
        predicate call. Wildcard subscribers always match. Custom-predicate
        subscribers fall through to a linear scan; reach for one of the
        indexed helpers if a custom predicate is on a hot path.
        """
        targets: list[Subscription] = []
        if isinstance(emission, ChannelSample):
            with suppress(TypeError, ValueError):
                self._last_values[emission.channel] = float(emission.value)
            channel_bucket = self._by_channel.get(emission.channel)
            if channel_bucket:
                targets.extend(channel_bucket)
            # ChannelSamples also reach subscribe_adapter subscribers whose
            # adapter matches the source-record prefix. Parsing once here
            # avoids the per-subscription predicate call.
            src_id = emission.source_record_id
            if src_id is not None:
                adapter, sep, _ = src_id.partition(":")
                if sep:
                    adapter_bucket = self._by_adapter.get(adapter)
                    if adapter_bucket:
                        targets.extend(adapter_bucket)
        elif isinstance(emission, SourceRecord | DeviceEvent | DeviceSnapshot):
            adapter_bucket = self._by_adapter.get(emission.adapter)
            if adapter_bucket:
                targets.extend(adapter_bucket)
        if self._wildcard:
            targets.extend(self._wildcard)
        if self._custom:
            for sub in self._custom:
                if sub.predicate(emission):
                    targets.append(sub)
        return targets

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
        for bucket in self._by_channel.values():
            for sub in bucket:
                sub.queue.close()
        for bucket in self._by_adapter.values():
            for sub in bucket:
                sub.queue.close()
        for sub in self._wildcard:
            sub.queue.close()
        for sub in self._custom:
            sub.queue.close()
        self._by_channel.clear()
        self._by_adapter.clear()
        self._wildcard.clear()
        self._custom.clear()
        self._last_values.clear()

    @property
    def subscription_names(self) -> tuple[str, ...]:
        names: list[str] = []
        for bucket in self._by_channel.values():
            names.extend(sub.name for sub in bucket)
        for bucket in self._by_adapter.values():
            names.extend(sub.name for sub in bucket)
        names.extend(sub.name for sub in self._wildcard)
        names.extend(sub.name for sub in self._custom)
        return tuple(names)


# ---------------------------------------------------------------------------
# Predicate helpers
# ---------------------------------------------------------------------------


def _reject_block_policy(policy: BackpressurePolicy) -> None:
    """Reject ``BLOCK`` at the DataBus boundary.

    ``BLOCK`` on a subscription queue means a stuck consumer freezes the
    fan-out, which back-pressures the producer queue, which freezes every
    adapter. Must-not-drop subscribers should use
    :meth:`DataBus.subscribe_critical` instead — same guarantee, but with
    a deadline that turns "subscriber stuck" into "run crashed cleanly"
    rather than "rig hangs".
    """
    if policy is BackpressurePolicy.BLOCK:
        raise ValueError(
            "DataBus subscribers must not use BackpressurePolicy.BLOCK; "
            "use DataBus.subscribe_critical(...) for must-not-drop "
            "subscriptions (ABORT_RUN with a deadline)."
        )


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
    "DataBusLoopError",
    "PublishFn",
    "Subscription",
]
