"""Unit tests for :class:`capa.runtime.bridge.ThreadBridge`.

The bridge spans two ``asyncio`` loops on two threads. Most tests here
spin up a real producer thread + producer loop and use the test's running
loop as the consumer (or vice versa). The ``_DualLoop`` helper hides the
boilerplate.

Coverage targets the cross-thread cases:

* order preservation,
* close-drain semantics,
* BLOCK throttling,
* DROP_OLDEST eviction,
* DROP_NEWEST drops,
* latency p99 observability,
* ``blocked_since_ms`` observability during a sustained block,
* loop-affinity guards on attach.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future

import pytest

from capa.runtime.bridge import (
    BridgePolicy,
    ThreadBridge,
    ThreadBridgeClosedError,
)

# ---------------------------------------------------------------------------
# Helpers: a separate thread running its own asyncio loop, with a
# call-into-thread RPC that returns a concurrent.futures.Future.
# ---------------------------------------------------------------------------


class _ProducerThread:
    """A separate thread running a fresh asyncio loop.

    Tests use this to host the producer side of a ThreadBridge while the
    pytest event loop runs the consumer side.
    """

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="bridge-producer")

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        try:
            self.loop.run_forever()
        finally:
            # Drain any remaining tasks before close.
            pending = asyncio.all_tasks(self.loop)
            for t in pending:
                t.cancel()
            self.loop.close()

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(timeout=2.0)
        assert self.loop is not None

    def run_coro(self, coro_factory: Callable[[], object]) -> Future[object]:
        """Schedule ``coro_factory()`` on the producer loop, return a Future."""
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coro_factory(), self.loop)  # type: ignore[arg-type]

    def call_sync(self, fn: Callable[[], object]) -> Future[object]:
        """Schedule a sync callable on the producer loop, return a Future."""
        assert self.loop is not None
        fut: Future[object] = Future()

        def _wrapped() -> None:
            try:
                fut.set_result(fn())
            except BaseException as exc:
                fut.set_exception(exc)

        self.loop.call_soon_threadsafe(_wrapped)
        return fut

    def stop(self) -> None:
        assert self.loop is not None
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=2.0)


@pytest.fixture
def producer() -> Iterator[_ProducerThread]:
    p = _ProducerThread()
    p.start()
    yield p
    p.stop()


async def _attach_both(bridge: ThreadBridge[object], producer: _ProducerThread) -> None:
    """Run attach_consumer on the test loop and attach_producer on the
    producer loop. Awaits both to settle."""
    bridge.attach_consumer()
    fut = producer.call_sync(lambda: bridge.attach_producer(producer.loop))  # type: ignore[arg-type]
    await asyncio.wrap_future(fut)


# ---------------------------------------------------------------------------
# Order / FIFO
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bridge_put_get_across_threads_preserves_order(
    producer: _ProducerThread,
) -> None:
    consumer_loop = asyncio.get_running_loop()
    bridge: ThreadBridge[int] = ThreadBridge(
        name="t", capacity=8, consumer_loop=consumer_loop, policy=BridgePolicy.BLOCK
    )
    await _attach_both(bridge, producer)  # type: ignore[arg-type]

    n = 50

    async def produce() -> None:
        for i in range(n):
            await bridge.put(i)

    producer.run_coro(produce)

    received: list[int] = []
    for _ in range(n):
        item = await bridge.get()
        assert item is not None
        received.append(item)

    assert received == list(range(n))
    assert bridge.metrics.enqueued_total == n
    assert bridge.metrics.dequeued_total == n


# ---------------------------------------------------------------------------
# Close + drain
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bridge_close_drains_pending_items_then_signals_eof(
    producer: _ProducerThread,
) -> None:
    consumer_loop = asyncio.get_running_loop()
    bridge: ThreadBridge[int] = ThreadBridge(
        name="drain", capacity=8, consumer_loop=consumer_loop, policy=BridgePolicy.BLOCK
    )
    await _attach_both(bridge, producer)  # type: ignore[arg-type]

    async def produce_three() -> None:
        await bridge.put(1)
        await bridge.put(2)
        await bridge.put(3)

    await asyncio.wrap_future(producer.run_coro(produce_three))
    bridge.close()

    drained: list[int] = []
    async for item in bridge:
        drained.append(item)
    assert drained == [1, 2, 3]

    # Subsequent get() returns None forever (idempotent drained state).
    assert await bridge.get() is None
    assert await bridge.get() is None


@pytest.mark.anyio
async def test_bridge_put_after_close_raises(
    producer: _ProducerThread,
) -> None:
    consumer_loop = asyncio.get_running_loop()
    bridge: ThreadBridge[int] = ThreadBridge(
        name="closed", capacity=4, consumer_loop=consumer_loop, policy=BridgePolicy.BLOCK
    )
    await _attach_both(bridge, producer)  # type: ignore[arg-type]
    bridge.close()

    async def try_put() -> Exception | None:
        try:
            await bridge.put(1)
            return None
        except ThreadBridgeClosedError as exc:
            return exc

    result = await asyncio.wrap_future(producer.run_coro(try_put))
    assert isinstance(result, ThreadBridgeClosedError)


@pytest.mark.anyio
async def test_bridge_close_is_idempotent(producer: _ProducerThread) -> None:
    consumer_loop = asyncio.get_running_loop()
    bridge: ThreadBridge[int] = ThreadBridge(
        name="t", capacity=4, consumer_loop=consumer_loop, policy=BridgePolicy.BLOCK
    )
    await _attach_both(bridge, producer)  # type: ignore[arg-type]
    bridge.close()
    bridge.close()  # must not raise
    assert bridge.closed


# ---------------------------------------------------------------------------
# BLOCK policy
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bridge_block_policy_throttles_producer(
    producer: _ProducerThread,
) -> None:
    """Capacity 2, three puts, no consumer reads — third put must block.

    We poll ``blocked_since_ms`` from the consumer side (this loop) while
    the producer is parked on the semaphore acquire.
    """
    consumer_loop = asyncio.get_running_loop()
    bridge: ThreadBridge[int] = ThreadBridge(
        name="block", capacity=2, consumer_loop=consumer_loop, policy=BridgePolicy.BLOCK
    )
    await _attach_both(bridge, producer)  # type: ignore[arg-type]

    async def push_three() -> None:
        await bridge.put(1)
        await bridge.put(2)
        await bridge.put(3)  # this one must block

    push_fut = producer.run_coro(push_three)

    # Wait until producer is parked on the third put.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if bridge.metrics.blocked_since_ms is not None:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("producer never entered blocked state")

    # Confirm the block is observable and increasing.
    first = bridge.metrics.blocked_since_ms
    assert first is not None and first >= 0
    await asyncio.sleep(0.05)
    second = bridge.metrics.blocked_since_ms
    assert second is not None and second >= first

    # Drain one — producer unblocks.
    item = await bridge.get()
    assert item == 1
    await asyncio.wrap_future(push_fut)

    # After the unblock, blocked_since_ms is None and blocked_total_ms > 0.
    metrics = bridge.metrics
    assert metrics.blocked_since_ms is None
    assert metrics.blocked_total_ms > 0


# ---------------------------------------------------------------------------
# DROP_OLDEST policy
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bridge_drop_oldest_evicts_head_under_pressure(
    producer: _ProducerThread,
) -> None:
    consumer_loop = asyncio.get_running_loop()
    bridge: ThreadBridge[int] = ThreadBridge(
        name="drop_old",
        capacity=3,
        consumer_loop=consumer_loop,
        policy=BridgePolicy.DROP_OLDEST,
    )
    await _attach_both(bridge, producer)  # type: ignore[arg-type]

    async def push_many() -> None:
        for i in range(6):
            await bridge.put(i)

    await asyncio.wrap_future(producer.run_coro(push_many))

    # Let the consumer-loop callbacks settle.
    await asyncio.sleep(0.05)

    drained: list[int] = []
    while bridge.metrics.depth > 0:
        item = await bridge.get()
        if item is None:
            break
        drained.append(item)

    # The last 3 items must have landed; earlier ones evicted.
    assert drained == [3, 4, 5]
    assert bridge.metrics.dropped_total == 3


@pytest.mark.anyio
async def test_bridge_drop_oldest_put_nowait_from_async_caller(
    producer: _ProducerThread,
) -> None:
    """The Conductor → UI bridge uses ``put_nowait`` from the conductor's
    async drain task. Confirm the DROP_OLDEST sync path works.
    """
    consumer_loop = asyncio.get_running_loop()
    bridge: ThreadBridge[int] = ThreadBridge(
        name="ui",
        capacity=2,
        consumer_loop=consumer_loop,
        policy=BridgePolicy.DROP_OLDEST,
    )
    # No attach_producer — DROP_OLDEST does not need a producer semaphore,
    # and put_nowait is callable from any thread.
    bridge.attach_consumer()

    # Producer thread pushes via put_nowait.
    def push() -> None:
        for i in range(5):
            assert bridge.put_nowait(i) is True

    fut = producer.call_sync(push)
    await asyncio.wrap_future(fut)
    await asyncio.sleep(0.05)

    drained: list[int] = []
    while bridge.metrics.depth > 0:
        item = await bridge.get()
        if item is None:
            break
        drained.append(item)
    assert drained == [3, 4]
    assert bridge.metrics.dropped_total == 3


# ---------------------------------------------------------------------------
# DROP_NEWEST policy
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bridge_drop_newest_discards_new_under_pressure(
    producer: _ProducerThread,
) -> None:
    consumer_loop = asyncio.get_running_loop()
    bridge: ThreadBridge[int] = ThreadBridge(
        name="drop_new",
        capacity=2,
        consumer_loop=consumer_loop,
        policy=BridgePolicy.DROP_NEWEST,
    )
    await _attach_both(bridge, producer)  # type: ignore[arg-type]

    async def push_four() -> None:
        for i in range(4):
            await bridge.put(i)

    await asyncio.wrap_future(producer.run_coro(push_four))
    await asyncio.sleep(0.05)

    drained: list[int] = []
    while bridge.metrics.depth > 0:
        item = await bridge.get()
        if item is None:
            break
        drained.append(item)
    # The first two get in; the last two are dropped because the semaphore
    # is fully consumed and no get() has freed capacity.
    assert drained == [0, 1]
    assert bridge.metrics.dropped_total == 2


# ---------------------------------------------------------------------------
# Loop-affinity guards
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bridge_attach_consumer_on_wrong_loop_raises() -> None:
    """attach_consumer must run on the loop passed as consumer_loop."""
    # Build a bridge whose consumer_loop is a different loop than the one
    # this test runs on.
    other_loop = asyncio.new_event_loop()
    try:
        bridge: ThreadBridge[int] = ThreadBridge(
            name="t", capacity=1, consumer_loop=other_loop, policy=BridgePolicy.BLOCK
        )
        with pytest.raises(RuntimeError, match="consumer loop"):
            bridge.attach_consumer()
    finally:
        other_loop.close()


@pytest.mark.anyio
async def test_bridge_attach_producer_on_wrong_loop_raises(
    producer: _ProducerThread,
) -> None:
    consumer_loop = asyncio.get_running_loop()
    bridge: ThreadBridge[int] = ThreadBridge(
        name="t", capacity=1, consumer_loop=consumer_loop, policy=BridgePolicy.BLOCK
    )
    bridge.attach_consumer()

    # Schedule attach_producer on the producer loop but pass a DIFFERENT
    # loop reference — must raise.
    fake_loop = asyncio.new_event_loop()
    try:
        fut = producer.call_sync(lambda: bridge.attach_producer(fake_loop))
        with pytest.raises(RuntimeError, match="producer loop"):
            await asyncio.wrap_future(fut)
    finally:
        fake_loop.close()


@pytest.mark.anyio
async def test_bridge_put_nowait_rejects_block_policy(
    producer: _ProducerThread,
) -> None:
    consumer_loop = asyncio.get_running_loop()
    bridge: ThreadBridge[int] = ThreadBridge(
        name="t", capacity=1, consumer_loop=consumer_loop, policy=BridgePolicy.BLOCK
    )
    await _attach_both(bridge, producer)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="put_nowait is incompatible with BLOCK"):
        bridge.put_nowait(1)


# ---------------------------------------------------------------------------
# Metrics: latency p99, depth tracking
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_bridge_latency_metrics_track_p99(
    producer: _ProducerThread,
) -> None:
    """Push exactly capacity items so producer never blocks; drain and
    confirm latency quantiles are non-negative and monotone."""
    consumer_loop = asyncio.get_running_loop()
    n = 12
    bridge: ThreadBridge[int] = ThreadBridge(
        name="lat", capacity=n, consumer_loop=consumer_loop, policy=BridgePolicy.BLOCK
    )
    await _attach_both(bridge, producer)  # type: ignore[arg-type]

    async def push() -> None:
        for i in range(n):
            await bridge.put(i)

    await asyncio.wrap_future(producer.run_coro(push))
    await asyncio.sleep(0.05)

    for _ in range(n):
        await bridge.get()

    assert bridge.metrics.dequeued_total == n
    assert bridge.metrics.latency_p99_ms >= 0
    assert bridge.metrics.latency_p50_ms <= bridge.metrics.latency_p99_ms + 1e-6


@pytest.mark.anyio
async def test_bridge_depth_max_tracks_high_water_mark(
    producer: _ProducerThread,
) -> None:
    consumer_loop = asyncio.get_running_loop()
    bridge: ThreadBridge[int] = ThreadBridge(
        name="hi", capacity=8, consumer_loop=consumer_loop, policy=BridgePolicy.BLOCK
    )
    await _attach_both(bridge, producer)  # type: ignore[arg-type]

    async def push_five() -> None:
        for i in range(5):
            await bridge.put(i)

    await asyncio.wrap_future(producer.run_coro(push_five))
    await asyncio.sleep(0.05)

    assert bridge.metrics.depth_max == 5
    while bridge.metrics.depth > 0:
        await bridge.get()
    assert bridge.metrics.depth == 0
    assert bridge.metrics.depth_max == 5  # high-water preserved


# ---------------------------------------------------------------------------
# Capacity validation
# ---------------------------------------------------------------------------


def test_bridge_rejects_zero_capacity() -> None:
    with pytest.raises(ValueError, match="capacity must be >= 1"):
        ThreadBridge[int](
            name="t",
            capacity=0,
            consumer_loop=asyncio.new_event_loop(),
            policy=BridgePolicy.BLOCK,
        )


def test_bridge_introspection_properties() -> None:
    """The public introspection properties are pure reads — no loop needed."""
    loop = asyncio.new_event_loop()
    try:
        bridge: ThreadBridge[int] = ThreadBridge(
            name="introspect",
            capacity=42,
            consumer_loop=loop,
            policy=BridgePolicy.DROP_OLDEST,
        )
        assert bridge.name == "introspect"
        assert bridge.capacity == 42
        assert bridge.policy is BridgePolicy.DROP_OLDEST
        assert bridge.closed is False
    finally:
        loop.close()
