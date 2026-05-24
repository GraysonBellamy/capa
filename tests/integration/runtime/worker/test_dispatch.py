"""Dispatch state-gating for :class:`Worker`.

``dispatch()`` is permitted in IDLE, ARMED, SAMPLING; refused in DRAINING
and CLOSED. The state gate runs *on the worker loop*, not on the caller side
— these tests verify
both the happy paths and the refusal paths.

Cancellation-shield tests live in ``test_cancellation_shield.py``; here we
only verify routing and exception propagation, not the shield.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from capa.devices.adapter import CommandResult
from capa.runtime.errors import UnknownDeviceError, WorkerStateError
from capa.runtime.runner import InlineRunner, ThreadedRunner, WorkerRunner
from capa.runtime.worker import Worker
from tests.integration.runtime.fakes import (
    fake_command,
    make_fake_adapter,
    make_run_context,
)


@pytest.fixture(params=["inline", "threaded"])
def make_runner(request: pytest.FixtureRequest) -> Callable[[str], WorkerRunner]:
    kind = request.param

    def _factory(name: str) -> WorkerRunner:
        if kind == "inline":
            return InlineRunner(name=name)
        return ThreadedRunner(name=name)

    return _factory


async def _wait(fut: object) -> object:
    return await asyncio.wrap_future(fut)  # type: ignore[arg-type]


class TestDispatchAllowedStates:
    @pytest.mark.anyio
    async def test_dispatch_in_idle(self, make_runner: Callable[[str], WorkerRunner]) -> None:
        adapter = make_fake_adapter("a")
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=make_runner("disp-idle"),
        )
        await worker.async_start()
        try:
            result = await _wait(worker.dispatch("a", fake_command()))
            assert isinstance(result, CommandResult)
            assert result.accepted is True
            assert len(adapter.commands_completed) == 1
        finally:
            await worker.async_close(grace_s=1.0)

    @pytest.mark.anyio
    async def test_dispatch_in_armed(self, make_runner: Callable[[str], WorkerRunner]) -> None:
        adapter = make_fake_adapter("a")
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=make_runner("disp-armed"),
        )
        await worker.async_start()
        try:
            await worker.async_arm(make_run_context())
            result = await _wait(worker.dispatch("a", fake_command()))
            assert result.accepted is True
        finally:
            await worker.async_disarm(grace_s=1.0)
            await worker.async_close(grace_s=1.0)


class TestDispatchRefusedStates:
    @pytest.mark.anyio
    async def test_dispatch_refused_in_closed(
        self, make_runner: Callable[[str], WorkerRunner]
    ) -> None:
        """CLOSED means the worker thread isn't even running. The sync
        facade catches this synchronously via the runner state error
        rather than the worker state — either way the caller observes a
        failed future. We accept either error class here."""
        adapter = make_fake_adapter("a")
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=make_runner("disp-closed"),
        )
        # Worker constructed but not started — runner is also not started.
        with pytest.raises(Exception):
            await _wait(worker.dispatch("a", fake_command()))

    @pytest.mark.anyio
    async def test_dispatch_unknown_adapter_raises_synchronously(
        self, make_runner: Callable[[str], WorkerRunner]
    ) -> None:
        """Unknown name is a config error, not a state error — the worker
        knows its adapter map at construction time and can refuse without
        crossing the thread seam."""
        adapter = make_fake_adapter("a")
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=make_runner("disp-unknown"),
        )
        await worker.async_start()
        try:
            with pytest.raises(UnknownDeviceError):
                await _wait(worker.dispatch("nonexistent", fake_command()))
        finally:
            await worker.async_close(grace_s=1.0)

    @pytest.mark.anyio
    async def test_dispatch_refused_in_draining(
        self, make_runner: Callable[[str], WorkerRunner]
    ) -> None:
        """The DRAINING gate is on the worker loop (). To force
        the race deterministically we slow the disarm's adapter.stop()
        path and submit a dispatch concurrently.

        Easier deterministic alternative: induce DRAINING by submitting a
        disarm with a slow-stop adapter, then quickly submit a dispatch.
        We use an adapter whose ``stop()`` awaits a long delay to keep
        the worker in DRAINING for the duration of the test."""
        adapter = make_fake_adapter("a")

        async def _slow_stop() -> None:
            await asyncio.sleep(0.5)

        # Replace stop() with a slow version. Bind directly to instance.
        adapter.stop = _slow_stop  # type: ignore[method-assign]

        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=make_runner("disp-drain"),
        )
        await worker.async_start()
        try:
            await worker.async_arm(make_run_context())
            # Kick off disarm; it will hold the worker in DRAINING for ~500ms.
            disarm_task = asyncio.create_task(worker.async_disarm(grace_s=2.0))
            # Wait briefly for the disarm to actually enter DRAINING.
            await asyncio.sleep(0.1)
            from capa.runtime.lifecycle import WorkerState

            assert worker.state is WorkerState.DRAINING
            # Now dispatch must be refused.
            with pytest.raises(WorkerStateError, match="dispatch refused"):
                await _wait(worker.dispatch("a", fake_command()))
            # Wait for disarm to finish so close() doesn't see DRAINING.
            await disarm_task
        finally:
            await worker.async_close(grace_s=1.0)


class TestDispatchExceptionPropagation:
    @pytest.mark.anyio
    async def test_command_exception_surfaces_to_caller(
        self, make_runner: Callable[[str], WorkerRunner]
    ) -> None:
        adapter = make_fake_adapter("a")

        class CmdBoomError(RuntimeError):
            pass

        adapter.command_raises = CmdBoomError("device refused")

        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=make_runner("disp-exc"),
        )
        await worker.async_start()
        try:
            with pytest.raises(CmdBoomError, match="device refused"):
                await _wait(worker.dispatch("a", fake_command()))
            # Failure is recorded.
            assert worker.metrics.commands_failed == 1
            assert worker.metrics.commands_total == 1
            assert worker.metrics.commands_inflight == 0
        finally:
            await worker.async_close(grace_s=1.0)


class TestMultiAdapterWorker:
    """A worker can host multiple adapters that share a resource_id (e.g.
    two Watlows on one RS-485 bus). Dispatch must route by adapter name."""

    @pytest.mark.anyio
    async def test_two_adapters_same_resource(
        self, make_runner: Callable[[str], WorkerRunner]
    ) -> None:
        a = make_fake_adapter("heater_1", resource_id="serial:COMTEST")
        b = make_fake_adapter("heater_2", resource_id="serial:COMTEST")
        worker = Worker(
            resource_id="serial:COMTEST",
            adapters=[a, b],
            runner=make_runner("multi"),
        )
        await worker.async_start()
        try:
            await _wait(worker.dispatch("heater_1", fake_command()))
            await _wait(worker.dispatch("heater_2", fake_command()))
            await _wait(worker.dispatch("heater_1", fake_command()))
            assert len(a.commands_completed) == 2
            assert len(b.commands_completed) == 1
        finally:
            await worker.async_close(grace_s=1.0)


class TestSnapshot:
    @pytest.mark.anyio
    async def test_snapshot_returns_emission(
        self, make_runner: Callable[[str], WorkerRunner]
    ) -> None:
        adapter = make_fake_adapter("a")
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=make_runner("snapshot"),
        )
        await worker.async_start()
        try:
            from capa.devices.records import DeviceSnapshot

            snap = await _wait(worker.snapshot("a"))
            assert isinstance(snap, DeviceSnapshot)
        finally:
            await worker.async_close(grace_s=1.0)

    @pytest.mark.anyio
    async def test_snapshot_unknown_raises(
        self, make_runner: Callable[[str], WorkerRunner]
    ) -> None:
        adapter = make_fake_adapter("a")
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=make_runner("snapshot-unknown"),
        )
        await worker.async_start()
        try:
            with pytest.raises(UnknownDeviceError):
                await _wait(worker.snapshot("not-there"))
        finally:
            await worker.async_close(grace_s=1.0)
