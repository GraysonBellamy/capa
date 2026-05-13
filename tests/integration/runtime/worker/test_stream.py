"""Streaming + disarm tests for :class:`Worker`.

Covers:

* ``begin_sampling`` builds the outbound bridge on the worker loop and
  spawns one stream task per adapter.
* Emissions flow over the bridge in order, with the bridge's own latency
  metric incrementing.
* ``disarm(grace_s=...)`` returns ``DisarmResult.OK`` when streams exit
  cleanly within grace, ``DisarmResult.FORCED`` when they don't.
* The outbound bridge closes after disarm — consumer iteration sees EOF.
* ``begin_sampling`` rolls back cleanly if ``adapter.start()`` raises.

Migration doc references: §3.4 (data flow), §3.7 (drain-before-preflight
— here just the local equivalent: bridge attached before sampling), §3.8
(disarm Phase A / Phase B), §4.1 (Worker state machine).
"""

from __future__ import annotations

import asyncio

import pytest

from capa.runtime.lifecycle import WorkerState
from capa.runtime.metrics import DisarmResult
from capa.runtime.runner import ThreadedRunner
from capa.runtime.worker import Worker
from tests.integration.runtime.fakes import (
    make_fake_adapter,
    make_run_context,
)


async def _wait(fut: object) -> object:
    return await asyncio.wrap_future(fut)  # type: ignore[arg-type]


async def _start_sampling(worker: Worker) -> object:
    """Helper: drive a worker to SAMPLING and return its bridge with the
    consumer attached on the test's loop."""
    await _wait(worker.start())
    await _wait(worker.arm(make_run_context()))
    bridge = await _wait(worker.begin_sampling(consumer_loop=asyncio.get_running_loop()))
    return bridge


class TestBeginSampling:
    @pytest.mark.anyio
    async def test_begin_sampling_transitions_state(self) -> None:
        adapter = make_fake_adapter("a", tick_period_s=0.05, emit_limit=3)
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=ThreadedRunner(name="stream-state"),
        )
        await _wait(worker.start())
        try:
            await _wait(worker.arm(make_run_context()))
            assert worker.state is WorkerState.ARMED
            bridge = await _wait(worker.begin_sampling(consumer_loop=asyncio.get_running_loop()))
            try:
                assert worker.state is WorkerState.SAMPLING
                assert adapter.start_calls == 1
                assert bridge.name.startswith("worker-")  # type: ignore[union-attr]
            finally:
                await _wait(worker.disarm(grace_s=1.0))
        finally:
            await _wait(worker.close(grace_s=1.0))

    @pytest.mark.anyio
    async def test_begin_sampling_refused_in_idle(self) -> None:
        adapter = make_fake_adapter("a")
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=ThreadedRunner(name="stream-refused"),
        )
        await _wait(worker.start())
        try:
            from capa.runtime.errors import WorkerStateError

            with pytest.raises(WorkerStateError, match="requires ARMED"):
                await _wait(worker.begin_sampling(consumer_loop=asyncio.get_running_loop()))
        finally:
            await _wait(worker.close(grace_s=1.0))

    @pytest.mark.anyio
    async def test_begin_sampling_rolls_back_on_start_failure(self) -> None:
        """Two adapters; second's start raises. The first must be stopped
        and the bridge must not leak."""
        good = make_fake_adapter("good", resource_id="serial:shared", emit_limit=10)
        bad = make_fake_adapter("bad", resource_id="serial:shared")
        bad._start_raises = RuntimeError("bad cannot start")

        worker = Worker(
            resource_id="serial:shared",
            adapters=[good, bad],
            runner=ThreadedRunner(name="stream-rollback"),
        )
        await _wait(worker.start())
        try:
            await _wait(worker.arm(make_run_context()))
            with pytest.raises(RuntimeError, match="bad cannot start"):
                await _wait(worker.begin_sampling(consumer_loop=asyncio.get_running_loop()))
            # Worker stayed in ARMED; rollback returned us there.
            assert worker.state is WorkerState.ARMED
            # Good was started and then stopped during rollback.
            assert good.start_calls == 1
            assert good.stop_calls == 1
        finally:
            await _wait(worker.disarm(grace_s=1.0))
            await _wait(worker.close(grace_s=1.0))


class TestEmissionFlow:
    @pytest.mark.anyio
    async def test_emissions_flow_in_order(self) -> None:
        adapter = make_fake_adapter("a", tick_period_s=0.01, emit_limit=5)
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=ThreadedRunner(name="stream-order"),
        )
        try:
            bridge = await _start_sampling(worker)

            # Drain the bridge for the five emissions.
            received = []
            for _ in range(5):
                emission = await bridge.get()  # type: ignore[union-attr]
                assert emission is not None
                received.append(emission)

            assert len(received) == 5
            # Each emission is a DeviceSnapshot from FakeAdapter; sequence
            # numbers (in .fields["seq"]) should be monotonic.
            seqs = [int(e.fields["seq"]) for e in received]  # type: ignore[union-attr]
            assert seqs == sorted(seqs)
            assert len(set(seqs)) == 5  # no duplicates

            # Bridge latency metric incremented.
            assert bridge.metrics.dequeued_total == 5  # type: ignore[union-attr]
        finally:
            await _wait(worker.disarm(grace_s=2.0))
            await _wait(worker.close(grace_s=1.0))

    @pytest.mark.anyio
    async def test_worker_metrics_count_emissions(self) -> None:
        adapter = make_fake_adapter("a", tick_period_s=0.005, emit_limit=10)
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=ThreadedRunner(name="stream-metrics"),
        )
        try:
            bridge = await _start_sampling(worker)
            for _ in range(10):
                await bridge.get()  # type: ignore[union-attr]
        finally:
            await _wait(worker.disarm(grace_s=2.0))
            await _wait(worker.close(grace_s=1.0))
        assert worker.metrics.samples_emitted == 10


class TestDisarm:
    @pytest.mark.anyio
    async def test_disarm_returns_ok_on_clean_drain(self) -> None:
        adapter = make_fake_adapter("a", tick_period_s=0.01, emit_limit=2)
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=ThreadedRunner(name="disarm-ok"),
        )
        try:
            bridge = await _start_sampling(worker)
            # Drain the two emissions.
            await bridge.get()  # type: ignore[union-attr]
            await bridge.get()  # type: ignore[union-attr]

            result = await _wait(worker.disarm(grace_s=2.0))
            assert result is DisarmResult.OK
            assert worker.state is WorkerState.IDLE
            assert adapter.stop_calls == 1

            # Bridge closed: get() yields None (sentinel EOF).
            assert await bridge.get() is None  # type: ignore[union-attr]
        finally:
            await _wait(worker.close(grace_s=1.0))

    @pytest.mark.anyio
    async def test_disarm_returns_forced_on_grace_expiry(self) -> None:
        """Adapter with a long tick period AND ignores lifecycle stops:
        we make stream() refuse to exit until cancelled. Disarm with
        small grace should escalate to FORCED."""
        adapter = make_fake_adapter("stuck", tick_period_s=5.0, emit_limit=None)
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=ThreadedRunner(name="disarm-forced"),
        )
        try:
            bridge = await _start_sampling(worker)
            # Wait for first emission so stream_task is parked in sleep.
            first = await bridge.get()  # type: ignore[union-attr]
            assert first is not None

            # Grace is shorter than tick_period_s (5s) — the stream task is
            # parked in anyio.sleep(5.0). Even after adapter.stop() flips
            # the lifecycle, the stream loop won't re-check until the sleep
            # returns. The worker must cancel the task on grace expiry.
            result = await _wait(worker.disarm(grace_s=0.3))
            assert result is DisarmResult.FORCED
            assert worker.state is WorkerState.IDLE
        finally:
            await _wait(worker.close(grace_s=1.0))

    @pytest.mark.anyio
    async def test_bridge_closes_after_disarm(self) -> None:
        adapter = make_fake_adapter("a", tick_period_s=0.01, emit_limit=2)
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=ThreadedRunner(name="disarm-bridge-close"),
        )
        try:
            bridge = await _start_sampling(worker)
            # Don't drain — let the producer fill some, then disarm.
            await asyncio.sleep(0.05)
            await _wait(worker.disarm(grace_s=2.0))
            assert bridge.closed is True  # type: ignore[union-attr]
        finally:
            await _wait(worker.close(grace_s=1.0))


class TestMultipleSamplingCycles:
    @pytest.mark.anyio
    async def test_two_sample_disarm_cycles(self) -> None:
        """Stretch of the load-bearing 'pool supports multiple arm/disarm
        cycles without reopen' to include actual streaming."""
        adapter = make_fake_adapter("a", tick_period_s=0.01, emit_limit=3)
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=ThreadedRunner(name="cycles"),
        )
        await _wait(worker.start())
        try:
            for _ in range(2):
                await _wait(worker.arm(make_run_context()))
                bridge = await _wait(
                    worker.begin_sampling(consumer_loop=asyncio.get_running_loop())
                )
                # Drain 3 emissions then disarm
                for _i in range(3):
                    em = await bridge.get()
                    assert em is not None
                result = await _wait(worker.disarm(grace_s=2.0))
                assert result is DisarmResult.OK
                assert worker.state is WorkerState.IDLE
        finally:
            await _wait(worker.close(grace_s=1.0))
        # Adapter was opened once, started twice, stopped twice.
        assert adapter.open_calls == 1
        assert adapter.close_calls == 1
        assert adapter.start_calls == 2
        assert adapter.stop_calls == 2
