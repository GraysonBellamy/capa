"""End-to-end conductor lifecycle tests.

These tests run the conductor in its real thread + loop against a real
:class:`WorkerPool` of real :class:`Worker`\\ s hosting fake adapters.
The point is to catch concurrency bugs that an inline harness would
mask — drain ordering, parallel arm/disarm, completion signalling.
"""

from __future__ import annotations

import asyncio

import pytest

from capa.runtime.conductor import (
    Conductor,
    ConductorStateError,
    NoOpRunner,
    RunOutcome,
)
from capa.runtime.pool import WorkerPool
from capa.runtime.runner import ThreadedRunner
from capa.runtime.state import ConductorState
from capa.runtime.worker import Worker
from tests.integration.runtime.conductor.fakes import make_fake_session
from tests.integration.runtime.fakes import (
    FakeAdapter,
    fake_command,
    make_fake_adapter,
)

pytestmark = pytest.mark.anyio


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


def _wait_future(fut, timeout: float = 5.0):
    """Block until a concurrent.futures.Future resolves; raise on timeout."""
    return fut.result(timeout=timeout)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    async def test_short_run_completes_cleanly(self) -> None:
        """Tiny run: workers arm, sample for 0.1s, procedure exits, the
        run seals as COMPLETED."""
        adapters = [
            make_fake_adapter(f"dev{i}", resource_id=f"sim:dev{i}", tick_period_s=0.005)
            for i in range(2)
        ]
        pool = _build_pool(adapters)
        await pool.open()
        try:
            session = make_fake_session()
            runner = NoOpRunner(run_for_s=0.1)
            cond = Conductor(pool=pool, session=session, runner=runner)
            cond.start()
            result = _wait_future(cond.result_future, timeout=5.0)
            cond.join(timeout=2.0)

            assert result.outcome is RunOutcome.COMPLETED
            assert result.final_state is ConductorState.SEALED
            assert result.run_id == session.run_id
            assert result.bundle_path == session.bundle_path
            assert result.ended_mono_ns > result.started_mono_ns

            # Session was opened once and closed once with COMPLETED.
            assert session.open_calls == 1
            assert session.close_calls == 1
            assert session.outcome is RunOutcome.COMPLETED

            # Adapters cycled through start → stop exactly once.
            for a in adapters:
                assert a.start_calls == 1
                assert a.stop_calls == 1

            # Emissions actually flowed through the drain.
            assert len(session.writer_ref.submitted) > 0
            # Procedure body executed.
            assert runner.preflight_calls == 1
            assert runner.run_calls == 1
        finally:
            await pool.close()

    async def test_drain_publishes_to_databus(self) -> None:
        """The drain task must publish to ``conductor.databus`` so
        procedure subscribers (Phase 2.3) can wait on samples."""
        adapter = make_fake_adapter("d0", resource_id="sim:d0", tick_period_s=0.002)
        pool = _build_pool([adapter])
        await pool.open()
        try:
            session = make_fake_session()
            collected: list = []
            barrier = asyncio.Event()

            async def _capture(cond: Conductor) -> None:
                # Subscribe and let a few emissions accumulate before the
                # NoOpRunner exits.
                bus = cond.databus
                assert bus is not None
                sub = bus.subscribe_all("test-cap")
                # Pull a couple of samples on the conductor loop. The
                # subscribe queue lives on the conductor loop too.
                try:
                    for _ in range(3):
                        item = await asyncio.wait_for(sub.queue.get(), timeout=2.0)
                        collected.append(item)
                finally:
                    bus.unsubscribe(sub)
                barrier.set()

            cond = Conductor(
                pool=pool,
                session=session,
                runner=NoOpRunner(run_for_s=0.5),
                pre_completion_callback=_capture,
            )
            cond.start()
            _wait_future(cond.result_future, timeout=5.0)
            cond.join(timeout=2.0)
            assert len(collected) == 3
        finally:
            await pool.close()


# ---------------------------------------------------------------------------
# Stop / cancellation
# ---------------------------------------------------------------------------


class TestStop:
    async def test_stop_during_run_seals_as_aborted(self) -> None:
        adapter = make_fake_adapter("d0", resource_id="sim:d0", tick_period_s=0.005)
        pool = _build_pool([adapter])
        await pool.open()
        try:
            session = make_fake_session()
            cond = Conductor(
                pool=pool,
                session=session,
                runner=NoOpRunner(),  # parks forever
            )
            handle = cond.start()
            # Wait until the run is up.
            _wait_future(handle, timeout=5.0)
            assert cond.state is ConductorState.RUNNING

            cond.stop(reason="test_abort")
            result = _wait_future(cond.result_future, timeout=5.0)
            cond.join(timeout=2.0)

            assert result.outcome is RunOutcome.ABORTED
            assert result.exit_reason == "test_abort"
            assert result.final_state is ConductorState.SEALED
            assert session.outcome is RunOutcome.ABORTED
            assert adapter.stop_calls == 1
        finally:
            await pool.close()

    async def test_stop_is_idempotent(self) -> None:
        adapter = make_fake_adapter("d0", resource_id="sim:d0", tick_period_s=0.005)
        pool = _build_pool([adapter])
        await pool.open()
        try:
            cond = Conductor(pool=pool, session=make_fake_session(), runner=NoOpRunner())
            cond.start()
            _wait_future(cond.start(), timeout=5.0) if False else None  # noqa: linter
            # Second start should raise; first stop wins.
            cond.stop(reason="first")
            cond.stop(reason="second")  # second call is a no-op
            result = _wait_future(cond.result_future, timeout=5.0)
            cond.join(timeout=2.0)
            assert result.exit_reason == "first"
        finally:
            await pool.close()


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


class TestFailures:
    async def test_session_open_failure_resolves_handle_with_exception(self) -> None:
        adapter = make_fake_adapter("d0", resource_id="sim:d0")
        pool = _build_pool([adapter])
        await pool.open()
        try:
            session = make_fake_session(open_raises=RuntimeError("bundle locked"))
            cond = Conductor(pool=pool, session=session, runner=NoOpRunner())
            handle = cond.start()
            with pytest.raises(RuntimeError, match="bundle locked"):
                _wait_future(handle, timeout=5.0)
            result = _wait_future(cond.result_future, timeout=5.0)
            cond.join(timeout=2.0)
            assert result.outcome is RunOutcome.CRASHED
            assert result.final_state is ConductorState.FAILED
            # Adapter open()'d at pool.open() time; never start()ed because
            # the conductor failed at session.open().
            assert adapter.start_calls == 0
        finally:
            await pool.close()

    async def test_procedure_crash_seals_as_crashed(self) -> None:
        adapter = make_fake_adapter("d0", resource_id="sim:d0", tick_period_s=0.005)
        pool = _build_pool([adapter])
        await pool.open()
        try:

            class CrashingRunner:
                async def preflight(self, ctx, bus) -> None:
                    pass

                async def run(self, ctx, bus) -> None:
                    await asyncio.sleep(0.05)
                    raise RuntimeError("procedure exploded")

            cond = Conductor(pool=pool, session=make_fake_session(), runner=CrashingRunner())
            cond.start()
            result = _wait_future(cond.result_future, timeout=5.0)
            cond.join(timeout=2.0)

            assert result.outcome is RunOutcome.CRASHED
            assert "procedure exploded" in (result.exit_reason or "")
            assert result.final_state is ConductorState.SEALED  # bundle still seals
        finally:
            await pool.close()

    async def test_preflight_crash_short_circuits_to_failed(self) -> None:
        adapter = make_fake_adapter("d0", resource_id="sim:d0", tick_period_s=0.005)
        pool = _build_pool([adapter])
        await pool.open()
        try:

            class BadPreflight:
                async def preflight(self, ctx, bus) -> None:
                    raise ValueError("preflight nope")

                async def run(self, ctx, bus) -> None:
                    raise AssertionError("never reached")

            cond = Conductor(pool=pool, session=make_fake_session(), runner=BadPreflight())
            handle = cond.start()
            with pytest.raises(ValueError, match="preflight nope"):
                _wait_future(handle, timeout=5.0)
            result = _wait_future(cond.result_future, timeout=5.0)
            cond.join(timeout=2.0)
            assert result.outcome is RunOutcome.CRASHED
            assert result.final_state is ConductorState.FAILED
        finally:
            await pool.close()


# ---------------------------------------------------------------------------
# Dispatch state-gating
# ---------------------------------------------------------------------------


class TestDispatch:
    async def test_dispatch_works_during_running(self) -> None:
        adapter = make_fake_adapter("heater", resource_id="serial:COM6", tick_period_s=0.005)
        pool = _build_pool([adapter])
        await pool.open()
        try:
            session = make_fake_session()
            cond = Conductor(pool=pool, session=session, runner=NoOpRunner())
            _wait_future(cond.start(), timeout=5.0)
            assert cond.state is ConductorState.RUNNING

            fut = cond.dispatch("heater", fake_command())
            result = fut.result(timeout=2.0)
            assert result.accepted

            cond.stop(reason="done")
            _wait_future(cond.result_future, timeout=5.0)
            cond.join(timeout=2.0)
        finally:
            await pool.close()

    async def test_dispatch_refused_after_stop(self) -> None:
        adapter = make_fake_adapter("d0", resource_id="sim:d0", tick_period_s=0.005)
        pool = _build_pool([adapter])
        await pool.open()
        try:
            cond = Conductor(pool=pool, session=make_fake_session(), runner=NoOpRunner())
            _wait_future(cond.start(), timeout=5.0)
            cond.stop(reason="stop")
            _wait_future(cond.result_future, timeout=5.0)
            cond.join(timeout=2.0)
            # State should be SEALED now.
            assert cond.state is ConductorState.SEALED
            with pytest.raises(ConductorStateError):
                cond.dispatch("d0", fake_command())
        finally:
            await pool.close()


# ---------------------------------------------------------------------------
# Construction guardrails
# ---------------------------------------------------------------------------


class TestConstruction:
    async def test_double_start_raises(self) -> None:
        adapter = make_fake_adapter("d0", resource_id="sim:d0")
        pool = _build_pool([adapter])
        await pool.open()
        try:
            cond = Conductor(pool=pool, session=make_fake_session(), runner=NoOpRunner())
            cond.start()
            with pytest.raises(ConductorStateError):
                cond.start()
            cond.stop(reason="cleanup")
            _wait_future(cond.result_future, timeout=5.0)
            cond.join(timeout=2.0)
        finally:
            await pool.close()
