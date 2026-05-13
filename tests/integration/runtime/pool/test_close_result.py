"""Tests for :class:`PoolCloseResult` aggregation.

``WorkerPool.close()`` returns a structured aggregate: ``clean=True``
iff every worker closed without adapter errors and its runner thread
joined. Adapter errors are captured per-worker in
:class:`WorkerCloseResult.adapter_close_errors`; pool-level crashes go
into :attr:`PoolCloseResult.errors`.
"""

from __future__ import annotations

import pytest

from capa.runtime.pool import WorkerPool
from capa.runtime.runner import ThreadedRunner
from capa.runtime.shutdown import PoolCloseResult, WorkerShutdownConfig
from capa.runtime.worker import Worker
from tests.integration.runtime.fakes import (
    make_fake_adapter,
    make_hanging_close_adapter,
)


@pytest.mark.anyio
async def test_pool_close_returns_clean_result_for_happy_path() -> None:
    adapters = [make_fake_adapter(f"dev{i}", resource_id=f"sim:dev{i}") for i in range(2)]
    workers: dict[str, Worker] = {
        a.resource_id: Worker(
            resource_id=a.resource_id,
            adapters=[a],
            runner=ThreadedRunner(name=f"worker-{a.resource_id}"),
        )
        for a in adapters
    }
    pool = WorkerPool(
        workers=workers,
        device_to_resource={a.name: a.resource_id for a in adapters},
    )
    await pool.open()
    result = await pool.close()

    assert isinstance(result, PoolCloseResult)
    assert result.clean is True
    assert result.errors == ()
    assert len(result.worker_results) == 2
    for r in result.worker_results:
        assert r.adapter_close_errors == ()
        assert r.runner_stop.joined is True


@pytest.mark.anyio
async def test_pool_close_marks_degraded_when_an_adapter_close_times_out() -> None:
    good = make_fake_adapter("good", resource_id="sim:good")
    bad = make_hanging_close_adapter("bad")
    bad.resource_id = "sim:bad"
    cfg = WorkerShutdownConfig(
        adapter_close_grace_s=0.2,
        runner_stop_grace_s=1.0,
    )
    workers: dict[str, Worker] = {
        "sim:good": Worker(
            resource_id="sim:good",
            adapters=[good],
            runner=ThreadedRunner(name="worker-good"),
            shutdown_config=cfg,
        ),
        "sim:bad": Worker(
            resource_id="sim:bad",
            adapters=[bad],
            runner=ThreadedRunner(name="worker-bad"),
            shutdown_config=cfg,
        ),
    }
    pool = WorkerPool(
        workers=workers,
        device_to_resource={good.name: "sim:good", bad.name: "sim:bad"},
    )
    await pool.open()
    result = await pool.close()

    assert isinstance(result, PoolCloseResult)
    assert result.clean is False
    assert len(result.worker_results) == 2
    bad_result = next(r for r in result.worker_results if r.resource_id == "sim:bad")
    good_result = next(r for r in result.worker_results if r.resource_id == "sim:good")
    assert any("timeout" in e for e in bad_result.adapter_close_errors)
    # The good worker still closed cleanly.
    assert good_result.adapter_close_errors == ()
    assert good_result.runner_stop.joined is True


@pytest.mark.anyio
async def test_close_on_already_closed_pool_returns_empty_clean_result() -> None:
    adapter = make_fake_adapter("dev", resource_id="sim:dev")
    workers = {
        adapter.resource_id: Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=ThreadedRunner(name="w"),
        )
    }
    pool = WorkerPool(
        workers=workers,
        device_to_resource={adapter.name: adapter.resource_id},
    )
    # Already CLOSED.
    result = await pool.close()
    assert isinstance(result, PoolCloseResult)
    assert result.clean is True
    assert result.worker_results == ()
    assert result.errors == ()
