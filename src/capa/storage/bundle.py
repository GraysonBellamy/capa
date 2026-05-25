""":class:`RunBundleWriter` — opens a run dir, owns sink lifecycle, drives the
``bundle_status`` state machine, writes ``manifest.json`` at start and
finalize.

The writer is the only object that knows the bundle exists
as a single coordinated unit; sinks know about themselves. The surface
exposed here is what the engine bolts onto.

Lifecycle:

::

    open() ──► record(emission) × N ──► finalize(run_status="completed")
                                       │
                                       └─► or finalize(run_status="aborted")
                                       │     after operator/safety stop
                                       └─► or close()-without-finalize, then
                                             finalize_in_place(...) recovers
                                             the bundle as run_status="crashed"

State invariants:

* ``open()`` writes ``manifest.json`` with ``bundle_status="open"`` and
  ``run_status="running"``.
* ``finalize()`` invokes :func:`~capa.storage.finalize.finalize_in_place`
  which drives the bundle through ``finalizing`` to ``sealed`` (or
  ``verification_failed``).
* ``close()`` without ``finalize()`` leaves the bundle in its current state
  (``open`` plus in-flight files) — recoverable by a later
  :func:`~capa.storage.finalize.finalize_in_place` call.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import tomli_w

from capa.core.errors import CapaError
from capa.core.plugins_lock import PluginsLock
from capa.core.provenance import Provenance, gather_provenance
from capa.devices.camera.base import CameraSpec, FrameReceipt
from capa.devices.records import (
    ChannelSample,
    DeviceEvent,
    DeviceSnapshot,
    SourceRecord,
)
from capa.experiment.config import ExperimentConfig
from capa.storage.channel_samples_sink import ChannelSamplesSink
from capa.storage.device_records_sink import DeviceRecordsSink
from capa.storage.events_sink import EventsSink
from capa.storage.finalize import FinalizeResult, finalize_in_place
from capa.storage.log_sink import LogSink
from capa.storage.manifest import (
    BundleManifest,
    CameraEntry,
    DomainProfileBlock,
    OperatorBlock,
    ProcedureBlock,
    RecordingBlock,
    RecordingPolicyMode,
    RunStatus,
    SampleBlock,
)
from capa.storage.status_sink import StatusSink
from capa.storage.video_sink import VIDEO_DIRNAME, FramesSink


class BundleWriterError(CapaError):
    """Raised on bundle-writer state errors (record before open, etc.)."""


@dataclass(slots=True)
class _Sinks:
    """Bag of every sink the writer owns. Each is opened in :meth:`open` and
    closed in :meth:`_close_sinks`."""

    channel_samples: ChannelSamplesSink
    device_records: DeviceRecordsSink
    events: EventsSink
    status: StatusSink
    log: LogSink
    frames: dict[str, FramesSink] = field(default_factory=dict)
    """One :class:`FramesSink` per declared camera. Lazily created the first
    time a frame is recorded for a given camera; closed in
    :meth:`_close_sinks`."""


class RunBundleWriter:
    """Open and finalize a single run bundle.

    Construct with the experiment config plus a runs-root path; call
    :meth:`open` to materialize the directory + sinks; feed
    :meth:`record` from the (synthetic or real) producer pipeline; call
    :meth:`finalize` at end of run. ``close()`` without ``finalize()``
    leaves the bundle recoverable.
    """

    __slots__ = (
        "_bundle_path",
        "_config",
        "_finalized",
        "_opened",
        "_provenance",
        "_run_id",
        "_sinks",
        "_started_mono_ns_anchor",
        "_started_utc",
    )

    def __init__(
        self,
        config: ExperimentConfig,
        *,
        runs_root: str | Path,
        run_id: str,
        started_utc: datetime | None = None,
        started_mono_ns_anchor: int = 0,
    ) -> None:
        self._config = config
        self._run_id = run_id
        self._bundle_path = Path(runs_root) / run_id
        self._started_utc = started_utc or datetime.now(UTC)
        self._started_mono_ns_anchor = started_mono_ns_anchor
        self._opened = False
        self._finalized = False
        self._sinks: _Sinks | None = None
        self._provenance: Provenance | None = None

    # ------------------------------------------------------------------ props

    @property
    def bundle_path(self) -> Path:
        return self._bundle_path

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def is_open(self) -> bool:
        return self._opened

    @property
    def is_finalized(self) -> bool:
        return self._finalized

    @property
    def log_sink(self) -> LogSink:
        """Return the in-bundle :class:`LogSink`. The engine points
        :func:`configure_logging` at this so log lines tee into ``run.log``.
        """
        if not self._opened or self._sinks is None:
            raise BundleWriterError("log_sink requires open()")
        return self._sinks.log

    @property
    def config(self) -> ExperimentConfig:
        return self._config

    # ------------------------------------------------------------------ open

    def open(
        self,
        *,
        repo_root: Path | None = None,
        lockfile_source: Path | None = None,
        plugins_lock: PluginsLock | None = None,
        engine_version: str | None = None,
    ) -> None:
        """Materialize the bundle directory, snapshot config + provenance,
        write the initial ``manifest.json``, and open every sink.

        ``repo_root`` and ``lockfile_source`` are forwarded to
        :func:`gather_provenance`. ``plugins_lock`` is the parsed
        ``plugins.lock`` at startup (mirrored verbatim into the manifest).
        ``engine_version`` is the engine revision marker recorded into
        :attr:`CapaBlock.engine_version` ().
        """
        if self._opened:
            raise BundleWriterError("RunBundleWriter is already open")
        self._bundle_path.mkdir(parents=True, exist_ok=False)
        env_dir = self._bundle_path / "env"
        env_dir.mkdir()

        self._provenance = gather_provenance(
            repo_root=repo_root,
            lockfile_source=lockfile_source,
            plugins_lock=plugins_lock,
            engine_version=engine_version,
        )

        # env/ snapshot — lockfile is optional (None when no uv.lock found).
        if self._provenance.lockfile_bytes is not None:
            (env_dir / "uv.lock").write_bytes(self._provenance.lockfile_bytes)
        (env_dir / "packages.json").write_bytes(self._provenance.packages_json_bytes)

        # config.toml — (canonicalized). model_dump(mode="json") emits
        # TOML-friendly primitives (tuples → lists, datetimes → ISO strings);
        # _toml_safe drops Nones and any other tomli_w-hostile shape.
        config_json = self._config.model_dump(mode="json")
        (self._bundle_path / "config.toml").write_text(
            tomli_w.dumps(_toml_safe(config_json)), encoding="utf-8"
        )
        # method.toml is broken out for diff-friendliness when present.
        if self._config.method is not None:
            method_dump = (
                self._config.method.model_dump(mode="json")
                if hasattr(self._config.method, "model_dump")
                else self._config.method
            )
            (self._bundle_path / "method.toml").write_text(
                tomli_w.dumps(_toml_safe(method_dump)), encoding="utf-8"
            )

        # profiles/<id>.toml — dedicated snapshot of the active
        # domain profile's metadata, mirrored verbatim from config so a
        # downstream reader can pick it up without parsing the larger
        # config.toml.
        if self._config.domain_profile is not None:
            profile_dir = self._bundle_path / "profiles"
            profile_dir.mkdir(exist_ok=True)
            short_id = self._config.domain_profile.id.rsplit(".", 1)[-1]
            snapshot = {
                "id": self._config.domain_profile.id,
                "standard_refs": list(self._config.domain_profile.standard_refs),
                **self._config.domain_profile.metadata,
            }
            (profile_dir / f"{short_id}.toml").write_text(
                tomli_w.dumps(_toml_safe(snapshot)), encoding="utf-8"
            )

        # equipment.toml stub — populated once adapters report firmware.
        equipment_stub: dict[str, Any] = {"devices": []}
        for dev in self._config.hardware.devices:
            equipment_stub["devices"].append({"name": dev.name, "adapter": dev.adapter})
        (self._bundle_path / "equipment.toml").write_text(
            tomli_w.dumps(equipment_stub), encoding="utf-8"
        )

        # calibration.json — current reference snapshot. Records the
        # calibration-set name + revision; the resolved curves snapshot
        # lands when the calibration runtime is wired in.
        calibration_block = {
            "name": self._config.calibration_set.name,
            "revision": self._config.calibration_set.revision,
        }
        (self._bundle_path / "calibration.json").write_text(
            _json_dumps(calibration_block), encoding="utf-8"
        )

        # Open sinks.
        self._sinks = _Sinks(
            channel_samples=ChannelSamplesSink(self._bundle_path),
            device_records=DeviceRecordsSink(self._bundle_path),
            events=EventsSink(self._bundle_path),
            status=StatusSink(self._bundle_path),
            log=LogSink(self._bundle_path),
        )

        # Initial manifest.
        self._write_initial_manifest()
        self._opened = True

    # ------------------------------------------------------------------ record

    def record_sample(self, sample: ChannelSample) -> None:
        if not self._opened or self._sinks is None:
            raise BundleWriterError("record_sample() requires open()")
        self._sinks.channel_samples.write(sample)

    def record_source(self, record: SourceRecord) -> None:
        if not self._opened or self._sinks is None:
            raise BundleWriterError("record_source() requires open()")
        self._sinks.device_records.write(record)

    def record_event(self, event: DeviceEvent) -> None:
        if not self._opened or self._sinks is None:
            raise BundleWriterError("record_event() requires open()")
        self._sinks.events.write_device_event(event)

    def record_snapshot(self, snapshot: DeviceSnapshot) -> None:
        if not self._opened or self._sinks is None:
            raise BundleWriterError("record_snapshot() requires open()")
        self._sinks.status.write(snapshot)

    def record_frame(self, receipt: FrameReceipt) -> None:
        """Append a :class:`FrameReceipt` to the camera's frame-index parquet.

        The :class:`FramesSink` for the camera is created lazily on first
        receipt. Adapters that never write a frame (e.g. a camera that
        opened but never recorded) end up with no in-flight file at all,
        which is the right outcome — finalize will skip them.
        """
        if not self._opened or self._sinks is None:
            raise BundleWriterError("record_frame() requires open()")
        sink = self._sinks.frames.get(receipt.name)
        if sink is None:
            sink = FramesSink(self._bundle_path, camera=receipt.name)
            self._sinks.frames[receipt.name] = sink
        sink.write(receipt)

    def record(
        self,
        emission: ChannelSample | SourceRecord | DeviceEvent | DeviceSnapshot,
    ) -> None:
        """Polymorphic dispatch over a ``DeviceEmission`` union.

        Mirrors what the engine fan-out does. The synthetic harness uses
        this to drive the writer end-to-end with a single call.
        """
        if isinstance(emission, ChannelSample):
            self.record_sample(emission)
        elif isinstance(emission, SourceRecord):
            self.record_source(emission)
        elif isinstance(emission, DeviceEvent):
            self.record_event(emission)
        elif isinstance(emission, DeviceSnapshot):
            self.record_snapshot(emission)
        else:  # pragma: no cover - guarded by type union
            raise BundleWriterError(f"unknown emission type: {type(emission).__name__}")

    # ------------------------------------------------------------------ event helpers

    def write_event(
        self,
        *,
        kind: str,
        message: str,
        severity: str = "info",
        source: str = "engine",
        t_mono_ns: int,
        t_utc: datetime,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Free-form event from the procedure / operator / safety layers."""
        if not self._opened or self._sinks is None:
            raise BundleWriterError("write_event() requires open()")
        self._sinks.events.write(
            kind=kind,
            message=message,
            severity=severity,
            source=source,
            t_mono_ns=t_mono_ns,
            t_utc=t_utc,
            metadata=metadata,
        )

    # ------------------------------------------------------------------ recording plan

    def update_recording_plan(
        self,
        *,
        policy_mode: RecordingPolicyMode,
        plan: Any,
    ) -> None:
        """Rewrite ``manifest.json`` with the resolved recording plan.

        Called by the conductor after :meth:`Procedure.plan_capture` runs
        and before workers arm. Re-reads the manifest from disk, applies
        the new ``recording`` block, and updates each ``cameras`` entry's
        ``recorded`` / ``suppressed_reason`` fields against the plan.

        ``plan`` is typed as :class:`Any` to avoid an import of
        :class:`~capa.runtime.recording.ResolvedRecordingPlan` at this
        layer — the storage module stays runtime-independent. The plan
        must expose ``channel_mode``, ``recorded_channels``,
        ``camera_mode``, ``recorded_cameras``, ``source``,
        ``native_device_records``, and ``allows_camera(name)``.

        Idempotent: calling twice with the same plan is a no-op rewrite.
        Reading the manifest fresh each call avoids state drift if
        another writer (the finalize path) edited it in between.
        """
        if not self._opened:
            raise BundleWriterError("update_recording_plan() requires open()")
        manifest_path = self._bundle_path / "manifest.json"
        manifest = BundleManifest.read(manifest_path)
        recording = RecordingBlock(
            policy=policy_mode,
            source=plan.source,
            channel_mode=plan.channel_mode,
            recorded_channels=plan.recorded_channels,
            camera_mode=plan.camera_mode,
            recorded_cameras=plan.recorded_cameras,
            native_device_records=plan.native_device_records,
        )
        updated_cameras = tuple(
            entry.model_copy(
                update={
                    "recorded": plan.allows_camera(entry.name),
                    "suppressed_reason": (
                        None if plan.allows_camera(entry.name) else "recording_policy"
                    ),
                }
            )
            for entry in manifest.cameras
        )
        manifest = manifest.model_copy(update={"recording": recording, "cameras": updated_cameras})
        manifest.write(manifest_path)

    # ------------------------------------------------------------------ finalize / close

    def close_sinks(self) -> None:
        """Close every open sink. Idempotent. Used both by :meth:`finalize`
        and by callers that want to drop the bundle in a recoverable state
        (subsequent :func:`finalize_in_place` will pick it up)."""
        if self._sinks is None:
            return
        # Channel + device records first (Parquet), then SQLite, then log.
        self._sinks.channel_samples.close()
        self._sinks.device_records.close()
        for frames_sink in self._sinks.frames.values():
            frames_sink.close()
        self._sinks.events.close()
        self._sinks.status.close()
        self._sinks.log.close()

    def finalize(
        self,
        *,
        run_status: RunStatus = "completed",
        exit_reason: str | None = None,
        ended_utc: datetime | None = None,
        queue_health: dict[str, dict[str, float]] | None = None,
        equipment: list[dict[str, Any]] | None = None,
        cameras: list[dict[str, Any]] | None = None,
    ) -> FinalizeResult:
        """Close sinks and run :func:`finalize_in_place` against the bundle.

        Returns the :class:`FinalizeResult` from the rewrite path so callers
        (tests, the future engine) can assert on integrity status.

        ``equipment``: optional list of per-device blocks (``name``,
        ``adapter``, ``identity``) sourced from live adapters at run end.
        When supplied, ``equipment.toml`` is rewritten with this content
        *before* the integrity walk so the manifest checksum covers the
        identity-populated file. ``None`` keeps the open()-time stub —
        used by crash recovery and tests where adapters never opened.

        ``cameras``: same shape as ``equipment`` but for cameras. Drives a
        ``[[cameras]]`` section in ``equipment.toml`` and overrides
        ``manifest.json.cameras[*].model`` / ``serial`` at finalize time
        from the per-camera ``identity`` block. V4L2 identity is probed
        before finalize so it reaches both artefacts.
        """
        if not self._opened:
            raise BundleWriterError("finalize() requires open()")
        if self._finalized:
            raise BundleWriterError("finalize() already called")
        self.close_sinks()
        if equipment is not None or cameras is not None:
            self._rewrite_equipment_toml(equipment or [], cameras or [])
        result = finalize_in_place(
            self._bundle_path,
            run_status=run_status,
            exit_reason=exit_reason,
            ended_utc=ended_utc,
            queue_health=queue_health,
            cameras=cameras,
        )
        self._finalized = True
        return result

    def _rewrite_equipment_toml(
        self,
        equipment: list[dict[str, Any]],
        cameras: list[dict[str, Any]],
    ) -> None:
        """Replace the open()-time ``equipment.toml`` stub with the live
        per-adapter blocks. The stub captured only configured ``name`` +
        ``adapter`` and never reflected the actual probed identity (Watlow
        part number, Sartorius serial, …). The ``[[cameras]]`` section surfaces
        V4L2 / vendor camera identity too.
        """
        payload: dict[str, Any] = {"devices": equipment}
        if cameras:
            payload["cameras"] = cameras
        (self._bundle_path / "equipment.toml").write_text(
            tomli_w.dumps(_toml_safe(payload)), encoding="utf-8"
        )

    def __enter__(self) -> RunBundleWriter:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> None:
        if exc_type is not None:
            # Exceptional path: close sinks but don't finalize. Caller (or a
            # later finalize_in_place) decides whether the bundle is salvageable.
            self.close_sinks()
            return
        if self._opened and not self._finalized:
            self.close_sinks()

    # ------------------------------------------------------------------ internals

    def _write_initial_manifest(self) -> None:
        """Write ``manifest.json`` with ``bundle_status="open"``,
        ``run_status="running"``, full provenance, and an empty data_shape.
        """
        assert self._provenance is not None
        manifest = BundleManifest(
            run_id=self._run_id,
            started_utc=self._started_utc,
            started_mono_ns_anchor=self._started_mono_ns_anchor,
            run_status="running",
            bundle_status="open",
            operator=OperatorBlock(
                id=self._config.operator.id,
                display_name=self._config.operator.display_name,
            ),
            sample=SampleBlock.model_validate(self._config.sample.model_dump(mode="json")),
            procedure=ProcedureBlock(
                id=self._config.procedure.id,
                version=self._config.procedure.version,
            ),
            domain_profile=(
                DomainProfileBlock(
                    id=self._config.domain_profile.id,
                    standard_refs=self._config.domain_profile.standard_refs,
                )
                if self._config.domain_profile is not None
                else None
            ),
            tags=self._config.tags,
            capa=self._provenance.capa,
            python=self._provenance.python,
            platform=self._provenance.platform,
            lockfile=self._provenance.lockfile,
            plugins=self._provenance.plugins,
            cameras=tuple(
                _seed_camera_entry(c, run_id=self._run_id) for c in self._config.hardware.cameras
            ),
        )
        manifest.write(self._bundle_path / "manifest.json")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _seed_camera_entry(
    spec: CameraSpec,
    *,
    run_id: str,
    recorded: bool = True,
) -> CameraEntry:
    """Build a :class:`CameraEntry` from a :class:`CameraSpec` at arm-time.

    Counts and frames-path are filled in at finalize; the seed entry only
    captures spec-derived fields so a reader catching the bundle mid-run
    sees what the operator declared. ``recorded=False`` marks the entry
    as suppressed-by-policy — paired with the manifest's ``recording``
    block, it tells the reader "no video file landed for this camera and
    that's the intended outcome, not a failure."
    """
    ext = ".csq" if spec.kind == "ir" else ".mkv"
    bundle_rel = f"{VIDEO_DIRNAME}/{spec.name}{ext}"
    if spec.output_root is not None:
        # Keep this in sync with CameraDeviceAdapter._resolve_output_path().
        external = str(
            Path(spec.output_root).expanduser()
            / run_id
            / VIDEO_DIRNAME
            / f"{spec.name}{ext}"
        )
    else:
        external = None
    return CameraEntry(
        name=spec.name,
        adapter=spec.adapter,
        kind=spec.kind,
        model=spec.model_hint,
        serial=spec.serial,
        output_path=bundle_rel,
        output_path_external=external,
        on_failure=spec.on_failure,
        recorded=recorded,
        suppressed_reason=None if recorded else "recording_policy",
    )


def _toml_safe(value: Any) -> Any:
    """Recursively coerce Pydantic-dump output into TOML-safe primitives.

    tomli_w refuses ``None`` values, tuples-of-mixed-type, and any object
    outside its built-in primitive whitelist (str/int/float/bool/datetime).
    Equipment-block identities may carry vendor-library types like
    ``FirmwareVersion`` that survive the JSON-mode dump — coerce them via
    ``str()`` rather than letting them blow up the finalize.
    """
    if isinstance(value, dict):
        return {k: _toml_safe(v) for k, v in value.items() if v is not None}
    if isinstance(value, list | tuple):
        return [_toml_safe(v) for v in value if v is not None]
    if (
        isinstance(value, str | int | float | bool | _dt.datetime | _dt.date | _dt.time)
        or value is None
    ):
        return value
    return str(value)


def _json_dumps(value: Any) -> str:
    """Stable JSON dump, two-space indent, trailing newline."""
    return json.dumps(value, indent=2, sort_keys=False) + "\n"


__all__ = [
    "BundleWriterError",
    "RunBundleWriter",
]
