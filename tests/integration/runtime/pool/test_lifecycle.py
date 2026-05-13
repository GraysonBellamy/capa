"""Pool open/close + arm/disarm tests.

Migration doc §4.3. Pool tests use prebuilt worker maps (via the
:class:`WorkerPool` constructor) rather than ``from_config`` so test
fakes inject directly without going through TOML.

Each test asserts both the pool's state and (where relevant) the
per-worker counters from :mod:`tests.integration.runtime.fakes` —
the multi-arm-disarm tests in particular need to verify ``open_calls``
stays at 1 across cycles, which is the load-bearing manual-control-
between-runs property (migration doc §10.2 acceptance criterion #6).
"""

from __future__ import annotations

import asyncio

import pytest

from capa.runtime.errors import PoolStateError, UnknownDeviceError
from capa.runtime.lifecycle import PoolState, WorkerState
from capa.runtime.pool import WorkerPool
from capa.runtime.runner import ThreadedRunner
from capa.runtime.worker import Worker
from tests.integration.runtime.fakes import (
    FakeAdapter,
    fake_command,
    make_fake_adapter,
    make_run_context,
)


def _build_pool(adapters: list[FakeAdapter]) -> WorkerPool:
    """Construct a pool with one Worker per adapter (one resource each)."""
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


def _build_shared_pool(adapters: list[FakeAdapter], resource_id: str) -> WorkerPool:
    """Construct a pool where all adapters share one resource (one worker)."""
    worker = Worker(
        resource_id=resource_id,
        adapters=adapters,
        runner=ThreadedRunner(name=f"worker-{resource_id}"),
    )
    workers = {resource_id: worker}
    device_to_resource = {a.name: resource_id for a in adapters}
    return WorkerPool(workers=workers, device_to_resource=device_to_resource)


class TestOpenClose:
    @pytest.mark.anyio
    async def test_open_brings_all_workers_to_idle(self) -> None:
        adapters = [make_fake_adapter(f"dev{i}", resource_id=f"sim:dev{i}") for i in range(3)]
        pool = _build_pool(adapters)
        assert pool.state is PoolState.CLOSED

        await pool.open()
        try:
            assert pool.state is PoolState.OPEN
            for worker in pool.workers.values():
                assert worker.state is WorkerState.IDLE
            for adapter in adapters:
                assert adapter.open_calls == 1
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_close_returns_to_closed(self) -> None:
        adapters = [make_fake_adapter(f"dev{i}", resource_id=f"sim:dev{i}") for i in range(2)]
        pool = _build_pool(adapters)
        await pool.open()
        await pool.close()
        assert pool.state is PoolState.CLOSED
        for adapter in adapters:
            assert adapter.close_calls == 1

    @pytest.mark.anyio
    async def test_close_idempotent_on_already_closed(self) -> None:
        adapters = [make_fake_adapter("a", resource_id="sim:a")]
        pool = _build_pool(adapters)
        await pool.close()  # CLOSED → CLOSED, no error
        assert pool.state is PoolState.CLOSED

    @pytest.mark.anyio
    async def test_close_refused_when_worker_armed(self) -> None:
        adapters = [make_fake_adapter("a", resource_id="sim:a")]
        pool = _build_pool(adapters)
        await pool.open()
        try:
            await pool.arm_all(make_run_context())
            # Pool's close() must refuse — worker is ARMED, not IDLE.
            with pytest.raises(PoolStateError, match="not IDLE"):
                await pool.close()
        finally:
            await pool.disarm_all(grace_s=1.0)
            await pool.close()


class TestWorkerLookup:
    @pytest.mark.anyio
    async def test_worker_for_routes_by_name(self) -> None:
        a = make_fake_adapter("dev_a", resource_id="sim:a")
        b = make_fake_adapter("dev_b", resource_id="sim:b")
        pool = _build_pool([a, b])
        await pool.open()
        try:
            wa = pool.worker_for("dev_a")
            wb = pool.worker_for("dev_b")
            assert wa.resource_id == "sim:a"
            assert wb.resource_id == "sim:b"
            assert wa is not wb
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_worker_for_unknown_raises(self) -> None:
        pool = _build_pool([make_fake_adapter("a", resource_id="sim:a")])
        await pool.open()
        try:
            with pytest.raises(UnknownDeviceError):
                pool.worker_for("not_configured")
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_shared_resource_one_worker(self) -> None:
        """Two adapters sharing a resource_id share a worker."""
        a = make_fake_adapter("heater_1", resource_id="serial:COM6")
        b = make_fake_adapter("heater_2", resource_id="serial:COM6")
        pool = _build_shared_pool([a, b], resource_id="serial:COM6")
        await pool.open()
        try:
            assert pool.worker_for("heater_1") is pool.worker_for("heater_2")
            assert len(pool.workers) == 1
        finally:
            await pool.close()


class TestDispatchRouting:
    @pytest.mark.anyio
    async def test_dispatch_routes_to_correct_worker(self) -> None:
        a = make_fake_adapter("dev_a", resource_id="sim:a")
        b = make_fake_adapter("dev_b", resource_id="sim:b")
        pool = _build_pool([a, b])
        await pool.open()
        try:
            await asyncio.wrap_future(pool.dispatch("dev_a", fake_command()))
            await asyncio.wrap_future(pool.dispatch("dev_b", fake_command()))
            await asyncio.wrap_future(pool.dispatch("dev_a", fake_command()))
            assert len(a.commands_completed) == 2
            assert len(b.commands_completed) == 1
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_dispatch_unknown_device(self) -> None:
        pool = _build_pool([make_fake_adapter("a", resource_id="sim:a")])
        await pool.open()
        try:
            with pytest.raises(UnknownDeviceError):
                pool.dispatch("nope", fake_command())
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_snapshot_routes_via_pool(self) -> None:
        from capa.devices.records import DeviceSnapshot

        a = make_fake_adapter("dev_a", resource_id="sim:a")
        pool = _build_pool([a])
        await pool.open()
        try:
            snap = await asyncio.wrap_future(pool.snapshot("dev_a"))
            assert isinstance(snap, DeviceSnapshot)
        finally:
            await pool.close()


class TestRunLifecycle:
    """Migration doc §3.7 / §3.8: arm_all, begin_sampling_all, disarm_all."""

    @pytest.mark.anyio
    async def test_arm_all_transitions_every_worker(self) -> None:
        adapters = [make_fake_adapter(f"d{i}", resource_id=f"sim:d{i}") for i in range(3)]
        pool = _build_pool(adapters)
        await pool.open()
        try:
            await pool.arm_all(make_run_context())
            for worker in pool.workers.values():
                assert worker.state is WorkerState.ARMED
        finally:
            await pool.disarm_all(grace_s=1.0)
            await pool.close()

    @pytest.mark.anyio
    async def test_arm_all_refused_when_pool_closed(self) -> None:
        pool = _build_pool([make_fake_adapter("a", resource_id="sim:a")])
        with pytest.raises(PoolStateError, match="requires OPEN"):
            await pool.arm_all(make_run_context())

    @pytest.mark.anyio
    async def test_begin_sampling_all_returns_bridges(self) -> None:
        adapters = [
            make_fake_adapter(f"d{i}", resource_id=f"sim:d{i}", tick_period_s=0.01, emit_limit=2)
            for i in range(2)
        ]
        pool = _build_pool(adapters)
        await pool.open()
        try:
            await pool.arm_all(make_run_context())
            bridges = await pool.begin_sampling_all(consumer_loop=asyncio.get_running_loop())
            try:
                assert set(bridges) == {"sim:d0", "sim:d1"}
                for worker in pool.workers.values():
                    assert worker.state is WorkerState.SAMPLING
                # Each bridge yields its adapter's 2 emissions.
                for bridge in bridges.values():
                    em1 = await bridge.get()
                    em2 = await bridge.get()
                    assert em1 is not None and em2 is not None
            finally:
                await pool.disarm_all(grace_s=2.0)
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_disarm_all_returns_result_per_worker(self) -> None:
        from capa.runtime.metrics import DisarmResult

        adapters = [
            make_fake_adapter(f"d{i}", resource_id=f"sim:d{i}", tick_period_s=0.01, emit_limit=1)
            for i in range(3)
        ]
        pool = _build_pool(adapters)
        await pool.open()
        try:
            await pool.arm_all(make_run_context())
            bridges = await pool.begin_sampling_all(consumer_loop=asyncio.get_running_loop())
            for b in bridges.values():
                await b.get()  # drain one
            results = await pool.disarm_all(grace_s=2.0)
            assert set(results) == {"sim:d0", "sim:d1", "sim:d2"}
            for r in results.values():
                assert r is DisarmResult.OK
            for worker in pool.workers.values():
                assert worker.state is WorkerState.IDLE
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_disarm_all_skips_idle_workers(self) -> None:
        """If a worker never armed, disarm_all skips it rather than erroring."""
        pool = _build_pool([make_fake_adapter("a", resource_id="sim:a")])
        await pool.open()
        try:
            results = await pool.disarm_all(grace_s=1.0)
            assert results == {}
        finally:
            await pool.close()


class TestEmptyMap:
    def test_empty_pool_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            WorkerPool(workers={}, device_to_resource={})
