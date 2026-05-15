"""Adapter-error handling tests for :class:`Worker`.

When ``adapter.stream()`` raises mid-stream, the worker must

1. record an event into the run bundle (``worker_adapter_error``),
2. set ``worker.fatal_error`` so future conductor policy code can read it,
3. exit the stream task; the rest of the worker stays consistent.
"""

from __future__ import annotations

import asyncio

import pytest

from capa.runtime.runner import ThreadedRunner
from capa.runtime.worker import Worker
from tests.integration.runtime.fakes import (
    FakeWriterRef,
    make_fake_adapter,
    make_run_context,
)


class TestStreamRaises:
    @pytest.mark.anyio
    async def test_mid_stream_error_records_event(self) -> None:
        """Adapter emits 2 items, then raises. The worker writes one
        ``worker_adapter_error`` event into the run context's writer."""

        class StreamBoomError(RuntimeError):
            pass

        adapter = make_fake_adapter(
            "a",
            tick_period_s=0.01,
            stream_raises=StreamBoomError("stream lost"),
            raise_after=2,
        )
        writer = FakeWriterRef()
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=ThreadedRunner(name="stream-err-event"),
        )
        await worker.async_start()
        try:
            ctx = make_run_context(writer=writer)
            await worker.async_arm(ctx)
            bridge = await worker.async_begin_sampling(consumer_loop=asyncio.get_running_loop())

            # Drain the two clean emissions.
            await bridge.get()
            await bridge.get()

            # The next get() yields the close sentinel — the stream task
            # raised, and disarm must be invoked by the caller to clean up.
            # The error event lands in the writer once the stream task's
            # exception path runs.
            # We give the worker a moment to record before disarming.
            await asyncio.sleep(0.1)

            assert worker.fatal_error is not None
            assert isinstance(worker.fatal_error, StreamBoomError)

            event_kinds = [e["kind"] for e in writer.events]
            assert "worker_adapter_error" in event_kinds
            err_event = next(e for e in writer.events if e["kind"] == "worker_adapter_error")
            assert err_event["metadata"]["adapter"] == "a"
            assert err_event["metadata"]["error_type"] == "StreamBoomError"
        finally:
            await worker.async_disarm(grace_s=2.0)
            await worker.async_close(grace_s=1.0)

    @pytest.mark.anyio
    async def test_disarm_after_stream_error_still_returns(self) -> None:
        """Even after the stream task raised, disarm must complete and
        return the worker to IDLE — the conductor's per-run cleanup
        depends on this."""

        class StreamBoomError(RuntimeError):
            pass

        adapter = make_fake_adapter(
            "a",
            tick_period_s=0.01,
            stream_raises=StreamBoomError("crash"),
            raise_after=1,
        )
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=ThreadedRunner(name="stream-err-cleanup"),
        )
        await worker.async_start()
        try:
            await worker.async_arm(make_run_context())
            bridge = await worker.async_begin_sampling(consumer_loop=asyncio.get_running_loop())
            await bridge.get()  # one good emission
            # Stream task is about to raise. Wait briefly.
            await asyncio.sleep(0.1)

            # Disarm should still return OK (the stream task already exited
            # via exception; disarm's asyncio.wait sees it as "done" and
            # doesn't need to cancel).
            result = await worker.async_disarm(grace_s=2.0)
            # The result type matters less than reaching IDLE.
            from capa.runtime.lifecycle import WorkerState

            assert worker.state is WorkerState.IDLE
            assert result is not None
            # fatal_error surfaces from the stream task's exception.
            assert worker.fatal_error is not None
        finally:
            await worker.async_close(grace_s=1.0)

    @pytest.mark.anyio
    async def test_fatal_error_cleared_on_next_arm(self) -> None:
        """After a failed run, ``arm()`` on the next run must reset
        ``fatal_error`` — otherwise stale state would leak across runs."""

        class StreamBoomError(RuntimeError):
            pass

        adapter = make_fake_adapter(
            "a",
            tick_period_s=0.01,
            stream_raises=StreamBoomError("first run failed"),
            raise_after=1,
        )
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=ThreadedRunner(name="stream-err-clear"),
        )
        await worker.async_start()
        try:
            # Run 1: stream fails.
            await worker.async_arm(make_run_context())
            bridge = await worker.async_begin_sampling(consumer_loop=asyncio.get_running_loop())
            await bridge.get()
            await asyncio.sleep(0.1)
            await worker.async_disarm(grace_s=2.0)
            assert worker.fatal_error is not None

            # Run 2: clear stream_raises, observe that arm() clears the
            # carry-over.
            adapter.stream_raises = None
            adapter.raise_after = 0
            await worker.async_arm(make_run_context())
            assert worker.fatal_error is None
            await worker.async_disarm(grace_s=1.0)
        finally:
            await worker.async_close(grace_s=1.0)
