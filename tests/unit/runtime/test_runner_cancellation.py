"""Caller-cancellation tests for :mod:`capa.runtime.runner`.

The shielded-dispatch contract says the worker-side coroutine
must run to completion even when the caller cancels its future. Before
Both runners' ``_bridge`` callbacks once called ``out.set_result(...)``
or ``out.set_exception(...)`` unconditionally — when the caller had
cancelled ``out`` via :func:`asyncio.wrap_future` chaining, the set raised
``concurrent.futures.InvalidStateError`` on the runner's loop. The failure
was silent (it landed in asyncio's default exception handler) but real.

These tests assert:

1. Caller cancellation does not produce ``InvalidStateError`` on the
   runner's loop (a recorded exception handler observes no such call).
2. The worker-side coroutine still runs to completion despite the cancel.
3. Edge cases: cancellation before ``_kick`` runs; factory raises after
   cancellation; coroutine raises after cancellation.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from typing import Any

import pytest

from capa.runtime.runner import InlineRunner, ThreadedRunner, WorkerRunner

# ---------------------------------------------------------------------------
# Cross-runner parametrization (same factory pattern as test_runner.py).
# ---------------------------------------------------------------------------


def _make_threaded() -> WorkerRunner:
    return ThreadedRunner(name="cancel-threaded")


def _make_inline() -> WorkerRunner:
    return InlineRunner(name="cancel-inline")


@pytest.fixture(params=[_make_threaded, _make_inline], ids=["threaded", "inline"])
def runner_factory(request: pytest.FixtureRequest) -> Callable[[], WorkerRunner]:
    return request.param  # type: ignore[no-any-return]


def _wrap[T](fut: Future[T]) -> Awaitable[T]:
    return asyncio.wrap_future(fut)


# ---------------------------------------------------------------------------
# Loop exception-handler capture.
# ---------------------------------------------------------------------------


class _ExceptionRecorder:
    """Records asyncio loop exceptions for assertion.

    The runner's loop runs on its own thread (ThreadedRunner) or on the
    test loop (InlineRunner). Installing the handler is thread-safe — we
    call ``loop.set_exception_handler`` directly, and the loop reads it
    on next dispatch.
    """

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def __call__(self, loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        self.entries.append(context)

    def has_invalid_state_error(self) -> bool:
        from concurrent.futures import InvalidStateError

        for ctx in self.entries:
            exc = ctx.get("exception")
            if isinstance(exc, InvalidStateError):
                return True
        return False


async def _install_exception_handler(runner: WorkerRunner, handler: _ExceptionRecorder) -> None:
    """Install ``handler`` as the exception handler on the runner's loop.

    For ThreadedRunner the loop lives on another thread; we route the
    install through ``call_soon_threadsafe`` and await an asyncio.Future
    bridged from a concurrent.futures.Future so we know the install has
    landed before the test proceeds.
    """
    done: Future[None] = Future()

    def _install() -> None:
        runner.loop.set_exception_handler(handler)
        done.set_result(None)

    runner.loop.call_soon_threadsafe(_install)
    await _wrap(done)


# ---------------------------------------------------------------------------
# Canonical case: caller cancellation produces no loop-level error.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_caller_cancel_does_not_log_invalid_state_error(
    runner_factory: Callable[[], WorkerRunner],
) -> None:
    """The load-bearing assertion: when the caller cancels the
    asyncio wrapper of a runner-issued future, the underlying
    ``concurrent.futures.Future`` is cancelled by ``wrap_future`` chaining.
    The runner's done-callback must drop the result silently instead of
    raising :class:`InvalidStateError` on the loop."""
    runner = runner_factory()
    await _wrap(runner.start())
    recorder = _ExceptionRecorder()
    await _install_exception_handler(runner, recorder)
    try:
        coro_complete = asyncio.Event()

        async def _slow() -> int:
            await asyncio.sleep(0.05)
            return 42

        fut = runner.submit(_slow)
        wrapped = _wrap(fut)
        # Give the runner loop a chance to start the task.
        await asyncio.sleep(0.01)
        if isinstance(wrapped, asyncio.Future):
            wrapped.cancel()
        else:  # pragma: no cover - wrap_future returns Future
            asyncio.ensure_future(wrapped).cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await wrapped

        # The shielded worker coroutine must still run to completion.
        # Wait long enough for the slow task to finish.
        await asyncio.sleep(0.1)

        assert not recorder.has_invalid_state_error(), (
            f"runner loop logged InvalidStateError: {recorder.entries}"
        )

        # Allow the recorder reference to be inspected by the assertion above.
        del coro_complete
    finally:
        await _wrap(runner.stop(grace_s=1.0))


@pytest.mark.anyio
async def test_caller_cancel_does_not_interrupt_coroutine(
    runner_factory: Callable[[], WorkerRunner],
) -> None:
    """The shield rule, asserted at the runner level (not just the worker):
    the coroutine runs to completion despite caller cancellation.

    We assert via a shared list mutation rather than the future result,
    because the future result is (correctly) dropped on the floor."""
    runner = runner_factory()
    await _wrap(runner.start())
    try:
        completed: list[str] = []

        async def _slow() -> None:
            await asyncio.sleep(0.05)
            completed.append("done")

        fut = runner.submit(_slow)
        wrapped = _wrap(fut)
        await asyncio.sleep(0.01)
        if isinstance(wrapped, asyncio.Future):
            wrapped.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await wrapped

        await asyncio.sleep(0.1)
        assert completed == ["done"]
    finally:
        await _wrap(runner.stop(grace_s=1.0))


# ---------------------------------------------------------------------------
# Edge cases.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_caller_cancel_before_kick_runs_threaded() -> None:
    """ThreadedRunner-specific: cancelling ``out`` between ``submit()``
    returning and the runner loop running ``_kick`` exercises the
    factory-success → bridge-drop path. The task still runs (cancellation
    has no way to propagate to a not-yet-created task), and the bridge
    drops the result silently."""
    runner = ThreadedRunner(name="pre-kick")
    await _wrap(runner.start())
    recorder = _ExceptionRecorder()
    await _install_exception_handler(runner, recorder)
    try:
        completed: list[str] = []

        async def _work() -> int:
            completed.append("ran")
            return 1

        fut = runner.submit(_work)
        # Cancel the concurrent future directly, before _kick has had any
        # chance to run on the worker loop. (Even with call_soon_threadsafe
        # already queued, _kick has not executed yet because we have not
        # yielded.)
        fut.cancel()
        # Now let the loop run.
        await asyncio.sleep(0.05)

        assert completed == ["ran"]
        assert not recorder.has_invalid_state_error(), recorder.entries
    finally:
        await _wrap(runner.stop(grace_s=1.0))


@pytest.mark.anyio
async def test_factory_raises_after_caller_cancels(
    runner_factory: Callable[[], WorkerRunner],
) -> None:
    """Covers ``_kick``'s factory-exception path: ``out`` is already
    cancelled when the factory raises. ``_fail_or_drop`` must swallow the
    exception instead of raising ``InvalidStateError``."""
    runner = runner_factory()
    await _wrap(runner.start())
    recorder = _ExceptionRecorder()
    await _install_exception_handler(runner, recorder)
    try:

        def _bad_factory() -> Any:
            raise RuntimeError("factory boom")

        fut = runner.submit(_bad_factory)
        fut.cancel()
        await asyncio.sleep(0.05)
        assert not recorder.has_invalid_state_error(), recorder.entries
    finally:
        await _wrap(runner.stop(grace_s=1.0))


@pytest.mark.anyio
async def test_caller_cancel_then_task_raises(
    runner_factory: Callable[[], WorkerRunner],
) -> None:
    """Covers ``_bridge``'s exception branch: caller cancels, coroutine
    then raises. The bridge must drop the exception silently — there is
    no observable channel for it once the caller has gone."""
    runner = runner_factory()
    await _wrap(runner.start())
    recorder = _ExceptionRecorder()
    await _install_exception_handler(runner, recorder)
    try:

        async def _slow_raise() -> None:
            await asyncio.sleep(0.05)
            raise RuntimeError("coro boom")

        fut = runner.submit(_slow_raise)
        wrapped = _wrap(fut)
        await asyncio.sleep(0.01)
        if isinstance(wrapped, asyncio.Future):
            wrapped.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await wrapped
        await asyncio.sleep(0.1)
        assert not recorder.has_invalid_state_error(), recorder.entries
    finally:
        await _wrap(runner.stop(grace_s=1.0))
