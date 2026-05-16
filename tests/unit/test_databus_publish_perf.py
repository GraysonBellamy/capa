"""DataBus publish dispatch is indexed, not linear.

The plan's benchmark spec: with 100 channel subscribers and 1 wildcard,
publishing 10k samples for one channel should be dramatically faster than
a linear ``for sub in subscriptions`` walk would be. The exact ratio is
host-dependent; the test pins a generous but defensive threshold so it
fails loudly if the index regresses to O(N) without false-positiving on
slow CI.
"""

from __future__ import annotations

import time

import anyio
import pytest

from capa.core.backpressure import BackpressurePolicy
from capa.core.databus import DataBus
from capa.devices.records import ChannelSample


def _sample(channel: str, value: float) -> ChannelSample:
    return ChannelSample(
        channel=channel,
        t_mono_ns=1,
        t_mono_s=1e-9,
        value=value,
        unit="V",
    )


@pytest.mark.anyio
async def test_channel_publish_visits_only_matching_subscribers() -> None:
    """Correctness check for the index: publishing to one channel must
    not enqueue anything onto the 99 sibling-channel subscribers."""
    bus = DataBus()
    subs = [bus.subscribe_channel(f"sub-{i}", channel=f"ch-{i}", capacity=4) for i in range(100)]
    wildcard = bus.subscribe_all("everything", capacity=4)

    for _ in range(3):
        await bus.publish(_sample("ch-7", 1.0))

    # Target subscriber and wildcard received everything; siblings got nothing.
    assert subs[7].queue.depth == 3
    assert wildcard.queue.depth == 3
    for i, sub in enumerate(subs):
        if i == 7:
            continue
        assert sub.queue.depth == 0, f"sub-{i} received unexpected sample"

    bus.close()


@pytest.mark.anyio
async def test_indexed_dispatch_scales_better_than_linear_walk() -> None:
    """A 100-subscriber bus publishing 1k samples to one channel should
    finish in time consistent with O(1) dispatch — not O(N). The previous
    implementation called ``sub.predicate(emission)`` for every subscriber
    on every publish, so a regression to that shape would 100× the work
    per emission. The threshold below has plenty of slack for slow CI
    while still catching a linear-walk regression."""
    bus = DataBus()
    for i in range(100):
        bus.subscribe_channel(
            f"sub-{i}",
            channel=f"ch-{i}",
            capacity=10_000,
            policy=BackpressurePolicy.DROP_OLDEST,
        )
    bus.subscribe_all("everything", capacity=10_000, policy=BackpressurePolicy.DROP_OLDEST)

    n_publishes = 1_000
    start = time.perf_counter()
    for _ in range(n_publishes):
        await bus.publish(_sample("ch-7", 1.0))
    elapsed = time.perf_counter() - start

    # On a dev machine indexed dispatch is well under 100 ms for 1k publishes
    # against 100 subscribers. Allow 5× slack for the noisiest CI hardware
    # — still well below the ~1 s a linear walk would take with the same
    # subscriber count and per-emission predicate call.
    assert elapsed < 0.5, (
        f"indexed publish should be << 500 ms for 1000×100 fan-out; observed {elapsed:.3f} s"
    )
    bus.close()


@pytest.mark.anyio
async def test_unsubscribe_removes_from_indexed_bucket() -> None:
    """``unsubscribe`` on an indexed subscriber must remove it from the
    channel bucket, not just close its queue — otherwise the next publish
    would still dispatch into a closed queue and crash."""
    bus = DataBus()
    sub = bus.subscribe_channel("once", channel="ch1")
    await bus.publish(_sample("ch1", 1.0))
    bus.unsubscribe(sub)
    # Second publish must not raise even though the bucket is now empty.
    await bus.publish(_sample("ch1", 2.0))
    # And the sub's queue must hold only the first sample.
    item = await sub.queue.get()
    assert isinstance(item, ChannelSample) and item.value == 1.0
    assert sub.queue.depth == 0
    bus.close()


@pytest.mark.anyio
async def test_adapter_subscriber_still_receives_channel_samples() -> None:
    """The original ``_adapter_predicate`` matched ChannelSamples whose
    ``source_record_id`` started with ``"{adapter}:"``. The indexed dispatch
    must preserve that — adapter subscribers expect to see the channel
    samples flowing through their adapter, not just SourceRecords."""
    bus = DataBus()
    sub = bus.subscribe_adapter("alicat-all", adapter="alicat")
    sample = ChannelSample(
        channel="flow",
        t_mono_ns=1,
        t_mono_s=1e-9,
        value=1.5,
        unit="sccm",
        source_record_id="alicat:dev0:1",
    )
    await bus.publish(sample)

    with anyio.fail_after(0.5):
        received = await sub.queue.get()
    assert isinstance(received, ChannelSample)
    assert received.source_record_id == "alicat:dev0:1"
    bus.close()
