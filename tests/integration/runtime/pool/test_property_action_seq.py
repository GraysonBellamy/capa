"""Property-style stress test for :class:`WorkerPool`.

calls for a Hypothesis state-machine test that randomly mixes
``arm`` / ``disarm`` / ``dispatch`` / ``cancel`` actions and asserts:

1. The pool always reaches a quiescent state (every worker in IDLE)
   within the grace budget.
2. No raised :class:`BridgeClosedError` escapes to the harness.
3. No deadlock — the test completes within a hard wall-clock budget.

This file delivers that property check using stdlib ``random`` with
seeded fuzzing (the rest of the project doesn't depend on Hypothesis;
adding a dev-only dep mid-project is out of scope here). A future
contributor can migrate this to ``hypothesis.stateful.RuleBasedStateMachine``
without changing the assertions — the action grammar below is
intentionally Hypothesis-shaped.

Coverage is bounded but meaningful: 5 trials × ~10 actions each = ~50
random transitions per CI run. A real Hypothesis port would explore the
state space more deeply, especially around the cancellation-during-DRAINING
edge where transitions race the disarm.
"""

from __future__ import annotations

import asyncio
import contextlib
import random

import pytest

from capa.runtime.errors import WorkerStateError
from capa.runtime.lifecycle import PoolState, WorkerState
from capa.runtime.pool import WorkerPool
from capa.runtime.runner import ThreadedRunner
from capa.runtime.worker import Worker
from tests.integration.runtime.fakes import (
    fake_command,
    make_fake_adapter,
    make_run_context,
)

_NUM_TRIALS = 5
_ACTIONS_PER_TRIAL = 10
_WALL_CLOCK_BUDGET_S = 20.0


async def _run_random_sequence(seed: int, num_devices: int = 2) -> None:
    """Drive a pool through ``_ACTIONS_PER_TRIAL`` random actions, then
    assert quiescent state."""
    rng = random.Random(seed)
    adapters = [
        make_fake_adapter(f"dev{i}", resource_id=f"sim:dev{i}", tick_period_s=0.01, emit_limit=3)
        for i in range(num_devices)
    ]
    workers = {
        a.resource_id: Worker(
            resource_id=a.resource_id,
            adapters=[a],
            runner=ThreadedRunner(name=f"worker-{a.resource_id}"),
        )
        for a in adapters
    }
    device_to_resource = {a.name: a.resource_id for a in adapters}
    pool = WorkerPool(workers=workers, device_to_resource=device_to_resource)
    await pool.open()
    bridges: dict[str, object] = {}

    # State the action picker tracks (parallel to the pool's own state).
    armed = False
    sampling = False

    actions = [
        "arm",
        "begin_sampling",
        "disarm",
        "dispatch",
        "dispatch_cancel",
    ]

    try:
        for _ in range(_ACTIONS_PER_TRIAL):
            action = rng.choice(actions)
            try:
                if action == "arm" and not armed:
                    await pool.arm_all(make_run_context())
                    armed = True
                elif action == "begin_sampling" and armed and not sampling:
                    bridges = await pool.begin_sampling_all(
                        consumer_loop=asyncio.get_running_loop()
                    )
                    sampling = True
                elif action == "disarm" and armed:
                    # Drain any pending bridge items so disarm sees clean
                    # streams. We do this best-effort with a short timeout
                    # per bridge.
                    if sampling:
                        for b in bridges.values():
                            with contextlib.suppress(asyncio.TimeoutError):
                                await asyncio.wait_for(b.get(), timeout=0.05)  # type: ignore[arg-type]
                    await pool.disarm_all(grace_s=2.0)
                    armed = False
                    sampling = False
                    bridges = {}
                elif action == "dispatch":
                    name = rng.choice(list(device_to_resource))
                    with contextlib.suppress(WorkerStateError):
                        await asyncio.wrap_future(pool.dispatch(name, fake_command()))
                elif action == "dispatch_cancel":
                    name = rng.choice(list(device_to_resource))
                    fut = pool.dispatch(name, fake_command())
                    wrapped = asyncio.wrap_future(fut)
                    # Cancel immediately; the shield guarantees the worker
                    # finishes its side regardless.
                    wrapped.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await wrapped
                # Other actions in invalid states are skipped (no-op).
            except WorkerStateError:
                # Racing state changes can produce these (e.g. arm while
                # already DRAINING from a previous action). Property test
                # tolerates them.
                pass

        # End-of-sequence: drive pool to quiescent IDLE.
        if sampling or armed:
            if sampling:
                for b in bridges.values():
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(b.get(), timeout=0.05)  # type: ignore[arg-type]
            await pool.disarm_all(grace_s=2.0)

        # All workers reached IDLE; pool still OPEN.
        for worker in pool.workers.values():
            assert worker.state is WorkerState.IDLE, (
                f"worker {worker.resource_id!r} stuck at {worker.state} after "
                f"random sequence with seed={seed}"
            )
        assert pool.state is PoolState.OPEN
    finally:
        await pool.close()


class TestRandomActionSequences:
    @pytest.mark.anyio
    @pytest.mark.parametrize("seed", list(range(_NUM_TRIALS)))
    async def test_sequence_reaches_idle(self, seed: int) -> None:
        """For each seed, the pool reaches a quiescent state."""
        # Hard wall-clock guard against deadlock.
        try:
            await asyncio.wait_for(
                _run_random_sequence(seed=seed),
                timeout=_WALL_CLOCK_BUDGET_S,
            )
        except TimeoutError:
            pytest.fail(
                f"random action sequence (seed={seed}) deadlocked — exceeded "
                f"{_WALL_CLOCK_BUDGET_S}s wall-clock budget"
            )

    @pytest.mark.anyio
    async def test_three_device_sequence(self) -> None:
        """A larger pool stresses the parallel arm/sample/disarm paths."""
        await asyncio.wait_for(
            _run_random_sequence(seed=42, num_devices=3),
            timeout=_WALL_CLOCK_BUDGET_S,
        )
