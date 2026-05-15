"""Bounded-disarm tests covering the secondary cancellation deadline.

The worker's :meth:`_disarm_impl` must:

1. Wrap each ``adapter.stop()`` in ``asyncio.wait_for`` with
   ``adapter_stop_grace_s``. A stop that ignores its deadline is
   recorded as an error and the disarm event still fires.
2. Wait for stream tasks to exit cooperatively with the caller-supplied
   ``grace_s`` (cooperative stop grace).
3. After cancelling stragglers (forced-cancel grace), bound the
   ``gather(*pending, return_exceptions=True)`` itself with
   ``stream_cancel_grace_s``. A stream task that swallows
   :class:`asyncio.CancelledError` and keeps running can not be killed
   from inside the worker; the application-level fuse is the hard backstop.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from capa.runtime.metrics import DisarmResult
from capa.runtime.runner import ThreadedRunner
from capa.runtime.shutdown import WorkerShutdownConfig
from capa.runtime.worker import Worker
from tests.integration.runtime.fakes import (
    make_cancel_ignoring_adapter,
    make_hanging_stop_adapter,
    make_run_context,
)


@pytest.mark.anyio
async def test_adapter_stop_is_bounded() -> None:
    """``adapter.stop()`` is wrapped in ``asyncio.wait_for`` — a stop
    call that hangs forever still lets disarm complete within roughly
    ``adapter_stop_grace_s + stream_stop_grace_s``."""
    adapter = make_hanging_stop_adapter("hangs-stop")
    cfg = WorkerShutdownConfig(
        adapter_stop_grace_s=0.2,
        stream_stop_grace_s=0.2,
        stream_cancel_grace_s=0.2,
        adapter_close_grace_s=1.0,
        runner_stop_grace_s=1.0,
    )
    worker = Worker(
        resource_id=adapter.resource_id,
        adapters=[adapter],
        runner=ThreadedRunner(name="bounded-stop"),
        shutdown_config=cfg,
    )
    await worker.async_start()
    try:
        await worker.async_arm(make_run_context())
        await worker.async_begin_sampling(consumer_loop=asyncio.get_running_loop())

        t0 = time.monotonic()
        result = await worker.async_disarm(grace_s=cfg.stream_stop_grace_s)
        elapsed = time.monotonic() - t0

        # adapter.stop hung — disarm reports FORCED and bounded near the
        # sum of the two grace windows. Generous upper bound so a slow
        # CI machine doesn't flake.
        assert result is DisarmResult.FORCED
        assert elapsed < 3.0, f"disarm took {elapsed:.2f}s, expected bounded"
    finally:
        await worker.async_close(grace_s=1.0)


@pytest.mark.anyio
async def test_secondary_gather_bound_unblocks_cancel_ignoring_stream() -> None:
    """A stream task that swallows ``CancelledError`` would otherwise
    wedge ``asyncio.gather(*pending)`` in disarm's forced-cancel step. The
    secondary ``stream_cancel_grace_s`` bound is what keeps disarm bounded —
    the test confirms it returns ``FORCED`` and within a tight envelope."""
    adapter = make_cancel_ignoring_adapter(
        "ignores-cancel",
        cancel_swallow_s=5.0,  # well beyond stream_cancel_grace_s below
    )
    cfg = WorkerShutdownConfig(
        adapter_stop_grace_s=0.2,
        stream_stop_grace_s=0.2,
        stream_cancel_grace_s=0.25,
        adapter_close_grace_s=1.0,
        runner_stop_grace_s=1.0,
    )
    worker = Worker(
        resource_id=adapter.resource_id,
        adapters=[adapter],
        runner=ThreadedRunner(name="bounded-cancel"),
        shutdown_config=cfg,
    )
    await worker.async_start()
    try:
        await worker.async_arm(make_run_context())
        await worker.async_begin_sampling(consumer_loop=asyncio.get_running_loop())
        # Let the stream task actually emit so it's in its sleep.
        await asyncio.sleep(0.05)

        t0 = time.monotonic()
        result = await worker.async_disarm(grace_s=cfg.stream_stop_grace_s)
        elapsed = time.monotonic() - t0

        assert result is DisarmResult.FORCED
        # Cooperative stop grace (0.2) + forced-cancel secondary grace (0.25)
        # plus a bit of overhead. The cancel-swallow is 5s — without the
        # secondary bound this would block ~5s.
        assert elapsed < 2.0, f"secondary bound failed; disarm took {elapsed:.2f}s"
    finally:
        # close: the worker is now IDLE, runner stop will join the
        # thread cleanly since the swallow-cancel task is detached.
        result_close = await worker.async_close(grace_s=1.5)
        # The cancel-swallow may still hold the loop alive past the
        # close — we just don't want it to wedge the test forever.
        assert result_close is not None  # WorkerCloseResult
