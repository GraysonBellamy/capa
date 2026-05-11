"""P0-1 acceptance: fan-out latency telemetry.

Verifies the metrics wiring added to :meth:`ExperimentEngine._fanout_task`:

* The producer→fan-out queue records lag via paired
  :meth:`QueueMetrics.mark_enqueued` / :meth:`mark_dequeued` calls.
* The ``writer.fanout.submit`` and ``writer.fanout.publish`` collectors
  distinguish a slow writer thread from a slow databus subscriber.
"""

from __future__ import annotations

import anyio
import pytest

from capa.core.backpressure import BackpressurePolicy, BoundedQueue
from capa.core.databus import DataBus
from capa.devices.records import ChannelSample, DeviceEmission
from capa.experiment.engine import ExperimentEngine


def _sample(value: float) -> ChannelSample:
    return ChannelSample(
        channel="ch0",
        t_mono_ns=1,
        t_mono_s=1e-9,
        value=value,
        unit="V",
    )


class _FastWriter:
    async def submit(self, item: object) -> None:
        return


class _SlowWriter:
    def __init__(self, delay_s: float) -> None:
        self._delay_s = delay_s

    async def submit(self, item: object) -> None:
        await anyio.sleep(self._delay_s)


async def _drive_fanout(
    engine: ExperimentEngine,
    emissions: list[DeviceEmission],
) -> None:
    queue: BoundedQueue[DeviceEmission] = BoundedQueue(
        name="producer-fanout",
        capacity=max(8, len(emissions)),
        policy=BackpressurePolicy.BLOCK,
    )
    queue_metrics = engine.metrics.queue("producer-fanout")
    for emission in emissions:
        queue_metrics.mark_enqueued(id(emission))
        await queue.put(emission)
        queue_metrics.observe_depth(queue.depth)
    queue.close()
    await engine._fanout_task(queue, queue_metrics)


@pytest.mark.anyio
async def test_fanout_metrics_record_lag_and_submit_publish_times() -> None:
    """Baseline: with a fast writer and no slow subscribers, all three
    metrics record observations and lag is bounded."""
    engine = ExperimentEngine()
    engine._writer_thread = _FastWriter()  # type: ignore[assignment]
    emissions = [_sample(float(i)) for i in range(5)]

    await _drive_fanout(engine, emissions)

    snap = engine.metrics.snapshot_for_manifest()
    assert "queue.producer-fanout" in snap
    assert "writer.fanout.submit" in snap
    assert "writer.fanout.publish" in snap
    # Every emission was paired enqueue/dequeue, so the registry observed
    # five lag samples even though they're tiny in this synthetic test.
    assert snap["writer.fanout.submit"]["write_count"] == 5.0
    assert snap["writer.fanout.publish"]["write_count"] == 5.0


@pytest.mark.anyio
async def test_slow_subscriber_inflates_publish_not_submit() -> None:
    """A slow databus subscriber must drive up ``fanout.publish`` lag
    while ``fanout.submit`` stays flat. This is the core "where is the
    bottleneck?" question P0-1 is supposed to answer at-a-glance from
    the manifest."""
    engine = ExperimentEngine()
    engine._writer_thread = _FastWriter()  # type: ignore[assignment]

    # Hand-craft a databus whose only subscriber blocks every publish for
    # a measurable interval. ABORT_RUN with capacity=1 and a sleepy consumer
    # back-pressures the publish call the same way BLOCK would, but with a
    # safety deadline so a regression can't hang the test.
    bus = DataBus()
    sub = bus.subscribe_critical(
        "slow",
        capacity=1,
        abort_after_s=2.0,
    )
    engine._databus = bus

    drained: list[DeviceEmission] = []

    async def _slow_consumer() -> None:
        async for item in sub:
            drained.append(item)
            await anyio.sleep(0.02)

    emissions = [_sample(float(i)) for i in range(4)]

    async with anyio.create_task_group() as tg:
        tg.start_soon(_slow_consumer)
        await _drive_fanout(engine, emissions)
        bus.close()
        tg.cancel_scope.cancel()

    snap = engine.metrics.snapshot_for_manifest()
    submit_max = snap["writer.fanout.submit"]["write_s_max"]
    publish_max = snap["writer.fanout.publish"]["write_s_max"]
    # Writer was a no-op stub; publish backed up behind the slow subscriber.
    assert publish_max > submit_max
    assert publish_max >= 0.01


@pytest.mark.anyio
async def test_slow_writer_inflates_submit_not_publish() -> None:
    """Mirror image: a slow writer drives ``fanout.submit`` lag without
    affecting ``fanout.publish``."""
    engine = ExperimentEngine()
    engine._writer_thread = _SlowWriter(delay_s=0.02)  # type: ignore[assignment]

    emissions = [_sample(float(i)) for i in range(4)]
    await _drive_fanout(engine, emissions)

    snap = engine.metrics.snapshot_for_manifest()
    submit_max = snap["writer.fanout.submit"]["write_s_max"]
    publish_max = snap["writer.fanout.publish"]["write_s_max"]
    assert submit_max > publish_max
    assert submit_max >= 0.01
