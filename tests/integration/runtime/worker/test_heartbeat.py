"""Per-worker heartbeat lifecycle.

Phase 5 of the unified-architecture cleanup wired a
:func:`~capa.runtime.heartbeat.heartbeat_task` inside each worker's loop
so :attr:`WorkerMetrics.loop_lag` has a real producer. These tests pin
that contract: observations accumulate while the worker is open, and the
task is torn down at :meth:`Worker.async_close`.
"""

from __future__ import annotations

import asyncio

import pytest

from capa.runtime.runner import ThreadedRunner
from capa.runtime.worker import Worker
from tests.integration.runtime.fakes import make_fake_adapter


@pytest.mark.anyio
async def test_heartbeat_observes_loop_lag_while_open() -> None:
    """After a worker has been open for a few heartbeat periods, the
    metric ring has real observations — not the structural zero of an
    un-wired field."""
    adapter = make_fake_adapter("a")
    worker = Worker(
        resource_id=adapter.resource_id,
        adapters=[adapter],
        runner=ThreadedRunner(name="worker-heartbeat-open"),
    )
    await worker.async_start()
    try:
        # Heartbeat fires every 50 ms; 250 ms gives ~5 observations.
        await asyncio.sleep(0.25)
        assert worker.metrics.loop_lag.samples_total > 0
    finally:
        await worker.async_close(grace_s=1.0)


@pytest.mark.anyio
async def test_heartbeat_stops_at_close() -> None:
    """The heartbeat task is bound to the open lifetime, so it must
    not survive close. After close, the worker's task handle is cleared
    and no further observations can land."""
    adapter = make_fake_adapter("a")
    worker = Worker(
        resource_id=adapter.resource_id,
        adapters=[adapter],
        runner=ThreadedRunner(name="worker-heartbeat-close"),
    )
    await worker.async_start()
    await asyncio.sleep(0.1)
    await worker.async_close(grace_s=1.0)
    # Internal handles cleared.
    assert worker._heartbeat_task is None
    assert worker._heartbeat_stop is None
    # Capture *after* close completes, then verify the count is frozen.
    # A final in-flight tick may land between the test's capture and the
    # stop event being set, so we sample after close (no further loop is
    # running) rather than before.
    samples_after_close = worker.metrics.loop_lag.samples_total
    await asyncio.sleep(0.2)
    assert worker.metrics.loop_lag.samples_total == samples_after_close
