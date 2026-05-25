"""Pool open partial-failure rollback.

If any worker fails to start during :meth:`WorkerPool.open`, every
successfully-started worker must be closed
in reverse order before the original exception propagates and the pool
returns to :attr:`PoolState.CLOSED`.
"""

from __future__ import annotations

import pytest

from capa.runtime.lifecycle import PoolState
from capa.runtime.pool import WorkerPool
from capa.runtime.progress import DeviceInitProgress, DeviceInitStatus
from capa.runtime.runner import ThreadedRunner
from capa.runtime.worker import Worker
from tests.integration.runtime.fakes import (
    FakeAdapter,
    make_fake_adapter,
    make_open_failing_adapter,
)


def _pool_with_one_failure() -> tuple[WorkerPool, list[FakeAdapter]]:
    """One good adapter + one open-failing adapter, in separate workers
    (separate resources).

    Returns ``(pool, [good, bad])``."""
    good = make_fake_adapter("good", resource_id="sim:good")
    bad = make_open_failing_adapter("bad")
    bad.resource_id = "sim:bad"

    workers = {
        "sim:good": Worker(
            resource_id="sim:good",
            adapters=[good],
            runner=ThreadedRunner(name="worker-sim:good"),
        ),
        "sim:bad": Worker(
            resource_id="sim:bad",
            adapters=[bad],
            runner=ThreadedRunner(name="worker-sim:bad"),
        ),
    }
    device_to_resource = {"good": "sim:good", "bad": "sim:bad"}
    return WorkerPool(workers=workers, device_to_resource=device_to_resource), [good, bad]


class TestRollback:
    @pytest.mark.anyio
    async def test_open_partial_failure_raises(self) -> None:
        pool, _ = _pool_with_one_failure()
        with pytest.raises(RuntimeError, match="cannot open"):
            await pool.open()

    @pytest.mark.anyio
    async def test_open_partial_failure_closes_good_adapter(self) -> None:
        """The good adapter opened successfully — rollback must close it
        so its serial port / DAQmx handle / etc. is released."""
        pool, adapters = _pool_with_one_failure()
        good, bad = adapters
        with pytest.raises(RuntimeError):
            await pool.open()
        # Good was opened once and then closed during rollback.
        assert good.open_calls == 1
        assert good.close_calls == 1
        # Bad's open was attempted but failed — its close was never called
        # by the worker (the worker's start failed before it transitioned
        # to IDLE). The rollback closes only workers that DID reach IDLE.
        assert bad.open_calls == 1
        assert bad.close_calls == 0

    @pytest.mark.anyio
    async def test_open_partial_failure_returns_to_closed(self) -> None:
        pool, _ = _pool_with_one_failure()
        with pytest.raises(RuntimeError):
            await pool.open()
        assert pool.state is PoolState.CLOSED

    @pytest.mark.anyio
    async def test_open_partial_failure_emits_failed_and_rolled_back_progress(self) -> None:
        pool, _ = _pool_with_one_failure()
        events: list[DeviceInitProgress] = []

        with pytest.raises(RuntimeError):
            await pool.open(progress_callback=events.append)

        by_name: dict[str, list[DeviceInitStatus]] = {}
        for event in events:
            by_name.setdefault(event.name, []).append(event.status)

        assert DeviceInitStatus.FAILED in by_name["bad"]
        assert DeviceInitStatus.ROLLED_BACK in by_name["good"]

    @pytest.mark.anyio
    async def test_open_after_rollback_not_supported(self) -> None:
        """A pool that has been through a failed open can in principle be
        re-opened after the operator fixes the config. But since workers
        themselves can't be restarted (Worker.start requires CLOSED state
        — which they're in after the runner.stop), a fresh attempt with
        the same instances WILL try to re-start the runners.

        Document the observed behaviour: re-open against the same Worker
        instances is NOT a supported flow — a config-reload constructs
        new instances. We assert that pool.state returned to CLOSED and
        leave the operator-facing 'retry' to be a config-reload."""
        pool, _ = _pool_with_one_failure()
        with pytest.raises(RuntimeError):
            await pool.open()
        # Pool is back at CLOSED; the proper recovery is to discard this
        # pool and construct a new one from a fixed config.
        assert pool.state is PoolState.CLOSED


class TestNoLeakedThreadsOnRollback:
    """After a failed open + rollback, no worker-* thread persists."""

    @pytest.mark.anyio
    async def test_no_threads_remain(self) -> None:
        import threading

        before = {t.ident for t in threading.enumerate() if t.is_alive()}
        pool, _ = _pool_with_one_failure()
        with pytest.raises(RuntimeError):
            await pool.open()
        # Allow rollback's stop().add_done_callback chain a moment to finish.
        import asyncio as _asyncio

        await _asyncio.sleep(0.2)
        after = {t.ident for t in threading.enumerate() if t.is_alive()}
        # No new threads beyond what was there before the test.
        leaked = after - before
        # Tiny tolerance for noise from other tests' lingering helper
        # threads — only assert no thread named "worker-*" persists.
        worker_threads = [
            t
            for t in threading.enumerate()
            if t.is_alive() and t.ident in leaked and (t.name or "").startswith("worker-")
        ]
        assert worker_threads == []
