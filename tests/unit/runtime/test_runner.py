"""Unit tests for :mod:`capa.runtime.runner`.

The runner abstraction (plan §3.1) lets :class:`Worker` run inline (fast,
deterministic tests) and threaded (production). Both implementations satisfy
the same :class:`WorkerRunner` protocol, so most tests run against both via
``pytest.mark.parametrize`` over a factory fixture.

Cases covered:

* basic submit-and-await returns a value;
* exception propagation preserves identity;
* :class:`RunnerStateError` is raised on misuse (submit before start,
  submit after stop, double start, double stop is idempotent);
* :attr:`loop` is the loop submitted coroutines see as ``get_running_loop``;
* ThreadedRunner.thread_ident is the real OS thread; InlineRunner returns
  None;
* ThreadedRunner.stop joins the thread within grace;
* InlineRunner shares the test's loop (verified via id());
* coroutine factory is invoked on the runner's loop (loop-affinity of
  constructed asyncio primitives is preserved).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from typing import TypeVar

import pytest

from capa.runtime.errors import RunnerStateError
from capa.runtime.runner import InlineRunner, ThreadedRunner, WorkerRunner

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Cross-runner parametrization.
# ---------------------------------------------------------------------------


def _make_threaded() -> WorkerRunner:
    return ThreadedRunner(name="test-threaded")


def _make_inline() -> WorkerRunner:
    return InlineRunner(name="test-inline")


@pytest.fixture(params=[_make_threaded, _make_inline], ids=["threaded", "inline"])
def runner_factory(request: pytest.FixtureRequest) -> Callable[[], WorkerRunner]:
    """Yield each runner constructor in turn. Tests using this fixture run
    against both implementations."""
    return request.param  # type: ignore[no-any-return]


def _wrap[T](fut: Future[T]) -> Awaitable[T]:
    """Bridge a ``concurrent.futures.Future`` into an awaitable on the
    current asyncio loop."""
    return asyncio.wrap_future(fut)


# ---------------------------------------------------------------------------
# Basic submit/await/return semantics — cross-runner.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_submit_returns_value(runner_factory: Callable[[], WorkerRunner]) -> None:
    runner = runner_factory()
    await _wrap(runner.start())
    try:

        async def _work() -> int:
            return 7

        result = await _wrap(runner.submit(_work))
        assert result == 7
    finally:
        await _wrap(runner.stop(grace_s=1.0))


@pytest.mark.anyio
async def test_submit_propagates_exception(
    runner_factory: Callable[[], WorkerRunner],
) -> None:
    runner = runner_factory()
    await _wrap(runner.start())
    try:

        class SentinelError(Exception):
            pass

        async def _raise() -> None:
            raise SentinelError("boom")

        with pytest.raises(SentinelError, match="boom"):
            await _wrap(runner.submit(_raise))
    finally:
        await _wrap(runner.stop(grace_s=1.0))


@pytest.mark.anyio
async def test_coro_runs_on_runners_loop(
    runner_factory: Callable[[], WorkerRunner],
) -> None:
    """The whole point of the factory pattern: the coroutine is constructed
    on the runner's loop, so primitives like ``asyncio.Event`` built inside
    bind to the right loop."""
    runner = runner_factory()
    await _wrap(runner.start())
    try:

        async def _capture_loop() -> asyncio.AbstractEventLoop:
            return asyncio.get_running_loop()

        observed = await _wrap(runner.submit(_capture_loop))
        assert observed is runner.loop
    finally:
        await _wrap(runner.stop(grace_s=1.0))


@pytest.mark.anyio
async def test_multiple_submits_complete(
    runner_factory: Callable[[], WorkerRunner],
) -> None:
    runner = runner_factory()
    await _wrap(runner.start())
    try:

        async def _doubled(x: int) -> int:
            return x * 2

        futs = [runner.submit(lambda i=i: _doubled(i)) for i in range(10)]
        results = await asyncio.gather(*(_wrap(f) for f in futs))
        assert results == [i * 2 for i in range(10)]
    finally:
        await _wrap(runner.stop(grace_s=1.0))


@pytest.mark.anyio
async def test_submit_before_start_raises(
    runner_factory: Callable[[], WorkerRunner],
) -> None:
    runner = runner_factory()
    with pytest.raises(RunnerStateError, match="before start"):
        runner.submit(lambda: asyncio.sleep(0))


@pytest.mark.anyio
async def test_submit_after_stop_raises(
    runner_factory: Callable[[], WorkerRunner],
) -> None:
    runner = runner_factory()
    await _wrap(runner.start())
    await _wrap(runner.stop(grace_s=1.0))
    with pytest.raises(RunnerStateError, match="after stop"):
        runner.submit(lambda: asyncio.sleep(0))


@pytest.mark.anyio
async def test_double_start_raises(
    runner_factory: Callable[[], WorkerRunner],
) -> None:
    runner = runner_factory()
    await _wrap(runner.start())
    try:
        with pytest.raises(RunnerStateError, match="twice"):
            runner.start()
    finally:
        await _wrap(runner.stop(grace_s=1.0))


@pytest.mark.anyio
async def test_double_stop_is_idempotent(
    runner_factory: Callable[[], WorkerRunner],
) -> None:
    runner = runner_factory()
    await _wrap(runner.start())
    await _wrap(runner.stop(grace_s=1.0))
    # Second stop resolves immediately, no error.
    await _wrap(runner.stop(grace_s=1.0))


@pytest.mark.anyio
async def test_loop_property_raises_before_start(
    runner_factory: Callable[[], WorkerRunner],
) -> None:
    runner = runner_factory()
    with pytest.raises(RunnerStateError):
        _ = runner.loop


# ---------------------------------------------------------------------------
# Threaded-specific behaviour.
# ---------------------------------------------------------------------------


class TestThreadedRunner:
    """Tests that only make sense for the real-thread runner."""

    @pytest.mark.anyio
    async def test_loop_is_different_from_test_loop(self) -> None:
        runner = ThreadedRunner(name="distinct-loop")
        await _wrap(runner.start())
        try:
            test_loop = asyncio.get_running_loop()
            assert runner.loop is not test_loop
        finally:
            await _wrap(runner.stop(grace_s=1.0))

    @pytest.mark.anyio
    async def test_thread_ident_is_set_after_start(self) -> None:
        runner = ThreadedRunner(name="ident")
        await _wrap(runner.start())
        try:

            async def _capture_ident() -> int:
                return threading.get_ident()

            observed = await _wrap(runner.submit(_capture_ident))
            assert observed == runner.thread_ident
            assert observed != threading.get_ident()  # different from test thread
        finally:
            await _wrap(runner.stop(grace_s=1.0))

    @pytest.mark.anyio
    async def test_stop_joins_thread_within_grace(self) -> None:
        runner = ThreadedRunner(name="join")
        await _wrap(runner.start())
        ident = runner.thread_ident
        await _wrap(runner.stop(grace_s=2.0))
        # After stop's future resolves the helper thread has joined.
        # Iterate enumerate() once to confirm no live thread with that ident.
        alive_idents = {t.ident for t in threading.enumerate() if t.is_alive()}
        assert ident not in alive_idents


# ---------------------------------------------------------------------------
# Inline-specific behaviour.
# ---------------------------------------------------------------------------


class TestInlineRunner:
    """Tests that only make sense for the deterministic inline runner."""

    @pytest.mark.anyio
    async def test_loop_is_the_test_loop(self) -> None:
        runner = InlineRunner()
        await _wrap(runner.start())
        try:
            assert runner.loop is asyncio.get_running_loop()
        finally:
            await _wrap(runner.stop(grace_s=1.0))

    @pytest.mark.anyio
    async def test_thread_ident_is_none(self) -> None:
        runner = InlineRunner()
        await _wrap(runner.start())
        try:
            assert runner.thread_ident is None
        finally:
            await _wrap(runner.stop(grace_s=1.0))

    def test_start_outside_running_loop_raises(self) -> None:
        runner = InlineRunner()
        with pytest.raises(RunnerStateError, match="running asyncio loop"):
            runner.start()
