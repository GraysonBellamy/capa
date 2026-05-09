"""Pure-function finalize-in-place.

Plan §8.5 / §13.3. Walks a bundle directory and:

1. Rewrites every ``*.in-flight.arrows`` (Arrow IPC stream) to its final
   ``.parquet`` form with large row groups, sorted by ``t_mono_ns`` where
   present, ``zstd:6``-compressed. Torn / unreadable in-flight files are
   logged to ``manifest.custom["finalize_warnings"]`` and removed.
2. Updates :class:`~capa.storage.manifest.BundleManifest` ``ended_utc``,
   ``run_status``, and ``data_shape``.
3. Computes ``manifest.sha256`` and writes it.
4. Stamps ``bundle_status`` (``finalizing`` → ``finalized_unverified`` →
   ``sealed`` or ``verification_failed``) and ``integrity.status`` along the way.

Idempotent: running on an already-sealed bundle is a no-op (same digest;
manifest unchanged). Running on a bundle whose in-flight files are missing
but final files exist behaves as "verify and seal."

This module is library-only — the ``capa finalize`` CLI lands in P0c. The
function is exposed here so the bundle writer's normal-exit path and the
crash-recovery path can both call it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from capa.core.errors import CapaError
from capa.storage._ipc import read_recoverable
from capa.storage.channel_samples_sink import (
    FINAL_FILENAME as CHANNEL_SAMPLES_FINAL,
)
from capa.storage.channel_samples_sink import (
    INFLIGHT_FILENAME as CHANNEL_SAMPLES_INFLIGHT,
)
from capa.storage.device_records_sink import (
    DEVICE_RECORDS_DIRNAME,
    FINAL_SUFFIX,
    INFLIGHT_SUFFIX,
)
from capa.storage.integrity import (
    MANIFEST_FILENAME,
    VerifyResult,
    verify,
    write_manifest_sha256,
)
from capa.storage.manifest import (
    BundleManifest,
    CameraEntry,
    DataShape,
    DataShapeChannelSamples,
    DataShapeRecord,
    IntegrityBlock,
    QueueHealthEntry,
    RunStatus,
    is_legal_finalize_combination,
)
from capa.storage.video_sink import (
    FINAL_SUFFIX as FRAMES_FINAL_SUFFIX,
)
from capa.storage.video_sink import (
    INFLIGHT_SUFFIX as FRAMES_INFLIGHT_SUFFIX,
)
from capa.storage.video_sink import (
    VIDEO_DIRNAME,
)

FINAL_ROW_GROUP_ROWS = 262_144
"""Plan §8.5: 256k rows per row group in the finalized files. Tuned for
DuckDB / Polars / Arrow scan throughput. Module-level so tests can drop it
to exercise the rewrite on small synthetic runs."""

FINAL_COMPRESSION = "zstd"
FINAL_COMPRESSION_LEVEL = 6


class FinalizeError(CapaError):
    """Raised on finalize-side failures (illegal state transitions, malformed
    in-flight files that can't be rewritten)."""


@dataclass(frozen=True, slots=True)
class FinalizeResult:
    """Summary of what :func:`finalize_in_place` did."""

    rewrote: tuple[str, ...]
    """Relative POSIX paths of files written/refreshed in this call."""

    skipped_already_final: tuple[str, ...]
    """Paths that were already final and untouched."""

    integrity: VerifyResult
    """Outcome of the post-write verification walk."""


# ---------------------------------------------------------------------------
# In-flight rewrite — the IPC-stream → final-parquet stage of plan §8.5.
# ---------------------------------------------------------------------------


def _rewrite_inflight_to_parquet(in_flight: Path, final: Path) -> bool:
    """Read ``in_flight`` (Arrow IPC stream) and rewrite it to ``final`` parquet.

    Sorts by ``t_mono_ns`` if that column is present (it always is for the
    sinks P0b ships). The rewrite is whole-file in memory — fine for the
    sample sizes capa produces (an hour of 60 Hz × 30 channels = ~6.5M rows
    of 13 thin columns, easily under 1 GiB). A future P6 task can stream it.

    Returns ``True`` if the final parquet was written, ``False`` if the
    in-flight file is unrecoverable (torn before its first flush boundary)
    or has zero rows. The caller is expected to log a finalize warning and
    remove the unrecoverable file in the False case.
    """
    table = read_recoverable(in_flight)
    if table is None or table.num_rows == 0:
        return False
    if "t_mono_ns" in table.column_names:
        table = table.sort_by([("t_mono_ns", "ascending")])

    tmp = final.with_suffix(final.suffix + ".tmp")
    pq.write_table(
        table,
        tmp,
        compression=FINAL_COMPRESSION,
        compression_level=FINAL_COMPRESSION_LEVEL,
        row_group_size=FINAL_ROW_GROUP_ROWS,
        data_page_version="2.0",
    )
    tmp.replace(final)
    return True


def _scan_inflight_pairs(bundle_root: Path) -> list[tuple[Path, Path]]:
    """Find every ``*.in-flight.arrows`` and the final path it should land at.

    Returns ``[(in_flight_path, final_path), ...]``. Covers the top-level
    ``scalars.in-flight.arrows``, ``device_records/<adapter>.in-flight.arrows``,
    and per-camera ``video/<camera>.frames.in-flight.arrows``.
    """
    pairs: list[tuple[Path, Path]] = []

    cs_inflight = bundle_root / CHANNEL_SAMPLES_INFLIGHT
    cs_final = bundle_root / CHANNEL_SAMPLES_FINAL
    if cs_inflight.is_file():
        pairs.append((cs_inflight, cs_final))

    dr_dir = bundle_root / DEVICE_RECORDS_DIRNAME
    if dr_dir.is_dir():
        for path in sorted(dr_dir.iterdir()):
            if not path.is_file() or not path.name.endswith(INFLIGHT_SUFFIX):
                continue
            adapter = path.name[: -len(INFLIGHT_SUFFIX)]
            final = dr_dir / f"{adapter}{FINAL_SUFFIX}"
            pairs.append((path, final))

    # Per-camera frame-index parquets (plan §12.5). One in-flight file per
    # camera that actually emitted frames; cameras that opened but never
    # recorded leave nothing on disk.
    video_dir = bundle_root / VIDEO_DIRNAME
    if video_dir.is_dir():
        for path in sorted(video_dir.iterdir()):
            if not path.is_file() or not path.name.endswith(FRAMES_INFLIGHT_SUFFIX):
                continue
            camera = path.name[: -len(FRAMES_INFLIGHT_SUFFIX)]
            final = video_dir / f"{camera}{FRAMES_FINAL_SUFFIX}"
            pairs.append((path, final))
    return pairs


def _device_records_data_shape(bundle_root: Path) -> tuple[DataShapeRecord, ...]:
    """Build manifest ``data_shape.device_records`` entries from on-disk
    final files. Walks ``device_records/`` after the rewrite."""
    dr_dir = bundle_root / DEVICE_RECORDS_DIRNAME
    if not dr_dir.is_dir():
        return ()
    out: list[DataShapeRecord] = []
    for path in sorted(dr_dir.iterdir()):
        if not path.is_file() or not path.name.endswith(FINAL_SUFFIX):
            continue
        if INFLIGHT_SUFFIX in path.name:
            continue  # safety net — shouldn't happen post-rewrite
        adapter = path.name[: -len(FINAL_SUFFIX)]
        # Detect the layout from the first row's columns. Heuristics keyed
        # on adapter name keep the manifest's ``layout`` honest without
        # re-inferring shape from Arrow metadata.
        layout = _infer_layout_for_adapter(adapter)
        out.append(
            DataShapeRecord(
                adapter=adapter,
                path=f"{DEVICE_RECORDS_DIRNAME}/{path.name}",
                layout=layout,
            )
        )
    return tuple(out)


_KNOWN_LAYOUTS: dict[str, str] = {
    "alicat": "wide_row",
    "watlow": "long_row",
    "sartorius": "single_value_row",
    "nidaq_polled": "wide_row",
    "nidaq_block": "block",
}


def _infer_layout_for_adapter(adapter: str) -> str:
    """Look up the documented layout for an adapter family.

    Falls back to ``wide_row`` for unknown adapters — that's the safest
    default for "I don't know what this library emits." The plan's §8.9
    table is the source of truth; new adapters add an entry here at the
    same time they ship.
    """
    return _KNOWN_LAYOUTS.get(adapter, "wide_row")


def _refresh_cameras_block(
    bundle_root: Path,
    seeded: tuple[CameraEntry, ...],
    *,
    identity_overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[CameraEntry, ...]:
    """Update the manifest's cameras list with finalize-time facts.

    For each seeded entry, look up the on-disk artifacts and refresh
    ``frames_path``, ``frame_count``, ``meta_path``, and the
    ``started_mono_ns_offset`` (read from the meta sidecar when present).
    Cameras that never recorded anything keep the seeded values minus
    ``frames_path`` and ``meta_path``, both of which stay ``None``.

    ``identity_overrides`` (camera name → identity dict) overrides
    ``model`` / ``serial`` from a live-probed source. Hardware-day
    2026-05-09 PM finding #2 — the seed entries read static
    ``CameraSpec.model_hint`` / ``serial`` at arm-time, before
    :meth:`Camera.open` runs the V4L2 / vendor probe. Finalize-time
    overrides plumb the live values through.
    """
    video_dir = bundle_root / VIDEO_DIRNAME
    out: list[CameraEntry] = []
    for entry in seeded:
        frames_path: str | None = None
        meta_path: str | None = None
        frame_count = 0
        started_offset = entry.started_mono_ns_offset
        frames_file = video_dir / f"{entry.name}{FRAMES_FINAL_SUFFIX}"
        if frames_file.is_file():
            frames_path = f"{VIDEO_DIRNAME}/{frames_file.name}"
            try:
                table = pq.read_metadata(frames_file)
                frame_count = int(table.num_rows)
            except Exception:
                frame_count = 0
        # IR sim writes ``<name>.csq.meta.json``; webcam writes nothing
        # right now. Look for either.
        for candidate in (
            video_dir / f"{entry.name}.csq.meta.json",
            video_dir / f"{entry.name}.meta.json",
        ):
            if candidate.is_file():
                meta_path = f"{VIDEO_DIRNAME}/{candidate.name}"
                offset = _read_meta_anchor(candidate)
                if offset is not None:
                    started_offset = offset
                break
        update: dict[str, Any] = {
            "frames_path": frames_path,
            "meta_path": meta_path,
            "frame_count": frame_count,
            "started_mono_ns_offset": started_offset,
        }
        if identity_overrides is not None:
            override = identity_overrides.get(entry.name)
            if override:
                # Only the manifest-visible identity fields. Other entries
                # in the identity dict (firmware, family, …) belong in
                # ``equipment.toml`` only.
                if override.get("model") is not None:
                    update["model"] = override["model"]
                if override.get("serial") is not None:
                    update["serial"] = override["serial"]
        out.append(entry.model_copy(update=update))
    return tuple(out)


def _read_meta_anchor(meta_path: Path) -> int | None:
    """Pull the ``started_mono_ns_offset`` from a meta-JSON sidecar if present."""
    try:
        with open(meta_path, "rb") as fp:
            data = json.load(fp)
    except (OSError, json.JSONDecodeError):
        return None
    value = data.get("started_mono_ns_offset")
    return int(value) if isinstance(value, int) else None


# ---------------------------------------------------------------------------
# Top-level finalize
# ---------------------------------------------------------------------------


def finalize_in_place(
    bundle_root: Path,
    *,
    run_status: RunStatus,
    exit_reason: str | None = None,
    inferred_ended_utc: bool | None = None,
    ended_utc: datetime | None = None,
    queue_health: dict[str, dict[str, float]] | None = None,
    cameras: list[dict[str, Any]] | None = None,
) -> FinalizeResult:
    """Rewrite in-flight Parquet, compute integrity, seal the bundle.

    Args:
        bundle_root: directory containing ``manifest.json``.
        run_status: ``"completed" | "aborted" | "crashed"`` — the scientific
            outcome. Plan §13.3: a crashed bundle still seals as a sealed
            artifact with ``run_status="crashed"``.
        exit_reason: human-readable detail when ``run_status`` is non-normal.
            Recorded in the manifest ``exit_reason`` field.
        inferred_ended_utc: when ``True``, mark in the manifest's ``custom``
            block that ``ended_utc`` was reconstructed (plan §13.3). Used by
            crash-recovery callers when the engine never wrote a clean
            ``ended_utc``.
        ended_utc: explicit end timestamp. ``None`` (default) means "use
            now-UTC unless the manifest already records one."
        queue_health: per-collector histogram dict produced by
            :meth:`MetricsRegistry.snapshot_for_manifest`. Plan §7.1 / §13.1
            — folded into ``manifest.queue_health`` if provided.

    Returns:
        A :class:`FinalizeResult` describing what changed.

    Raises:
        FinalizeError: on illegal state combinations, missing manifest,
            unrewriteable in-flight files.
    """
    bundle_root = Path(bundle_root).resolve()
    manifest_path = bundle_root / "manifest.json"
    if not manifest_path.is_file():
        raise FinalizeError(f"bundle has no manifest.json: {bundle_root}")

    manifest = BundleManifest.read(manifest_path)

    # Stamp finalizing — the bundle is now mid-transition. Atomic write means
    # a reader that catches us mid-rewrite sees ``finalizing`` rather than
    # the half-stale ``open``.
    if manifest.bundle_status == "open":
        manifest = manifest.model_copy(update={"bundle_status": "finalizing"})
        manifest.write(manifest_path)

    # Stage 1: in-flight → final rewrite. Torn files (no readable schema)
    # are logged via finalize_warnings and removed; sinks are already closed
    # by the time finalize runs, so an event row isn't an option.
    rewrote: list[str] = []
    skipped: list[str] = []
    finalize_warnings: list[dict[str, str]] = []
    for in_flight, final in _scan_inflight_pairs(bundle_root):
        wrote = _rewrite_inflight_to_parquet(in_flight, final)
        in_flight.unlink()
        if wrote:
            rewrote.append(final.relative_to(bundle_root).as_posix())
        else:
            finalize_warnings.append(
                {
                    "path": in_flight.relative_to(bundle_root).as_posix(),
                    "reason": "in-flight stream unrecoverable (torn before first flush)",
                }
            )

    # Track files that were already final (no in-flight pair). Useful for
    # idempotency on a re-run of finalize.
    cs_final = bundle_root / CHANNEL_SAMPLES_FINAL
    if cs_final.is_file() and CHANNEL_SAMPLES_FINAL not in rewrote:
        skipped.append(CHANNEL_SAMPLES_FINAL)
    dr_dir = bundle_root / DEVICE_RECORDS_DIRNAME
    if dr_dir.is_dir():
        for path in sorted(dr_dir.iterdir()):
            if not path.is_file() or not path.name.endswith(FINAL_SUFFIX):
                continue
            rel = path.relative_to(bundle_root).as_posix()
            if rel not in rewrote:
                skipped.append(rel)

    # Stage 2: refresh manifest's data_shape + run/bundle status.
    update: dict[str, object] = {}
    update["run_status"] = run_status
    if exit_reason is not None:
        update["exit_reason"] = exit_reason

    if manifest.ended_utc is None:
        update["ended_utc"] = ended_utc or datetime.now(UTC)

    new_data_shape = DataShape(
        channel_samples=(
            DataShapeChannelSamples(path=CHANNEL_SAMPLES_FINAL) if cs_final.is_file() else None
        ),
        device_records=_device_records_data_shape(bundle_root),
    )
    update["data_shape"] = new_data_shape

    if manifest.cameras:
        identity_map: dict[str, Mapping[str, Any]] | None = None
        if cameras is not None:
            identity_map = {}
            for block in cameras:
                identity = block.get("identity")
                if identity:
                    identity_map[block["name"]] = identity
        update["cameras"] = _refresh_cameras_block(
            bundle_root,
            manifest.cameras,
            identity_overrides=identity_map,
        )

    if inferred_ended_utc or finalize_warnings:
        custom = dict(manifest.custom)
        if inferred_ended_utc:
            custom["inferred_ended_utc"] = True
        if finalize_warnings:
            custom["finalize_warnings"] = finalize_warnings
        update["custom"] = custom

    if queue_health is not None:
        update["queue_health"] = {
            name: QueueHealthEntry.model_validate(stats) for name, stats in queue_health.items()
        }

    # Stage 3: optimistic seal — write the *target* manifest first so its
    # bytes are what manifest.sha256 records. Plan §8.2 has manifest.sha256
    # cover every artifact including manifest.json; the chicken-and-egg
    # resolves cleanly if we lock the manifest's final form before hashing.
    update["bundle_status"] = "sealed"
    update["integrity"] = IntegrityBlock(
        status="ok",
        manifest_sha256_path=MANIFEST_FILENAME,
    )

    if not is_legal_finalize_combination(run_status, "sealed"):
        raise FinalizeError(
            f"illegal run/bundle combination during finalize: run={run_status} bundle=sealed"
        )

    manifest = manifest.model_copy(update=update)
    manifest.write(manifest_path)

    # Stage 4: compute manifest.sha256 across the now-sealed bundle. If
    # hashing or verification fails (disk full, race with an external
    # process), drop bundle_status back to verification_failed so a reader
    # knows not to trust the seal.
    try:
        write_manifest_sha256(bundle_root)
        result = verify(bundle_root)
    except Exception as exc:
        manifest = manifest.model_copy(
            update={
                "bundle_status": "verification_failed",
                "integrity": IntegrityBlock(
                    status="unknown",
                    manifest_sha256_path=MANIFEST_FILENAME,
                ),
                "exit_reason": (manifest.exit_reason or "") + f" finalize-integrity failed: {exc}",
            }
        )
        manifest.write(manifest_path)
        raise FinalizeError(f"integrity step failed: {exc}") from exc

    if result.status != "ok":
        manifest = manifest.model_copy(
            update={
                "bundle_status": "verification_failed",
                "integrity": IntegrityBlock(
                    status=result.status,
                    manifest_sha256_path=MANIFEST_FILENAME,
                ),
            }
        )
        manifest.write(manifest_path)

    return FinalizeResult(
        rewrote=tuple(rewrote),
        skipped_already_final=tuple(skipped),
        integrity=result,
    )


__all__ = [
    "FINAL_COMPRESSION",
    "FINAL_COMPRESSION_LEVEL",
    "FINAL_ROW_GROUP_ROWS",
    "FinalizeError",
    "FinalizeResult",
    "finalize_in_place",
]
