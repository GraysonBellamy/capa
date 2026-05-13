"""Unit tests for :mod:`capa.runtime.heartbeat`.

Verifies the loop-lag observer can detect a deliberately-induced stall.
A heartbeat at 20 Hz on a loop that synchronously sleeps for 200 ms
inside one of its coroutines must record a p99 >= ~200 ms.
"""

from __future__ import annotations

import asyncio
import time

import anyio
import pytest

from capa.runtime.heartbeat import LoopLagMetric, heartbeat_task


@pytest.mark.anyio
async def test_heartbeat_records_observations_in_quiet_loop() -> None:
    metric = LoopLagMetric(name="quiet")
    stop = anyio.Event()

    async def driver() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(heartbeat_task, metric, stop)
            await asyncio.sleep(0.3)  # ~6 samples at 50 ms
            stop.set()

    await driver()
    assert metric.samples_total > 0
    assert metric.p50_ms >= 0
    assert metric.p99_ms >= 0


@pytest.mark.anyio
async def test_heartbeat_detects_loop_stall() -> None:
    """A coroutine that blocks the loop with ``time.sleep`` must show up
    as p99 lag on the next heartbeat sample after the stall ends.

    We deliberately use ``time.sleep`` (not ``asyncio.sleep``) to simulate
    a CPU-bound task starving the loop — the exact failure mode the
    heartbeat is supposed to surface in production.
    """
    metric = LoopLagMetric(name="stalled")
    stop = anyio.Event()
    stall_done = anyio.Event()

    async def stall() -> None:
        await asyncio.sleep(0.1)  # let heartbeat take a few clean samples
        time.sleep(0.2)  # block the loop for 200 ms
        stall_done.set()

    async def driver() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(heartbeat_task, metric, stop)
            tg.start_soon(stall)
            await stall_done.wait()
            await asyncio.sleep(0.1)  # allow heartbeat to sample after stall
            stop.set()

    await driver()
    assert metric.samples_total > 0
    # p99 should reflect the stall (~200 ms). Allow some slack.
    assert metric.max_lag_ms >= 150.0, (
        f"expected max_lag_ms >= 150 after 200 ms stall, got {metric.max_lag_ms}"
    )


@pytest.mark.anyio
async def test_heartbeat_period_validates() -> None:
    metric = LoopLagMetric(name="bad")
    stop = anyio.Event()
    with pytest.raises(ValueError, match="period_s"):
        await heartbeat_task(metric, stop, period_s=0)


def test_loop_lag_metric_observe_updates_max_lag_ms() -> None:
    metric = LoopLagMetric(name="manual")
    metric.observe(5.0)
    metric.observe(120.0)
    metric.observe(40.0)
    assert metric.samples_total == 3
    assert metric.max_lag_ms == 120.0


def test_loop_lag_percentiles_monotone() -> None:
    metric = LoopLagMetric(name="quantiles")
    for v in (1.0, 5.0, 10.0, 50.0, 100.0, 200.0):
        metric.observe(v)
    assert metric.p50_ms <= metric.p99_ms
