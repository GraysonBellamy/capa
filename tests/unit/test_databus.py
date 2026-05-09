"""Tests for :mod:`capa.core.databus`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from capa.core.backpressure import BackpressurePolicy
from capa.core.databus import DataBus
from capa.devices.records import (
    ChannelSample,
    DeviceEvent,
    SourceRecord,
)


def _sample(channel: str, value: float, *, source_record_id: str | None = None) -> ChannelSample:
    return ChannelSample(
        channel=channel,
        t_mono_ns=1,
        t_mono_s=1e-9,
        value=value,
        unit="V",
        source_record_id=source_record_id,
    )


def _source(adapter: str, device: str = "dev0") -> SourceRecord:
    return SourceRecord(
        record_id=f"{adapter}:{device}:1",
        adapter=adapter,
        device=device,
        shape="long_row",
        t_mono_ns=1,
        t_utc=datetime.now(UTC),
        row={"x": 1.0},
    )


@pytest.mark.anyio
async def test_subscribe_all_receives_every_emission() -> None:
    bus = DataBus()
    sub = bus.subscribe_all("all")
    await bus.publish(_sample("ch1", 1.0))
    await bus.publish(_source("watlow"))
    await bus.publish(
        DeviceEvent(
            adapter="x",
            device="d",
            t_mono_ns=1,
            t_utc=datetime.now(UTC),
            kind="info",
            message="hi",
        )
    )

    out: list[object] = []
    for _ in range(3):
        out.append(await sub.queue.get())
    assert isinstance(out[0], ChannelSample)
    assert isinstance(out[1], SourceRecord)
    assert isinstance(out[2], DeviceEvent)
    bus.close()


@pytest.mark.anyio
async def test_subscribe_channel_filters() -> None:
    bus = DataBus()
    sub = bus.subscribe_channel("only-ch1", channel="ch1")
    await bus.publish(_sample("ch1", 1.0))
    await bus.publish(_sample("ch2", 2.0))
    await bus.publish(_sample("ch1", 3.0))

    first = await sub.queue.get()
    second = await sub.queue.get()
    assert isinstance(first, ChannelSample) and first.value == 1.0
    assert isinstance(second, ChannelSample) and second.value == 3.0
    bus.close()


@pytest.mark.anyio
async def test_subscribe_adapter_filters_source_records() -> None:
    bus = DataBus()
    sub = bus.subscribe_adapter("watlow", adapter="watlow")
    await bus.publish(_source("watlow"))
    await bus.publish(_source("alicat"))
    item = await sub.queue.get()
    assert isinstance(item, SourceRecord) and item.adapter == "watlow"
    bus.close()


@pytest.mark.anyio
async def test_drop_oldest_keeps_freshness() -> None:
    bus = DataBus()
    sub = bus.subscribe_all("ring", capacity=2, policy=BackpressurePolicy.DROP_OLDEST)
    await bus.publish(_sample("ch", 1.0))
    await bus.publish(_sample("ch", 2.0))
    await bus.publish(_sample("ch", 3.0))  # evicts the first

    first = await sub.queue.get()
    second = await sub.queue.get()
    assert isinstance(first, ChannelSample)
    assert isinstance(second, ChannelSample)
    assert (first.value, second.value) == (2.0, 3.0)
    bus.close()


@pytest.mark.anyio
async def test_unsubscribe_stops_delivery() -> None:
    bus = DataBus()
    sub = bus.subscribe_all("once")
    await bus.publish(_sample("ch", 1.0))
    bus.unsubscribe(sub)
    await bus.publish(_sample("ch", 2.0))
    # First publish landed, second did not.
    received = await sub.queue.get()
    assert isinstance(received, ChannelSample) and received.value == 1.0
    assert sub.queue.depth == 0
    bus.close()


@pytest.mark.anyio
async def test_close_clears_subscriptions() -> None:
    bus = DataBus()
    bus.subscribe_all("a")
    bus.subscribe_all("b")
    bus.close()
    assert bus.subscription_names == ()
