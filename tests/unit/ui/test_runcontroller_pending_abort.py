"""Pending-abort latch tests for :class:`RunController`.

``RunController.request_abort()`` does not drop on the floor when
``_conductor is None``: if a run task is in flight (i.e. ``_run()`` is
preparing the conductor), the abort reason is latched and ``_run()``
consumes it right after assigning ``self._conductor``.

These are pure-unit tests of the latch state machine. End-to-end
coverage (abort lands during PREPARING and the bundle reports
``aborted``) sits in the
:class:`~capa.ui.shutdown.ShutdownCoordinator` integration tests.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from capa.ui.state import RunController


@pytest.mark.anyio
async def test_request_abort_no_active_run_is_noop(tmp_path: Path) -> None:
    controller = RunController(runs_root=tmp_path)
    # Nothing in flight — request_abort is a no-op and no latch is set.
    controller.request_abort(mode="safe_shutdown")
    assert controller._pending_abort_reason is None
    assert controller.conductor is None


@pytest.mark.anyio
async def test_request_abort_latches_when_task_active_but_conductor_missing(
    tmp_path: Path,
) -> None:
    """``_run()`` creates the task before assigning ``_conductor``. If
    the operator hits Abort in that window the reason must be latched
    so ``_run()`` can apply it once the conductor exists."""
    controller = RunController(runs_root=tmp_path)

    # Simulate the window: _task is active, _conductor is still None.
    async def _placeholder() -> None:
        await asyncio.sleep(0.5)

    controller._task = asyncio.create_task(_placeholder())
    try:
        assert controller._conductor is None
        controller.request_abort(mode="immediate")
        assert controller._pending_abort_reason == "operator_immediate"

        # A second abort overrides (last-write-wins; matches request_abort
        # semantics for the post-conductor path too).
        controller.request_abort(mode="safe_shutdown")
        assert controller._pending_abort_reason == "operator_safe_shutdown"
    finally:
        controller._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await controller._task


@pytest.mark.anyio
async def test_request_abort_forwards_when_conductor_exists(tmp_path: Path) -> None:
    """When the conductor exists, ``request_abort`` forwards to it and
    does NOT touch the latch — the latch is only for the pre-conductor
    window."""
    controller = RunController(runs_root=tmp_path)

    stopped: list[str] = []

    class FakeConductor:
        def stop(self, *, reason: str) -> None:
            stopped.append(reason)

    controller._conductor = FakeConductor()  # type: ignore[assignment]
    controller.request_abort(mode="safe_shutdown")
    assert stopped == ["operator_safe_shutdown"]
    assert controller._pending_abort_reason is None


@pytest.mark.anyio
async def test_request_abort_after_run_done_does_nothing(tmp_path: Path) -> None:
    """If the run task already finished, request_abort is a no-op even
    though ``_task is not None``. Otherwise an abort dispatched after
    SEALED would leak a latched reason into the next run."""
    controller = RunController(runs_root=tmp_path)

    async def _quick() -> None:
        return None

    controller._task = asyncio.create_task(_quick())
    await controller._task  # task.done() is now True

    controller.request_abort(mode="immediate")
    assert controller._pending_abort_reason is None


@pytest.mark.anyio
async def test_pending_abort_is_consumed_and_forwarded_on_conductor_assignment(
    tmp_path: Path,
) -> None:
    """Smoke-test the consumer side: simulate the exact sequence inside
    ``_run()`` — task is active, latch is set before conductor exists,
    then the controller code assigns ``self._conductor = conductor``
    and immediately consumes the latch."""
    controller = RunController(runs_root=tmp_path)

    async def _placeholder() -> None:
        await asyncio.sleep(0.5)

    controller._task = asyncio.create_task(_placeholder())
    try:
        controller.request_abort(mode="immediate")
        assert controller._pending_abort_reason == "operator_immediate"

        stopped: list[str] = []

        class FakeConductor:
            def stop(self, *, reason: str) -> None:
                stopped.append(reason)

        # Inline replay of the consume-block from RunController._run().
        fake = FakeConductor()
        controller._conductor = fake  # type: ignore[assignment]
        pending_reason = controller._pending_abort_reason
        controller._pending_abort_reason = None
        if pending_reason is not None:
            fake.stop(reason=pending_reason)

        assert stopped == ["operator_immediate"]
        assert controller._pending_abort_reason is None
    finally:
        controller._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await controller._task
