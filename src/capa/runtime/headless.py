""":func:`run_headless` — the conductor-based headless entry point.

Replaces :meth:`ExperimentEngine.run` for command-line runs (migration doc
§8 Phase 2). The function assembles the full conductor stack:

1. Resolve the procedure plugin.
2. Build a :class:`WorkerPool` from config and open it (adapters open here).
3. Build a :class:`RealRunSession` (bundle writer, writer thread, clock).
4. Build a :class:`Conductor` with a ``runner_factory`` that wires a
   :class:`ProcedureRunner` against the session's open resources.
5. Start the conductor; wait for the result future on its own thread.
6. Close the pool (adapters close).
7. Return a :class:`HeadlessResult` shaped like today's :class:`EngineResult`
   so CLI exit-code logic is unchanged across the cutover.

What this module does NOT do:

* Construct cameras. The sim config used for headless smoke tests has no
  cameras; full camera-bearing configs are Phase 3.
* Drive the GUI. The GUI cutover is Phase 4; today it still goes through
  :class:`ExperimentEngine`.
* Re-open adapters between runs. The pool stays open for the whole
  process — though the headless entry point closes it on exit, the
  same design supports many runs against one pool.

External-stop integration: the caller wires a SIGINT handler that sets
``stop_event``; the headless runner polls it and stops the conductor on
trip.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import anyio
import structlog

from capa.channels.registry import ChannelRegistry
from capa.core.databus import DataBus
from capa.core.plugins_runtime import ProcedureRegistry, resolve_mode
from capa.experiment.executor import MethodExecutor
from capa.experiment.procedures.base import ProcedureContext, ProcedureError
from capa.experiment.procedures.builtin.batch import Batch as _Batch
from capa.runtime.camera_adapter import CameraDeviceAdapter
from capa.runtime.conductor import (
    Conductor,
    ConductorConfig,
    ConductorRunner,
    RunOutcome,
    RunResult,
    RunSession,
)
from capa.runtime.dispatch import PoolDispatcher
from capa.runtime.errors import ResourceConflict
from capa.runtime.pool import WorkerPool
from capa.runtime.procedure import ProcedureRunner
from capa.runtime.runcontext import RunContext
from capa.runtime.session import RealRunSession
from capa.storage.manifest import BundleManifest

if TYPE_CHECKING:
    from capa.core.plugins_lock import PluginsLock
    from capa.experiment.config import ExperimentConfig
    from capa.storage.catalog import RunCatalog


_logger = structlog.get_logger("capa.runtime.headless")


# ---------------------------------------------------------------------------
# Result type — shape-compatible with EngineResult for CLI exit-code parity
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HeadlessResult:
    """Outcome of one :func:`run_headless` invocation.

    The shape mirrors :class:`capa.experiment.engine.EngineResult` so the
    CLI's exit-code logic doesn't branch on which path produced the run.
    """

    run_id: str
    bundle_path: Path | None
    run_status: str
    bundle_status: str
    integrity_status: str
    exit_reason: str | None = None

    def exit_code(self) -> int:
        if self.bundle_status == "verification_failed":
            return 3
        if self.run_status == "completed" and self.bundle_status == "sealed":
            return 0
        if self.run_status == "aborted":
            return 1
        if self.run_status == "crashed":
            return 2
        return 5


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run_headless(
    config: ExperimentConfig,
    *,
    runs_root: Path,
    plugins_lock: PluginsLock | None = None,
    repo_root: Path | None = None,
    lockfile_source: Path | None = None,
    external_stop: anyio.Event | None = None,
    catalog: RunCatalog | None = None,
    run_id: str | None = None,
    conductor_config: ConductorConfig | None = None,
    saturation_deadline_s: float | None = None,
) -> HeadlessResult:
    """Run one experiment via the conductor / pool stack.

    Mirrors the surface of :meth:`ExperimentEngine.run` so the CLI can
    swap callers without reshaping its options. The caller owns
    ``runs_root`` (this function does not create it) and the optional
    ``catalog`` (lifecycle stays with the caller's ``with`` block).
    """
    if conductor_config is None:
        conductor_config = (
            ConductorConfig(saturation_deadline_s=saturation_deadline_s)
            if saturation_deadline_s is not None
            else ConductorConfig()
        )

    # 1. Resolve the procedure. Refusal here = no bundle on disk.
    try:
        plugin_mode = resolve_mode()
        registry = ProcedureRegistry.discover(plugins_lock=plugins_lock, mode=plugin_mode)
        plugin_id = config.procedure.id
        if plugin_id not in registry:
            available = ", ".join(registry.ids()) or "<none>"
            raise ProcedureError(
                f"procedure {plugin_id!r} is not in the trusted registry "
                f"(mode={plugin_mode}); available: {available}"
            )
        procedure = registry.instantiate(plugin_id, config.procedure.config)
    except ProcedureError as exc:
        _logger.error("headless.procedure_resolution.failed", error=str(exc))
        return HeadlessResult(
            run_id=run_id or "preflight-refused",
            bundle_path=None,
            run_status="aborted",
            bundle_status="open",
            integrity_status="unknown",
            exit_reason=f"procedure_resolution: {exc}",
        )

    # Batch procedure needs to know runs_root for child bundles. Lifted
    # straight from engine.py:536-539 — small enough to inline rather than
    # invent a generic "procedure post-construct hook" API.
    if isinstance(procedure, _Batch):
        procedure.configure_runs_root(runs_root)

    # 2. Build + open the worker pool.
    try:
        pool = WorkerPool.from_config(config)
    except ResourceConflict as exc:
        _logger.error("headless.resource_conflict", error=str(exc))
        return HeadlessResult(
            run_id=run_id or "preflight-refused",
            bundle_path=None,
            run_status="aborted",
            bundle_status="open",
            integrity_status="unknown",
            exit_reason=f"resource_conflict: {exc}",
        )

    try:
        await pool.open()
    except BaseException as exc:
        _logger.error("headless.pool_open.failed", error=str(exc), error_type=type(exc).__name__)
        # Pool failed to open; nothing to close.
        return HeadlessResult(
            run_id=run_id or "preflight-refused",
            bundle_path=None,
            run_status="crashed",
            bundle_status="open",
            integrity_status="unknown",
            exit_reason=f"pool_open: {exc}",
        )

    try:
        # 3. Collect adapter maps for the bundle's equipment + camera
        #    identity blocks at finalize. Cameras are wrapped in
        #    :class:`CameraDeviceAdapter` (migration doc §6); the
        #    session's collector reads ``device_info`` off the
        #    underlying :class:`Camera`, so we expose ``.camera`` here
        #    rather than the wrapper itself.
        adapter_by_device: dict[str, Any] = {}
        adapter_by_camera: dict[str, Any] = {}
        for worker in pool.workers.values():
            for name, adapter in cast(Mapping[str, object], worker.adapters).items():
                if isinstance(adapter, CameraDeviceAdapter):
                    adapter_by_camera[name] = adapter.camera
                else:
                    adapter_by_device[name] = adapter

        # 4. Build the session. NOT opened yet — the conductor opens it
        #    inside its task group.
        session = RealRunSession(
            config=config,
            runs_root=runs_root,
            run_id=run_id,
            plugins_lock=plugins_lock,
            repo_root=repo_root,
            lockfile_source=lockfile_source,
            adapter_by_device=adapter_by_device,
            adapter_by_camera=adapter_by_camera,
            catalog=catalog,
        )

        # 5. Build the runner factory. The factory is invoked by the
        #    conductor on its loop AFTER session.open() returns; that's
        #    when the bundle writer + clock are valid.
        # `_conductor_holder` is a single-element list mutated after
        # Conductor construction (right below) so the factory can read
        # the live conductor reference at invocation time — a cheap
        # forward-reference cell that avoids reshaping the public API.
        _conductor_holder: list[Conductor] = []

        def _runner_factory(s: RunSession, ctx: RunContext) -> ConductorRunner:
            # The factory captures everything that's known pre-conductor
            # (procedure, config, channel registry) and resolves
            # post-open() resources lazily off the session.
            assert isinstance(s, RealRunSession), "headless runner factory requires RealRunSession"
            # Build the frozen channel registry the executor + procedure
            # resolve names against. Same shape as engine.py:634-635.
            channel_registry = ChannelRegistry.from_specs(list(config.hardware.channels))
            channel_registry.freeze()

            # Procedure-side dispatcher. PoolDispatcher (not
            # ConductorDispatcher) avoids the conductor-self-reference
            # circular dep — and the worker-level state gate is
            # sufficient: the procedure task is cancelled by the
            # conductor's task group before disarm starts, so a
            # procedure-issued command can never land during DRAINING.
            dispatcher = PoolDispatcher(pool)

            # MethodExecutor is built only when a method exists. The
            # executor's ctx must share the conductor's authoritative
            # DataBus — the executor's ``_wait_for`` subscribes to
            # channels here, and only the conductor's drain tasks publish
            # into it. Wiring a fresh DataBus would silently hang every
            # wait. The conductor's databus is created before the runner
            # factory is invoked (see Conductor._run), so reading it
            # off `_conductor_holder[0]` is safe.
            method_executor: MethodExecutor | None = None
            if config.method is not None:
                assert _conductor_holder, "runner_factory invoked before conductor was holdered"
                conductor_databus = _conductor_holder[0].databus
                assert conductor_databus is not None
                method_executor = _build_method_executor_for_runner(
                    config=config,
                    clock=s.clock,
                    bundle_writer=s.bundle_writer,
                    databus=conductor_databus,
                    channel_registry=channel_registry,
                    adapter_by_device=adapter_by_device,
                    dispatcher=dispatcher,
                    authorization=s.authorization,
                )

            # Wire the procedure's loop-local external_stop to the
            # conductor's completion event so procedures awaiting
            # ``ctx.external_stop.wait()`` (e.g. FreeRun) exit cleanly
            # on operator stop / saturation rather than via raised
            # CancelledError.
            stop_signal: asyncio.Event | None = None
            if _conductor_holder:
                stop_signal = _conductor_holder[0].completion_event

            runner = ProcedureRunner(
                procedure=procedure,
                config=config,
                channel_registry=channel_registry,
                dispatcher=dispatcher,
                authorization=s.authorization,
                adapters=adapter_by_device,
                bundle_writer=s.bundle_writer,
                method_executor=method_executor,
                stop_signal=stop_signal,
            )
            return runner

        # 6. Construct + start the conductor.
        conductor = Conductor(
            pool=pool,
            session=session,
            runner_factory=_runner_factory,
            config=conductor_config,
        )
        # Late-bind the conductor reference into the factory's closure cell
        # so it can wire the procedure's stop_signal when the factory is
        # invoked on the conductor's loop.
        _conductor_holder.append(conductor)

        # 7. Wire external_stop → conductor.stop(). The external_stop
        #    is an anyio.Event living on the caller's loop; we poll it
        #    in a background task and call conductor.stop() when it
        #    fires.
        stop_watcher_done = asyncio.Event()
        stop_watcher_task: asyncio.Task[None] | None = None
        if external_stop is not None:
            stop_watcher_task = asyncio.create_task(
                _watch_external_stop(external_stop, conductor, stop_watcher_done)
            )

        conductor.start()
        # Wait for the result on the calling loop. The conductor's result
        # future is a concurrent.futures.Future resolved from the conductor
        # thread; asyncio.wrap_future bridges back here.
        result: RunResult = await asyncio.wrap_future(conductor.result_future)
        conductor.join(timeout=5.0)
        stop_watcher_done.set()
        if stop_watcher_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await stop_watcher_task

        return _result_to_headless(result, session)
    finally:
        # Close the pool — adapters close here. Best-effort: a misbehaving
        # adapter must not prevent us returning a result.
        try:
            await pool.close()
        except BaseException as exc:
            _logger.error(
                "headless.pool_close.failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_method_executor_for_runner(
    *,
    config: ExperimentConfig,
    clock: Any,
    bundle_writer: Any,
    databus: DataBus,
    channel_registry: Any,
    adapter_by_device: dict[str, Any],
    dispatcher: Any,
    authorization: Any,
) -> MethodExecutor:
    """Pre-build the :class:`MethodExecutor` for the procedure.

    The executor captures its ctx at construction; we build a
    :class:`ProcedureContext` that matches what
    :meth:`ProcedureRunner._build_proc_ctx` will later produce so the
    executor sees a coherent ctx the moment a procedure invokes
    ``ctx.method_executor.run_to_completion(...)``.

    Same pattern as :meth:`ExperimentEngine.run` lines 645-648. The
    databus must be the conductor's authoritative bus so executor
    ``_wait_for`` subscriptions actually receive emissions.
    """
    exec_ctx = ProcedureContext(
        clock=clock,
        config=config,
        bundle_writer=bundle_writer,
        databus=databus,
        logger=structlog.get_logger("capa").bind(component="method_executor"),
        external_stop=anyio.Event(),
        instruments=channel_registry,
        adapters=adapter_by_device,
        dispatcher=dispatcher,
        authorization=authorization,
        metadata={},
    )
    executor = MethodExecutor(ctx=exec_ctx)
    exec_ctx.method_executor = executor
    return executor


async def _watch_external_stop(
    external_stop: anyio.Event,
    conductor: Conductor,
    done: asyncio.Event,
) -> None:
    """Poll the external-stop event and trip the conductor when set.

    Returns when either the stop event fires or ``done`` is set (signalling
    that the conductor finished naturally).
    """
    # anyio.Event.wait is awaitable; race against `done`.
    stop_task = asyncio.create_task(external_stop.wait())
    done_task = asyncio.create_task(done.wait())
    try:
        finished, pending = await asyncio.wait(
            {stop_task, done_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        if stop_task in finished:
            conductor.stop(reason="external_stop")
    except asyncio.CancelledError:
        for t in (stop_task, done_task):
            if not t.done():
                t.cancel()
        raise


def _result_to_headless(result: RunResult, session: RealRunSession) -> HeadlessResult:
    """Translate the conductor's :class:`RunResult` into the
    CLI-shaped :class:`HeadlessResult`. Mirrors the exit-status logic in
    :meth:`ExperimentEngine.run` so exit codes match across the cutover.
    """
    run_status = _outcome_to_run_status(result.outcome)
    # Bundle status is read off the session's manifest after finalize.
    bundle_status, integrity_status = _read_bundle_status(session)
    return HeadlessResult(
        run_id=result.run_id,
        bundle_path=result.bundle_path,
        run_status=run_status,
        bundle_status=bundle_status,
        integrity_status=integrity_status,
        exit_reason=result.exit_reason,
    )


def _outcome_to_run_status(outcome: RunOutcome) -> str:
    match outcome:
        case RunOutcome.COMPLETED:
            return "completed"
        case RunOutcome.ABORTED:
            return "aborted"
        case RunOutcome.CRASHED | RunOutcome.CRASHED_BUT_SEALED:
            return "crashed"


def _read_bundle_status(session: RealRunSession) -> tuple[str, str]:
    """Read ``bundle_status`` / ``integrity_status`` from the finalized
    manifest. Returns sensible defaults if the manifest can't be read."""
    bundle_path = session.bundle_path
    if bundle_path is None:
        return "open", "unknown"
    try:
        manifest = BundleManifest.read(bundle_path / "manifest.json")
        return (
            str(manifest.bundle_status),
            str(manifest.integrity.status),
        )
    except Exception:
        return "open", "unknown"


__all__ = ["HeadlessResult", "run_headless"]
