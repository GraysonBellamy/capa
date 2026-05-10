""":class:`ExperimentEngine` — task group + lifecycle for one run.

Plan §3 / §7. One AnyIO task group per run, one :class:`RunBundleWriter`
opened inside it, one entry point :meth:`ExperimentEngine.run` that the CLI
(``capa run --headless``) and (P1) the UI both call.

Inside the task group:

1. **Adapter producers.** One task per adapter, ``async for emission in
   adapter.stream():`` → fan-out queue (``BLOCK``).
2. **Fan-out.** Single consumer of the producer queue. Routes:

   * ``ChannelSample`` / ``SourceRecord`` / ``DeviceEvent`` /
     ``DeviceSnapshot`` → :class:`RunBundleWriter` durable sinks.
   * Every emission → :class:`DataBus` (subscribers consume async).

3. **Procedure.** :class:`Procedure` runs as its own task. Returning normally
   = clean completion; raising = crash.

The engine owns lifecycle:

* ``running`` is the run-status while the task group is alive.
* ``completed`` if the procedure returned normally.
* ``aborted`` if ``external_stop`` fired and the procedure exited cleanly.
* ``crashed`` if any task raised. The bundle still finalizes (plan §13.3).

The bundle is **always finalized** in the ``finally`` block — a crashed run
leaves a sealed artifact with ``run_status="crashed"``, never a half-broken
artifact. Plan §13.3.

UI consumption (plan §10): :attr:`databus`, :attr:`metrics`, and
:attr:`external_stop` are constructed in ``__init__`` so the UI can take
references and subscribe *before* :meth:`run` begins emitting samples. Live
lifecycle is observable via :attr:`state` plus the
:attr:`on_state_changed` callback. Operator-initiated stops go through
:meth:`request_abort` — the ``mode`` flag is recorded as a device event so
later procedure phases (P3 cooldown, P5 calibration teardown) can branch on
it without an event-schema migration.
"""

from __future__ import annotations

import enum
import importlib
import re
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

import anyio
import structlog

from capa.channels.registry import ChannelRegistry
from capa.core.backpressure import BackpressurePolicy, BoundedQueue
from capa.core.clock import RunClock
from capa.core.databus import DataBus
from capa.core.errors import AdapterError, BackpressureAbortError, CapaError
from capa.core.ingest import IngestConfig, IngestServer
from capa.core.logging import (
    bind_run_context,
    clear_run_context,
    configure_logging,
)
from capa.core.metrics import MetricsRegistry
from capa.core.plugins_lock import PluginsLock
from capa.core.plugins_runtime import ProcedureRegistry, resolve_mode
from capa.devices.camera.base import Camera, CameraEvent
from capa.devices.records import (
    ChannelSample,
    DeviceEmission,
    DeviceEvent,
    DeviceSnapshot,
    SourceRecord,
)
from capa.experiment.authorization import Authorization
from capa.experiment.cameras import (
    camera_output_path,
    camera_task,
    construct_cameras,
    disk_space_preflight_problems,
)
from capa.experiment.config import ExperimentConfig
from capa.experiment.executor import MethodExecutor
from capa.experiment.procedures.base import (
    Problem,
    Procedure,
    ProcedureContext,
    ProcedureError,
)
from capa.experiment.profiles.runtime import (
    Category as ProfileCheckCategory,
    ProfilePreflightContext,
    filter_by_category,
    run_profile_preflight,
)
from capa.storage.bundle import RunBundleWriter
from capa.storage.catalog import RunCatalog
from capa.storage.finalize import FinalizeResult
from capa.storage.manifest import BundleManifest

ENGINE_VERSION: Final[str] = "0.1.0-p0c"
"""Plan §13.1: engine code revision marker mirrored into
:attr:`CapaBlock.engine_version`. Bumped manually when engine semantics
change in a way that affects bundle interpretation."""


PRODUCER_QUEUE_CAPACITY: Final[int] = 256
"""Producer → fan-out buffer. Sized for the 3–60 Hz envelope; the producer
side is BLOCK so a slow fan-out backs up at the producer rather than dropping."""


# ---------------------------------------------------------------------------
# Engine state — UI-facing lifecycle, distinct from the bundle's run_status.
# Plan §10.1 lists Idle / Armed / Running / Aborting / Finalizing / Sealed as
# the Run-tab header states. Bundle ``run_status`` (running/completed/aborted/
# crashed) and ``bundle_status`` (open/finalizing/sealed/...) stay unchanged.
# ---------------------------------------------------------------------------


class EngineState(enum.StrEnum):
    """Live engine state observable by the UI.

    The values are stable strings so the UI can render them directly. Bundle
    on-disk fields are unaffected by this enum — they're populated from
    :class:`EngineResult` at finalize.
    """

    IDLE = "idle"
    """Engine constructed; :meth:`run` not yet called."""

    PREPARING = "preparing"
    """Inside :meth:`run` before the task group starts: procedure resolution,
    bundle writer open, adapter construction, preflight."""

    RUNNING = "running"
    """Task group active; producers and procedure are running."""

    ABORTING = "aborting"
    """Operator requested stop or fault tripped; task group draining."""

    FINALIZING = "finalizing"
    """Task group exited; bundle writer is rewriting Parquet and computing
    integrity hashes."""

    SEALED = "sealed"
    """Bundle finalized cleanly. Inspect :attr:`EngineResult.run_status` to
    distinguish completed / aborted / crashed runs."""

    FAILED = "failed"
    """Bundle finalize itself failed (e.g. ``verification_failed``) or the
    engine never opened a bundle (preflight refusal)."""


StateCallback = Callable[[EngineState], None]
"""Type of the optional :attr:`ExperimentEngine.on_state_changed` hook."""


AbortMode = Literal["safe_shutdown", "immediate"]
"""Plan §9 / §13.2. P1 cancels the task group either way; the mode is
recorded so P3 cooldown procedures and P5 calibration teardown can branch."""


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EngineResult:
    """Outcome of one :meth:`ExperimentEngine.run` invocation.

    The CLI uses this to derive an exit code (plan §14):

    * 0 — ``run_status == "completed"`` ∧ ``bundle_status == "sealed"``.
    * 1 — ``run_status == "aborted"``.
    * 2 — ``run_status == "crashed"``.
    * 3 — ``bundle_status == "verification_failed"``.
    * 4 — preflight refusal (no bundle written).
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
        return 5  # bundle absent / unknown — preflight or pre-open failure


# ---------------------------------------------------------------------------
# Engine errors
# ---------------------------------------------------------------------------


class EngineError(CapaError):
    """Raised by the engine on lifecycle violations (running while running,
    finalize before run, etc.). Distinct from procedure / adapter errors so
    callers can distinguish "engine bug" from "device/procedure failure"."""


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


_INVALID_RUN_ID_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def make_run_id(*, sample_id: str, started_utc: datetime | None = None) -> str:
    """Plan §8 directory shape: ``YYYY-MM-DD_HHMMSS_<sample-slug>``.

    Sample id is slugged for filesystem safety.
    """
    started = started_utc or datetime.now(UTC)
    stamp = started.strftime("%Y-%m-%d_%H%M%S")
    slug = _INVALID_RUN_ID_CHARS.sub("-", sample_id) or "sample"
    return f"{stamp}_{slug}"


class ExperimentEngine:
    """Owns the per-run AnyIO task group.

    Construct once per run; reuse is not supported — :meth:`run` consumes
    the engine's :class:`MetricsRegistry`.

    The :attr:`databus`, :attr:`metrics`, and :attr:`external_stop` are
    available immediately after construction, before :meth:`run` is awaited.
    The UI uses this window to subscribe to channel samples and read live
    queue/writer metrics so no early sample is missed by plot consumers.
    """

    __slots__ = (
        "_abort_mode",
        "_adapter_by_camera",
        "_adapter_by_device",
        "_adapters",
        "_authorization",
        "_camera_event_callback",
        "_cameras",
        "_clock",
        "_databus",
        "_enable_ingest",
        "_external_stop",
        "_ingest_server",
        "_instruments",
        "_logger",
        "_method_executor",
        "_metrics",
        "_plugin_mode",
        "_preview_callback",
        "_procedure",
        "_procedure_registry",
        "_run_id",
        "_runs_root",
        "_state",
        "_state_callback",
        "_writer",
    )

    def __init__(
        self,
        *,
        external_stop: anyio.Event | None = None,
        on_state_changed: StateCallback | None = None,
        procedure_registry: ProcedureRegistry | None = None,
        plugin_mode: str | None = None,
        enable_ingest: bool = False,
    ) -> None:
        self._adapters: list[Any] = []
        self._adapter_by_device: dict[str, Any] = {}
        self._cameras: list[Camera] = []
        self._adapter_by_camera: dict[str, Camera] = {}
        self._enable_ingest: bool = enable_ingest
        self._clock: RunClock | None = None
        # Constructed eagerly so a UI consumer can subscribe / read counters
        # before run() is awaited. Plan §7: UI bridge consumes from the
        # DataBus on its own DROP_OLDEST queue — independent from durable
        # sinks, so a slow disk never starves repaints.
        self._databus: DataBus = DataBus()
        self._external_stop: anyio.Event = external_stop or anyio.Event()
        self._ingest_server: IngestServer | None = None
        self._instruments: ChannelRegistry | None = None
        self._logger: structlog.stdlib.BoundLogger | None = None
        self._method_executor: MethodExecutor | None = None
        self._metrics: MetricsRegistry = MetricsRegistry()
        self._plugin_mode = resolve_mode(plugin_mode)  # type: ignore[arg-type]
        self._procedure: Procedure | None = None
        self._procedure_registry: ProcedureRegistry | None = procedure_registry
        self._run_id: str | None = None
        self._runs_root: Path | None = None
        self._writer: RunBundleWriter | None = None
        self._authorization: Authorization | None = None
        self._state: EngineState = EngineState.IDLE
        self._state_callback: StateCallback | None = on_state_changed
        self._abort_mode: AbortMode | None = None
        self._preview_callback: Callable[[str, bytes], None] | None = None
        self._camera_event_callback: Callable[[CameraEvent], None] | None = None

    # ------------------------------------------------------------------ properties

    @property
    def databus(self) -> DataBus:
        """In-process pub/sub fed by the fan-out task. Subscribe before
        :meth:`run` to guarantee every emission is observed."""
        return self._databus

    @property
    def metrics(self) -> MetricsRegistry:
        """Live queue-depth and writer-lag counters. The UI status bar
        polls this at 1 Hz (plan §10.4)."""
        return self._metrics

    @property
    def external_stop(self) -> anyio.Event:
        """Set this event (or call :meth:`request_abort`) to stop a running
        engine. The CLI's SIGINT handler sets it; the UI's Abort button
        goes through :meth:`request_abort`."""
        return self._external_stop

    @property
    def state(self) -> EngineState:
        """Current lifecycle state. Plan §10.1 Run-tab header states."""
        return self._state

    @property
    def run_id(self) -> str | None:
        return self._run_id

    @property
    def preview_callback(self) -> Callable[[str, bytes], None] | None:
        """UI sink for camera-preview JPEG bytes (plan §10.2). Set before
        :meth:`run` so the per-camera drain tasks can pick it up.

        Signature is ``(camera_name, jpeg_bytes) -> None``. The callback is
        invoked from the engine's asyncio loop; with ``qasync`` that loop
        runs on Qt's main thread, so a ``pyqtSignal.emit`` is safe to call
        directly without ``QueuedConnection``.
        """
        return self._preview_callback

    @preview_callback.setter
    def preview_callback(self, callback: Callable[[str, bytes], None] | None) -> None:
        self._preview_callback = callback

    @property
    def camera_event_callback(self) -> Callable[[CameraEvent], None] | None:
        """UI sink for :class:`CameraEvent` (plan §10.2). Set before
        :meth:`run` so each per-camera event drain task can fan events out
        to the camera-preview dock (``pump_warning`` / ``pump_failed`` /
        ``recording_stopped``) in addition to writing them to
        ``events.sqlite``.

        Threading mirrors :attr:`preview_callback`: invoked from the
        engine's asyncio loop, which under ``qasync`` runs on Qt's main
        thread, so a ``pyqtSignal.emit`` is safe without
        ``QueuedConnection``.
        """
        return self._camera_event_callback

    @camera_event_callback.setter
    def camera_event_callback(self, callback: Callable[[CameraEvent], None] | None) -> None:
        self._camera_event_callback = callback

    @property
    def abort_mode(self) -> AbortMode | None:
        """The mode of the most recent :meth:`request_abort` call, or
        ``None`` if no abort has been requested."""
        return self._abort_mode

    def set_state_callback(self, cb: StateCallback | None) -> None:
        """Install (or clear) the state-change callback. Fires synchronously
        on the engine's task whenever :attr:`state` transitions; the UI
        wraps this in a Qt signal emitter."""
        self._state_callback = cb

    def _set_state(self, new: EngineState) -> None:
        if new is self._state:
            return
        self._state = new
        cb = self._state_callback
        if cb is not None:
            try:
                cb(new)
            except Exception:
                # A misbehaving callback must not crash the engine. We log
                # via structlog if a logger exists, otherwise swallow —
                # the callback is the UI's problem, not the run's.
                if self._logger is not None:
                    self._logger.warning("engine.state_callback_failed", state=str(new))

    # ------------------------------------------------------------------ control

    def request_abort(self, *, mode: AbortMode = "safe_shutdown") -> None:
        """Request engine stop. Idempotent; the first call wins.

        Sets :attr:`external_stop`, records ``mode`` for later phases, and
        transitions state to :attr:`EngineState.ABORTING` if currently
        ``RUNNING``. Safe to call from any thread / any task —
        ``anyio.Event.set`` is thread-safe.

        Plan §9 / §13.2: the mode flag distinguishes the UI's red-button
        default (cooldown phase first) from "Emergency abort" (cancel now).
        P1 cancels the task group either way; P3 procedures will branch on
        ``self._abort_mode`` inside their cooldown step.
        """
        if self._abort_mode is None:
            self._abort_mode = mode
        if not self._external_stop.is_set():
            self._external_stop.set()
        # Engine may not yet be running; ABORTING only makes sense from RUNNING.
        if self._state is EngineState.RUNNING:
            self._set_state(EngineState.ABORTING)

    # ------------------------------------------------------------------ run

    async def run(
        self,
        config: ExperimentConfig,
        *,
        runs_root: Path,
        run_id: str | None = None,
        plugins_lock: PluginsLock | None = None,
        repo_root: Path | None = None,
        lockfile_source: Path | None = None,
        external_stop: anyio.Event | None = None,
        catalog: RunCatalog | None = None,
        configure_logging_for_bundle: bool = True,
    ) -> EngineResult:
        """Execute one run end-to-end.

        Args:
            config: validated :class:`ExperimentConfig`.
            runs_root: parent directory under which the bundle is created.
            run_id: optional override; default is
                :func:`make_run_id` from ``config.sample.id``.
            plugins_lock: parsed :class:`PluginsLock`. Recorded in the
                manifest's ``plugins`` block.
            repo_root, lockfile_source: forwarded to
                :func:`gather_provenance`.
            external_stop: pre-existing :class:`anyio.Event`. The CLI sets
                this on SIGINT. ``None`` (default) means "use the event
                constructed at __init__"; passing a new event here replaces
                that. UI callers should pass the engine's own event back
                through ``request_abort`` rather than constructing one.
            catalog: optional :class:`RunCatalog`. When given, the engine
                inserts at run-open and updates at finalize.
            configure_logging_for_bundle: when ``True`` (default), the
                engine reconfigures :mod:`capa.core.logging` so log lines
                tee into the bundle's ``run.log``. CLI callers want this;
                tests that already configured logging pass ``False``.

        Returns:
            :class:`EngineResult`. The bundle (if opened) is finalized
            before return — the engine never leaves a half-written bundle.
        """
        run_id = run_id or make_run_id(sample_id=config.sample.id)
        self._run_id = run_id
        self._runs_root = runs_root
        if external_stop is not None:
            self._external_stop = external_stop
        bundle_path: Path | None = None
        exit_reason: str | None = None
        run_status = "running"

        self._set_state(EngineState.PREPARING)

        # Procedure resolution (preflight first — no bundle if it refuses).
        try:
            self._procedure = self._resolve_procedure(config, plugins_lock=plugins_lock)
        except ProcedureError as exc:
            self._set_state(EngineState.FAILED)
            return EngineResult(
                run_id=run_id,
                bundle_path=None,
                run_status="aborted",
                bundle_status="open",
                integrity_status="unknown",
                exit_reason=f"procedure_resolution: {exc}",
            )

        # Authorization handle — minted before preflight so every audit-stamped
        # command (including those issued by preflight checks) carries an id.
        self._authorization = Authorization(
            operator_id=config.operator.id,
            run_id=run_id,
        )

        # Configure Batch with the runs_root before its preflight; Batch needs
        # to know where to put child bundles and the engine knows that.
        from capa.experiment.procedures.builtin.batch import Batch as _Batch  # noqa: PLC0415

        if isinstance(self._procedure, _Batch):
            self._procedure.configure_runs_root(runs_root)

        bind_run_context(
            run_id=run_id,
            operator_id=config.operator.id,
            procedure_id=config.procedure.id,
        )
        try:
            self._writer = RunBundleWriter(
                config,
                runs_root=runs_root,
                run_id=run_id,
                started_utc=datetime.now(UTC),
                started_mono_ns_anchor=0,
            )
            self._writer.open(
                repo_root=repo_root,
                lockfile_source=lockfile_source,
                plugins_lock=plugins_lock,
                engine_version=ENGINE_VERSION,
            )
            bundle_path = self._writer.bundle_path

            # Reconfigure logging now that the bundle's run.log exists.
            if configure_logging_for_bundle:
                self._logger = configure_logging(bundle_log_sink=self._writer.log_sink)
            else:
                self._logger = structlog.get_logger("capa")

            self._clock = RunClock.now()
            # Reflect the captured anchor in the manifest. Re-write via
            # internal hook so the on-disk manifest matches reality.
            _stamp_clock_anchor(self._writer, self._clock)

            self._logger.info(
                "engine.run.start",
                run_id=run_id,
                bundle_path=str(bundle_path),
                engine_version=ENGINE_VERSION,
                operator_id=config.operator.id,
            )

            if catalog is not None:
                # The manifest object inside the writer is the source of
                # truth for the catalog row; re-read from disk for safety.
                manifest = BundleManifest.read(bundle_path / "manifest.json")
                catalog.insert_run_at_open(manifest, bundle_path=bundle_path)

            # Construct adapters.
            self._adapters = _construct_adapters(config)
            for adapter in self._adapters:
                if hasattr(adapter, "configure_channels"):
                    adapter.configure_channels(list(config.hardware.channels))
            self._adapter_by_device = {a.name: a for a in self._adapters if hasattr(a, "name")}

            # Construct cameras (plan §12). Cameras are peers of devices
            # but live in their own list because their lifecycle and
            # emissions differ — they own their own output containers and
            # emit FrameReceipts rather than ChannelSamples.
            self._cameras = construct_cameras(config, clock=self._clock)
            self._adapter_by_camera = {c.spec.name: c for c in self._cameras}

            # Build the frozen ChannelRegistry the executor + procedures use
            # for name → binding resolution. Plan §5.1 — registry is frozen
            # at run-arm so later config edits don't change historical meaning.
            self._instruments = ChannelRegistry.from_specs(list(config.hardware.channels))
            self._instruments.freeze()

            # Build the MethodExecutor only when a method is present; FreeRun
            # has no method so skipping the construction keeps the context
            # minimal for record-only runs. The executor and the procedure
            # share the same ProcedureContext instance — the executor's
            # constructor stores a reference, and we install the executor
            # back into the context's slot so the procedure (which gets a
            # fresh context per call) sees a populated executor.
            self._method_executor = None
            if config.method is not None:
                exec_ctx = self._build_context()
                self._method_executor = MethodExecutor(ctx=exec_ctx)
                exec_ctx.method_executor = self._method_executor

            # Camera disk-space preflight (plan §12.6). Runs before the
            # procedure preflight so a guaranteed-to-fail run never reaches
            # adapter.start() — bundle root is locked to disk only after
            # a green preflight.
            if config.hardware.cameras:
                disk_problems = disk_space_preflight_problems(
                    config, bundle_root=self._writer.bundle_path
                )
                self._handle_preflight_problems(disk_problems, source="camera_disk")

            # Procedure preflight — collect Problem records, refuse on any
            # blocking entry. Plan §11.
            problems = await self._procedure.preflight(self._build_context())
            self._handle_preflight_problems(problems, source="procedure")

            # Profile preflight (static phase) — config / filesystem checks
            # that don't need live data. Plan §5.4.1. The dynamic phase runs
            # later, inside the task group after adapters have started.
            if config.domain_profile is not None:
                await self._run_domain_profile_preflight(
                    config, category="static", adapters_started=False
                )

            self._set_state(EngineState.RUNNING)
            # The main task-group dance.
            run_status = await self._run_task_group(config)

        except ProcedureError as exc:
            run_status = "aborted"
            exit_reason = f"preflight: {exc}"
            if self._logger is not None:
                self._logger.error("engine.preflight.failed", error=str(exc))
        except BackpressureAbortError as exc:
            run_status = "crashed"
            exit_reason = f"backpressure: {exc}"
            if self._logger is not None:
                self._logger.error("engine.backpressure.abort", error=str(exc))
        except BaseExceptionGroup as eg:
            # anyio task groups wrap their inner exception(s) in an
            # ExceptionGroup. Unwrap to get the same routing as if the
            # exception had been raised outside the task group.
            unwrapped = _unwrap_single(eg)
            if isinstance(unwrapped, ProcedureError):
                run_status = "aborted"
                exit_reason = f"preflight: {unwrapped}"
                if self._logger is not None:
                    self._logger.error("engine.preflight.failed", error=str(unwrapped))
            elif isinstance(unwrapped, BackpressureAbortError):
                run_status = "crashed"
                exit_reason = f"backpressure: {unwrapped}"
                if self._logger is not None:
                    self._logger.error("engine.backpressure.abort", error=str(unwrapped))
            else:
                run_status = "crashed"
                exit_reason = f"engine: {type(unwrapped).__name__}: {unwrapped}"
                if self._logger is not None:
                    self._logger.exception("engine.run.crashed", error=str(unwrapped))
        except BaseException as exc:
            run_status = "crashed"
            exit_reason = f"engine: {type(exc).__name__}: {exc}"
            if self._logger is not None:
                self._logger.exception("engine.run.crashed", error=str(exc))
        finally:
            ended_utc = datetime.now(UTC)
            integrity_status = "unknown"
            bundle_status = "open"
            self._set_state(EngineState.FINALIZING)
            # Log run.end *before* finalize() closes the sinks. The
            # bundle's run.log is the audit trail of the run; engine.run.end
            # belongs in it, not just on stdout.
            if self._logger is not None and self._writer is not None and self._writer.is_open:
                self._logger.info(
                    "engine.run.end",
                    run_status=run_status,
                    exit_reason=exit_reason,
                )
            try:
                if self._writer is not None and self._writer.is_open:
                    queue_health = self._metrics.snapshot_for_manifest()
                    if not self._writer.is_finalized:
                        equipment_blocks = self._collect_equipment_blocks(config)
                        camera_blocks = self._collect_camera_blocks(config)
                        result: FinalizeResult = self._writer.finalize(
                            run_status=run_status,  # type: ignore[arg-type]
                            exit_reason=exit_reason,
                            ended_utc=ended_utc,
                            queue_health=queue_health,
                            equipment=equipment_blocks,
                            cameras=camera_blocks,
                        )
                        integrity_status = result.integrity.status
                        bundle_status = (
                            "verification_failed"
                            if integrity_status not in ("ok", "unknown")
                            else "sealed"
                        )
                        if integrity_status == "ok":
                            bundle_status = "sealed"
                if catalog is not None and bundle_path is not None:
                    try:
                        manifest = BundleManifest.read(bundle_path / "manifest.json")
                        catalog.update_at_finalize(manifest, bundle_path=bundle_path)
                    except Exception as exc:
                        if self._logger is not None:
                            self._logger.warning(
                                "engine.catalog.update_failed",
                                error=str(exc),
                            )
            finally:
                # Disarm the run-arm authorization so any straggler procedure
                # task that survives shutdown cannot keep issuing commands.
                if self._authorization is not None:
                    self._authorization.disarm()
                if self._ingest_server is not None:
                    try:
                        await self._ingest_server.stop()
                    except Exception as exc:
                        if self._logger is not None:
                            self._logger.warning("engine.ingest.stop_failed", error=str(exc))
                # Adapter cleanup. Best-effort: a misbehaving adapter must
                # not prevent us returning a sealed bundle.
                for adapter in self._adapters:
                    try:
                        await adapter.stop()
                        await adapter.close()
                    except Exception as exc:
                        if self._logger is not None:
                            self._logger.warning(
                                "engine.adapter.cleanup_failed",
                                adapter=getattr(adapter, "name", "?"),
                                error=str(exc),
                            )

                self._databus.close()
                if self._logger is not None:
                    # Final stdout-only summary line; the bundle log is closed
                    # by now (finalize sealed it) so this only hits the
                    # console handler.
                    self._logger.info(
                        "engine.sealed",
                        run_status=run_status,
                        bundle_status=bundle_status,
                        integrity_status=integrity_status,
                    )
                clear_run_context()
                # Final state: SEALED if the bundle finalized cleanly,
                # FAILED otherwise (verification_failed, or no bundle at all).
                if bundle_status == "sealed":
                    self._set_state(EngineState.SEALED)
                else:
                    self._set_state(EngineState.FAILED)

        return EngineResult(
            run_id=run_id,
            bundle_path=bundle_path,
            run_status=run_status,
            bundle_status=bundle_status,
            integrity_status=integrity_status,
            exit_reason=exit_reason,
        )

    # ------------------------------------------------------------------ internals

    def _collect_equipment_blocks(self, config: ExperimentConfig) -> list[dict[str, Any]]:
        """Build the per-device block list passed to ``writer.finalize``.

        Walks ``config.hardware.devices`` (canonical ordering, never the
        live adapter list which is mutation-prone) and pairs each declared
        device with its live adapter via ``self._adapter_by_device``. The
        block carries the configured name + adapter import path so the
        bundle is self-describing even when adapters fail to open;
        ``identity`` is populated from ``adapter.device_info`` when present.

        Sim adapters and adapters that never reached identify return
        ``identity: None``. Hardware-day §10 — ``equipment.toml`` was
        previously a stub with only name + adapter.
        """
        blocks: list[dict[str, Any]] = []
        for dev in config.hardware.devices:
            block: dict[str, Any] = {
                "name": dev.name,
                "adapter": dev.adapter,
                "identity": None,
            }
            adapter = self._adapter_by_device.get(dev.name)
            if adapter is not None:
                info = getattr(adapter, "device_info", None)
                if info is not None:
                    extracted = _identity_from_device_info(info)
                    if extracted:
                        block["identity"] = extracted
            blocks.append(block)
        return blocks

    def _collect_camera_blocks(self, config: ExperimentConfig) -> list[dict[str, Any]]:
        """Build the per-camera identity block list passed to ``writer.finalize``.

        Mirrors :meth:`_collect_equipment_blocks` for cameras: walks the
        canonical config order, pairs each declared camera with its live
        adapter via ``self._adapter_by_camera``, and probes
        ``adapter.device_info`` (a :class:`CameraInfo` for the webcam adapter,
        ``None`` for sim cameras) through the same duck-typed
        :func:`_identity_from_device_info` helper. Hardware-day 2026-05-09 PM
        finding #2 — V4L2 identity probed correctly but never reached
        ``equipment.toml`` or ``manifest.json.cameras`` because the
        equipment-collection wired only ``[[devices]]``.
        """
        blocks: list[dict[str, Any]] = []
        for cam in config.hardware.cameras:
            block: dict[str, Any] = {
                "name": cam.name,
                "adapter": cam.adapter,
                "identity": None,
            }
            adapter = self._adapter_by_camera.get(cam.name)
            if adapter is not None:
                info = getattr(adapter, "device_info", None)
                if info is not None:
                    extracted = _identity_from_device_info(info)
                    if extracted:
                        block["identity"] = extracted
            blocks.append(block)
        return blocks

    def _build_context(self) -> ProcedureContext:
        assert self._clock is not None
        assert self._writer is not None
        assert self._logger is not None
        assert self._instruments is not None
        assert self._authorization is not None
        return ProcedureContext(
            clock=self._clock,
            config=self._writer.config,
            bundle_writer=self._writer,
            databus=self._databus,
            logger=self._logger.bind(component="procedure"),
            external_stop=self._external_stop,
            instruments=self._instruments,
            adapters=self._adapter_by_device,
            authorization=self._authorization,
            method_executor=self._method_executor,
            metadata={},
        )

    def _resolve_procedure(
        self,
        config: ExperimentConfig,
        *,
        plugins_lock: PluginsLock | None,
    ) -> Procedure:
        """Resolve ``config.procedure`` via the trusted :class:`ProcedureRegistry`.

        The registry is built once on first call (cached on the engine
        instance). Production mode requires a ``plugins.lock``; refusing to
        run without one is intentional — it forces the operator to either
        provide the lockfile or explicitly downgrade to dev mode.
        """
        if self._procedure_registry is None:
            self._procedure_registry = ProcedureRegistry.discover(
                plugins_lock=plugins_lock,
                mode=self._plugin_mode,
            )
        registry = self._procedure_registry
        plugin_id = config.procedure.id
        if plugin_id not in registry:
            available = ", ".join(registry.ids()) or "<none>"
            raise ProcedureError(
                f"procedure {plugin_id!r} is not in the trusted registry "
                f"(mode={self._plugin_mode}); available: {available}"
            )
        try:
            return registry.instantiate(plugin_id, config.procedure.config)
        except ProcedureError:
            raise
        except Exception as exc:
            raise ProcedureError(f"failed to instantiate procedure {plugin_id!r}: {exc}") from exc

    def _handle_preflight_problems(
        self,
        problems: list[Problem],
        *,
        source: str,
    ) -> None:
        """Record preflight Problems into the bundle and abort on blockers.

        Every problem becomes an event so the audit trail captures what the
        run was warned about. Blocking problems escalate to a
        :class:`ProcedureError` that the engine's outer ``except`` converts
        into a clean refusal."""
        assert self._writer is not None
        assert self._clock is not None
        assert self._logger is not None
        if not problems:
            return
        for p in problems:
            severity = p.severity if not p.blocking else "error"
            self._writer.write_event(
                kind=f"preflight.{source}.problem",
                message=f"[{p.code}] {p.message}",
                severity=severity,
                source=f"engine.preflight:{source}",
                t_mono_ns=self._clock.t_mono_ns(),
                t_utc=datetime.now(UTC),
                metadata={
                    "code": p.code,
                    "blocking": p.blocking,
                    "source": source,
                    **p.metadata,
                },
            )
            log_method = self._logger.error if p.blocking else self._logger.warning
            log_method(f"engine.preflight.{source}", code=p.code, message=p.message)
        blockers = [p for p in problems if p.blocking]
        if blockers:
            codes = ", ".join(p.code for p in blockers)
            raise ProcedureError(f"{source} preflight blocked by: {codes}")

    async def _run_domain_profile_preflight(
        self,
        config: ExperimentConfig,
        *,
        category: ProfileCheckCategory,
        adapters_started: bool,
    ) -> None:
        """Resolve the active profile's check ids and execute the subset
        registered under ``category``.

        Static checks run before adapters open (no live samples available).
        Dynamic checks run inside the engine task group, after every
        ``adapter.start()`` has returned, so they can observe live data and
        treat silent channels as blocking errors instead of warnings.
        """
        assert config.domain_profile is not None
        assert self._instruments is not None

        all_ids = _resolve_profile_check_ids(config.domain_profile.id)
        check_ids = filter_by_category(all_ids, category)
        if not check_ids:
            return
        ctx = ProfilePreflightContext(
            config=config,
            instruments=self._instruments,
            databus=self._databus,
            profile_metadata=dict(config.domain_profile.metadata),
            adapters_started=adapters_started,
        )
        problems = await run_profile_preflight(ctx, check_ids)
        self._handle_preflight_problems(problems, source=f"profile.{category}")

    async def _run_task_group(self, config: ExperimentConfig) -> str:
        """Spawn producer + fan-out + procedure tasks. Return the
        ``run_status`` string.

        Shutdown flow:

        1. Procedure exits *or* ``external_stop`` fires.
        2. Stop coordinator calls :meth:`adapter.stop` on every adapter so
           each ``stream()`` exits its while-loop cleanly.
        3. Last producer to exit closes the producer queue, waking the
           fan-out.
        4. Fan-out drains remaining items, then exits.
        """
        assert self._procedure is not None
        assert self._writer is not None

        producer_queue: BoundedQueue[DeviceEmission] = BoundedQueue(
            name="producer-fanout",
            capacity=PRODUCER_QUEUE_CAPACITY,
            policy=BackpressurePolicy.BLOCK,
        )
        queue_metrics = self._metrics.queue("producer-fanout")
        procedure_completed = anyio.Event()
        producers_alive = _Counter()

        # Cameras run on their own stop signal so a clean procedure
        # completion (free_run timer expiring, recipe finishing) tears them
        # down without pretending the operator hit Abort. Plan §12: cameras
        # are peers of devices; the engine drives both lifecycles in concert.
        cameras_stop = anyio.Event()

        async with anyio.create_task_group() as tg:
            for adapter in self._adapters:
                await adapter.open()
                await adapter.start(self._clock)
                producers_alive.inc()
                tg.start_soon(
                    self._producer_task,
                    adapter,
                    producer_queue,
                    queue_metrics,
                    producers_alive,
                )

            for camera in self._cameras:
                tg.start_soon(self._camera_task_runner, camera, cameras_stop)

            # Camera-only runs have zero producers; without this the fan-out
            # would block forever waiting for a queue close that no producer
            # is ever going to trigger.
            if producers_alive.value == 0:
                producer_queue.close()

            tg.start_soon(self._fanout_task, producer_queue)

            # Profile preflight (dynamic phase) — runs after every adapter
            # has started and the fanout is pumping samples onto the
            # databus, but before the procedure task spawns so a silent
            # channel can abort the run before any commands are issued.
            # Plan §5.4.1; raises ProcedureError on blocking problems,
            # which cancels the task group and is caught by the outer
            # try/except in run().
            if config.domain_profile is not None:
                await self._run_domain_profile_preflight(
                    config, category="dynamic", adapters_started=True
                )

            tg.start_soon(self._procedure_task, procedure_completed)
            tg.start_soon(self._shutdown_coordinator, procedure_completed, cameras_stop)
            tg.start_soon(self._watchdog_task, procedure_completed)
            if self._enable_ingest:
                await self._maybe_start_ingest(tg)

        # If the operator (or SIGINT handler) signaled stop, classify the run
        # as ``aborted`` even when the procedure cooperated and exited cleanly
        # — that's still an operator-requested stop, not a self-completed run.
        return "aborted" if self._external_stop.is_set() else "completed"

    async def _shutdown_coordinator(
        self,
        procedure_completed: anyio.Event,
        cameras_stop: anyio.Event,
    ) -> None:
        """Wait for whichever of ``procedure_completed`` / ``external_stop``
        fires first, then ask each adapter to stop sampling and signal
        cameras to wind down their recordings.

        Stopping the adapters lets each ``stream()`` exit naturally. The
        producers then exit one by one; the last one closes the producer
        queue, which wakes the fan-out. We do *not* cancel the task group
        directly — that would interrupt the fan-out mid-write."""
        assert self._logger is not None

        async with anyio.create_task_group() as inner:
            inner.start_soon(_wait_event, procedure_completed, inner.cancel_scope)
            inner.start_soon(_wait_event, self._external_stop, inner.cancel_scope)

        # Camera tasks watch their own event so a clean procedure exit
        # doesn't get mis-classified as an operator abort.
        cameras_stop.set()

        for adapter in self._adapters:
            try:
                await adapter.stop()
            except Exception as exc:
                self._logger.warning(
                    "engine.adapter.stop_failed",
                    adapter=getattr(adapter, "name", "?"),
                    error=str(exc),
                )

        # Close the ingest listener so its accept tasks (which would
        # otherwise loop forever) wake on ClosedResourceError and exit. The
        # outer ``finally`` block also calls stop(); both are idempotent.
        if self._ingest_server is not None:
            try:
                await self._ingest_server.stop()
            except Exception as exc:
                self._logger.warning("engine.ingest.stop_failed", error=str(exc))

    async def _producer_task(
        self,
        adapter: Any,
        queue: BoundedQueue[DeviceEmission],
        metrics: Any,
        producers_alive: _Counter,
    ) -> None:
        """Drain ``adapter.stream()`` into ``queue`` until the stream ends.

        The last producer to exit closes the queue, signalling the fan-out
        to drain and exit too."""
        assert self._logger is not None
        try:
            async for emission in adapter.stream():
                await queue.put(emission)
                metrics.observe_depth(queue.depth)
        except (AdapterError, BackpressureAbortError):
            raise
        except Exception as exc:
            self._logger.error(
                "engine.producer.failed",
                adapter=getattr(adapter, "name", "?"),
                error=str(exc),
            )
            raise
        finally:
            producers_alive.dec()
            if producers_alive.value == 0:
                queue.close()

    async def _camera_task_runner(
        self,
        camera: Camera,
        cameras_stop: anyio.Event,
    ) -> None:
        """Dispatch into :func:`capa.experiment.cameras.camera_task`.

        ``cameras_stop`` is the unified signal — set by the shutdown
        coordinator on either natural procedure completion or operator
        abort. Camera tasks tear down on this event without watching
        ``external_stop`` directly, so a clean procedure exit doesn't get
        misclassified.
        """
        assert self._writer is not None
        assert self._clock is not None
        assert self._logger is not None
        assert self._run_id is not None
        output_path = camera_output_path(self._writer.bundle_path, camera.spec, run_id=self._run_id)

        def _on_failure(spec: Any, event: Any) -> None:
            """Apply the camera's on_failure policy.

            ``warn`` records the event and lets the run continue;
            ``abort_run`` and ``safe_shutdown`` set ``external_stop`` so the
            shutdown coordinator drives a clean teardown. The full
            SafetyMonitor escalation lands in a later phase — this is the
            minimum that honors the spec's contract.
            """
            if spec.on_failure in ("abort_run", "safe_shutdown"):
                self._abort_mode = spec.on_failure
                self._external_stop.set()

        await camera_task(
            camera,
            writer=self._writer,
            output_path=output_path,
            clock=self._clock,
            external_stop=cameras_stop,
            logger=self._logger,
            on_failure_callback=_on_failure,
            preview_callback=self._preview_callback,
            camera_event_callback=self._camera_event_callback,
        )

    async def _fanout_task(
        self,
        queue: BoundedQueue[DeviceEmission],
    ) -> None:
        """Drain ``queue`` and route each emission. Exits when the queue is
        closed *and* empty."""
        assert self._writer is not None
        writer_metrics = self._metrics.writer("bundle")
        while True:
            try:
                emission = await queue.get()
            except RuntimeError:
                # Queue closed and empty. Normal termination.
                return
            with writer_metrics.time_write():
                self._route_emission(emission)
            await self._databus.publish(emission)

    def _route_emission(self, emission: DeviceEmission) -> None:
        assert self._writer is not None
        match emission:
            case ChannelSample():
                self._writer.record_sample(emission)
            case SourceRecord():
                self._writer.record_source(emission)
            case DeviceEvent():
                self._writer.record_event(emission)
            case DeviceSnapshot():
                self._writer.record_snapshot(emission)

    async def _procedure_task(self, procedure_completed: anyio.Event) -> None:
        assert self._procedure is not None
        try:
            await self._procedure.run(self._build_context())
        finally:
            procedure_completed.set()

    async def _maybe_start_ingest(self, tg: anyio.abc.TaskGroup) -> None:
        """Bind the per-run external-event ingest endpoint, if possible.

        Plan §11.1. Bind failures (stale socket, AF_UNIX missing on
        ancient Windows) downgrade to "ingest disabled for this run" — they
        do not abort. Recorded into ``manifest.json.ingest`` is left for
        a follow-up; for now the bind state shows up in the run log.
        """
        assert self._writer is not None
        assert self._logger is not None
        assert self._clock is not None

        socket_path = self._writer.bundle_path / ".ingest.sock"

        async def _sink(event: dict[str, Any]) -> None:
            assert self._writer is not None and self._clock is not None
            t_mono = event.get("t_mono_ns_anchor")
            if not isinstance(t_mono, int):
                t_mono = self._clock.t_mono_ns()
            metadata: dict[str, Any] = dict(event.get("payload") or {})
            channel = event.get("channel")
            if channel:
                metadata["channel"] = channel
            metadata["source"] = "ingest"
            self._writer.write_event(
                kind=event["kind"],
                message=event.get("message", ""),
                severity=event.get("severity", "info"),
                source="ingest.external",
                t_mono_ns=t_mono,
                t_utc=event["t_utc"],
                metadata=metadata,
            )

        server = IngestServer(
            config=IngestConfig(socket_path=socket_path),
            sink=_sink,
            logger=self._logger.bind(component="ingest"),
        )
        try:
            await server.start(tg)
        except Exception as exc:
            self._logger.warning("engine.ingest.start_failed", error=str(exc))
            return
        self._ingest_server = server

    async def _watchdog_task(self, procedure_completed: anyio.Event) -> None:
        """Periodically check each adapter's :class:`WatchdogState` and log
        a ``device_silent`` warning when a producer goes quiet past
        ``2 / sample_rate_hz`` (plan §13.2).

        For P2 the watchdog is observation-only: it logs a structured
        warning and writes a :class:`DeviceEvent` so the bundle records
        the gap. SafetyMonitor (P3) will read the same
        :meth:`adapter.watchdog_state()` view and apply configured
        actions (``abort_run`` / ``safe_shutdown`` / ``warn``).
        """
        assert self._logger is not None
        assert self._writer is not None
        # Filter to adapters that opted into the watchdog hook.
        targets = [a for a in self._adapters if hasattr(a, "watchdog_state")]
        if not targets:
            return
        # Grace period before the first sweep so adapters can produce their
        # first sample.
        warned: set[str] = set()
        try:
            while not procedure_completed.is_set() and not self._external_stop.is_set():
                await anyio.sleep(1.0)
                now_ns = self._clock.t_mono_ns() if self._clock is not None else 0
                for adapter in targets:
                    try:
                        state = adapter.watchdog_state()
                    except Exception as exc:
                        self._logger.warning(
                            "engine.watchdog.state_failed",
                            adapter=getattr(adapter, "name", "?"),
                            error=str(exc),
                        )
                        continue
                    if not state.is_silent(now_t_mono_ns=now_ns):
                        # Producer recovered — clear the latched warning.
                        warned.discard(state.device)
                        continue
                    if state.device in warned:
                        continue  # already logged; don't spam
                    warned.add(state.device)
                    self._logger.warning(
                        "engine.watchdog.device_silent",
                        device=state.device,
                        last_t_mono_ns=state.last_t_mono_ns,
                        expected_period_ns=state.expected_period_ns,
                    )
                    self._writer.record_event(
                        DeviceEvent(
                            adapter="engine.watchdog",
                            device=state.device,
                            t_mono_ns=now_ns,
                            t_utc=datetime.now(UTC),
                            kind="device_silent",
                            severity="warning",
                            message=(
                                f"adapter {state.device!r} has not emitted in "
                                f"more than {2 * state.expected_period_ns / 1e9:.2f}s"
                            ),
                        )
                    )
        except anyio.get_cancelled_exc_class():
            raise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Counter:
    """Trivial mutable int. AnyIO has no atomic counter primitive but we
    only mutate from inside the same task group, so a plain int is fine."""

    value: int = 0

    def inc(self) -> None:
        self.value += 1

    def dec(self) -> None:
        self.value -= 1


async def _wait_event(event: anyio.Event, scope: anyio.CancelScope) -> None:
    """Wait on ``event`` then cancel ``scope``. Used by the shutdown
    coordinator to race two events."""
    await event.wait()
    scope.cancel()


_IDENTITY_FIELDS: tuple[str, ...] = (
    "part_number",
    "model",
    "product_type",
    "manufacturer",
    "serial_number",
    "serial",
    "firmware_id",
    "firmware",
    "hardware_id",
    "family",
    "software",
    "chassis",
    "physical_module",
)
"""Field names probed off an adapter's ``device_info`` to populate
``equipment.toml`` identity. Adapters expose heterogeneous shapes
(Watlow's ``DeviceInfo`` is a dataclass, Sartorius's is a Pydantic
model, NIDAQ's is a frozen dataclass); duck-typing keeps the engine
ignorant of those differences. ``product_type`` / ``chassis`` /
``physical_module`` are NIDAQ-specific but harmless on adapters that
don't expose them (the extractor drops missing fields)."""


def _identity_from_device_info(info: Any) -> dict[str, Any]:
    """Extract a uniform identity dict from ``adapter.device_info``.

    Reads :data:`_IDENTITY_FIELDS` by name; missing or ``None`` fields
    are dropped. Coerces enum values via ``.value`` and Watlow-style
    structured fields via ``.raw`` so the output is TOML-friendly.
    """
    extracted: dict[str, Any] = {}
    for field in _IDENTITY_FIELDS:
        value = getattr(info, field, None)
        if value is None:
            continue
        # Watlow PartNumber exposes ``raw``; some enums expose ``value``.
        if hasattr(value, "raw"):
            value = value.raw
        if hasattr(value, "value"):
            value = value.value
        if value is None or value == "":
            continue
        extracted[field] = value
    return extracted


def _construct_adapters(config: ExperimentConfig) -> list[Any]:
    """Walk ``config.hardware.devices`` and instantiate each adapter via its
    declared module path.

    Resolution order per adapter class:

    1. If the class defines a ``from_params(name=..., **params)``
       classmethod, call it. This is the TOML-friendly path: sim adapters
       use it to materialise ``signals`` dicts into actual
       :class:`SignalFn` callables.
    2. Otherwise call ``cls(name=..., **params)`` directly. Real adapters
       (P0d+) typically take this shape — their params are all
       JSON/TOML-native (serial port, baud, polling rate, …).
    """
    out: list[Any] = []
    for dev in config.hardware.devices:
        cls = _import_adapter_class(dev.adapter)
        from_params = getattr(cls, "from_params", None)
        try:
            if callable(from_params):
                adapter = from_params(name=dev.name, **dev.params)
            else:
                adapter = cls(name=dev.name, **dev.params)
        except TypeError as exc:
            raise EngineError(
                f"failed to construct adapter {dev.name!r} ({dev.adapter}): {exc}"
            ) from exc
        out.append(adapter)
    return out


def _import_adapter_class(module_path: str) -> type:
    """Resolve ``capa.devices.sim.alicat_sim`` → ``AlicatSim`` (and the real
    counterpart ``capa.devices.watlow`` → ``WatlowAdapter``).

    Convention: the module exports exactly one adapter class whose name is
    one of:

    * ``<Leaf>`` — direct CamelCase of the leaf module (``alicat`` → ``Alicat``).
    * ``<Leaf>Sim`` — sim adapters with the ``_sim`` suffix stripped
      (``alicat_sim`` → ``AlicatSim``).
    * ``<Leaf>Adapter`` — the real-adapter naming used in plan §5.2
      (``watlow`` → ``WatlowAdapter``, ``alicat`` → ``AlicatAdapter``).
    * Bare-acronym variants where the first leaf segment is upper-cased
      (``nidaq`` → ``NIDAQAdapter``, ``nidaq_polled_sim`` → ``NIDAQPolledSim``).
      Without this, every acronym adapter (NIDAQ, LCR, MFC, PWM, …) had to
      ship a CamelCase alias to satisfy the resolver.
    """
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise EngineError(f"adapter module {module_path!r} not importable: {exc}") from exc
    leaf = module_path.rsplit(".", 1)[-1]
    leaf_no_sim = leaf.removesuffix("_sim")
    base = _snake_to_camel(leaf)
    base_no_sim = _snake_to_camel(leaf_no_sim)
    upper_first = _snake_to_camel_upper_first(leaf)
    upper_first_no_sim = _snake_to_camel_upper_first(leaf_no_sim)
    candidate_names = [
        base,
        base_no_sim + "Sim",
        base + "Adapter",
        base_no_sim + "Adapter",
        upper_first,
        upper_first_no_sim + "Sim",
        upper_first + "Adapter",
        upper_first_no_sim + "Adapter",
    ]
    seen: list[str] = []
    for name in candidate_names:
        if name in seen:
            continue
        seen.append(name)
        cls = getattr(module, name, None)
        if isinstance(cls, type):
            return cls
    raise EngineError(f"adapter module {module_path!r} does not expose any of {seen}")


def _snake_to_camel(name: str) -> str:
    return "".join(part.title() for part in name.split("_"))


def _snake_to_camel_upper_first(name: str) -> str:
    """Like :func:`_snake_to_camel` but the leading segment stays uppercase.

    ``nidaq`` → ``NIDAQ`` (single segment), ``nidaq_polled_sim`` →
    ``NIDAQPolledSim``. This is the resolver's bare-acronym fallback for
    adapters whose canonical class name begins with an acronym kept all-
    caps (``NIDAQAdapter``, ``LCRAdapter``, ``MFCAdapter``, …)."""
    parts = name.split("_")
    if not parts:
        return name
    return parts[0].upper() + "".join(p.title() for p in parts[1:])


def _unwrap_single(eg: BaseExceptionGroup) -> BaseException:
    """Return the deepest single exception in an ExceptionGroup chain.

    anyio task groups wrap one inner exception in an ``ExceptionGroup``
    instance even though there's only one underlying error. When the group
    contains exactly one exception (typical for our preflight-fails-fast
    paths), drill through nested groups to recover the original. Groups
    with multiple sub-exceptions are returned unchanged."""
    current: BaseException = eg
    while isinstance(current, BaseExceptionGroup) and len(current.exceptions) == 1:
        current = current.exceptions[0]
    return current


def _resolve_profile_check_ids(profile_id: str) -> tuple[str, ...]:
    """Defer-import the profile module and read its ``preflight_checks``.

    Avoids a top-level cycle (engine → profiles.runtime → profile module)
    while keeping the resolver in one place. Returns an empty tuple for
    unknown profiles."""
    if "capa_pyrolysis" in profile_id:
        from capa.experiment.profiles.capa_pyrolysis import PREFLIGHT_CHECKS  # noqa: PLC0415

        return tuple(c.id for c in PREFLIGHT_CHECKS)
    if "cone_calorimeter" in profile_id:
        from capa.experiment.profiles.cone_calorimeter import PREFLIGHT_CHECKS  # noqa: PLC0415

        return tuple(c.id for c in PREFLIGHT_CHECKS)
    return ()


def _stamp_clock_anchor(writer: RunBundleWriter, clock: RunClock) -> None:
    """Re-write ``manifest.json`` with the clock's anchor pair.

    The writer's :meth:`open` writes an initial manifest with
    ``started_mono_ns_anchor=0`` (we hadn't taken the clock yet). Now that
    the clock exists, mirror its anchor into the manifest so a later reader
    can correlate ``t_mono_ns`` columns with wall time.
    """
    manifest_path = writer.bundle_path / "manifest.json"
    manifest = BundleManifest.read(manifest_path)
    manifest = manifest.model_copy(
        update={
            "started_utc": clock.started_utc,
            "started_mono_ns_anchor": clock.started_mono_ns,
        }
    )
    manifest.write(manifest_path)


# ---------------------------------------------------------------------------
# Signal handler helper for the CLI
# ---------------------------------------------------------------------------


def install_sigint_handler(stop_event: anyio.Event) -> None:
    """Install a ``SIGINT`` handler that sets ``stop_event``.

    Called by ``capa run --headless``. Idempotent against re-entry: a second
    Ctrl-C terminates the process via the default handler.
    """
    triggered = False

    def _handler(signum: int, frame: object) -> None:
        nonlocal triggered
        if triggered:
            sys.stderr.write("\nsecond SIGINT — exiting hard\n")
            sys.stderr.flush()
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            return
        triggered = True
        sys.stderr.write("\nSIGINT received — initiating graceful stop (Ctrl-C again to force)\n")
        sys.stderr.flush()
        stop_event.set()

    signal.signal(signal.SIGINT, _handler)


__all__ = [
    "ENGINE_VERSION",
    "AbortMode",
    "EngineError",
    "EngineResult",
    "EngineState",
    "ExperimentEngine",
    "StateCallback",
    "install_sigint_handler",
    "make_run_id",
]
