""":class:`WorkerRunner` — pluggable host for a worker's coroutine surface.

Every :class:`~capa.runtime.worker.Worker` is constructed with a
:class:`WorkerRunner`. The runner owns the *thread + loop* dimension; the
worker owns the *state machine + adapter handle* dimension. Splitting them
along that seam lets the same :class:`Worker` code run two ways:

* :class:`ThreadedRunner` — production. Spawns one ``threading.Thread`` with
  a dedicated ``asyncio.new_event_loop()``.
* :class:`InlineRunner` — unit tests. Runs the worker's coroutines on the
  test's own loop. Deterministic; ~10× faster; no thread to join.

Both runners satisfy the same :class:`WorkerRunner` protocol; the worker
itself doesn't know which it has. Cross-runner test parameterization
catches semantic drift between the two implementations.

The runner is a deliberately small surface — submit a coroutine factory,
get back a future. It is **not** a general-purpose loop wrapper; it doesn't
expose ``call_soon``, ``call_later``, or schedule helpers because the worker
should never need them. If a worker needs them it has reached into the loop
when it should be expressing intent through the runner.

Note: the :class:`Worker` uses the runner's :attr:`loop` directly when it
builds loop-affine primitives (``asyncio.Event``, ``asyncio.Queue``, the
outbound :class:`~capa.runtime.bridge.ThreadBridge`). That is by design:
the worker constructs those *inside* a coroutine it submitted via
:meth:`submit`, so the loop is the running loop at construction time. The
:attr:`loop` property exists for the bridge's ``attach_*`` calls, which must
quote a target loop explicitly to validate thread affinity.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future, InvalidStateError
from typing import Any, Protocol, TypeVar, runtime_checkable

import structlog

from capa.runtime.errors import RunnerStateError
from capa.runtime.shutdown import RunnerStopResult

T = TypeVar("T")

_logger = structlog.get_logger("capa.runtime.runner")


def _bridge_task_to_future[T](out: Future[T], task: asyncio.Task[T]) -> None:
    """Bridge an asyncio task's outcome onto a caller-owned future,
    dropping the outcome if the caller has already cancelled (or otherwise
    finalized) ``out``.

    Why drop instead of error: the worker-side coroutine is shielded (see
    :meth:`capa.runtime.worker.Worker._dispatch_impl`) and must run to
    completion regardless of caller cancellation — that is the §4.2 rule
    that keeps in-flight hardware transactions intact. If the caller has
    abandoned the future before the task finishes, the result has nowhere
    to go; setting it would raise ``InvalidStateError`` on the runner's
    loop. The early return is the fast path; the ``except`` clause covers
    the tight race where ``out`` becomes cancelled between the guard and
    the set (cancellation can be propagated cross-thread via
    :func:`asyncio.wrap_future` chaining).
    """
    if out.cancelled() or out.done():
        return
    with contextlib.suppress(InvalidStateError):
        if task.cancelled():
            out.cancel()
        elif (exc := task.exception()) is not None:
            out.set_exception(exc)
        else:
            out.set_result(task.result())


def _fail_or_drop[T](out: Future[T], exc: BaseException) -> None:
    """Surface a pre-task failure (factory raised before the coroutine was
    created) onto ``out``, dropping the failure if the caller has already
    finalized the future. Same rationale as :func:`_bridge_task_to_future`.
    """
    if out.cancelled() or out.done():
        return
    with contextlib.suppress(InvalidStateError):
        out.set_exception(exc)


@runtime_checkable
class WorkerRunner(Protocol):
    """Pluggable thread/loop host for a worker.

    Methods are all sync; cross-thread communication uses
    :class:`concurrent.futures.Future`. Async callers bridge with
    :func:`asyncio.wrap_future`.

    Lifecycle: :meth:`start` once, any number of :meth:`submit` calls while
    started, :meth:`stop` once. After :meth:`stop`, no further :meth:`submit`
    is permitted.
    """

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The event loop this runner hosts.

        For :class:`ThreadedRunner` this is the loop owned by the runner's
        thread. For :class:`InlineRunner` it is whatever loop was running
        when :meth:`start` was called. Callers building loop-affine
        primitives (``asyncio.Queue``, ``ThreadBridge`` attach handshake)
        quote this as the expected loop.
        """
        ...

    @property
    def thread_ident(self) -> int | None:
        """Thread ID hosting the loop, or ``None`` for :class:`InlineRunner`.

        Used by the worker for ``sys._current_frames()[thread_ident]`` when
        capturing a stack on hard-stop. Inline mode runs on the caller's
        thread, so the stack capture would be self-referential and is
        skipped.
        """
        ...

    def start(self) -> Future[None]:
        """Bring the runner up. For :class:`ThreadedRunner` this spawns the
        thread and constructs the loop; the future resolves once the loop
        is running and ready to accept :meth:`submit`. For :class:`InlineRunner`
        this just records the existing loop and resolves immediately.

        Raises :class:`RunnerStateError` if called twice.
        """
        ...

    def submit(self, coro_factory: Callable[[], Coroutine[Any, Any, T]]) -> Future[T]:
        """Schedule a coroutine on the runner's loop and return a future.

        The factory pattern (instead of "pass a coroutine") avoids
        cross-thread coroutine construction: the coroutine is built *on*
        the runner's loop. This matters because some coroutines capture
        the running loop at construction (e.g. an ``asyncio.Event`` built
        in the coroutine body would otherwise bind to the wrong loop).

        Raises :class:`RunnerStateError` if the runner is not started or
        has been stopped.
        """
        ...

    def stop(self, *, grace_s: float = 5.0) -> Future[RunnerStopResult]:
        """Tear the runner down. Resolves once the loop has stopped and
        (for :class:`ThreadedRunner`) the thread-join attempt has finished.

        ``grace_s`` bounds the thread join. On expiry the
        :class:`ThreadedRunner` resolves with
        :attr:`RunnerStopResult.joined` ``= False``. The runner does
        **not** attempt to convert a non-daemon thread into a daemon — an
        already-started ``threading.Thread`` cannot have its daemon flag
        toggled — and it does not pretend the thread exited. Callers
        treat non-joined threads as degraded shutdown; the
        :class:`~capa.ui.shutdown.ShutdownCoordinator` owns the
        process-wide hard fuse.

        :class:`InlineRunner` ignores the grace, has no thread to join,
        and always resolves with :attr:`RunnerStopResult.joined` ``= True``.
        """
        ...


class ThreadedRunner:
    """Production runner: dedicated thread, dedicated asyncio loop.

    Constructed but not started. Call :meth:`start` to spawn the thread.
    The thread is non-daemon (``daemon=False``) so a misbehaving worker
    is visible at process exit rather than silently dropped.

    Stop policy: the thread stays non-daemon for the worker's lifetime.
    ``stop()`` either joins within grace or returns
    :class:`RunnerStopResult` with ``joined=False``. The runner makes no
    attempt to convert a non-daemon thread into a daemon — an already-
    started ``threading.Thread`` cannot have its daemon flag toggled.
    Callers treat a non-joined runner as degraded shutdown; the
    :class:`~capa.ui.shutdown.ShutdownCoordinator` owns the process-wide
    hard fuse.
    """

    def __init__(self, *, name: str) -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = False
        self._stopped = False
        # Bound at start(); set when the loop is running and ready.
        self._loop_ready: Future[None] = Future()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise RunnerStateError(
                f"ThreadedRunner {self._name!r}: loop not constructed; call start() first"
            )
        return self._loop

    @property
    def thread_ident(self) -> int | None:
        return self._thread.ident if self._thread is not None else None

    def start(self) -> Future[None]:
        if self._started:
            raise RunnerStateError(f"ThreadedRunner {self._name!r}: start() called twice")
        self._started = True
        self._thread = threading.Thread(
            target=self._thread_main,
            name=self._name,
            daemon=False,
        )
        self._thread.start()
        return self._loop_ready

    def submit(self, coro_factory: Callable[[], Coroutine[Any, Any, T]]) -> Future[T]:
        if not self._started:
            raise RunnerStateError(f"ThreadedRunner {self._name!r}: submit() before start()")
        if self._stopped:
            raise RunnerStateError(f"ThreadedRunner {self._name!r}: submit() after stop()")
        loop = self._loop
        if loop is None:
            raise RunnerStateError(
                f"ThreadedRunner {self._name!r}: loop not yet ready; "
                f"await start() future before submitting"
            )

        out: Future[T] = Future()

        def _kick() -> None:
            # Runs on the runner's loop. Constructs the coroutine here so
            # any loop-affine primitives it builds bind to the right loop.
            try:
                coro = coro_factory()
            except BaseException as exc:
                _fail_or_drop(out, exc)
                return
            task: asyncio.Task[T] = loop.create_task(coro)
            task.add_done_callback(lambda t: _bridge_task_to_future(out, t))

        loop.call_soon_threadsafe(_kick)
        return out

    def stop(self, *, grace_s: float = 5.0) -> Future[RunnerStopResult]:
        if not self._started:
            raise RunnerStateError(f"ThreadedRunner {self._name!r}: stop() before start()")
        out: Future[RunnerStopResult] = Future()
        thread_ident_at_call = self._thread.ident if self._thread is not None else None
        if self._stopped:
            # Idempotent; resolve with a synthetic "already stopped" result.
            # We can't re-check liveness reliably (the original join may have
            # already happened on the first stop call), so report joined=True.
            out.set_result(
                RunnerStopResult(
                    name=self._name,
                    joined=True,
                    grace_s=grace_s,
                    thread_ident=thread_ident_at_call,
                )
            )
            return out
        self._stopped = True

        loop = self._loop
        thread = self._thread
        assert thread is not None

        def _finalize_after_join() -> None:
            thread.join(timeout=grace_s)
            joined = not thread.is_alive()
            if not joined:
                # The loop refused to stop within grace, or a submitted
                # coroutine is wedged in a non-cancellable native call.
                # We cannot safely interrupt it. Surface the degraded
                # state in the result; the ShutdownCoordinator decides
                # whether to escalate to its hard fuse.
                _logger.warning(
                    "runner.thread_did_not_join",
                    name=self._name,
                    grace_s=grace_s,
                    thread_ident=thread.ident,
                )
            out.set_result(
                RunnerStopResult(
                    name=self._name,
                    joined=joined,
                    grace_s=grace_s,
                    thread_ident=thread.ident,
                )
            )

        if loop is None:
            # start() finished but the loop never bound; nothing to stop.
            _finalize_after_join()
            return out

        loop.call_soon_threadsafe(loop.stop)
        # Joining blocks; do it on a tiny helper thread so the caller's
        # thread isn't pinned. The helper completes ``out`` regardless of
        # join success.
        threading.Thread(
            target=_finalize_after_join,
            name=f"{self._name}-stop",
            daemon=True,
        ).start()
        return out

    # ----- thread entry -----

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        structlog.contextvars.bind_contextvars(thread=self._name)
        with contextlib.suppress(BaseException):
            # Signal readiness AFTER the loop is bound but BEFORE run_forever,
            # so a submitter using call_soon_threadsafe always lands.
            self._loop_ready.set_result(None)
        try:
            loop.run_forever()
        finally:
            try:
                # Cancel any straggler tasks before closing the loop so we
                # don't leak "Task was destroyed but it is pending!" warnings.
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except BaseException:  # pragma: no cover - defensive
                pass
            loop.close()


class InlineRunner:
    """Deterministic test runner: hosts the worker on the caller's loop.

    The runner doesn't own a thread; it just records the loop that
    called :meth:`start` and routes :meth:`submit` calls through that
    loop's ``create_task``.

    Why ``loop`` is captured at :meth:`start` rather than at construction:
    pytest-anyio constructs the runner before the test's event loop is
    running. Lazy capture lets the same runner instance be reused after the
    enclosing fixture starts the loop.
    """

    def __init__(self, *, name: str = "inline-runner") -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False
        self._stopped = False

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise RunnerStateError(
                f"InlineRunner {self._name!r}: loop not captured; call start() first"
            )
        return self._loop

    @property
    def thread_ident(self) -> int | None:
        # Same thread as the caller; stack capture would be self-referential.
        return None

    def start(self) -> Future[None]:
        if self._started:
            raise RunnerStateError(f"InlineRunner {self._name!r}: start() called twice")
        self._started = True
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RunnerStateError(
                f"InlineRunner {self._name!r}: start() must be called from "
                f"within a running asyncio loop"
            ) from exc
        out: Future[None] = Future()
        out.set_result(None)
        return out

    def submit(self, coro_factory: Callable[[], Coroutine[Any, Any, T]]) -> Future[T]:
        if not self._started:
            raise RunnerStateError(f"InlineRunner {self._name!r}: submit() before start()")
        if self._stopped:
            raise RunnerStateError(f"InlineRunner {self._name!r}: submit() after stop()")
        loop = self._loop
        assert loop is not None

        out: Future[T] = Future()

        # Same-loop submit: build the coroutine right now (we're on the
        # loop) and create the task directly. No call_soon_threadsafe
        # round-trip — it would still work but is needless indirection
        # inline mode is supposed to avoid.
        try:
            coro = coro_factory()
        except BaseException as exc:
            _fail_or_drop(out, exc)
            return out
        task: asyncio.Task[T] = loop.create_task(coro)
        task.add_done_callback(lambda t: _bridge_task_to_future(out, t))
        return out

    def stop(self, *, grace_s: float = 5.0) -> Future[RunnerStopResult]:
        if not self._started:
            raise RunnerStateError(f"InlineRunner {self._name!r}: stop() before start()")
        out: Future[RunnerStopResult] = Future()
        result = RunnerStopResult(
            name=self._name,
            joined=True,  # no thread to join — always "joined" by definition
            grace_s=grace_s,
            thread_ident=None,
        )
        if self._stopped:
            out.set_result(result)
            return out
        self._stopped = True
        out.set_result(result)
        return out


__all__ = [
    "InlineRunner",
    "ThreadedRunner",
    "WorkerRunner",
]
