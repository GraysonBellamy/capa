""":class:`BundleManifest` — Pydantic model for ``manifest.json``.

The manifest is the bundle's index card: every read tool starts
here, and every field is required (or explicitly ``None`` with a comment).
The structure mirrors the example block in the plan exactly.

Populates everything that does not depend on the engine task group; the
``queue_health`` and ``dropped_samples`` blocks are populated by the metrics module.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from capa.storage.schema import BUNDLE_SCHEMA_VERSION, BundleSchemaError, migrate

RunStatus = Literal["running", "completed", "aborted", "crashed"]
"""* ``running``: acquisition active.
* ``completed``: method or free-run ended normally.
* ``aborted``: operator/safety stopped the run.
* ``crashed``: recovered/finalized after abnormal termination.
"""


BundleStatus = Literal[
    "open",
    "finalizing",
    "finalized_unverified",
    "sealed",
    "verification_failed",
]
"""* ``open``: files may still be mid-write.
* ``finalizing``: sinks closed, two-stage rewrite in progress.
* ``finalized_unverified``: data is readable, integrity hashes pending.
* ``sealed``: ``manifest.sha256`` written; safe to copy/archive.
* ``verification_failed``: enough finalized to inspect, but integrity failed.
"""


IntegrityStatus = Literal["unknown", "ok", "mismatch", "partial"]


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class OperatorBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    display_name: str | None = None


class SampleBlock(BaseModel):
    """Free-form copy of :class:`~capa.experiment.config.SampleInfo` plus extras.

    Stored as ``dict[str, Any]`` rather than the Pydantic model so we can
    round-trip extra/profile fields without re-validating against today's
    SampleInfo schema.
    """

    model_config = ConfigDict(extra="allow")
    id: str


class ProcedureBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    version: str | None = None


class DomainProfileBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    standard_refs: tuple[str, ...] = Field(default_factory=tuple)


class CapaBlock(BaseModel):
    """Capa-version provenance, captured by :func:`gather_provenance`."""

    model_config = ConfigDict(extra="forbid")
    version: str
    git_sha: str | None = None
    git_dirty: bool | None = None
    build_time: datetime | None = None
    engine_version: str | None = None
    """Engine-task-group revision marker. Populated by the engine at run-start
    so a post-mortem can tell "which engine code wrote this bundle" even when
    the package version is unchanged. """


class PythonBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    implementation: str
    executable: str


class PlatformBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    os: str
    machine: str
    node: str


class LockfileBlock(BaseModel):
    """Pointer to the in-bundle ``env/uv.lock`` plus its sha256."""

    model_config = ConfigDict(extra="forbid")
    path: str | None
    """Relative to the bundle root. ``None`` when no lockfile was found at
    snapshot time (the bundle still records the absence honestly)."""
    sha256: str | None


class PluginEntryBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    version: str
    package: str
    entry_point: str
    distribution_hash: str


class DataShapeRecord(BaseModel):
    """One entry in ``data_shape.device_records``."""

    model_config = ConfigDict(extra="forbid")
    adapter: str
    path: str
    layout: Literal["wide_row", "long_row", "single_value_row", "block"]


class DataShapeChannelSamples(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    layout: Literal["normalized_long"] = "normalized_long"


class DataShape(BaseModel):
    """maps each on-disk artifact to its layout tag."""

    model_config = ConfigDict(extra="forbid")
    channel_samples: DataShapeChannelSamples | None = None
    device_records: tuple[DataShapeRecord, ...] = Field(default_factory=tuple)


class QueueHealthEntry(BaseModel):
    """One row in :attr:`BundleManifest.queue_health`.

    Queue collectors populate ``depth_*`` / ``lag_s_max``; writer collectors
    populate ``write_*`` extras (allowed via ``extra="allow"`` so adding a
    new collector doesn't require a schema bump).
    """

    model_config = ConfigDict(extra="allow")
    depth_p50: float = 0.0
    depth_p99: float = 0.0
    depth_max: float = 0.0
    lag_s_max: float = 0.0


class IntegrityBlock(BaseModel):
    """post-finalize integrity verdict."""

    model_config = ConfigDict(extra="forbid")
    status: IntegrityStatus = "unknown"
    manifest_sha256_path: str = "manifest.sha256"
    algorithm: Literal["sha256"] = "sha256"


class CameraEntry(BaseModel):
    """One row in :attr:`BundleManifest.cameras`. Captures everything a downstream tool needs to match a camera's frames
    back to the bundle without parsing the container itself: which adapter
    wrote it, where the file lives (in-bundle or via the §12.4 escape
    hatch), the frame-index parquet, the meta-JSON sidecar (IR only), the
    final frame count, and the run-relative ``started_mono_ns_offset``
    captured at ``start_recording`` (anchor).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    adapter: str
    """Adapter id (``"webcam"``, ``"flir_ir_sim"``, ``"flir_ir"``)."""
    kind: Literal["visible", "ir"]
    model: str | None = None
    serial: str | None = None
    output_path: str
    """Bundle-relative POSIX path to the container file. Even when the §12.4
    ``output_root`` escape hatch is in effect, this still records the
    relative-reference name so analysis tools can locate the file given
    the ``output_path_external`` absolute path below."""
    output_path_external: str | None = None
    """Absolute path when the camera's :attr:`CameraSpec.output_root`
    overrode the bundle directory (). ``None`` for the common
    case where the file lives inside the bundle."""
    frames_path: str | None = None
    """Bundle-relative path to ``<name>.frames.parquet``. ``None`` until the
    finalize stage rewrites the in-flight file."""
    meta_path: str | None = None
    """Bundle-relative path to ``<name>.csq.meta.json`` (IR sim) or any other
    adapter-specific sidecar. ``None`` for visible cameras that don't write
    a sidecar."""
    frame_count: int = 0
    started_mono_ns_offset: int = 0
    """``RunClock.t_mono_ns()`` captured at :meth:`Camera.start_recording`
    ()."""
    on_failure: Literal["warn", "abort_run", "safe_shutdown"] = "warn"
    healthy: bool = True
    error: str | None = None


# ---------------------------------------------------------------------------
# BundleManifest — the top-level model
# ---------------------------------------------------------------------------


class BundleManifest(BaseModel):
    """Top-level model for ``manifest.json``.

    Every field corresponds to the example. ``extra="forbid"`` keeps
    accidental drift out of the canonical surface; sub-models that genuinely
    need extensibility (``SampleBlock``, ``custom``) opt in explicitly.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    bundle_schema_version: int = BUNDLE_SCHEMA_VERSION

    started_utc: datetime
    ended_utc: datetime | None = None
    started_mono_ns_anchor: int

    run_status: RunStatus = "running"
    bundle_status: BundleStatus = "open"
    exit_reason: str | None = None

    operator: OperatorBlock
    sample: SampleBlock
    procedure: ProcedureBlock
    domain_profile: DomainProfileBlock | None = None
    tags: tuple[str, ...] = Field(default_factory=tuple)

    capa: CapaBlock
    python: PythonBlock
    platform: PlatformBlock
    lockfile: LockfileBlock
    plugins: tuple[PluginEntryBlock, ...] = Field(default_factory=tuple)

    data_shape: DataShape = Field(default_factory=DataShape)

    queue_health: dict[str, QueueHealthEntry] = Field(default_factory=dict)
    """Populated by the metrics module."""

    dropped_samples: dict[str, int] = Field(default_factory=dict)
    """Populated by the ringbuffer/metrics module."""

    integrity: IntegrityBlock = Field(default_factory=IntegrityBlock)

    cameras: tuple[CameraEntry, ...] = Field(default_factory=tuple)
    """Per-camera summary (). Populated by the bundle writer at
    arm-time with the spec-derived fields and refreshed at finalize with
    the final frame count + frames.parquet path."""

    custom: dict[str, Any] = Field(default_factory=dict)
    """Free-form bag for procedure/profile-specific summary metadata. Sinks
    never write here; procedures may stamp run-summary numbers at finalize."""

    # ------------------------------------------------------------------ I/O

    def to_json_bytes(self) -> bytes:
        """Stable, indented, ASCII-safe JSON (always trailing newline).

        Stable: Pydantic's ``model_dump`` with ``mode="json"`` produces the
        same key order across runs. ``ensure_ascii=False`` would let unicode
        through, but operator names with non-ASCII are still safer round-
        tripped via ``\\u`` escapes — readers don't need to guess encoding.
        """
        payload = self.model_dump(mode="json")
        return (json.dumps(payload, indent=2, sort_keys=False) + "\n").encode("utf-8")

    @classmethod
    def read(cls, path: str | Path) -> BundleManifest:
        """Load and validate a manifest from disk, applying any registered
        schema migrations first.

        Raises :class:`~capa.storage.schema.BundleSchemaError` for unknown or
        unmigrateable versions.
        """
        with open(path, "rb") as fp:
            data = json.load(fp)
        migrated = migrate(data)
        return cls.model_validate(migrated)

    def write(self, path: str | Path) -> None:
        """Atomically write the manifest to ``path``.

        Atomic = "write to ``<path>.tmp`` then rename". Manifest readers (the
        catalog, capa finalize) must never observe a half-written file.
        """
        target = Path(path)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(self.to_json_bytes())
        tmp.replace(target)


def is_legal_finalize_combination(run_status: RunStatus, bundle_status: BundleStatus) -> bool:
    """``run_status`` and ``bundle_status`` are deliberately
    independent (an aborted or crashed run can still seal cleanly), but a
    handful of combinations don't make sense.

    Used by :class:`~capa.storage.bundle.RunBundleWriter.finalize` to refuse
    impossible callers. Specifically, the bundle cannot transition past
    ``open`` while the run is still ``running``.
    """
    return not (run_status == "running" and bundle_status != "open")


__all__ = [
    "BundleManifest",
    "BundleStatus",
    "CameraEntry",
    "CapaBlock",
    "DataShape",
    "DataShapeChannelSamples",
    "DataShapeRecord",
    "DomainProfileBlock",
    "IntegrityBlock",
    "IntegrityStatus",
    "LockfileBlock",
    "OperatorBlock",
    "PlatformBlock",
    "PluginEntryBlock",
    "ProcedureBlock",
    "PythonBlock",
    "QueueHealthEntry",
    "RunStatus",
    "SampleBlock",
    "is_legal_finalize_combination",
]


# ---------------------------------------------------------------------------
# Re-export at parent module so callers can `from capa.storage import migrate`
# without dragging the schema dance through every import. The schema module
# exposes BundleSchemaError too; we re-export in case downstream code wants
# both alongside the manifest.
# ---------------------------------------------------------------------------

# (Re-export — see __init__.py.)
_ = BundleSchemaError  # silence unused-import; kept for re-export ergonomics.
