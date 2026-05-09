from __future__ import annotations

import anyio
import pytest

from capa.core.backpressure import BackpressurePolicy, BoundedQueue
from capa.core.errors import BackpressureAbortError

pytestmark = pytest.mark.anyio


class TestBackpressurePolicy:
    async def test_block_blocks_until_consumer_drains(self, anyio_backend: str) -> None:
        q: BoundedQueue[int] = BoundedQueue(
            name="test_block", capacity=2, policy=BackpressurePolicy.BLOCK
        )
        await q.put(1)
        await q.put(2)
        assert q.depth == 2
        # Third put must block until consumer drains.

        async def producer() -> None:
            await q.put(3)

        async def consumer() -> None:
            await anyio.sleep(0.01)
            value = await q.get()
            assert value == 1

        async with anyio.create_task_group() as tg:
            tg.start_soon(producer)
            tg.start_soon(consumer)
        assert q.depth == 2  # consumed 1, then put 3
        assert q.stats.block_waits >= 1

    async def test_drop_oldest_evicts_oldest(self, anyio_backend: str) -> None:
        q: BoundedQueue[int] = BoundedQueue(
            name="test_drop", capacity=2, policy=BackpressurePolicy.DROP_OLDEST
        )
        await q.put(1)
        await q.put(2)
        await q.put(3)  # evicts 1
        assert q.depth == 2
        assert q.stats.dropped == 1
        assert await q.get() == 2
        assert await q.get() == 3

    async def test_abort_run_raises_after_timeout(self, anyio_backend: str) -> None:
        q: BoundedQueue[int] = BoundedQueue(
            name="test_abort",
            capacity=1,
            policy=BackpressurePolicy.ABORT_RUN,
            abort_after_s=0.05,
        )
        await q.put(1)
        with pytest.raises(BackpressureAbortError):
            await q.put(2)

    async def test_get_blocks_until_data(self, anyio_backend: str) -> None:
        q: BoundedQueue[int] = BoundedQueue(
            name="test_get", capacity=4, policy=BackpressurePolicy.BLOCK
        )

        result: list[int] = []

        async def consumer() -> None:
            value = await q.get()
            result.append(value)

        async def producer() -> None:
            await anyio.sleep(0.01)
            await q.put(42)

        async with anyio.create_task_group() as tg:
            tg.start_soon(consumer)
            tg.start_soon(producer)
        assert result == [42]

    async def test_high_water_mark(self, anyio_backend: str) -> None:
        q: BoundedQueue[int] = BoundedQueue(name="hw", capacity=4, policy=BackpressurePolicy.BLOCK)
        for i in range(3):
            await q.put(i)
        assert q.stats.depth_high_water == 3
        await q.get()
        await q.put(99)
        assert q.stats.depth_high_water == 3  # high water doesn't shrink
