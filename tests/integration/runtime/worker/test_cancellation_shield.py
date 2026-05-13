"""Cancellation-shield tests — the load-bearing §4.2 rule.

Migration doc §4.2 lines 685-719: when a caller cancels a ``dispatch``
future mid-flight, the worker's ``adapter.command()`` MUST run to
completion regardless. The original Watlow ReadResponse incident (§1.1) was
caused by a cancellation propagating into a serial transaction; the worker's
``asyncio.shield(...)`` around ``adapter.command()`` is what prevents that.

These tests cover:

1. **Mechanism test (FakeAdapter)** — the canonical, fully-observable
   scenario: a slow command, caller cancellation, assertion that the
   worker-side coroutine completed despite the cancel.

2. **Per-sim tests (one per adapter family)** — confirms the worker
   integrates the shield correctly against each real sim adapter type
   (the migration doc §10.2 list: Watlow, Alicat, Sartorius, NI-DAQ).

The doc's intent is one test per *real* adapter (§10.2 names them by
production name). Phase 1 stages the equivalent against the sim adapters;
Phase 5 re-runs the same tests against real hardware (plan §1 boundary).
"""

from __future__ import annotations

import asyncio

import pytest

from capa.devices.adapter import CommandResult, DeviceCommand
from capa.devices.sim.alicat_sim import AlicatSim
from capa.devices.sim.nidaq_polled_sim import NIDAQPolledSim
from capa.devices.sim.sartorius_sim import SartoriusSim
from capa.devices.sim.watlow_sim import WatlowSim
from capa.runtime.runner import ThreadedRunner
from capa.runtime.worker import Worker
from tests.integration.runtime.fakes import (
    fake_command,
    make_fake_adapter,
)


async def _wait(fut: object) -> object:
    return await asyncio.wrap_future(fut)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Mechanism test — FakeAdapter, fully controllable.
# ---------------------------------------------------------------------------


class TestShieldMechanism:
    """The §4.2 rule, observed end-to-end on a controllable adapter."""

    @pytest.mark.anyio
    async def test_caller_cancel_does_not_interrupt_adapter_command(self) -> None:
        """The canonical scenario:

        1. Caller submits dispatch with a slow command (200ms).
        2. Caller cancels the wrap_future shortly after submission.
        3. The caller observes ``CancelledError`` from its await.
        4. After waiting > command_delay_s, the adapter's commands_completed
           list contains the command — proving the worker-side coroutine
           ran to completion despite the cancellation.

        This is the exact mechanism that prevents the Watlow ReadResponse
        regression (§1.1)."""
        adapter = make_fake_adapter("a", command_delay_s=0.2)
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=ThreadedRunner(name="shield-cancel"),
        )
        await _wait(worker.start())
        try:
            cmd = fake_command(kind="slow_set")
            dispatch_fut = worker.dispatch("a", cmd)
            # Bridge into asyncio. Give the worker time to begin the
            # adapter.command() coroutine; the shield only matters once
            # the work has started.
            await asyncio.sleep(0.05)
            wrapped = asyncio.wrap_future(dispatch_fut)
            wrapped.cancel()
            with pytest.raises(asyncio.CancelledError):
                await wrapped

            # The worker-side coroutine is still running. Wait past the
            # adapter's delay then assert completion.
            await asyncio.sleep(0.3)
            assert len(adapter.commands_completed) == 1
            assert adapter.commands_completed[0].kind == "slow_set"
            # Metrics agree: the worker counted one full command lifecycle.
            assert worker.metrics.commands_total == 1
            assert worker.metrics.commands_failed == 0
        finally:
            await _wait(worker.close(grace_s=1.0))

    @pytest.mark.anyio
    async def test_subsequent_dispatch_reads_clean_state(self) -> None:
        """After a cancelled-but-completed command, the next dispatch must
        return its own clean result. On real hardware this is where the
        Watlow stale-bytes bug surfaced (§1.1)."""
        adapter = make_fake_adapter("a", command_delay_s=0.1)
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=ThreadedRunner(name="shield-clean"),
        )
        await _wait(worker.start())
        try:
            # First command: caller cancels.
            cmd1 = fake_command(kind="first")
            fut1 = worker.dispatch("a", cmd1)
            await asyncio.sleep(0.02)
            asyncio.wrap_future(fut1).cancel()
            await asyncio.sleep(0.2)  # let the worker finish the first

            # Second command: full success, distinct result.
            cmd2 = fake_command(kind="second")
            result2 = await _wait(worker.dispatch("a", cmd2))
            assert isinstance(result2, CommandResult)
            assert result2.accepted is True
            assert result2.detail == "fake ack second"

            # Both commands reached the adapter, in order.
            assert [c.kind for c in adapter.commands_completed] == [
                "first",
                "second",
            ]
        finally:
            await _wait(worker.close(grace_s=1.0))

    @pytest.mark.anyio
    async def test_metrics_record_completion_not_cancellation(self) -> None:
        """Per the §4.2 rule: the caller cancelled, but the *worker*
        observes completion. ``commands_total`` increments; ``commands_failed``
        stays at 0 because the adapter returned a CommandResult normally."""
        adapter = make_fake_adapter("a", command_delay_s=0.1)
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=ThreadedRunner(name="shield-metrics"),
        )
        await _wait(worker.start())
        try:
            fut = worker.dispatch("a", fake_command())
            await asyncio.sleep(0.02)
            asyncio.wrap_future(fut).cancel()
            await asyncio.sleep(0.2)
            assert worker.metrics.commands_total == 1
            assert worker.metrics.commands_failed == 0
            assert worker.metrics.commands_inflight == 0
        finally:
            await _wait(worker.close(grace_s=1.0))


# ---------------------------------------------------------------------------
# Per-sim tests — migration doc §10.2 list.
# ---------------------------------------------------------------------------


class _SlowCommandProxy:
    """Adapter proxy that delegates everything to an inner adapter and
    inserts ``delay_s`` of sleep before each :meth:`command` call.

    We can't monkey-patch the sims directly — they're ``@dataclass(slots=True)``
    so attribute assignment is rejected. A small proxy is the lightest
    workaround that doesn't require subclassing each sim."""

    def __init__(self, inner: object, *, delay_s: float) -> None:
        self._inner = inner
        self._delay_s = delay_s

    # Static surface (delegated via __getattr__ below would work but
    # explicit attributes keep MyPy / DeviceAdapter Protocol checks honest).

    @property
    def name(self) -> str:
        return self._inner.name  # type: ignore[attr-defined,no-any-return]

    @property
    def capabilities(self) -> object:
        return self._inner.capabilities  # type: ignore[attr-defined]

    @property
    def resource_id(self) -> str:
        return self._inner.resource_id  # type: ignore[attr-defined,no-any-return]

    @property
    def expected_emission_rate_hz(self) -> float | None:
        return getattr(self._inner, "expected_emission_rate_hz", None)

    async def open(self) -> None:
        await self._inner.open()  # type: ignore[attr-defined]

    async def close(self) -> None:
        await self._inner.close()  # type: ignore[attr-defined]

    async def start(self, clock: object | None = None) -> None:
        # Sims accept clock; pass through.
        await self._inner.start(clock)  # type: ignore[attr-defined]

    async def stop(self) -> None:
        await self._inner.stop()  # type: ignore[attr-defined]

    async def snapshot(self) -> object:
        return await self._inner.snapshot()  # type: ignore[attr-defined,no-any-return]

    def stream(self) -> object:
        return self._inner.stream()  # type: ignore[attr-defined,no-any-return]

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        await asyncio.sleep(self._delay_s)
        return await self._inner.command(cmd)  # type: ignore[attr-defined,no-any-return]


async def _run_shield_against_sim(
    sim_adapter: object,
    *,
    auth_cmd: DeviceCommand,
    sim_name: str,
) -> None:
    """Common shield-test body: wrap the sim in a slow-command proxy,
    dispatch+cancel, then dispatch a second command and assert it returns
    a clean accepted result."""
    proxy = _SlowCommandProxy(sim_adapter, delay_s=0.15)

    worker = Worker(
        resource_id=sim_adapter.resource_id,
        adapters=[proxy],  # type: ignore[list-item]
        runner=ThreadedRunner(name=f"shield-{sim_name}"),
    )
    await _wait(worker.start())
    try:
        # First: caller cancels mid-flight.
        fut1 = worker.dispatch(sim_name, auth_cmd)
        await asyncio.sleep(0.02)
        asyncio.wrap_future(fut1).cancel()
        # Give the worker enough time to complete the wrapped command.
        await asyncio.sleep(0.3)
        # First call completed inside the worker; metric proves it.
        assert worker.metrics.commands_total == 1

        # Second: clean dispatch returns a normal result.
        result = await _wait(worker.dispatch(sim_name, auth_cmd))
        assert isinstance(result, CommandResult)
        assert result.accepted is True
        assert worker.metrics.commands_total == 2
    finally:
        await _wait(worker.close(grace_s=1.0))


class TestPerSimShield:
    """One test per sim adapter family, mirroring migration doc §10.2."""

    @pytest.mark.anyio
    async def test_watlow_sim_dispatch_cancel_does_not_corrupt_next_call(
        self,
    ) -> None:
        sim = WatlowSim(name="heater", address=1)
        cmd = fake_command(
            kind="set_setpoint",
            target="setpoint:1",
            payload={"value": 600.0, "instance": 1},
        )
        await _run_shield_against_sim(sim, auth_cmd=cmd, sim_name="heater")

    @pytest.mark.anyio
    async def test_alicat_sim_dispatch_cancel_does_not_corrupt_next_call(
        self,
    ) -> None:
        sim = AlicatSim(name="purge_mfc")
        cmd = fake_command(
            kind="set_setpoint",
            target="setpoint",
            payload={"value": 100.0},
        )
        await _run_shield_against_sim(sim, auth_cmd=cmd, sim_name="purge_mfc")

    @pytest.mark.anyio
    async def test_sartorius_sim_dispatch_cancel_does_not_corrupt_next_call(
        self,
    ) -> None:
        sim = SartoriusSim(name="balance")
        cmd = fake_command(kind="tare", target=None, payload={})
        await _run_shield_against_sim(sim, auth_cmd=cmd, sim_name="balance")

    @pytest.mark.anyio
    async def test_nidaq_sim_dispatch_cancel_does_not_corrupt_next_call(
        self,
    ) -> None:
        sim = NIDAQPolledSim(name="cdaq1")
        cmd = fake_command(kind="noop", target=None, payload={})
        await _run_shield_against_sim(sim, auth_cmd=cmd, sim_name="cdaq1")
