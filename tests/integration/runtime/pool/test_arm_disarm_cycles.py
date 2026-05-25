"""The load-bearing manual-control-between-runs property.

    "Operators can issue commands and read PVs without an active run,
    without re-opening hardware."

This is the acceptance gate for the WorkerPool's manual-dispatch surface.
If ``adapter.open`` is called more than once per pool, behaviour has
regressed: the Sartorius cold-open race would pay its cost on every run,
and operators would lose the between-runs fiddling the pool enables.
"""

from __future__ import annotations

import asyncio

import pytest

from capa.runtime.lifecycle import PoolState, WorkerState
from capa.runtime.metrics import DisarmResult
from capa.runtime.pool import WorkerPool
from capa.runtime.runner import ThreadedRunner
from capa.runtime.worker import Worker
from tests.integration.runtime.fakes import (
    FakeAdapter,
    fake_command,
    make_fake_adapter,
    make_run_context,
)


def _build_pool(n_devices: int = 3) -> tuple[WorkerPool, list[FakeAdapter]]:
    adapters = [
        make_fake_adapter(f"dev{i}", resource_id=f"sim:dev{i}", tick_period_s=0.01, emit_limit=2)
        for i in range(n_devices)
    ]
    workers: dict[str, Worker] = {}
    device_to_resource: dict[str, str] = {}
    for adapter in adapters:
        rid = adapter.resource_id
        workers[rid] = Worker(
            resource_id=rid,
            adapters=[adapter],
            runner=ThreadedRunner(name=f"worker-{rid}"),
        )
        device_to_resource[adapter.name] = rid
    return (
        WorkerPool(workers=workers, device_to_resource=device_to_resource),
        adapters,
    )


class TestMultipleRunsNoReopen:
    @pytest.mark.anyio
    async def test_five_arm_disarm_cycles_one_open_call(self) -> None:
        """Multiple arm/disarm cycles reuse the already-open adapters."""
        pool, adapters = _build_pool(n_devices=3)
        await pool.open()
        # Snapshot open-call counts after pool.open.
        open_counter_before = sum(a.open_calls for a in adapters)
        try:
            for _ in range(5):
                ctx = make_run_context()
                await pool.arm_all(ctx)
                bridges = await pool.begin_sampling_all(consumer_loop=asyncio.get_running_loop())
                # Drain each bridge's 2 emissions.
                for b in bridges.values():
                    em1 = await b.get()
                    em2 = await b.get()
                    assert em1 is not None and em2 is not None
                results = await pool.disarm_all(grace_s=2.0)
                for r in results.values():
                    assert r is DisarmResult.OK
                # Workers back at IDLE after each cycle.
                for w in pool.workers.values():
                    assert w.state is WorkerState.IDLE
        finally:
            await pool.close()

        # adapter.open was called exactly once across all 5 cycles.
        open_counter_after_close = sum(a.open_calls for a in adapters)
        assert open_counter_after_close == open_counter_before
        # adapter.close was called exactly once per adapter (on pool.close).
        for a in adapters:
            assert a.close_calls == 1
            # adapter.start was called 5 times (once per cycle); adapter.stop
            # similarly. The connection layer (open/close) is split from the
            # sampling layer (start/stop) — pool.open / pool.close drive the
            # former, conductor arm/disarm drives the latter.
            assert a.start_calls == 5
            assert a.stop_calls == 5


class TestManualDispatchBetweenRuns:
    @pytest.mark.anyio
    async def test_dispatch_works_in_idle_between_runs(self) -> None:
        """The between-runs manual-control flow:
        open → arm → disarm → dispatch → arm → disarm → dispatch → close."""
        pool, adapters = _build_pool(n_devices=2)
        await pool.open()
        try:
            # First "run."
            await pool.arm_all(make_run_context())
            await pool.disarm_all(grace_s=1.0)

            # Now in IDLE. A manual command between runs lands successfully.
            r = await asyncio.wrap_future(pool.dispatch("dev0", fake_command()))
            assert r.accepted is True
            assert len(adapters[0].commands_completed) == 1

            # Second "run."
            await pool.arm_all(make_run_context())
            await pool.disarm_all(grace_s=1.0)

            # Another manual command between runs.
            r2 = await asyncio.wrap_future(pool.dispatch("dev1", fake_command()))
            assert r2.accepted is True
            assert len(adapters[1].commands_completed) == 1
        finally:
            await pool.close()

        # Still only one open per adapter.
        for a in adapters:
            assert a.open_calls == 1


class TestPoolStateAcrossRuns:
    @pytest.mark.anyio
    async def test_pool_stays_open_during_run(self) -> None:
        """Pool state does not change during a run — only worker states do."""
        pool, _ = _build_pool(n_devices=1)
        await pool.open()
        try:
            assert pool.state is PoolState.OPEN
            await pool.arm_all(make_run_context())
            assert pool.state is PoolState.OPEN  # pool unchanged
            bridges = await pool.begin_sampling_all(consumer_loop=asyncio.get_running_loop())
            assert pool.state is PoolState.OPEN
            for b in bridges.values():
                await b.get()
            await pool.disarm_all(grace_s=1.0)
            assert pool.state is PoolState.OPEN
        finally:
            await pool.close()
        assert getattr(pool, "state") is PoolState.CLOSED  # noqa: B009
