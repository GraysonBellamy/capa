"""P0-4 acceptance: must-not-drop subscribers use ABORT_RUN, not BLOCK.

A stuck ``BLOCK`` subscriber would freeze the engine fan-out — and through
it every producer adapter. The DataBus now rejects ``BLOCK`` at registration
time and steers callers to :meth:`DataBus.subscribe_critical`, which has a
deadline.
"""

from __future__ import annotations

import anyio
import pytest

from capa.core.backpressure import BackpressurePolicy
from capa.core.databus import DataBus
from capa.core.errors import BackpressureAbortError
from capa.devices.records import ChannelSample


def _sample(value: float = 1.0) -> ChannelSample:
    return ChannelSample(
        channel="ch0",
        t_mono_ns=1,
        t_mono_s=1e-9,
        value=value,
        unit="V",
    )


def test_subscribe_rejects_block_policy_with_pointer_at_subscribe_critical() -> None:
    bus = DataBus()
    with pytest.raises(ValueError, match="subscribe_critical"):
        bus.subscribe("bad", policy=BackpressurePolicy.BLOCK)
    bus.close()


def test_subscribe_channel_rejects_block_policy() -> None:
    bus = DataBus()
    with pytest.raises(ValueError, match="subscribe_critical"):
        bus.subscribe_channel("bad", channel="ch0", policy=BackpressurePolicy.BLOCK)
    bus.close()


def test_subscribe_adapter_rejects_block_policy() -> None:
    bus = DataBus()
    with pytest.raises(ValueError, match="subscribe_critical"):
        bus.subscribe_adapter("bad", adapter="alicat", policy=BackpressurePolicy.BLOCK)
    bus.close()


@pytest.mark.anyio
async def test_subscribe_critical_aborts_on_stuck_consumer() -> None:
    """The publish call into a critical subscription with a parked consumer
    must raise ``BackpressureAbortError`` once the deadline elapses. The
    engine catches this and surfaces a crashed-but-sealed run."""
    bus = DataBus()
    sub = bus.subscribe_critical("safety", capacity=1, abort_after_s=0.05)

    # First publish lands in the queue's single slot.
    await bus.publish(_sample(1.0))
    assert sub.queue.depth == 1

    # Consumer never reads; second publish must abort once the deadline
    # elapses inside the queue's ABORT_RUN path.
    with pytest.raises(BackpressureAbortError):
        with anyio.fail_after(2.0):
            await bus.publish(_sample(2.0))

    bus.close()


@pytest.mark.anyio
async def test_subscribe_critical_with_drained_consumer_does_not_abort() -> None:
    """Critical subscribers with a healthy consumer behave exactly like
    any other subscriber — the ABORT_RUN policy is only material when the
    consumer falls behind."""
    bus = DataBus()
    sub = bus.subscribe_critical("safety", capacity=4, abort_after_s=2.0)

    received: list[ChannelSample] = []

    async def _consumer() -> None:
        async for item in sub:
            assert isinstance(item, ChannelSample)
            received.append(item)
            if len(received) == 3:
                return

    async with anyio.create_task_group() as tg:
        tg.start_soon(_consumer)
        await bus.publish(_sample(1.0))
        await bus.publish(_sample(2.0))
        await bus.publish(_sample(3.0))

    assert [s.value for s in received] == [1.0, 2.0, 3.0]
    bus.close()
