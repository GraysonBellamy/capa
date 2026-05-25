"""Tests for :class:`ShutdownCoordinator`.

These tests exercise the coordinator's shutdown sequence, idempotency,
deadline-driven escalation, and the hard wall-clock fuse. The hard fuse
is asserted via an injected ``hard_exit`` callable so the test process
isn't killed by ``os._exit``.

A real :class:`RunController` is used (no pool / no config), which gives
us a working :class:`LifecycleRegistry` and the ``await_active_run`` /
``aclose_pool`` no-op paths. The coordinator never sees a real worker
pool here — that's covered by the integration tests against
:meth:`WorkerPool.shutdown_close`.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from pathlib import Path

import pytest

from capa.ui.lifecycle import LifecycleKind
from capa.ui.shutdown import (
    ShutdownCoordinator,
    ShutdownDeadlines,
    ShutdownResult,
    ShutdownStage,
    status_message_for_stage,
)
from capa.ui.state import RunController


def _make_coordinator(
    controller: RunController,
    *,
    deadlines: ShutdownDeadlines | None = None,
    hard_exit_calls: list[None] | None = None,
) -> tuple[ShutdownCoordinator, list[None]]:
    calls = hard_exit_calls if hard_exit_calls is not None else []
    coord = ShutdownCoordinator(
        controller=controller,
        catalog=None,
        deadlines=deadlines or ShutdownDeadlines(),
        hard_exit=lambda: calls.append(None),
    )
    return coord, calls


async def _await_shutdown(coord: ShutdownCoordinator, reason: str) -> ShutdownResult:
    """Drive shutdown to completion. Inside ``@pytest.mark.anyio`` tests a
    running loop is always present, so ``begin_shutdown`` never returns ``None``."""
    task = coord.begin_shutdown(reason)
    assert task is not None
    return await task


@pytest.mark.anyio
async def test_idle_shutdown_completes_cleanly(tmp_path: Path) -> None:
    controller = RunController(runs_root=tmp_path)
    coord, _ = _make_coordinator(controller)
    task = coord.begin_shutdown("test")
    assert task is not None
    result = await task
    assert isinstance(result, ShutdownResult)
    assert result.clean is True
    assert result.hard_exit_required is False
    assert result.final_stage is ShutdownStage.COMPLETE
    assert result.errors == ()
    assert result.pool_close_result is None  # no pool was ever bound
    # Controller is now in shutdown mode and refuses new work.
    assert controller.shutdown_requested is True


@pytest.mark.anyio
async def test_begin_shutdown_is_idempotent(tmp_path: Path) -> None:
    controller = RunController(runs_root=tmp_path)
    coord, _ = _make_coordinator(controller)
    task1 = coord.begin_shutdown("first")
    task2 = coord.begin_shutdown("second")
    assert task1 is task2
    assert task1 is not None
    result = await task1
    # The first reason wins — second call is a no-op observer.
    assert result.reason == "first"


@pytest.mark.anyio
async def test_stage_changed_signal_fires_per_meaningful_stage(tmp_path: Path) -> None:
    controller = RunController(runs_root=tmp_path)
    coord, _ = _make_coordinator(controller)
    seen: list[ShutdownStage] = []
    coord.stage_changed.connect(seen.append)
    await _await_shutdown(coord, "test")
    # We expect at least the DISABLE_UI through CLOSE_CATALOG stages to
    # have fired. Internal stages (REQUESTED, CANCEL_LIFECYCLE_TASKS,
    # STOP_UI_DRAINERS) intentionally don't emit operator messages, but
    # the signal still fires for every stage the coordinator enters.
    assert ShutdownStage.DISABLE_UI in seen
    assert ShutdownStage.ABORT_ACTIVE_RUN in seen
    assert ShutdownStage.WAIT_ACTIVE_RUN in seen
    assert ShutdownStage.CLOSE_POOL in seen
    assert ShutdownStage.CLOSE_CATALOG in seen


@pytest.mark.anyio
async def test_completed_signal_emits_result(tmp_path: Path) -> None:
    controller = RunController(runs_root=tmp_path)
    coord, _ = _make_coordinator(controller)
    received: list[ShutdownResult] = []
    coord.completed.connect(received.append)
    result = await _await_shutdown(coord, "emit-test")
    assert received == [result]


@pytest.mark.anyio
async def test_hard_wall_fuse_fires_when_stage_wedges(tmp_path: Path) -> None:
    """A stage that never completes triggers the hard wall-clock fuse.

    We inject a pool-close shim that awaits forever so the CLOSE_POOL
    stage wedges. With ``hard_wall_s`` shorter than the stage's own
    timeout, the threading.Timer is the one that exits the wait: by
    calling the injected ``hard_exit`` and latching ``hard_exit_fired``.
    """
    controller = RunController(runs_root=tmp_path)

    # Patch the controller's aclose_pool to hang forever so a stage
    # provides a wedge target. ``await_active_run`` is already a no-op
    # for an idle controller; ``aclose_pool`` returns immediately because
    # no pool is bound — so we monkeypatch it to a never-completing
    # coroutine.
    async def _hang() -> None:
        await asyncio.Event().wait()  # forever

    controller.aclose_pool = _hang  # type: ignore[method-assign]

    hard_exit_calls: list[None] = []
    coord = ShutdownCoordinator(
        controller=controller,
        catalog=None,
        # hard_wall_s shorter than pool_close_s so the hard fuse wins.
        deadlines=ShutdownDeadlines(
            cancel_tasks_s=0.05,
            active_run_s=0.1,
            pool_close_s=5.0,
            catalog_close_s=0.1,
            orphan_recovery_s=0.1,
            hard_wall_s=0.2,
        ),
        hard_exit=lambda: hard_exit_calls.append(None),
    )

    task = coord.begin_shutdown("wedge-test")
    assert task is not None
    # Give the threading.Timer time to fire.
    await asyncio.sleep(0.5)
    assert hard_exit_calls == [None]
    assert coord.hard_exit_fired is True

    # Cleanup: cancel the wedged task so the test loop can exit.
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.anyio
async def test_pool_close_timeout_is_recorded_as_error(tmp_path: Path) -> None:
    """When the pool-close stage hits its own deadline (not the hard
    wall), the coordinator records the timeout as an error and continues
    to the next stage rather than wedging."""
    controller = RunController(runs_root=tmp_path)

    async def _hang() -> None:
        await asyncio.Event().wait()

    controller.aclose_pool = _hang  # type: ignore[method-assign]
    coord, hard_calls = _make_coordinator(
        controller,
        deadlines=ShutdownDeadlines(
            cancel_tasks_s=0.05,
            active_run_s=0.05,
            pool_close_s=0.1,
            catalog_close_s=0.1,
            orphan_recovery_s=0.1,
            hard_wall_s=5.0,  # well above the sum
        ),
    )
    result = await _await_shutdown(coord, "pool-timeout")
    assert hard_calls == []  # hard fuse must NOT have fired
    assert result.hard_exit_required is False
    # Errors should mention the pool close timeout.
    assert any("close_pool" in e for e in result.errors)
    assert result.clean is False


@pytest.mark.anyio
async def test_non_critical_lifecycle_tasks_are_cancelled(tmp_path: Path) -> None:
    """The CANCEL_LIFECYCLE_TASKS stage cancels non-critical entries
    (preview drainers, state-poll, manual-command tasks) and awaits
    their exit with a short budget."""
    controller = RunController(runs_root=tmp_path)

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _drain() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(_drain())
    controller.lifecycle.register(LifecycleKind.PREVIEW_DRAIN, "test-drain", task, critical=False)
    await started.wait()

    coord, _ = _make_coordinator(controller)
    await _await_shutdown(coord, "cancel-test")
    assert cancelled.is_set()
    assert task.done()


@pytest.mark.anyio
async def test_critical_lifecycle_tasks_are_not_cancelled_in_cancel_stage(
    tmp_path: Path,
) -> None:
    """Critical entries (RUN, POOL_OPEN, OLD_POOL_CLOSE) must NOT be
    cancelled by the cancel-lifecycle stage; they're handled by their
    own stages. This test makes sure we don't accidentally cancel a
    run-in-flight in cancel_lifecycle_tasks."""
    controller = RunController(runs_root=tmp_path)

    started = asyncio.Event()

    async def _stop_after() -> None:
        started.set()
        await asyncio.sleep(0.05)  # natural exit

    task = asyncio.create_task(_stop_after())
    controller.lifecycle.register(LifecycleKind.RUN, "test-run", task, critical=True)
    await started.wait()

    coord, _ = _make_coordinator(controller)
    await _await_shutdown(coord, "critical-preserved")
    # The cancel-lifecycle stage MUST NOT have cancelled the critical
    # task. (It may still be running; the coordinator's WAIT_ACTIVE_RUN
    # stage only awaits controller._task, not arbitrarily-registered
    # critical entries.)
    assert not task.cancelled()
    # Let the task finish naturally so the test loop cleans up.
    await task
    assert task.done()
    assert not task.cancelled()


@pytest.mark.anyio
async def test_shutdown_disables_controller_ui_actions(tmp_path: Path) -> None:
    """After DISABLE_UI runs, the controller refuses new starts and
    silently ignores config-load calls."""
    controller = RunController(runs_root=tmp_path)
    coord, _ = _make_coordinator(controller)
    await _await_shutdown(coord, "ui-disable")
    assert controller.shutdown_requested is True
    with pytest.raises(RuntimeError, match="shutdown in progress"):
        controller.start(config=None)  # type: ignore[arg-type]


def test_status_message_for_stage_covers_user_facing_stages() -> None:
    """Helper accessor returns a string for the stages the status bar
    should display, and None for internal-only stages."""
    assert status_message_for_stage(ShutdownStage.CLOSE_POOL) is not None
    assert status_message_for_stage(ShutdownStage.WAIT_ACTIVE_RUN) is not None
    # Stages without operator-meaningful intent: REQUESTED, CANCEL_LIFECYCLE_TASKS,
    # STOP_UI_DRAINERS; no message expected.
    assert status_message_for_stage(ShutdownStage.REQUESTED) is None
    assert status_message_for_stage(ShutdownStage.CANCEL_LIFECYCLE_TASKS) is None
    assert status_message_for_stage(ShutdownStage.STOP_UI_DRAINERS) is None


def test_no_in_flight_before_begin(tmp_path: Path) -> None:
    controller = RunController(runs_root=tmp_path)
    coord, _ = _make_coordinator(controller)
    assert coord.is_in_flight is False
    assert coord.current_stage is ShutdownStage.REQUESTED


@pytest.mark.anyio
async def test_hard_wall_timer_cancelled_on_clean_completion(
    tmp_path: Path,
) -> None:
    """The hard fuse must not fire after a clean shutdown completes —
    even if the test runs long. Belt-and-suspenders for the
    _on_task_done callback."""
    controller = RunController(runs_root=tmp_path)
    hard_exit_calls: list[None] = []
    coord = ShutdownCoordinator(
        controller=controller,
        catalog=None,
        deadlines=ShutdownDeadlines(hard_wall_s=0.5),
        hard_exit=lambda: hard_exit_calls.append(None),
    )
    await _await_shutdown(coord, "clean-then-wait")
    # Wait past the hard_wall_s; the timer should be cancelled.
    await asyncio.sleep(0.7)
    assert hard_exit_calls == []
    # Check that no stray threads are still timing.
    active_timers = [t for t in threading.enumerate() if isinstance(t, threading.Timer)]
    assert all(not t.is_alive() for t in active_timers)
