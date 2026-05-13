"""Tests for :meth:`WorkerPool.shutdown_close`.

The best-effort shutdown variant must:

* Skip the IDLE-required precondition that :meth:`close` enforces.
* Disarm any worker still in ARMED/SAMPLING before closing.
* Aggregate per-worker results (degraded or not) into one
  :class:`PoolCloseResult` — the ShutdownCoordinator reads this.
* Return a clean result when the disarm + close path succeeds with no
  errors and every runner joins.
"""

from __future__ import annotations

import asyncio

import pytest

from capa.runtime.lifecycle import PoolState, WorkerState
from capa.runtime.pool import WorkerPool
from capa.runtime.runner import ThreadedRunner
from capa.runtime.worker import Worker
from tests.integration.runtime.fakes import (
    FakeAdapter,
    make_fake_adapter,
    make_run_context,
)


def _build_pool(adapters: list[FakeAdapter]) -> WorkerPool:
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
    return WorkerPool(workers=workers, device_to_resource=device_to_resource)


class TestShutdownCloseFromIdle:
    @pytest.mark.anyio
    async def test_idle_workers_close_cleanly(self) -> None:
        adapters = [make_fake_adapter(f"d{i}", resource_id=f"sim:d{i}") for i in range(2)]
        pool = _build_pool(adapters)
        await pool.open()
        # Pool is OPEN with all workers IDLE — same as the
        # config-load-then-close path.
        result = await pool.shutdown_close()
        assert result.clean is True
        assert result.errors == ()
        assert pool.state is PoolState.CLOSED
        assert len(result.worker_results) == 2
        for r in result.worker_results:
            assert r.runner_stop.joined is True

    @pytest.mark.anyio
    async def test_already_closed_pool_returns_clean_result(self) -> None:
        # Idempotent: shutdown_close on a CLOSED pool is a no-op clean
        # result. The coordinator may double-call this on a re-entry.
        adapters = [make_fake_adapter("d0", resource_id="sim:d0")]
        pool = _build_pool(adapters)
        await pool.open()
        await pool.shutdown_close()
        result = await pool.shutdown_close()
        assert result.clean is True
        assert result.worker_results == ()


class TestShutdownCloseFromSampling:
    @pytest.mark.anyio
    async def test_sampling_workers_get_disarmed_then_closed(self) -> None:
        adapters = [make_fake_adapter(f"d{i}", resource_id=f"sim:d{i}") for i in range(2)]
        pool = _build_pool(adapters)
        await pool.open()
        ctx = make_run_context()
        await pool.arm_all(ctx)
        # Begin sampling so the workers are not IDLE.
        loop = asyncio.get_running_loop()
        await pool.begin_sampling_all(consumer_loop=loop)
        for worker in pool.workers.values():
            assert worker.state is WorkerState.SAMPLING

        result = await pool.shutdown_close()
        # The disarm path is best-effort but the fake adapters cooperate,
        # so the close should still be clean.
        assert result.clean is True, f"errors={result.errors!r}"
        assert pool.state is PoolState.CLOSED
        # Every adapter should have seen stop() called as part of the
        # disarm + close.
        for adapter in adapters:
            assert adapter.stop_calls >= 1
            assert adapter.close_calls == 1

    @pytest.mark.anyio
    async def test_armed_workers_get_disarmed_then_closed(self) -> None:
        # ARMED is the harder case: streams haven't started, but the
        # state machine still requires DRAINING → IDLE before close().
        adapters = [make_fake_adapter("d0", resource_id="sim:d0")]
        pool = _build_pool(adapters)
        await pool.open()
        ctx = make_run_context()
        await pool.arm_all(ctx)
        for worker in pool.workers.values():
            assert worker.state is WorkerState.ARMED

        result = await pool.shutdown_close()
        assert result.clean is True
        assert pool.state is PoolState.CLOSED
