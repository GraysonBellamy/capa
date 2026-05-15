"""Integration tests for :class:`Worker` state-machine transitions.

Cross-runner: every test runs under both :class:`InlineRunner` and
:class:`ThreadedRunner` so semantic drift between the two backends is
caught (plan risk register §7).

These are *integration* tests because they exercise the runner thread/loop
machinery end-to-end, not unit tests of the state table — that's
``tests/unit/runtime/test_lifecycle.py``.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from capa.runtime.errors import WorkerStateError
from capa.runtime.lifecycle import WorkerState
from capa.runtime.metrics import DisarmResult
from capa.runtime.runner import InlineRunner, ThreadedRunner, WorkerRunner
from capa.runtime.worker import Worker
from tests.integration.runtime.fakes import (
    make_fake_adapter,
    make_open_failing_adapter,
    make_run_context,
)


@pytest.fixture(params=["inline", "threaded"])
def make_runner(request: pytest.FixtureRequest) -> Callable[[str], WorkerRunner]:
    kind = request.param

    def _factory(name: str) -> WorkerRunner:
        if kind == "inline":
            return InlineRunner(name=name)
        return ThreadedRunner(name=name)

    _factory.kind = kind  # type: ignore[attr-defined]
    return _factory


# ---------------------------------------------------------------------------
# CLOSED → IDLE → CLOSED
# ---------------------------------------------------------------------------


class TestOpenClose:
    """The CLOSED↔IDLE pair — start() and close()."""

    @pytest.mark.anyio
    async def test_start_opens_adapter_and_reaches_idle(
        self, make_runner: Callable[[str], WorkerRunner]
    ) -> None:
        adapter = make_fake_adapter("a")
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=make_runner("worker-open"),
        )
        assert worker.state is WorkerState.CLOSED
        await worker.async_start()
        try:
            assert worker.state is WorkerState.IDLE
            assert adapter.open_calls == 1
            assert adapter.close_calls == 0
        finally:
            await worker.async_close(grace_s=1.0)

    @pytest.mark.anyio
    async def test_close_calls_adapter_close_and_reaches_closed(
        self, make_runner: Callable[[str], WorkerRunner]
    ) -> None:
        adapter = make_fake_adapter("a")
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=make_runner("worker-close"),
        )
        await worker.async_start()
        await worker.async_close(grace_s=1.0)
        assert worker.state is WorkerState.CLOSED
        assert adapter.close_calls == 1

    @pytest.mark.anyio
    async def test_start_rolls_back_on_open_failure(
        self, make_runner: Callable[[str], WorkerRunner]
    ) -> None:
        """First adapter opens fine; second raises; the rollback closes
        the first one in reverse order."""
        good = make_fake_adapter("good")
        bad = make_open_failing_adapter("bad")
        worker = Worker(
            resource_id="serial:shared",
            adapters=[good, bad],
            runner=make_runner("worker-rollback"),
        )
        with pytest.raises(RuntimeError, match="cannot open"):
            await worker.async_start()
        # Good adapter was rolled back; bad never reached "open" lifecycle.
        assert good.open_calls == 1
        assert good.close_calls == 1
        assert bad.open_calls == 1
        assert bad.close_calls == 0
        assert worker.state is WorkerState.CLOSED
        # The runner was stopped automatically as part of start()'s failure
        # path — no leaked thread to clean up.

    @pytest.mark.anyio
    async def test_close_refused_outside_idle(
        self, make_runner: Callable[[str], WorkerRunner]
    ) -> None:
        adapter = make_fake_adapter("a")
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=make_runner("worker-close-bad"),
        )
        # CLOSED: close is refused
        with pytest.raises(WorkerStateError, match="requires IDLE"):
            await worker.async_close()
        # IDLE → ARMED: close is also refused
        await worker.async_start()
        try:
            ctx = make_run_context()
            await worker.async_arm(ctx)
            with pytest.raises(WorkerStateError, match="requires IDLE"):
                await worker.async_close()
        finally:
            await worker.async_disarm(grace_s=1.0)
            await worker.async_close(grace_s=1.0)

    @pytest.mark.anyio
    async def test_start_refused_when_already_running(
        self, make_runner: Callable[[str], WorkerRunner]
    ) -> None:
        adapter = make_fake_adapter("a")
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=make_runner("worker-restart"),
        )
        await worker.async_start()
        try:
            with pytest.raises(WorkerStateError, match="requires CLOSED"):
                await worker.async_start()
        finally:
            await worker.async_close(grace_s=1.0)


# ---------------------------------------------------------------------------
# IDLE → ARMED → DRAINING → IDLE (no streams path)
# ---------------------------------------------------------------------------


class TestArmDisarmWithoutSampling:
    """The ARMED → DRAINING edge that bypasses SAMPLING (disarm called
    before begin_sampling). Migration doc §3.3."""

    @pytest.mark.anyio
    async def test_arm_installs_run_context(
        self, make_runner: Callable[[str], WorkerRunner]
    ) -> None:
        adapter = make_fake_adapter("a")
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=make_runner("worker-arm"),
        )
        await worker.async_start()
        try:
            ctx = make_run_context(run_id="run-XYZ")
            await worker.async_arm(ctx)
            assert worker.state is WorkerState.ARMED
            assert worker._run_context is ctx
        finally:
            await worker.async_disarm(grace_s=1.0)
            await worker.async_close(grace_s=1.0)

    @pytest.mark.anyio
    async def test_disarm_from_armed_returns_ok(
        self, make_runner: Callable[[str], WorkerRunner]
    ) -> None:
        adapter = make_fake_adapter("a")
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=make_runner("worker-disarm-armed"),
        )
        await worker.async_start()
        try:
            await worker.async_arm(make_run_context())
            result = await worker.async_disarm(grace_s=1.0)
            assert result is DisarmResult.OK
            assert worker.state is WorkerState.IDLE
            assert worker._run_context is None
            # adapter.stop is still called even from ARMED — the doc's
            # cleanup path is uniform.
            assert adapter.stop_calls == 1
            # adapter.start was NOT called (we never reached SAMPLING).
            assert adapter.start_calls == 0
        finally:
            await worker.async_close(grace_s=1.0)

    @pytest.mark.anyio
    async def test_arm_refused_outside_idle(
        self, make_runner: Callable[[str], WorkerRunner]
    ) -> None:
        adapter = make_fake_adapter("a")
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=make_runner("worker-arm-bad"),
        )
        with pytest.raises(WorkerStateError, match="requires IDLE"):
            await worker.async_arm(make_run_context())
        await worker.async_start()
        try:
            await worker.async_arm(make_run_context())
            # ARMED: arm() again is refused
            with pytest.raises(WorkerStateError, match="requires IDLE"):
                await worker.async_arm(make_run_context())
        finally:
            await worker.async_disarm(grace_s=1.0)
            await worker.async_close(grace_s=1.0)

    @pytest.mark.anyio
    async def test_disarm_refused_in_idle(self, make_runner: Callable[[str], WorkerRunner]) -> None:
        adapter = make_fake_adapter("a")
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=make_runner("worker-disarm-idle"),
        )
        await worker.async_start()
        try:
            with pytest.raises(WorkerStateError, match="requires ARMED or SAMPLING"):
                await worker.async_disarm(grace_s=1.0)
        finally:
            await worker.async_close(grace_s=1.0)


# ---------------------------------------------------------------------------
# Multiple arm/disarm cycles on one worker — the load-bearing manual-control-
# between-runs property.
# ---------------------------------------------------------------------------


class TestMultipleRuns:
    """Migration doc §10.2 'test_pool_supports_multiple_arm_disarm_cycles_
    without_reopen' applies one level down: a single worker must support
    many arm/disarm cycles without re-opening its adapter."""

    @pytest.mark.anyio
    async def test_three_arm_disarm_cycles(
        self, make_runner: Callable[[str], WorkerRunner]
    ) -> None:
        adapter = make_fake_adapter("a")
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=make_runner("worker-cycles"),
        )
        await worker.async_start()
        try:
            for _i in range(3):
                await worker.async_arm(make_run_context())
                assert worker.state is WorkerState.ARMED
                result = await worker.async_disarm(grace_s=1.0)
                assert result is DisarmResult.OK
                assert worker.state is WorkerState.IDLE
        finally:
            await worker.async_close(grace_s=1.0)
        # adapter.open called exactly once — the entire point of pool-level
        # connection caching.
        assert adapter.open_calls == 1
        assert adapter.close_calls == 1


# ---------------------------------------------------------------------------
# State property is updated transactionally.
# ---------------------------------------------------------------------------


class TestStateInvariant:
    @pytest.mark.anyio
    async def test_state_progression(self, make_runner: Callable[[str], WorkerRunner]) -> None:
        adapter = make_fake_adapter("a")
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=make_runner("worker-state-progression"),
        )
        observed: list[WorkerState] = [worker.state]
        await worker.async_start()
        try:
            observed.append(worker.state)
            await worker.async_arm(make_run_context())
            observed.append(worker.state)
            await worker.async_disarm(grace_s=1.0)
            observed.append(worker.state)
        finally:
            await worker.async_close(grace_s=1.0)
            observed.append(worker.state)
        assert observed == [
            WorkerState.CLOSED,
            WorkerState.IDLE,
            WorkerState.ARMED,
            WorkerState.IDLE,
            WorkerState.CLOSED,
        ]
