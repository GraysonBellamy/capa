""":class:`RealRunSession` — production :class:`RunSession` impl.

Bundles the per-run resources opened at run-start: the
:class:`RunBundleWriter` (durable storage), the :class:`WriterThread`
(off-loop sink writes), the :class:`RunClock` (run-authoritative
monotonic clock), the :class:`Authorization` handle (audit-stamping for
device commands), and the catalog row registration. The conductor uses
it as an async context-manager-ish lifecycle (``open()`` /
``set_outcome()`` / ``close()``).

What this module owns (per run):

* The on-disk bundle: open at :meth:`open`, finalize at :meth:`close`.
* The writer thread: start at :meth:`open`, close at :meth:`close`.
* The clock: minted at :meth:`open` and exposed for downstream wiring
  (the conductor installs it into every worker via :class:`RunContext`).
* The authorization handle: minted at :meth:`open` so every audit-stamped
  command (preflight, procedure, manual) carries the same run-arm id.
* The catalog row: inserted at :meth:`open`, updated at :meth:`close`
  (best-effort — a catalog write failure must never prevent finalize).
* Equipment / camera identity blocks: built lazily from the live adapter
  map at :meth:`close` time so ``equipment.toml`` / ``manifest.json``
  carry the actually-resolved hardware identities.

What it does **not** own:

* The :class:`WorkerPool` — that's the caller's responsibility (one
  pool can host many runs).
* The procedure / :class:`MethodExecutor` / :class:`ChannelRegistry` —
  the caller assembles those into a :class:`ProcedureRunner`, threading
  the session's open resources through.
* Logging context binding — :func:`bind_run_context` is called at
  :meth:`open` and cleared at :meth:`close`; the conductor's own
  contextvars layer adds the thread tag on top.
"""

from __future__ import annotations

import os
import re
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import structlog

from capa.core.clock import RunClock
from capa.core.logging import (
    bind_run_context,
    clear_run_context,
    configure_logging,
)
from capa.experiment.authorization import Authorization
from capa.runtime.bundle_ref import BundleWriterRef
from capa.runtime.conductor import RunOutcome
from capa.runtime.outcomes import run_status_for_outcome
from capa.runtime.progress import identity_from_device_info as _identity_from_device_info
from capa.runtime.recovery import (
    ActiveCheckpoint,
    delete_active_checkpoint,
    write_active_checkpoint,
)
from capa.runtime.runcontext import RunContext
from capa.runtime.writer_ref import WriterThreadRef
from capa.storage.bundle import RunBundleWriter
from capa.storage.manifest import BundleManifest
from capa.storage.writer_thread import WriterThread, WriterThreadError

if TYPE_CHECKING:
    from capa.core.metrics import MetricsRegistry
    from capa.core.plugins_lock import PluginsLock
    from capa.devices.adapter import DeviceAdapter
    from capa.experiment.config import ExperimentConfig
    from capa.storage.catalog import RunCatalog


_logger = structlog.get_logger("capa.runtime.session")


# Run-id minting. Format must stay stable: existing bundles in the
# catalog are keyed on it.
_INVALID_RUN_ID_CHARS: Final = re.compile(r"[^A-Za-z0-9._-]")


def make_run_id(*, sample_id: str, started_utc: datetime | None = None) -> str:
    """Directory shape: ``YYYY-MM-DD_HHMMSS_<sample-slug>``."""
    started = started_utc or datetime.now(UTC)
    stamp = started.strftime("%Y-%m-%d_%H%M%S")
    slug = _INVALID_RUN_ID_CHARS.sub("-", sample_id) or "sample"
    return f"{stamp}_{slug}"


def _stamp_clock_anchor(writer: RunBundleWriter, clock: RunClock) -> None:
    """Re-write ``manifest.json`` with the clock's anchor pair.

    The writer's :meth:`open` writes an initial manifest with
    ``started_mono_ns_anchor=0``; once :class:`RunClock.now` is captured we
    update the manifest so downstream readers can correlate ``t_mono_ns``
    columns with wall time.
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
# RealRunSession
# ---------------------------------------------------------------------------


class RealRunSession:
    """Production :class:`RunSession` for the Conductor.

    Built by the headless entry point (and by the GUI). The session is
    per-run; reuse across runs is not supported.

    The :meth:`writer_thread` / :meth:`bundle_writer` / :meth:`clock` /
    :meth:`authorization` properties become valid only after :meth:`open`
    has run. Callers needing those for downstream wiring (e.g.
    :class:`ProcedureRunner` construction) get them from the conductor's
    ``runner_factory`` callback, which fires after :meth:`open` returns.
    """

    __slots__ = (
        "_adapter_by_camera",
        "_adapter_by_device",
        "_authorization",
        "_bundle_path",
        "_bundle_writer",
        "_catalog",
        "_checkpoint_written",
        "_clock",
        "_config",
        "_config_path",
        "_configure_logging_for_bundle",
        "_engine_version",
        "_exit_reason",
        "_extra_queue_health",
        "_lockfile_source",
        "_logger",
        "_metrics",
        "_opened",
        "_outcome",
        "_plugins_lock",
        "_repo_root",
        "_run_id",
        "_runs_root",
        "_writer_thread",
    )

    def __init__(
        self,
        *,
        config: ExperimentConfig,
        runs_root: Path,
        run_id: str | None = None,
        plugins_lock: PluginsLock | None = None,
        repo_root: Path | None = None,
        lockfile_source: Path | None = None,
        adapter_by_device: dict[str, DeviceAdapter] | None = None,
        adapter_by_camera: dict[str, Any] | None = None,
        catalog: RunCatalog | None = None,
        metrics: MetricsRegistry | None = None,
        engine_version: str = "conductor",
        configure_logging_for_bundle: bool = True,
        config_path: Path | None = None,
    ) -> None:
        self._config = config
        self._runs_root = runs_root
        self._run_id = run_id or make_run_id(sample_id=config.sample.id)
        self._plugins_lock = plugins_lock
        self._repo_root = repo_root
        self._lockfile_source = lockfile_source
        # The adapter maps are populated by the caller AFTER pool.open() (the
        # caller walks pool.workers to build them) and BEFORE conductor.start.
        # Empty dicts here = headless tests / runs that don't need equipment
        # blocks.
        self._adapter_by_device = adapter_by_device or {}
        self._adapter_by_camera = adapter_by_camera or {}
        self._catalog = catalog
        self._metrics = metrics
        self._engine_version = engine_version
        self._configure_logging_for_bundle = configure_logging_for_bundle
        self._config_path = config_path

        self._bundle_writer: RunBundleWriter | None = None
        self._writer_thread: WriterThread | None = None
        self._clock: RunClock | None = None
        self._authorization: Authorization | None = None
        self._logger: Any = structlog.get_logger("capa")
        self._bundle_path: Path | None = None
        self._opened = False
        # Active-bundle checkpoint state. Written immediately after the
        # bundle directory is created so a hard exit between
        # open and finalize leaves a recoverable breadcrumb at
        # ``<runs_root>/.runtime-active.json``. Cleared at clean close.
        self._checkpoint_written = False
        self._outcome: RunOutcome = RunOutcome.COMPLETED
        self._exit_reason: str | None = None
        # Per-loop / per-bridge / per-worker diagnostics handed in by the
        # Conductor before close. Merged into the manifest's queue_health
        # dict at finalize so the bundle's on-disk schema stays put.
        self._extra_queue_health: dict[str, dict[str, float]] = {}

    # ------------------------------------------------------------------ RunSession protocol

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def bundle_path(self) -> Path | None:
        return self._bundle_path

    @property
    def saturation_source(self) -> WriterThread | None:
        """The writer thread (which satisfies :class:`WriterSaturationSource`).

        ``None`` before :meth:`open`."""
        return self._writer_thread

    # ------------------------------------------------------------------ downstream-wiring accessors

    @property
    def clock(self) -> RunClock:
        """Run-authoritative monotonic clock. Valid only after :meth:`open`."""
        if self._clock is None:
            raise RuntimeError("RealRunSession.clock accessed before open()")
        return self._clock

    @property
    def bundle_writer(self) -> RunBundleWriter:
        """The open bundle writer. Valid only after :meth:`open`."""
        if self._bundle_writer is None:
            raise RuntimeError("RealRunSession.bundle_writer accessed before open()")
        return self._bundle_writer

    @property
    def writer_thread(self) -> WriterThread:
        """The running writer thread. Valid only after :meth:`open`."""
        if self._writer_thread is None:
            raise RuntimeError("RealRunSession.writer_thread accessed before open()")
        return self._writer_thread

    @property
    def authorization(self) -> Authorization:
        """Run-arm authorization handle. Valid only after :meth:`open`."""
        if self._authorization is None:
            raise RuntimeError("RealRunSession.authorization accessed before open()")
        return self._authorization

    @property
    def logger(self) -> Any:
        """Bundle-aware structlog logger. Pre-:meth:`open`, returns a plain
        structlog logger (no bundle log sink yet); post-open, the same logger
        also tees into the bundle's ``run.log``."""
        return self._logger

    def attach_adapters(
        self,
        *,
        adapter_by_device: dict[str, DeviceAdapter] | None = None,
        adapter_by_camera: dict[str, Any] | None = None,
    ) -> None:
        """Late-bind the adapter maps used for equipment/camera identity
        blocks at finalize.

        Callers that build the pool first (and therefore the workers, which
        own the adapter instances) walk ``pool.workers`` to assemble these
        maps and call :meth:`attach_adapters` before constructing the
        conductor. Idempotent — last write wins.
        """
        if adapter_by_device is not None:
            self._adapter_by_device = adapter_by_device
        if adapter_by_camera is not None:
            self._adapter_by_camera = adapter_by_camera

    # ------------------------------------------------------------------ lifecycle

    async def open(self) -> RunContext:
        """Open the bundle, start the writer thread, mint authorization,
        build the :class:`RunContext`.

        Idempotent — calling :meth:`open` twice returns the same
        :class:`RunContext`. The conductor calls this inside its own task
        group; if the caller has already opened the session externally
        (e.g. to construct a :class:`ProcedureRunner` against the bundle
        writer first), the second call is a no-op.
        """
        if self._opened:
            assert self._clock is not None
            assert self._bundle_writer is not None
            assert self._writer_thread is not None
            return self._make_run_context()

        bind_run_context(
            run_id=self._run_id,
            operator_id=self._config.operator.id,
            procedure_id=self._config.procedure.id,
        )

        # Mint authorization BEFORE the bundle so any failure here leaves
        # no half-open bundle on disk.
        self._authorization = Authorization(
            operator_id=self._config.operator.id,
            run_id=self._run_id,
        )

        try:
            self._bundle_writer = RunBundleWriter(
                self._config,
                runs_root=self._runs_root,
                run_id=self._run_id,
                started_utc=datetime.now(UTC),
                started_mono_ns_anchor=0,
            )
            self._bundle_writer.open(
                repo_root=self._repo_root,
                lockfile_source=self._lockfile_source,
                plugins_lock=self._plugins_lock,
                engine_version=self._engine_version,
            )
            self._bundle_path = self._bundle_writer.bundle_path

            # Active-bundle checkpoint: atomic JSON at
            # ``<runs_root>/.runtime-active.json`` so a hard exit before
            # finalize leaves a durable breadcrumb the next launch can
            # reconcile via :func:`recover_active_bundle_checkpoint`. We
            # write this BEFORE the writer thread starts because the
            # writer is one of the more likely places a future shutdown
            # path could wedge — the breadcrumb must exist before any
            # sampling can begin.
            now = datetime.now(UTC)
            try:
                write_active_checkpoint(
                    self._runs_root,
                    ActiveCheckpoint(
                        pid=os.getpid(),
                        run_id=self._run_id,
                        bundle_path=self._bundle_path,
                        config_path=self._config_path,
                        started_utc=now,
                        last_update_utc=now,
                    ),
                )
                self._checkpoint_written = True
                self._logger.info(
                    "shutdown.bundle_checkpoint_written",
                    run_id=self._run_id,
                    bundle_path=str(self._bundle_path),
                )
            except OSError as ckpt_exc:
                # A failed checkpoint write is not fatal — the run can
                # still proceed; we just lose the recovery breadcrumb.
                # Log loudly so an operator notices a misconfigured
                # runs_root (read-only volume, missing parent, etc.).
                self._logger.warning(
                    "shutdown.bundle_checkpoint_write_failed",
                    run_id=self._run_id,
                    bundle_path=str(self._bundle_path),
                    error=str(ckpt_exc),
                )

            # Reconfigure logging now that the bundle's run.log exists so
            # every log line from this point teeing into the bundle is
            # captured even if the process dies before finalize.
            if self._configure_logging_for_bundle:
                self._logger = configure_logging(bundle_log_sink=self._bundle_writer.log_sink)
            else:
                self._logger = structlog.get_logger("capa")

            # Spawn the writer thread BEFORE anything records into the
            # bundle, so no on-loop write path can race the worker.
            writer_metrics = self._metrics.writer("bundle") if self._metrics is not None else None
            self._writer_thread = WriterThread(
                self._bundle_writer,
                metrics=writer_metrics,
                logger=self._logger.bind(component="writer_thread"),
            )
            self._writer_thread.start()

            self._clock = RunClock.now()
            _stamp_clock_anchor(self._bundle_writer, self._clock)

            self._logger.info(
                "session.open",
                run_id=self._run_id,
                bundle_path=str(self._bundle_path),
                engine_version=self._engine_version,
                operator_id=self._config.operator.id,
            )
            # Compatibility breadcrumb for existing bundle/log consumers:
            # run.log carries the historical audit event names.
            self._logger.info(
                "engine.run.start",
                run_id=self._run_id,
                bundle_path=str(self._bundle_path),
                engine_version=self._engine_version,
                operator_id=self._config.operator.id,
            )

            # Catalog row at open time so a crashed run still leaves a
            # row marked as "running" the catalog can find.
            if self._catalog is not None:
                try:
                    manifest = BundleManifest.read(self._bundle_path / "manifest.json")
                    self._catalog.insert_run_at_open(manifest, bundle_path=self._bundle_path)
                except Exception as exc:
                    self._logger.warning(
                        "session.catalog.insert_failed",
                        error=str(exc),
                    )
        except BaseException:
            # Roll back partial state — but keep the bundle dir on disk so
            # an operator can inspect a half-written run for debugging.
            if self._writer_thread is not None:
                with suppress(WriterThreadError):
                    self._writer_thread.close()
                self._writer_thread = None
            self._authorization = None
            clear_run_context()
            raise

        self._opened = True
        return self._make_run_context()

    def set_outcome(self, outcome: RunOutcome, exit_reason: str | None) -> None:
        """Inform the session of the run's outcome so :meth:`close` can
        record the right ``run_status`` in the bundle manifest."""
        self._outcome = outcome
        self._exit_reason = exit_reason

    def set_runtime_diagnostics(self, diagnostics: dict[str, dict[str, float]]) -> None:
        """Stash per-loop / per-bridge / per-worker metrics produced by
        the Conductor for inclusion in the finalize manifest.

        Idempotent — last write wins. The conductor calls this once just
        before :meth:`close`; tests / subclassed sessions may also call
        it directly.
        """
        self._extra_queue_health = dict(diagnostics)

    async def close(self) -> None:
        """Drain the writer thread, finalize the bundle, update the catalog.

        Idempotent — safe to call after a failed :meth:`open`. Errors are
        logged but do not propagate; the conductor's job is to seal a
        bundle, even a degraded one.
        """
        if not self._opened:
            # Failed open path — best-effort tear-down already happened in
            # `open()`. Just clear logging context.
            clear_run_context()
            return

        ended_utc = datetime.now(UTC)
        writer_snapshot: dict[str, float] | None = None
        run_status = run_status_for_outcome(self._outcome)

        # Log before stopping the writer / finalizing sinks so run.log
        # captures the run-end audit record before the file rotates closed.
        if self._bundle_writer is not None and self._bundle_writer.is_open:
            self._logger.info(
                "engine.run.end",
                run_status=run_status,
                exit_reason=self._exit_reason,
            )

        # 1. Drain + stop the writer thread BEFORE finalize.
        if self._writer_thread is not None:
            writer_snapshot = self._writer_thread.snapshot()
            try:
                self._writer_thread.close()
            except WriterThreadError as exc:
                self._logger.error("session.writer_thread.close_failed", error=str(exc))
            self._writer_thread = None

        # 2. Finalize the bundle.
        try:
            if self._bundle_writer is not None and self._bundle_writer.is_open:
                queue_health: dict[str, dict[str, float]] = {}
                if self._metrics is not None:
                    queue_health = self._metrics.snapshot_for_manifest()
                if writer_snapshot is not None:
                    queue_health["queue.writer-inbox"] = writer_snapshot
                # Conductor-side runtime diagnostics.
                queue_health.update(self._extra_queue_health)
                if not self._bundle_writer.is_finalized:
                    self._bundle_writer.finalize(
                        run_status=run_status,
                        exit_reason=self._exit_reason,
                        ended_utc=ended_utc,
                        queue_health=queue_health,
                        equipment=self._collect_equipment_blocks(),
                        cameras=self._collect_camera_blocks(),
                    )
        except Exception as exc:
            self._logger.error("session.finalize_failed", error=str(exc))

        # 3. Catalog update at finalize.
        if self._catalog is not None and self._bundle_path is not None:
            try:
                manifest = BundleManifest.read(self._bundle_path / "manifest.json")
                self._catalog.update_at_finalize(manifest, bundle_path=self._bundle_path)
            except Exception as exc:
                self._logger.warning("session.catalog.update_failed", error=str(exc))

        # 4. Clear the active-bundle checkpoint. Only safe to delete
        # here — after finalize + catalog update — because that's when
        # the bundle is durably recoverable WITHOUT the checkpoint. A
        # failure between bundle finalize and this delete leaves a
        # harmless stale checkpoint that next launch's recovery helper
        # will notice and clear (the manifest is already ``sealed`` so
        # recovery becomes a no-op marker-write).
        if self._checkpoint_written:
            delete_active_checkpoint(self._runs_root)
            self._checkpoint_written = False

        # 5. Disarm authorization so any straggler tasks can't issue commands.
        if self._authorization is not None:
            self._authorization.disarm()

        clear_run_context()

    # ------------------------------------------------------------------ helpers

    def _make_run_context(self) -> RunContext:
        assert self._clock is not None
        assert self._writer_thread is not None
        assert self._bundle_writer is not None
        writer_ref = WriterThreadRef(
            writer_thread=self._writer_thread,
            clock=self._clock,
            source="conductor",
        )
        bundle_ref = BundleWriterRef.from_writer(self._bundle_writer)
        return RunContext(
            run_id=self._run_id,
            clock=self._clock,
            writer=writer_ref,
            bundle=bundle_ref,
        )

    def _collect_equipment_blocks(self) -> list[dict[str, Any]]:
        """Walk ``config.hardware.devices`` (canonical order) and pair each
        with its live adapter for identity introspection.

        Emits one ``equipment.toml`` block per device in canonical order.
        """
        blocks: list[dict[str, Any]] = []
        for dev in self._config.hardware.devices:
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

    def _collect_camera_blocks(self) -> list[dict[str, Any]]:
        """As :meth:`_collect_equipment_blocks` but for cameras."""
        blocks: list[dict[str, Any]] = []
        for cam in self._config.hardware.cameras:
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


__all__ = ["RealRunSession", "make_run_id"]
