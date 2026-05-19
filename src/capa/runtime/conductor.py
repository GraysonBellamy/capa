""":class:`Conductor` — per-run coordinator.

Owns the lifecycle of one run spawned through the runtime stack. The
conductor lives in **its own thread on its own asyncio loop**, separate
from the UI and from the pool's worker threads. This is what makes a
hung adapter (or a stalled writer fsync) unable to freeze the UI:
every cross-thread hand-off is bounded by a :class:`ThreadBridge` and
every blocking deadline is observed by the :class:`SaturationMonitor`.

Lifetime:

* Constructed per-run from a long-lived :class:`WorkerPool`. Does not own
  the pool — closing the conductor leaves the pool open for the next run.
* :meth:`start` spawns the conductor thread, builds the per-run resources
  via a caller-supplied :class:`RunSession`, arms the pool, opens drains
  before preflight, runs the procedure, and returns a :class:`RunHandle`
  once everything is up.
* :meth:`stop` initiates a cooperative drain. The procedure exits, workers
  disarm in parallel, the writer thread closes, and the bundle is finalized.
* The thread exits when :meth:`_run` returns; the run's result is published
  on a separate ``concurrent.futures.Future`` that the caller can await
  independently of the start-up handle.

Why a dedicated thread for the conductor:

* The UI loop must never block on serial I/O. The conductor's drain tasks
  call ``await writer.submit(...)`` and ``await databus.publish(...)``,
  both of which can park; doing that from the UI loop would defeat the
  whole runtime model.
* Headless and GUI flows share one code path: the conductor doesn't know
  whether a UI exists, so its design is invariant to that.

The conductor accepts a pluggable :class:`ConductorRunner`; the production
runner is :class:`~capa.runtime.procedure.ProcedureRunner` and tests inject
a no-op runner to exercise the conductor's lifecycle in isolation.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

import anyio
import structlog

from capa.core.databus import DataBus
from capa.devices.camera.base import CameraEvent, FrameReceipt
from capa.devices.records import ChannelSample
from capa.runtime.bridge import ThreadBridge
from capa.runtime.emissions import ProcedureTick, ProcedureUiSink, WorkerEmission
from capa.runtime.errors import ConductorStateError, PoolStateError
from capa.runtime.heartbeat import LoopLagMetric, heartbeat_task
from capa.runtime.recording import ResolvedRecordingPlan, resolve_recording_plan
from capa.runtime.saturation import (
    DEFAULT_POLL_PERIOD_S,
    DEFAULT_SATURATION_DEADLINE_S,
    SaturationEvent,
    SaturationMonitor,
    WriterSaturationSource,
)
from capa.runtime.state import ConductorState, conductor_edge_legal

if TYPE_CHECKING:
    from capa.devices.adapter import CommandResult, DeviceCommand
    from capa.devices.records import DeviceEmission
    from capa.experiment.config import ExperimentConfig, RuntimeConfig
    from capa.runtime.pool import WorkerPool
    from capa.runtime.runcontext import RunContext, WriterRef


_logger = structlog.get_logger("capa.runtime.conductor")


# ---------------------------------------------------------------------------
# Outcome categorization (mirrors today's EngineResult.run_status)
# ---------------------------------------------------------------------------


class RunOutcome(StrEnum):
    """How the run actually ended.

    Distinct from :class:`ConductorState` (which is "where am I in the
    lifecycle"). The bundle's ``run_status`` field is populated from this
    at finalize.
    """

    COMPLETED = "completed"
    """Procedure ran to natural completion."""

    ABORTED = "aborted"
    """Operator (or supervising code) called :meth:`stop` before completion."""

    CRASHED = "crashed"
    """Unhandled exception in procedure, drain task, or pool. Bundle is
    still sealed; the failure cause is recorded in events."""

    CRASHED_BUT_SEALED = "crashed_but_sealed"
    """Saturation deadline tripped. The conductor sealed the bundle anyway
    after disarming workers."""


# ---------------------------------------------------------------------------
# Per-run resource session
# ---------------------------------------------------------------------------


@runtime_checkable
class RunSession(Protocol):
    """Per-run resources, built by the caller, used by the conductor.

    The conductor uses this as an async context manager: :meth:`open`
    materializes the bundle / writer / clock and returns a
    :class:`RunContext`; :meth:`close` finalizes the bundle. The caller
    builds the session (and owns its lifecycle decisions like where the
    bundle lives) so the conductor stays focused on orchestration. The
    production session is :class:`RealRunSession` which wraps
    :class:`RunBundleWriter` + :class:`WriterThread`; tests use a fake.
    """

    @property
    def run_id(self) -> str:
        """Stable identifier; matches the bundle directory name."""
        ...

    @property
    def bundle_path(self) -> Path | None:
        """Filesystem path of the bundle, or ``None`` if the session hasn't
        materialized a bundle yet (e.g. in-memory test session)."""
        ...

    @property
    def config(self) -> ExperimentConfig:
        """The frozen run recipe. Read by the conductor at arm time for
        recording-plan resolution (``config.run_options.recording_policy``
        and ``config.hardware``). Valid pre- and post-:meth:`open`."""
        ...

    @property
    def saturation_source(self) -> WriterSaturationSource | None:
        """Writer-side saturation signal source. ``None`` when the session
        doesn't expose one (some tests)."""
        ...

    async def open(self) -> RunContext:
        """Materialize per-run resources. Returns the :class:`RunContext`
        that the conductor installs into every worker via ``pool.arm_all``.

        Failure here is fatal: the conductor cannot recover and the run
        ends in FAILED state. The session is responsible for cleaning up
        any partial resources before re-raising.
        """
        ...

    def set_outcome(self, outcome: RunOutcome, exit_reason: str | None) -> None:
        """Inform the session what to record at finalize. Called by the
        conductor before :meth:`close`. Set to ``COMPLETED`` by default
        until :meth:`close` is called.
        """
        ...

    async def close(self) -> None:
        """Finalize the bundle (Parquet rewrite, integrity, manifest seal)
        and close the writer thread. Idempotent; the conductor may call
        this after a partial open."""
        ...


# ---------------------------------------------------------------------------
# Runner protocol — implemented by ProcedureRunner.
# ---------------------------------------------------------------------------


@runtime_checkable
class ConductorRunner(Protocol):
    """The "procedure body" surface — what the conductor calls between
    workers-armed and workers-disarmed.

    Tests inject :class:`NoOpRunner` to exercise arm / drain / disarm in
    isolation; production wires :class:`~capa.runtime.procedure.ProcedureRunner`.

    All methods receive the per-run :class:`RunContext` and the conductor's
    authoritative :class:`DataBus` so procedure subscribers can wait for
    samples.
    """

    async def preflight(self, ctx: RunContext, bus: DataBus) -> None:
        """Dynamic preflight — run AFTER drains are pumping samples into
        the bus. Blocking failures here mean the run never enters RUNNING.
        """
        ...

    async def run(self, ctx: RunContext, bus: DataBus) -> None:
        """Drive the procedure to completion. Returning normally signals
        "procedure complete"; raising signals "procedure crashed". The
        conductor surfaces both into :class:`RunOutcome`.

        Cancellation (e.g. operator stop) is delivered via
        :class:`asyncio.CancelledError`; the runner must propagate it.
        """
        ...


class NoOpRunner:
    """Smallest viable :class:`ConductorRunner`.

    Used by tests to exercise the conductor's lifecycle without pulling
    :class:`MethodExecutor` into scope. :meth:`run` parks until cancelled
    (i.e. until :meth:`Conductor.stop` is called) or until the test's stop
    hook fires.
    """

    def __init__(self, *, run_for_s: float | None = None) -> None:
        """:param run_for_s: If set, the runner returns normally after this
        many seconds (simulates a procedure that finishes on its own). If
        ``None``, blocks until cancelled."""
        self._run_for_s = run_for_s
        self.preflight_calls = 0
        self.run_calls = 0

    async def preflight(self, ctx: RunContext, bus: DataBus) -> None:
        self.preflight_calls += 1

    async def run(self, ctx: RunContext, bus: DataBus) -> None:
        self.run_calls += 1
        if self._run_for_s is None:
            # Park forever; the conductor's task group cancel scope ends us.
            await asyncio.Event().wait()
        else:
            await asyncio.sleep(self._run_for_s)


# ---------------------------------------------------------------------------
# Configuration & result types
# ---------------------------------------------------------------------------


DEFAULT_SHUTDOWN_GRACE_S: Final[float] = 5.0
"""How long the conductor waits for each worker to drain before forcing
the shutdown protocol. Per-worker; the slowest worker bounds the overall
disarm time."""


@dataclass(frozen=True, slots=True)
class ConductorConfig:
    """Per-run knobs, all with sensible defaults so tests can omit them.

    Splits responsibilities with
    :class:`~capa.experiment.config.RuntimeConfig`:

    * ``RuntimeConfig`` carries the user-tunable knobs an operator may
      reasonably want to set per experiment (``shutdown_grace_s``,
      ``ui_bridge_capacity``, ``loop_lag_warn_ms``).
    * ``ConductorConfig`` carries the internal saturation-monitor
      timing in addition. The saturation knobs are not user-facing
      today — promote them to ``RuntimeConfig`` when a real experiment
      asks for it.

    :meth:`from_runtime` builds a :class:`ConductorConfig` by copying the
    user-tunable knobs off a :class:`RuntimeConfig` while keeping the
    saturation defaults.
    """

    saturation_deadline_s: float = DEFAULT_SATURATION_DEADLINE_S
    saturation_poll_period_s: float = DEFAULT_POLL_PERIOD_S
    shutdown_grace_s: float = DEFAULT_SHUTDOWN_GRACE_S
    loop_lag_warn_ms: float = 50.0

    @classmethod
    def from_runtime(
        cls,
        runtime: RuntimeConfig,
        *,
        saturation_deadline_s: float = DEFAULT_SATURATION_DEADLINE_S,
        saturation_poll_period_s: float = DEFAULT_POLL_PERIOD_S,
    ) -> ConductorConfig:
        """Build a :class:`ConductorConfig` from the user-facing
        :class:`RuntimeConfig`.

        Saturation parameters remain code defaults — overridable here
        only because the headless CLI currently exposes
        ``saturation_deadline_s`` as a top-level flag.
        """
        return cls(
            saturation_deadline_s=saturation_deadline_s,
            saturation_poll_period_s=saturation_poll_period_s,
            shutdown_grace_s=runtime.shutdown_grace_s,
            loop_lag_warn_ms=runtime.loop_lag_warn_ms,
        )


@dataclass(frozen=True, slots=True)
class RunHandle:
    """Returned from :meth:`Conductor.start` once the run is fully up."""

    run_id: str
    bundle_path: Path | None
    started_mono_ns: int


@dataclass(frozen=True, slots=True)
class RunResult:
    """Final outcome — published on :attr:`Conductor.result_future`."""

    run_id: str
    bundle_path: Path | None
    outcome: RunOutcome
    exit_reason: str | None
    final_state: ConductorState
    saturation_event: SaturationEvent | None
    started_mono_ns: int
    ended_mono_ns: int


# ---------------------------------------------------------------------------
# The Conductor itself
# ---------------------------------------------------------------------------


class Conductor:
    """Per-run coordinator. See module docstring for the big picture.

    Construction is cheap — no thread is spawned and no I/O happens until
    :meth:`start` is called. The pool and session are captured by
    reference; both must outlive the run.
    """

    __slots__ = (
        "_bridges",
        "_completion_event",
        "_config",
        "_databus",
        "_drain_count_observed",
        "_ended_mono_ns",
        "_exit_reason",
        "_handle_future",
        "_heartbeat_stop",
        "_loop",
        "_loop_lag",
        "_outcome",
        "_pool",
        "_pool_armed",
        "_pre_completion_callback",
        "_recording_plan",
        "_result_future",
        "_run_context",
        "_runner",
        "_runner_factory",
        "_saturation_event",
        "_session",
        "_session_closed",
        "_started_mono_ns",
        "_state",
        "_stop_requested",
        "_thread",
        "_thread_name",
        "_ui_bridge",
    )

    def __init__(
        self,
        *,
        pool: WorkerPool,
        session: RunSession,
        runner: ConductorRunner | None = None,
        runner_factory: Callable[[RunSession, RunContext], ConductorRunner] | None = None,
        config: ConductorConfig | None = None,
        thread_name: str = "capa-conductor",
        # Test seam — called on the conductor loop just before the
        # completion-event handshake. Tests use it to assert intermediate
        # state (e.g. "drain has seen N emissions"). Don't use in prod.
        pre_completion_callback: Any = None,
    ) -> None:
        if runner is not None and runner_factory is not None:
            raise ValueError("Conductor: provide `runner` OR `runner_factory`, not both")
        self._pool = pool
        self._session = session
        # `_runner` is the eventual ConductorRunner instance. When `runner` is
        # supplied at construction time we use it directly; when only
        # `runner_factory` is supplied we defer construction until
        # `session.open()` returns the RunContext, so the factory can wire
        # the runner against per-run resources (e.g. an open bundle writer).
        # Tests usually pass `runner` directly; production headless wiring
        # uses `runner_factory`.
        self._runner = runner
        self._runner_factory = runner_factory
        self._config = config or ConductorConfig()
        self._thread_name = thread_name
        self._pre_completion_callback = pre_completion_callback

        self._state: ConductorState = ConductorState.PREPARING
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._handle_future: Future[RunHandle] = concurrent.futures.Future()
        self._result_future: Future[RunResult] = concurrent.futures.Future()
        self._completion_event: asyncio.Event | None = None
        self._stop_requested = False
        self._outcome: RunOutcome = RunOutcome.COMPLETED
        self._exit_reason: str | None = None
        self._saturation_event: SaturationEvent | None = None
        self._started_mono_ns = 0
        self._ended_mono_ns = 0
        self._databus: DataBus | None = None
        self._run_context: RunContext | None = None
        self._recording_plan: ResolvedRecordingPlan | None = None
        self._bridges: dict[str, ThreadBridge[WorkerEmission]] = {}
        self._drain_count_observed = 0
        # UI bridge — optional Conductor → UI thread channel. Attached by
        # the UI before :meth:`start`; the drain task ``put_nowait``s every
        # emission after the writer/databus hop. Headless paths leave this
        # ``None`` and pay nothing for it.
        self._ui_bridge: ThreadBridge[WorkerEmission] | None = None
        # Loop-lag observability. A heartbeat task measures wake-up lag
        # against a 50 ms cadence; the percentile ring lands in the runtime
        # diagnostics emitted at finalize.
        self._loop_lag = LoopLagMetric(name="conductor")
        self._heartbeat_stop: anyio.Event | None = None
        # Idempotency flags for the unconditional cleanup callbacks. The
        # success path runs disarm + close explicitly; the AsyncExitStack
        # callbacks only fire on the failure-unwind path and skip if the
        # success path already ran them.
        self._pool_armed = False
        self._session_closed = False

    # ------------------------------------------------------------------ properties

    @property
    def state(self) -> ConductorState:
        """Atomic read; advisory. The actual transition happens on the
        conductor loop, but state writes are single-int stores."""
        return self._state

    @property
    def run_id(self) -> str:
        """Stable identifier of the run managed by this conductor."""
        return self._session.run_id

    @property
    def bundle_path(self) -> Path | None:
        """Filesystem path for the run bundle once the session has opened."""
        return self._session.bundle_path

    @property
    def databus(self) -> DataBus | None:
        """The authoritative :class:`DataBus`. ``None`` before :meth:`start`
        has reached the bus-construction step. Procedure subscribers and
        external analyzers attach here."""
        return self._databus

    @property
    def result_future(self) -> Future[RunResult]:
        """Resolves with the final :class:`RunResult` when the conductor
        thread exits. Independent of the start-up handle future — callers
        that only care about the end can ignore :meth:`start`'s return.
        """
        return self._result_future

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """The conductor's loop, or ``None`` before :meth:`start`."""
        return self._loop

    def runtime_diagnostics(self) -> dict[str, Any]:
        """Snapshot the per-loop / per-bridge / per-worker metrics.

        The dict shape is intended for ``manifest.queue_health`` consumption
        (one entry per queue/bridge/worker keyed by tag), so the existing
        manifest schema works without extension:

        * ``loop.conductor`` — conductor-loop lag percentiles.
        * ``loop.worker:<resource_id>`` — per-worker loop lag (zeros for
          now; workers expose their own LoopLagMetric and a future step
          plumbs it through here).
        * ``bridge.outbound:<resource_id>`` — per-worker outbound bridge
          latency + blocked time.
        * ``worker:<resource_id>`` — tick / poll durations, loop lag,
          command counts, last-sample age.

        Returns an empty dict before :meth:`_run` enters its task group
        (i.e. before bridges and workers exist).
        """
        out: dict[str, dict[str, float]] = {}
        # Runtime tunables — included so status-bar / observers can color
        # health values against the configured thresholds without having
        # to import ConductorConfig defaults.
        out["runtime"] = {
            "saturation_deadline_s": float(self._config.saturation_deadline_s),
            "loop_lag_warn_ms": float(self._config.loop_lag_warn_ms),
        }
        # Conductor loop lag.
        out["loop.conductor"] = {
            "samples": float(self._loop_lag.samples_total),
            "lag_p50_ms": self._loop_lag.p50_ms,
            "lag_p99_ms": self._loop_lag.p99_ms,
            "lag_max_ms": self._loop_lag.max_lag_ms,
        }
        # Per-worker bridges (outbound).
        for rid, bridge in self._bridges.items():
            m = bridge.metrics
            blocked_now = m.blocked_since_ms
            out[f"bridge.outbound:{rid}"] = {
                "depth": float(m.depth),
                "depth_max": float(m.depth_max),
                "capacity": float(bridge.capacity),
                "enqueued_total": float(m.enqueued_total),
                "dequeued_total": float(m.dequeued_total),
                "dropped_total": float(m.dropped_total),
                "blocked_total_ms": float(m.blocked_total_ms),
                "blocked_since_ms": float(blocked_now) if blocked_now is not None else -1.0,
                "latency_p50_ms": float(m.latency_p50_ms),
                "latency_p99_ms": float(m.latency_p99_ms),
            }
        # Per-worker tick/loop metrics. Per-adapter failure policy
        # (``WorkerMetrics.on_failure``) is intentionally not serialized
        # into this float-valued diagnostics block. # TODO(watchdog):
        # per-device stream-silence/fatal-error enforcement will attach
        # here, using the resolved policy metadata on each worker.
        for rid, worker in self._pool.workers.items():
            wm = worker.metrics
            out[f"worker:{rid}"] = {
                "samples_emitted": float(wm.samples_emitted),
                "polls_emitted": float(wm.polls_emitted),
                "commands_total": float(wm.commands_total),
                "commands_failed": float(wm.commands_failed),
                "tick_duration_p50_ms": float(wm.tick_duration_p50_ms),
                "tick_duration_p99_ms": float(wm.tick_duration_p99_ms),
                "poll_period_p50_ms": float(wm.poll_period_p50_ms),
                "poll_period_p99_ms": float(wm.poll_period_p99_ms),
                "poll_rate_hz": float(wm.poll_rate_hz),
                "loop_lag_p99_ms": float(wm.loop_lag_ms_p99),
                "last_sample_age_s": float(wm.last_sample_age_s),
            }
        return out

    @property
    def completion_event(self) -> asyncio.Event | None:
        """The conductor's loop-local shutdown signal.

        Set when the run completes (naturally, via stop, or saturation).
        Used by :class:`ProcedureRunner` to wire a loop-local
        ``external_stop`` event for procedures that ``await`` on it.

        ``None`` before :meth:`_run` enters its task group.
        """
        return self._completion_event

    # ------------------------------------------------------------------ sync facade

    def start(self) -> Future[RunHandle]:
        """Spawn the conductor thread and begin the run.

        Returns a future that resolves with :class:`RunHandle` once the
        run reaches RUNNING state (procedure has started). On failure
        before RUNNING, the handle future rejects and the result future
        resolves with a CRASHED outcome.

        Idempotent only in the failure direction — a second call after
        :meth:`start` was invoked raises :class:`ConductorStateError`.
        """
        if self._thread is not None:
            raise ConductorStateError("Conductor.start() called twice", current=self._state)
        self._thread = threading.Thread(
            target=self._thread_main, name=self._thread_name, daemon=False
        )
        self._thread.start()
        return self._handle_future

    def stop(self, *, reason: str = "operator_stop") -> Future[RunResult]:
        """Request a cooperative shutdown.

        Sets the completion event on the conductor loop and returns the
        same future as :attr:`result_future` for convenience. Idempotent:
        subsequent calls observe the existing shutdown but the recorded
        ``exit_reason`` is the first caller's.
        """
        if self._stop_requested:
            return self._result_future
        self._stop_requested = True
        self._exit_reason = reason
        # Default outcome assumes operator-initiated; the conductor may
        # override to CRASHED / CRASHED_BUT_SEALED if it discovers the
        # real cause first.
        if self._outcome is RunOutcome.COMPLETED:
            self._outcome = RunOutcome.ABORTED
        loop = self._loop
        ev = self._completion_event
        if loop is not None and ev is not None and not ev.is_set():
            loop.call_soon_threadsafe(ev.set)
        return self._result_future

    def dispatch(self, device: str, cmd: DeviceCommand) -> Future[CommandResult]:
        """Run-time command dispatch.

        Refused outside PREPARING / RUNNING. Routes through the pool to
        the worker hosting ``device``.
        """
        if not self._state.permits_dispatch():
            raise ConductorStateError(
                f"dispatch refused in state {self._state}", current=self._state
            )
        return self._pool.dispatch(device, cmd)

    def snapshot(self, device: str) -> Future[DeviceEmission]:
        """One-shot snapshot via the pool's worker for ``device``.

        Same state gate as :meth:`dispatch`.
        """
        if not self._state.permits_dispatch():
            raise ConductorStateError(
                f"snapshot refused in state {self._state}", current=self._state
            )
        return self._pool.snapshot(device)

    def attach_ui_bridge(self, bridge: ThreadBridge[WorkerEmission]) -> None:
        """Wire a Conductor → UI :class:`ThreadBridge`.

        Must be called BEFORE :meth:`start` so the drain task picks the
        reference up on first dispatch. Headless callers omit this; the
        drain stays a writer-and-bus-only fan-out.

        The UI side owns ``attach_consumer``/``attach_producer``: the
        producer is the conductor's loop, so we register that here in a
        deferred manner — the actual ``attach_producer`` call lands inside
        :meth:`_run` once the conductor's loop is running. The UI side
        calls ``attach_consumer`` from its own loop after attaching.
        """
        if self._thread is not None:
            raise ConductorStateError(
                "attach_ui_bridge must be called before start()",
                current=self._state,
            )
        self._ui_bridge = bridge

    def procedure_ui_sink(self) -> ProcedureUiSink:
        """Return a UI-only sink procedures can publish
        :class:`~capa.runtime.emissions.ProcedureTick` payloads through.

        Wraps :meth:`_publish_ui` so the procedure layer stays bridge-
        agnostic: a tick goes to the UI mirror with no writer write,
        no data-bus publish. Safe to call before :meth:`start` —
        no-op when no UI bridge is attached (headless run); the sink
        becomes live the moment :meth:`attach_ui_bridge` runs.

        The returned sink captures ``self`` and is intended to be
        invoked from the conductor loop (where the procedure runs).
        Calling from a different loop is undefined; the underlying
        bridge's ``put_nowait`` is thread-safe but the cross-thread
        timestamp ordering would be unreliable.
        """
        return _ConductorProcedureUiSink(conductor=self)

    # ------------------------------------------------------------------ thread body

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        structlog.contextvars.bind_contextvars(
            thread="conductor",
            run_id=self._session.run_id,
        )
        self._started_mono_ns = time.monotonic_ns()
        try:
            loop.run_until_complete(self._run())
        except BaseException as exc:
            _logger.error(
                "conductor.thread_main_crashed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            self._outcome = RunOutcome.CRASHED
            self._exit_reason = f"conductor_thread_crashed: {exc!r}"
            self._transition(ConductorState.FAILED)
            if not self._handle_future.done():
                self._handle_future.set_exception(exc)
        finally:
            self._ended_mono_ns = time.monotonic_ns()
            self._publish_result()
            try:
                loop.close()
            finally:
                self._loop = None
            structlog.contextvars.unbind_contextvars("thread", "run_id")

    # ------------------------------------------------------------------ orchestration

    async def _run(self) -> None:
        """The full per-run lifecycle on the conductor loop.

        Drain-before-preflight ordering is enforced explicitly: drain
        tasks start spinning **before** the runner's preflight runs, so
        any dynamic preflight subscriber sees live samples.
        """
        self._completion_event = asyncio.Event()
        if self._stop_requested:
            self._completion_event.set()
        self._databus = DataBus()
        self._databus.bind_loop(asyncio.get_running_loop())
        # If the UI attached a bridge before start(), bind its producer side
        # to this loop. The UI side has already (or will shortly) call
        # ``attach_consumer`` on its own loop; the two sides are independent.
        if self._ui_bridge is not None:
            self._ui_bridge.attach_producer(asyncio.get_running_loop())

        async with AsyncExitStack() as stack:
            try:
                # 1. Materialize per-run resources (bundle, writer, clock).
                ctx = await self._session.open()
                stack.push_async_callback(self._close_session_unconditional)
                self._run_context = ctx

                # 1b. Late-bind the runner if a factory was supplied. The
                # factory receives the open session + ctx so it can wire
                # itself against per-run resources (e.g. the bundle writer).
                if self._runner is None:
                    if self._runner_factory is not None:
                        self._runner = self._runner_factory(self._session, ctx)
                    else:
                        self._runner = NoOpRunner()

                # 1c. Resolve the recording plan. Runner exists; RunContext
                # is not yet handed to workers. The procedure's plan_capture
                # (if it has one) and the operator's run-options policy
                # collapse into the final plan, which is the source of
                # truth for both the camera-adapter suppression flag and
                # the conductor's dispatch gate.
                resolved_plan = self._resolve_recording_plan(default_plan=ctx.recording_plan)
                ctx = replace(ctx, recording_plan=resolved_plan)
                self._run_context = ctx
                self._recording_plan = resolved_plan

                # 1d. Snapshot the plan into the bundle manifest. Best-
                # effort: test sessions may not implement this hook.
                update_plan = getattr(self._session, "update_recording_plan", None)
                if update_plan is not None:
                    try:
                        update_plan(resolved_plan)
                    except BaseException as exc:
                        _logger.warning(
                            "conductor.recording_plan_snapshot_failed",
                            error=str(exc),
                            error_type=type(exc).__name__,
                        )

                # 2. Arm all workers with the same RunContext.
                await self._pool.arm_all(ctx)
                self._pool_armed = True
                # On any failure past this point, we MUST disarm to release
                # the pool back to IDLE for the next run.
                stack.push_async_callback(self._disarm_unconditional)

                # 3. Begin sampling — bridges land on this loop as the
                # consumer side. Pool returns one bridge per worker.
                bridges = await self._pool.begin_sampling_all(
                    consumer_loop=asyncio.get_running_loop()
                )
                self._bridges = bridges
            except BaseException as exc:
                _logger.error(
                    "conductor.preparation_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                self._outcome = RunOutcome.CRASHED
                if self._exit_reason is None:
                    self._exit_reason = f"preparation_failed: {exc!r}"
                if not self._handle_future.done():
                    self._handle_future.set_exception(exc)
                self._transition(ConductorState.FAILED)
                return

            # 4. Spawn drain tasks BEFORE preflight.
            #    Use anyio task group — uniform with the rest of the
            #    codebase and propagates exceptions cleanly.
            try:
                async with anyio.create_task_group() as tg:
                    # Heartbeat task — observes loop lag on the conductor's
                    # loop. The stop event is cancelled by the task-group
                    # teardown on shutdown.
                    self._heartbeat_stop = anyio.Event()
                    tg.start_soon(heartbeat_task, self._loop_lag, self._heartbeat_stop)

                    saturation = self._build_saturation_monitor()
                    tg.start_soon(saturation.run)

                    for rid, bridge in bridges.items():
                        tg.start_soon(self._drain_worker, rid, bridge)

                    # 5. Run dynamic preflight on this loop — samples are
                    #    already flowing into the databus.
                    try:
                        await self._runner.preflight(ctx, self._databus)
                    except BaseException as exc:
                        _logger.error(
                            "conductor.preflight_failed",
                            error=str(exc),
                            error_type=type(exc).__name__,
                        )
                        self._outcome = RunOutcome.CRASHED
                        self._exit_reason = f"preflight_failed: {exc!r}"
                        self._transition(ConductorState.FAILED)
                        if not self._handle_future.done():
                            self._handle_future.set_exception(exc)
                        self._completion_event.set()
                        tg.cancel_scope.cancel()
                        return

                    # 6. Enter RUNNING. The start-handle future resolves
                    #    here — callers waiting on it now learn the run is
                    #    up.
                    self._transition(ConductorState.RUNNING)
                    if not self._handle_future.done():
                        self._handle_future.set_result(
                            RunHandle(
                                run_id=self._session.run_id,
                                bundle_path=self._session.bundle_path,
                                started_mono_ns=self._started_mono_ns,
                            )
                        )

                    # 7. Spawn the procedure runner.
                    tg.start_soon(self._procedure_task)

                    # 8. Wait for completion (procedure-done OR stop OR
                    #    saturation). Test hook fires just before the wait
                    #    so a test can inspect drain state mid-flight.
                    if self._pre_completion_callback is not None:
                        await self._pre_completion_callback(self)
                    await self._completion_event.wait()
                    # Stop the heartbeat first so a final no-op lag sample
                    # doesn't get observed during teardown.
                    if self._heartbeat_stop is not None:
                        self._heartbeat_stop.set()
                    tg.cancel_scope.cancel()
            except BaseException as exc:
                # Anything escaping the task group is an unhandled crash.
                _logger.error(
                    "conductor.task_group_crashed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                if self._outcome is RunOutcome.COMPLETED:
                    self._outcome = RunOutcome.CRASHED
                if self._exit_reason is None:
                    self._exit_reason = f"task_group_crashed: {exc!r}"

            # 9. Drain — disarm workers in parallel.
            self._transition(ConductorState.DRAINING)
            disarm_results = await self._pool.disarm_all(grace_s=self._config.shutdown_grace_s)
            self._pool_armed = False
            for rid, dr in disarm_results.items():
                _logger.info(
                    "conductor.worker_disarmed",
                    resource_id=rid,
                    result=str(dr),
                )

            # 10. Finalize — close the session (writer + bundle).
            self._transition(ConductorState.FINALIZING)
            self._session.set_outcome(self._outcome, self._exit_reason)
            # Hand the runtime diagnostics to the session so finalize can
            # record them in the bundle manifest. Best-effort: the session
            # may ignore them if it doesn't support the hook.
            try:
                setter = getattr(self._session, "set_runtime_diagnostics", None)
                if setter is not None:
                    setter(self.runtime_diagnostics())
            except BaseException as exc:
                _logger.warning(
                    "conductor.diagnostics_publish_failed",
                    error=str(exc),
                )
            await self._session.close()
            self._session_closed = True

            self._transition(ConductorState.SEALED)

            # Close the UIBridge last so the UI drain task sees end-of-stream
            # (sentinel) and exits cleanly. The UI may have already torn down
            # its drain on window close — `close()` is idempotent.
            if self._ui_bridge is not None and not self._ui_bridge.closed:
                self._ui_bridge.close()

    async def _drain_worker(self, resource_id: str, bridge: ThreadBridge[WorkerEmission]) -> None:
        """One drain coroutine per worker bridge.

        Routes each emission by runtime type:

        * :class:`FrameReceipt` → :meth:`WriterThread.record_frame` (wraps
          in :class:`FrameItem`); skips databus — cameras do not
          participate in the procedure-side bus, matching today's engine
          behavior where frame receipts never reached the bus.
        * :class:`CameraEvent` → :meth:`WriterThread.write_event` with
          ``kind=f"camera.{event.kind}"`` and ``source=f"camera:{event.name}"``;
          camera events are written to the bundle but not published to the bus.
        * Everything else (the :data:`DeviceEmission` union) → durable
          submit + databus publish, same as the device-only path.

        The await on `bus.publish` honors per-subscription backpressure
        (BLOCK / ABORT_RUN); a sustained block surfaces as drain blocking
        and the saturation deadline catches it.

        Cancellation is honoured by the underlying ``bridge.get`` — when
        the bridge closes (worker disarmed) or the task group cancels,
        this coroutine returns.
        """
        assert self._run_context is not None
        assert self._databus is not None
        writer = self._run_context.writer
        bus = self._databus
        try:
            async for emission in bridge:
                self._drain_count_observed += 1
                await self._dispatch_emission(emission, writer=writer, bus=bus)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            _logger.error(
                "conductor.drain_failed",
                resource_id=resource_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            # A failing drain marks the run crashed but lets the conductor
            # finish its shutdown — never silently dies.
            if self._outcome is RunOutcome.COMPLETED:
                self._outcome = RunOutcome.CRASHED
            if self._exit_reason is None:
                self._exit_reason = f"drain_failed[{resource_id}]: {exc!r}"
            if self._completion_event is not None and not self._completion_event.is_set():
                self._completion_event.set()

    def _resolve_recording_plan(
        self, *, default_plan: ResolvedRecordingPlan
    ) -> ResolvedRecordingPlan:
        """Resolve the per-run recording plan.

        Only the procedure-wrapping runner (``ProcedureRunner``) exposes
        a ``procedure`` attribute; ``NoOpRunner`` and engine tests don't,
        so they fall through to the default full-rig plan via
        :func:`resolve_recording_plan`'s ``procedure=None`` branch.

        Returns ``default_plan`` unchanged when the session doesn't carry
        an :class:`ExperimentConfig` (test sessions that pre-date the
        plan-resolution path). Production :class:`RealRunSession` always
        exposes one.
        """
        config = getattr(self._session, "config", None)
        if config is None:
            return default_plan
        procedure = getattr(self._runner, "procedure", None)
        return resolve_recording_plan(
            hardware=config.hardware,
            procedure=procedure,
            policy=config.run_options.recording_policy,
        )

    async def _dispatch_emission(
        self,
        emission: WorkerEmission,
        *,
        writer: WriterRef,
        bus: DataBus,
    ) -> None:
        """Route one emission by runtime type. Single source of truth for
        the writer + databus fan-out so worker code stays type-blind.

        Camera emissions (:class:`FrameReceipt`, :class:`CameraEvent`) get
        their own writer methods and skip the procedure-side databus —
        nothing on the bus would subscribe to a frame receipt, so the
        databus carries only :class:`SourceRecord` / :class:`ChannelSample`.

        The resolved :class:`~capa.runtime.recording.ResolvedRecordingPlan`
        gates writer calls for :class:`ChannelSample` and
        :class:`FrameReceipt`. Three invariants enforced here:

        1. **DataBus is never filtered.** Procedure subscribers, the
           safety monitor, and :class:`MethodExecutor` all read live
           samples from the bus. Suppressing disk recording must not
           weaken safety.
        2. **UI mirror is never filtered.** Live plots and preview tiles
           paint regardless of what's recorded.
        3. **SourceRecord is not filtered in v1.** Adapter-native records
           bundle multiple channels' data in their row dict; column
           projection is invasive and the storage savings are small
           (tens of MB max). The manifest declares
           ``native_device_records="all"`` to make this explicit.
        4. **CameraEvent is not filtered.** Camera errors / status
           messages are tiny and diagnostically useful even when the
           camera's video is suppressed.
        """
        plan = self._recording_plan
        if isinstance(emission, ProcedureTick):
            # UI-only mirror: ticks never reach the writer or the data
            # bus by design. Soft-fail on bridge errors — losing a UI
            # frame must not crash the run.
            self._publish_ui(emission)
            return
        if isinstance(emission, FrameReceipt):
            # The camera adapter never opened its output file when
            # suppressed, so a FrameReceipt for a suppressed camera
            # would be impossible in normal operation. Belt-and-braces
            # — still publish to the UI so a misrouted frame is at
            # least visible.
            if plan is None or plan.allows_camera(emission.name):
                await writer.record_frame(emission)
            self._publish_ui(emission)
            return
        if isinstance(emission, CameraEvent):
            await writer.write_camera_event(
                kind=f"camera.{emission.kind}",
                message=emission.message,
                severity=emission.severity,
                source=f"camera:{emission.name}",
                t_mono_ns=emission.t_mono_ns,
                t_utc=emission.t_utc,
                metadata=dict(emission.metadata),
            )
            self._publish_ui(emission)
            return
        # Durable side first — losing an emission off-disk is worse than
        # losing it off the bus. Channel samples are gated; SourceRecord
        # always passes through (invariant 3).
        if (
            isinstance(emission, ChannelSample)
            and plan is not None
            and not plan.allows_channel(emission.channel)
        ):
            pass
        else:
            await writer.submit(emission)
        await bus.publish(emission)
        # UI mirror last — DROP_OLDEST under load. `publish_nowait` on the
        # UI side because the UI loop cannot honor blocking subscriber
        # backpressure.
        self._publish_ui(emission)

    def _publish_ui(self, emission: WorkerEmission) -> None:
        """Forward ``emission`` to the UIBridge if one is attached.

        Soft-fail: a bridge enqueue failure must not abort the run — the
        UI is a non-essential consumer. Errors are logged once.
        """
        bridge = self._ui_bridge
        if bridge is None or bridge.closed:
            return
        try:
            bridge.put_nowait(emission)
        except Exception as exc:
            _logger.warning(
                "conductor.ui_bridge_put_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    async def _procedure_task(self) -> None:
        """Wraps :meth:`ConductorRunner.run` and signals completion when
        it returns or raises.

        Cancellation propagates as :class:`asyncio.CancelledError` — the
        runner is expected to be cancel-safe (i.e. honour it within a
        reasonable window).
        """
        assert self._run_context is not None
        assert self._databus is not None
        runner = self._runner
        assert runner is not None
        try:
            await runner.run(self._run_context, self._databus)
            # Natural completion: leave outcome at COMPLETED unless a peer
            # task already escalated.
            if self._exit_reason is None:
                self._exit_reason = "procedure_complete"
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            _logger.error(
                "conductor.procedure_crashed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            if self._outcome is RunOutcome.COMPLETED:
                self._outcome = RunOutcome.CRASHED
            if self._exit_reason is None:
                self._exit_reason = f"procedure_crashed: {exc!r}"
        finally:
            if self._completion_event is not None and not self._completion_event.is_set():
                self._completion_event.set()

    def _build_saturation_monitor(self) -> SaturationMonitor:
        return SaturationMonitor(
            bridges=self._bridges,
            writer=self._session.saturation_source,
            on_saturated=self._on_saturated,
            deadline_s=self._config.saturation_deadline_s,
            poll_period_s=self._config.saturation_poll_period_s,
            stop_event=self._completion_event,
        )

    async def _on_saturated(self, event: SaturationEvent) -> None:
        """Called by the :class:`SaturationMonitor` when a deadline trips.

        Records the event on the conductor, marks the run as
        ``CRASHED_BUT_SEALED``, and fires the completion event so normal
        shutdown takes over (which still runs ``adapter.stop()`` per
        worker — hardware does not stay in an inconsistent state).
        """
        _logger.error(
            "conductor.saturation_escalation",
            reason=event.reason,
        )
        self._saturation_event = event
        self._outcome = RunOutcome.CRASHED_BUT_SEALED
        if self._exit_reason is None:
            self._exit_reason = event.reason
        # Best-effort write into the bundle event log; soft-fail because
        # the writer itself may be the wedged component.
        try:
            ctx = self._run_context
            if ctx is not None:
                await ctx.writer.write_event(
                    kind="saturation_deadline",
                    message=event.reason,
                    metadata=dict(event.details),
                )
        except BaseException as write_exc:
            _logger.warning(
                "conductor.saturation_event_write_failed",
                reason=event.reason,
                error=str(write_exc),
            )
        if self._completion_event is not None and not self._completion_event.is_set():
            self._completion_event.set()

    # ------------------------------------------------------------------ helpers

    async def _disarm_unconditional(self) -> None:
        """AsyncExitStack callback: disarm pool if we're still holding it.

        Used on early failures before the main drain path. The
        ``_pool_armed`` flag prevents a double-disarm when the success
        path already ran it.
        """
        if not self._pool_armed:
            return
        try:
            results = await self._pool.disarm_all(grace_s=self._config.shutdown_grace_s)
            self._pool_armed = False
            _logger.info(
                "conductor.unconditional_disarm",
                results={r: str(v) for r, v in results.items()},
            )
        except PoolStateError:
            # Pool was already closed; nothing to do.
            self._pool_armed = False
        except BaseException as exc:
            _logger.error(
                "conductor.unconditional_disarm_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    async def _close_session_unconditional(self) -> None:
        """AsyncExitStack callback: close the session on early failure.

        On the success path we close explicitly inside :meth:`_run` and
        set ``_session_closed=True``; this callback is then a no-op.
        """
        if self._session_closed:
            return
        try:
            self._session.set_outcome(self._outcome, self._exit_reason)
            # Hand the runtime diagnostics to the session so finalize can
            # record them in the bundle manifest. Best-effort: the session
            # may ignore them if it doesn't support the hook.
            try:
                setter = getattr(self._session, "set_runtime_diagnostics", None)
                if setter is not None:
                    setter(self.runtime_diagnostics())
            except BaseException as exc:
                _logger.warning(
                    "conductor.diagnostics_publish_failed",
                    error=str(exc),
                )
            await self._session.close()
            self._session_closed = True
        except BaseException as exc:
            _logger.error(
                "conductor.session_close_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def _transition(self, new_state: ConductorState) -> None:
        """Atomic-ish state advance. Records illegal edges as errors but
        does NOT raise — the conductor is on its shutdown path and we'd
        rather seal the bundle than crash deeper.
        """
        old = self._state
        if old is new_state:
            return
        if not conductor_edge_legal(old, new_state):
            _logger.error(
                "conductor.illegal_transition",
                from_state=str(old),
                to_state=str(new_state),
            )
        self._state = new_state
        _logger.debug("conductor.state", state=str(new_state))

    def _publish_result(self) -> None:
        """Resolve :attr:`result_future` with the final :class:`RunResult`.

        Always called from the conductor thread's finalize step; safe to
        call multiple times (the future ignores duplicate ``set_result``).
        """
        if self._result_future.done():
            return
        result = RunResult(
            run_id=self._session.run_id,
            bundle_path=self._session.bundle_path,
            outcome=self._outcome,
            exit_reason=self._exit_reason,
            final_state=self._state,
            saturation_event=self._saturation_event,
            started_mono_ns=self._started_mono_ns,
            ended_mono_ns=self._ended_mono_ns,
        )
        self._result_future.set_result(result)

    # ------------------------------------------------------------------ test helpers

    def join(self, timeout: float | None = None) -> bool:
        """Block until the conductor thread exits.

        Returns ``True`` if the thread joined within ``timeout``, ``False``
        otherwise. Intended for tests and for the CLI driver which awaits
        the result future synchronously."""
        if self._thread is None:
            return True
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()


@dataclass(frozen=True, slots=True)
class _ConductorProcedureUiSink:
    """Tiny adapter satisfying :class:`ProcedureUiSink` against a
    :class:`Conductor`. Captures the conductor instance and forwards
    :class:`ProcedureTick`\\ s to :meth:`Conductor._publish_ui`.

    The conductor's existing soft-fail discipline applies — a closed
    bridge or a put failure is logged but never raised, so a UI
    disconnect mid-run cannot crash the procedure.
    """

    conductor: Conductor

    def publish(self, tick: ProcedureTick) -> None:
        self.conductor._publish_ui(tick)


__all__ = [
    "Conductor",
    "ConductorConfig",
    "ConductorRunner",
    "NoOpRunner",
    "RunHandle",
    "RunOutcome",
    "RunResult",
    "RunSession",
]
