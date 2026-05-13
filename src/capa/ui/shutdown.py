""":class:`ShutdownCoordinator` — single owner of GUI shutdown.

This module:

* Funnels every close trigger (window X, File→Quit, Ctrl+Q) through
  :meth:`ShutdownCoordinator.begin_shutdown`, which is **idempotent** —
  repeat calls return the in-flight task.
* Drives a fixed phase ladder (see :class:`ShutdownPhase`) with per-
  phase ``asyncio.wait_for`` deadlines so no phase can hang the GUI.
* Arms a process-wide :class:`threading.Timer` at the start — the hard
  wall-clock fuse. If the asyncio loop wedges past
  :attr:`ShutdownDeadlines.hard_wall_s`, the timer's thread logs
  ``shutdown.os_exit`` with a snapshot of the last-known state and
  invokes the injected ``hard_exit`` callable (``os._exit(0)`` in
  production, a test mock in unit tests).
* Returns a structured :class:`ShutdownResult` so callers and tests can
  assert what actually happened — clean exit vs. degraded vs. hard
  exit.

Key constraint: Python cannot safely kill a stuck thread. This
coordinator's only hard guarantee is parent-process exit. Its
*cooperative* guarantee is that every blocking operation is wrapped in
``asyncio.wait_for`` so the parent fuse always gets a chance to fire.

The hard-wall timer runs on a separate non-asyncio thread because the
asyncio loop itself may be wedged when the deadline expires. Every
field the fuse needs is updated as a plain attribute on the coordinator
before each phase transition; the fuse reads attributes only.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from PySide6.QtCore import QObject, Signal

from capa.runtime.shutdown import PoolCloseResult
from capa.ui.lifecycle import LifecycleKind

if TYPE_CHECKING:
    from capa.storage.catalog import RunCatalog
    from capa.ui.state import RunController

_logger = structlog.get_logger("capa.ui.shutdown")


# ---------------------------------------------------------------------------
# Phase + deadline + result types
# ---------------------------------------------------------------------------


class ShutdownPhase(StrEnum):
    """Phase ladder the coordinator drives top-to-bottom.

    Declaration order is execution order. The coordinator records the
    current phase as a plain attribute before entering each one so the
    hard-wall timer's snapshot reads a meaningful value even if the
    asyncio loop is wedged.
    """

    REQUESTED = "requested"
    DISABLE_UI = "disable_ui"
    CANCEL_LIFECYCLE_TASKS = "cancel_lifecycle_tasks"
    ABORT_ACTIVE_RUN = "abort_active_run"
    WAIT_ACTIVE_RUN = "wait_active_run"
    CLOSE_POOL = "close_pool"
    STOP_UI_DRAINERS = "stop_ui_drainers"
    RECOVER_ORPHANS = "recover_orphans"
    CLOSE_CATALOG = "close_catalog"
    HARD_EXIT = "hard_exit"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class ShutdownDeadlines:
    """Per-phase wall-clock budgets.

    The hard wall is the absolute upper bound; it must be larger than
    the sum of the phase deadlines so the timer fires only when a phase
    itself wedges past its bound.
    """

    cancel_tasks_s: float = 1.0
    active_run_s: float = 10.0
    pool_close_s: float = 8.0
    catalog_close_s: float = 3.0
    orphan_recovery_s: float = 10.0
    hard_wall_s: float = 25.0


@dataclass(frozen=True, slots=True)
class ShutdownResult:
    """Outcome of one :meth:`ShutdownCoordinator.begin_shutdown` task.

    ``clean`` is true iff the pool closed cleanly AND no phase recorded
    an error. ``hard_exit_required`` is true if the hard-wall timer
    would have fired (only observable in tests; in production the fuse
    has already terminated the process by the time anyone could read
    this field).
    """

    reason: str
    clean: bool
    hard_exit_required: bool
    final_phase: ShutdownPhase
    active_run_id: str | None
    active_bundle_path: Path | None
    pool_close_result: PoolCloseResult | None
    errors: tuple[str, ...]
    elapsed_s: float


# ---------------------------------------------------------------------------
# Status-bar messages per phase
# ---------------------------------------------------------------------------


_PHASE_STATUS_MESSAGES: dict[ShutdownPhase, str] = {
    ShutdownPhase.DISABLE_UI: "Shutting down…",
    ShutdownPhase.ABORT_ACTIVE_RUN: "Stopping run…",
    ShutdownPhase.WAIT_ACTIVE_RUN: "Stopping run…",
    ShutdownPhase.CLOSE_POOL: "Closing hardware…",
    ShutdownPhase.RECOVER_ORPHANS: "Finalizing bundle…",
    ShutdownPhase.CLOSE_CATALOG: "Closing catalog…",
    ShutdownPhase.HARD_EXIT: "Shutdown is taking longer than expected; forcing exit if needed…",
    ShutdownPhase.COMPLETE: "Shutdown complete.",
}


def status_message_for_phase(phase: ShutdownPhase) -> str | None:
    """Operator-facing message for a phase, or ``None`` if the phase
    has no status-bar surface. The set of phases with messages is
    deliberately smaller than the phase enum: internal phases
    (REQUESTED, CANCEL_LIFECYCLE_TASKS, STOP_UI_DRAINERS) don't carry
    operator-meaningful intent."""
    return _PHASE_STATUS_MESSAGES.get(phase)


# ---------------------------------------------------------------------------
# Hard-exit default (production)
# ---------------------------------------------------------------------------


def _default_hard_exit() -> None:
    """Production hard-exit callable. Logs a final breadcrumb and calls
    ``os._exit(0)``.

    Defined at module level so tests can compare identity (``coordinator
    ._hard_exit is _default_hard_exit``) and so injection sites have a
    clear reference. The log/flush is best-effort; the only contract
    is that the process actually exits.
    """
    try:
        logging.shutdown()
    finally:
        os._exit(0)


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _CoordinatorState:
    """Mutable state the hard-wall timer's thread reads.

    Every field is a plain attribute set by the asyncio side BEFORE the
    relevant phase begins; the timer thread reads them without locks
    (single-writer / single-reader pattern, both via the GIL).
    """

    reason: str = ""
    phase: ShutdownPhase = ShutdownPhase.REQUESTED
    start_mono: float = 0.0
    active_run_id: str | None = None
    active_bundle_path: Path | None = None
    pending_lifecycle: tuple[str, ...] = field(default_factory=tuple)
    non_joined_workers: tuple[str, ...] = field(default_factory=tuple)


class ShutdownCoordinator(QObject):
    """Single owner of GUI shutdown.

    Constructed once per :class:`~capa.ui.main_window.MainWindow`. Every
    GUI close trigger calls :meth:`begin_shutdown`; the coordinator
    returns the same in-flight task for repeat callers.

    Signals (fire on the qasync loop):

    * :attr:`phase_changed` — emitted on every transition with the new
      :class:`ShutdownPhase`. The status bar wires to this.
    * :attr:`completed` — emitted once with the final
      :class:`ShutdownResult`. ``MainWindow`` wires this to flip
      ``_shutdown_complete`` and re-trigger :meth:`close`.
    """

    phase_changed = Signal(object)  # ShutdownPhase
    completed = Signal(object)  # ShutdownResult

    def __init__(
        self,
        *,
        controller: RunController,
        catalog: RunCatalog | None = None,
        deadlines: ShutdownDeadlines | None = None,
        hard_exit: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._catalog = catalog
        self._deadlines = deadlines or ShutdownDeadlines()
        self._hard_exit: Callable[[], None] = hard_exit or _default_hard_exit
        self._clock = clock
        self._task: asyncio.Task[ShutdownResult] | None = None
        self._hard_timer: threading.Timer | None = None
        self._state = _CoordinatorState()
        # Latched once the hard-wall timer fires so tests can assert
        # the fuse went off without actually calling ``os._exit``.
        self._hard_exit_fired = False
        # Last observed pool close result; coordinator stashes it
        # before the close phase exits so the timer snapshot can read
        # the non-joined worker list.
        self._last_pool_close: PoolCloseResult | None = None
        self._collected_errors: list[str] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def hard_exit_fired(self) -> bool:
        """``True`` once the hard-wall fuse fired. Test-only — in
        production the process has exited by the time anyone could read
        this."""
        return self._hard_exit_fired

    @property
    def current_phase(self) -> ShutdownPhase:
        return self._state.phase

    @property
    def is_in_flight(self) -> bool:
        """``True`` between :meth:`begin_shutdown` and the
        :attr:`completed` emit."""
        return self._task is not None and not self._task.done()

    def begin_shutdown(self, reason: str) -> asyncio.Task[ShutdownResult] | None:
        """Drive the phase ladder. Idempotent — repeat calls return the
        in-flight task without restarting.

        ``reason`` flows into the final :class:`ShutdownResult` and into
        every structured log event so log greps can correlate.

        Returns ``None`` if no asyncio loop is running (e.g. a pure-Qt
        unit test with no qasync setup) — there is nothing to clean up
        in that case, so the coordinator emits a synthetic clean
        :attr:`completed` signal synchronously and lets the caller
        proceed with the window close.
        """
        if self._task is not None:
            return self._task
        self._state.reason = reason
        self._state.start_mono = self._clock()
        self._state.phase = ShutdownPhase.REQUESTED
        _logger.info("shutdown.begin", reason=reason)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No qasync / asyncio loop. Nothing to drive — synthesize a
            # clean result so the close path can continue. No hard-wall
            # timer needed: there's no async work to wedge.
            self._state.phase = ShutdownPhase.COMPLETE
            result = ShutdownResult(
                reason=reason,
                clean=True,
                hard_exit_required=False,
                final_phase=ShutdownPhase.COMPLETE,
                active_run_id=None,
                active_bundle_path=None,
                pool_close_result=None,
                errors=(),
                elapsed_s=0.0,
            )
            try:
                self.completed.emit(result)
            except Exception:
                _logger.debug("shutdown.completed_emit_failed")
            return None

        # Arm the hard fuse BEFORE creating the asyncio task — if task
        # creation itself raises (unlikely), the fuse still saves us.
        self._hard_timer = threading.Timer(self._deadlines.hard_wall_s, self._on_hard_wall)
        self._hard_timer.daemon = True
        self._hard_timer.start()
        self._task = loop.create_task(self._drive(), name="ui-shutdown")
        # Register the shutdown task itself so a future caller (e.g. a
        # second close click after we're done but before MainWindow has
        # re-entered closeEvent) can see it.
        self._controller.lifecycle.register(
            LifecycleKind.SHUTDOWN, "shutdown", self._task, critical=True
        )
        self._task.add_done_callback(self._on_task_done)
        return self._task

    # ------------------------------------------------------------------
    # Phase driver
    # ------------------------------------------------------------------

    async def _drive(self) -> ShutdownResult:
        try:
            await self._phase_disable_ui()
            await self._phase_cancel_lifecycle_tasks()
            await self._phase_abort_active_run()
            await self._phase_wait_active_run()
            await self._phase_close_pool()
            await self._phase_stop_ui_drainers()
            await self._phase_recover_orphans()
            await self._phase_close_catalog()
        except BaseException as exc:
            # The phase methods catch their own exceptions and append to
            # _collected_errors. A bare exception here is a coordinator
            # bug; record it but still build a result so the window
            # closes rather than wedging.
            self._collected_errors.append(f"coordinator crashed: {exc!r}")
            _logger.exception("shutdown.coordinator_crashed")
        return self._finalize()

    def _enter_phase(self, phase: ShutdownPhase) -> None:
        self._state.phase = phase
        _logger.info(
            "shutdown.phase",
            phase=phase.value,
            elapsed_s=self._clock() - self._state.start_mono,
        )
        msg = _PHASE_STATUS_MESSAGES.get(phase)
        if msg is not None:
            try:
                self.phase_changed.emit(phase)
            except Exception:
                # Signal emit failure must never block shutdown.
                _logger.debug("shutdown.phase_emit_failed", phase=phase.value)

    async def _phase_disable_ui(self) -> None:
        self._enter_phase(ShutdownPhase.DISABLE_UI)
        self._controller.enter_shutdown_mode()
        # Capture the run id / bundle path NOW so the hard-wall snapshot
        # has them even if a later phase wedges before they would be set.
        self._state.active_run_id = self._controller.active_run_id
        self._state.active_bundle_path = self._controller.active_bundle_path

    async def _phase_cancel_lifecycle_tasks(self) -> None:
        self._enter_phase(ShutdownPhase.CANCEL_LIFECYCLE_TASKS)
        # Cancel non-critical entries; critical ones (RUN, POOL_OPEN,
        # OLD_POOL_CLOSE) are handled by their own phases.
        entries = self._controller.lifecycle.snapshot()
        self._state.pending_lifecycle = tuple(f"{e.kind.value}:{e.name}" for e in entries)
        non_critical = [e for e in entries if not e.critical]
        for entry in non_critical:
            entry.task.cancel()
        if non_critical:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(e.task for e in non_critical), return_exceptions=True),
                    timeout=self._deadlines.cancel_tasks_s,
                )
            except TimeoutError:
                self._collected_errors.append(
                    "cancel_lifecycle_tasks: timed out waiting for non-critical tasks"
                )
                _logger.warning("shutdown.cancel_tasks_timeout")

    async def _phase_abort_active_run(self) -> None:
        self._enter_phase(ShutdownPhase.ABORT_ACTIVE_RUN)
        if self._controller.is_active:
            _logger.info(
                "shutdown.abort_requested",
                active_run_id=self._state.active_run_id,
            )
            self._controller.request_abort(mode="immediate")

    async def _phase_wait_active_run(self) -> None:
        self._enter_phase(ShutdownPhase.WAIT_ACTIVE_RUN)
        if not self._controller.is_active:
            return
        try:
            await asyncio.wait_for(
                self._controller.await_active_run(),
                timeout=self._deadlines.active_run_s,
            )
        except TimeoutError:
            self._collected_errors.append("wait_active_run: timed out waiting for run task")
            _logger.warning(
                "shutdown.active_run_timeout",
                active_run_id=self._state.active_run_id,
            )

    async def _phase_close_pool(self) -> None:
        self._enter_phase(ShutdownPhase.CLOSE_POOL)
        try:
            result = await asyncio.wait_for(
                self._controller.aclose_pool(),
                timeout=self._deadlines.pool_close_s,
            )
        except TimeoutError:
            self._collected_errors.append("close_pool: timed out")
            _logger.warning("shutdown.pool_close_timeout")
            return
        except BaseException as exc:
            self._collected_errors.append(f"close_pool: {exc!r}")
            _logger.exception("shutdown.pool_close_failed")
            return
        self._last_pool_close = result
        if result is not None:
            self._state.non_joined_workers = tuple(
                w.resource_id for w in result.worker_results if not w.runner_stop.joined
            )
            if not result.clean:
                self._collected_errors.append(
                    f"pool_close_degraded: errors={result.errors}, "
                    f"non_joined={self._state.non_joined_workers}"
                )

    async def _phase_stop_ui_drainers(self) -> None:
        self._enter_phase(ShutdownPhase.STOP_UI_DRAINERS)
        # The aclose_pool() path already cancelled UI-side preview
        # drainers and closed the bridges. Any lingering non-critical
        # tasks (drainers that hadn't reached a yield point yet) get
        # one more sweep here. The registry self-prunes done tasks, so
        # the snapshot is the live set.
        leftover = [e for e in self._controller.lifecycle.snapshot() if not e.critical]
        for entry in leftover:
            entry.task.cancel()
        if leftover:
            await asyncio.gather(*(e.task for e in leftover), return_exceptions=True)

    async def _phase_recover_orphans(self) -> None:
        self._enter_phase(ShutdownPhase.RECOVER_ORPHANS)
        # The active-bundle checkpoint deletion lives on the session's
        # ``close`` path; if the run finished cleanly inside
        # ``WAIT_ACTIVE_RUN`` the checkpoint is already gone. If the run
        # was killed mid-finalize, the checkpoint persists so the NEXT
        # launch's recovery helper reconciles it. No work to do here in
        # the in-process design — kept as an explicit phase so the IPC
        # successor (Stage C) has an obvious extension point.
        return

    async def _phase_close_catalog(self) -> None:
        self._enter_phase(ShutdownPhase.CLOSE_CATALOG)
        if self._catalog is None:
            return
        try:
            # ``RunCatalog.close`` is sync (SQLite handle). Off-load to
            # a thread so a slow close doesn't block the asyncio loop —
            # the hard-wall fuse cannot pre-empt the loop, only the
            # process, so we keep the loop responsive at every step.
            await asyncio.wait_for(
                asyncio.to_thread(self._catalog.close),
                timeout=self._deadlines.catalog_close_s,
            )
        except TimeoutError:
            self._collected_errors.append("close_catalog: timed out")
            _logger.warning("shutdown.catalog_close_timeout")
        except BaseException as exc:
            self._collected_errors.append(f"close_catalog: {exc!r}")
            _logger.exception("shutdown.catalog_close_failed")

    # ------------------------------------------------------------------
    # Finalization
    # ------------------------------------------------------------------

    def _finalize(self) -> ShutdownResult:
        self._cancel_hard_timer()
        elapsed = self._clock() - self._state.start_mono
        clean = not self._collected_errors and (
            self._last_pool_close is None or self._last_pool_close.clean
        )
        result = ShutdownResult(
            reason=self._state.reason,
            clean=clean,
            hard_exit_required=self._hard_exit_fired,
            final_phase=ShutdownPhase.COMPLETE if clean else self._state.phase,
            active_run_id=self._state.active_run_id,
            active_bundle_path=self._state.active_bundle_path,
            pool_close_result=self._last_pool_close,
            errors=tuple(self._collected_errors),
            elapsed_s=elapsed,
        )
        self._state.phase = ShutdownPhase.COMPLETE
        _logger.info(
            "shutdown.complete",
            reason=self._state.reason,
            clean=clean,
            elapsed_s=elapsed,
            hard_exit_required=self._hard_exit_fired,
            errors=result.errors,
        )
        try:
            self.completed.emit(result)
        except Exception:
            _logger.debug("shutdown.completed_emit_failed")
        return result

    def _cancel_hard_timer(self) -> None:
        timer = self._hard_timer
        if timer is None:
            return
        self._hard_timer = None
        # ``Timer.cancel`` is safe to call after the timer has fired —
        # the documentation guarantees it does nothing in that case.
        timer.cancel()

    def _on_task_done(self, _task: asyncio.Task[ShutdownResult]) -> None:
        # Ensure the timer is cancelled even if `_drive` itself
        # propagated unexpectedly. `_finalize` cancels it on the happy
        # path; this is a belt-and-suspenders for the
        # `_drive`-raised-out-of-its-try case (which the try/except
        # makes very unlikely, but the fuse must not over-fire after
        # we're "done").
        self._cancel_hard_timer()

    # ------------------------------------------------------------------
    # Hard-wall fuse (runs on the threading.Timer's thread)
    # ------------------------------------------------------------------

    def _on_hard_wall(self) -> None:
        """Fires on the timer thread when ``hard_wall_s`` elapses.

        The asyncio loop may be wedged at this moment. We MUST NOT
        await, MUST NOT touch Qt signals (cross-thread without
        ``QMetaObject.invokeMethod`` is undefined), and MUST NOT take
        any lock that the loop's thread might also hold. Read attributes,
        log, call the injected hard-exit.
        """
        self._hard_exit_fired = True
        snapshot = {
            "reason": self._state.reason,
            "phase": self._state.phase.value,
            "elapsed_s": self._clock() - self._state.start_mono,
            "active_run_id": self._state.active_run_id,
            "active_bundle_path": (
                str(self._state.active_bundle_path)
                if self._state.active_bundle_path is not None
                else None
            ),
            "pending_lifecycle_tasks": self._state.pending_lifecycle,
            "non_joined_workers": self._state.non_joined_workers,
        }
        _logger.error("shutdown.os_exit", **snapshot)
        self._hard_exit()


__all__ = [
    "ShutdownCoordinator",
    "ShutdownDeadlines",
    "ShutdownPhase",
    "ShutdownResult",
    "status_message_for_phase",
]
