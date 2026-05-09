""":class:`RunCatalog` — cross-run SQLite index at ``<runs_root>/runs.sqlite``.

Plan §8.4. Indexes every bundle on disk so the Review tab and CLI can answer
"what runs do we have?" without walking ``runs/`` and parsing every manifest.

The catalog is **not** the source of truth — the bundle is. It's a rebuildable
index. On startup, any run whose ``ended_utc`` is null is flipped to
``run_status="crashed"`` (plan §13.3).

Tables:

* ``runs`` — one row per bundle, mirroring manifest fields.
* ``operators`` — operator id directory (mostly populated at run-insert).
* ``artifacts`` — sha256/size for each named artifact, used for the
  ``capa catalog verify`` summary view.

Schema is intentionally narrow — every column maps to a question someone will
ask when reading the catalog. Free-form metadata lives in ``tags_json`` /
``summary_json`` so adding a new tag does not require a schema migration.
"""

from __future__ import annotations

import builtins
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from capa.core.errors import CapaError
from capa.storage.integrity import VerifyResult, verify
from capa.storage.manifest import (
    BundleManifest,
    BundleStatus,
    IntegrityStatus,
    RunStatus,
)

CATALOG_FILENAME = "runs.sqlite"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    path              TEXT NOT NULL,
    started_utc       TEXT NOT NULL,
    ended_utc         TEXT,
    operator_id       TEXT,
    sample_id         TEXT,
    procedure         TEXT,
    capa_version      TEXT,
    capa_git_sha      TEXT,
    run_status        TEXT NOT NULL,
    bundle_status     TEXT NOT NULL,
    schema_version    INTEGER NOT NULL,
    integrity_status  TEXT NOT NULL,
    tags_json         TEXT,
    summary_json      TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_started_utc ON runs (started_utc);
CREATE INDEX IF NOT EXISTS idx_runs_run_status ON runs (run_status);
CREATE INDEX IF NOT EXISTS idx_runs_bundle_status ON runs (bundle_status);

CREATE TABLE IF NOT EXISTS operators (
    id            TEXT PRIMARY KEY,
    display_name  TEXT,
    active        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS artifacts (
    run_id        TEXT NOT NULL,
    kind          TEXT NOT NULL,
    path          TEXT NOT NULL,
    sha256        TEXT,
    size_bytes    INTEGER,
    metadata_json TEXT,
    PRIMARY KEY (run_id, path)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_run_id ON artifacts (run_id);
"""


class CatalogError(CapaError):
    """Raised on catalog-side failures (corrupt catalog file, missing bundle
    when a refresh asked for it)."""


# ---------------------------------------------------------------------------
# Row dataclass for read-side ergonomics. Inserts use the manifest directly.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CatalogRow:
    """Read-side projection of a row in ``runs``. Used by the CLI's
    ``capa catalog list`` command."""

    run_id: str
    path: str
    started_utc: datetime
    ended_utc: datetime | None
    operator_id: str | None
    sample_id: str | None
    procedure: str | None
    capa_version: str | None
    capa_git_sha: str | None
    run_status: RunStatus
    bundle_status: BundleStatus
    schema_version: int
    integrity_status: IntegrityStatus
    tags: tuple[str, ...]
    summary: dict[str, Any]


# ---------------------------------------------------------------------------
# RunCatalog — connection-owning class.
# ---------------------------------------------------------------------------


class RunCatalog:
    """SQLite-backed run index.

    One connection per :class:`RunCatalog`; commits are explicit (autocommit
    via ``isolation_level=None``). Every public method is safe to call
    repeatedly; the catalog is rebuildable from the bundles on disk via
    :meth:`rebuild_from_disk`.
    """

    __slots__ = ("_closed", "_conn", "_path", "_runs_root")

    def __init__(self, runs_root: str | Path) -> None:
        self._runs_root = Path(runs_root)
        self._runs_root.mkdir(parents=True, exist_ok=True)
        self._path = self._runs_root / CATALOG_FILENAME
        self._closed = False
        self._conn = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode = WAL;")
        self._conn.execute("PRAGMA synchronous = NORMAL;")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def runs_root(self) -> Path:
        return self._runs_root

    # ------------------------------------------------------------------ inserts

    def upsert_operator(self, operator_id: str, display_name: str | None) -> None:
        self._conn.execute(
            """
            INSERT INTO operators (id, display_name, active)
            VALUES (?, ?, 1)
            ON CONFLICT(id) DO UPDATE SET display_name = excluded.display_name;
            """,
            (operator_id, display_name),
        )

    def insert_run_at_open(self, manifest: BundleManifest, *, bundle_path: Path) -> None:
        """Called by the engine immediately after :meth:`RunBundleWriter.open`.

        Inserts a row reflecting the open bundle so an external observer (a
        second capa process, an operator running ``capa catalog list``) can
        see the run in flight.
        """
        self.upsert_operator(manifest.operator.id, manifest.operator.display_name)
        self._conn.execute(
            """
            INSERT INTO runs (
                run_id, path, started_utc, ended_utc,
                operator_id, sample_id, procedure,
                capa_version, capa_git_sha,
                run_status, bundle_status, schema_version, integrity_status,
                tags_json, summary_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                path = excluded.path,
                started_utc = excluded.started_utc,
                run_status = excluded.run_status,
                bundle_status = excluded.bundle_status,
                schema_version = excluded.schema_version,
                integrity_status = excluded.integrity_status;
            """,
            (
                manifest.run_id,
                str(bundle_path.resolve()),
                manifest.started_utc.isoformat(),
                manifest.ended_utc.isoformat() if manifest.ended_utc else None,
                manifest.operator.id,
                manifest.sample.id,
                manifest.procedure.id,
                manifest.capa.version,
                manifest.capa.git_sha,
                manifest.run_status,
                manifest.bundle_status,
                manifest.bundle_schema_version,
                manifest.integrity.status,
                json.dumps(list(manifest.tags)) if manifest.tags else None,
                None,
            ),
        )

    def update_at_finalize(self, manifest: BundleManifest, *, bundle_path: Path) -> None:
        """Mirror the post-finalize manifest fields back into the catalog row."""
        summary = {"queue_health": _serialize_queue_health(manifest)}
        self._conn.execute(
            """
            UPDATE runs SET
                ended_utc = ?,
                run_status = ?,
                bundle_status = ?,
                integrity_status = ?,
                summary_json = ?,
                path = ?
            WHERE run_id = ?;
            """,
            (
                manifest.ended_utc.isoformat() if manifest.ended_utc else None,
                manifest.run_status,
                manifest.bundle_status,
                manifest.integrity.status,
                json.dumps(summary),
                str(bundle_path.resolve()),
                manifest.run_id,
            ),
        )

    # ------------------------------------------------------------------ recovery

    def flip_orphans(self) -> list[str]:
        """Plan §13.3: any row with ``run_status="running"`` at process start
        is the corpse of a previous capa instance. Flip it to ``crashed`` so
        the operator sees what happened. Returns the affected run ids.
        """
        rows = self._conn.execute(
            "SELECT run_id FROM runs WHERE run_status = 'running';"
        ).fetchall()
        ids = [row["run_id"] for row in rows]
        if ids:
            self._conn.executemany(
                "UPDATE runs SET run_status = 'crashed' WHERE run_id = ?;",
                [(rid,) for rid in ids],
            )
        return ids

    # ------------------------------------------------------------------ list / get

    def get(self, run_id: str) -> CatalogRow | None:
        cursor = self._conn.execute("SELECT * FROM runs WHERE run_id = ?;", (run_id,))
        row = cursor.fetchone()
        return None if row is None else _row_to_catalog(row)

    def list(
        self,
        *,
        run_status: RunStatus | None = None,
        bundle_status: BundleStatus | None = None,
        since: datetime | None = None,
    ) -> list[CatalogRow]:
        clauses: list[str] = []
        params: list[Any] = []
        if run_status is not None:
            clauses.append("run_status = ?")
            params.append(run_status)
        if bundle_status is not None:
            clauses.append("bundle_status = ?")
            params.append(bundle_status)
        if since is not None:
            clauses.append("started_utc >= ?")
            params.append(since.isoformat())
        sql = "SELECT * FROM runs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY started_utc DESC;"
        cursor = self._conn.execute(sql, tuple(params))
        return [_row_to_catalog(row) for row in cursor.fetchall()]

    # ------------------------------------------------------------------ verify

    def verify_one(self, run_id: str) -> VerifyResult:
        """Re-walk the bundle's artifacts vs. ``manifest.sha256`` and update
        the catalog's ``integrity_status``."""
        row = self.get(run_id)
        if row is None:
            raise CatalogError(f"unknown run_id: {run_id!r}")
        bundle = Path(row.path)
        if not bundle.is_dir():
            self._conn.execute(
                "UPDATE runs SET integrity_status = 'partial' WHERE run_id = ?;",
                (run_id,),
            )
            raise CatalogError(f"bundle directory missing: {bundle}")
        result = verify(bundle)
        self._conn.execute(
            "UPDATE runs SET integrity_status = ? WHERE run_id = ?;",
            (result.status, run_id),
        )
        return result

    def verify_all(self) -> builtins.list[tuple[str, VerifyResult | str]]:
        """Verify every cataloged run. Returns ``[(run_id, result_or_error), ...]``.

        A missing bundle directory produces the literal string ``"missing"``
        rather than raising — bulk verify shouldn't abort on the first
        mismatch.
        """
        out: list[tuple[str, VerifyResult | str]] = []
        for row in self.list():
            try:
                out.append((row.run_id, self.verify_one(row.run_id)))
            except CatalogError as exc:
                out.append((row.run_id, str(exc)))
        return out

    # ------------------------------------------------------------------ rebuild

    def rebuild_from_disk(self) -> int:
        """Walk ``runs_root`` for ``manifest.json`` files and refresh every
        row from the on-disk truth. Returns the number of bundles indexed.

        Existing rows are overwritten; rows whose bundle directory is gone
        are deleted.
        """
        seen: set[str] = set()
        count = 0
        for manifest_path in sorted(self._runs_root.rglob("manifest.json")):
            bundle = manifest_path.parent
            if bundle == self._runs_root:
                continue  # would only happen if a manifest.json sits at the root
            try:
                manifest = BundleManifest.read(manifest_path)
            except Exception:
                continue
            self.insert_run_at_open(manifest, bundle_path=bundle)
            if manifest.ended_utc is not None or manifest.bundle_status != "open":
                self.update_at_finalize(manifest, bundle_path=bundle)
            seen.add(manifest.run_id)
            count += 1

        # Drop rows whose bundle dir disappeared.
        existing = {
            row["run_id"] for row in self._conn.execute("SELECT run_id FROM runs;").fetchall()
        }
        for run_id in existing - seen:
            self._conn.execute("DELETE FROM runs WHERE run_id = ?;", (run_id,))
            self._conn.execute("DELETE FROM artifacts WHERE run_id = ?;", (run_id,))
        return count

    # ------------------------------------------------------------------ misc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        finally:
            self._conn.close()

    def __enter__(self) -> RunCatalog:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@contextmanager
def open_catalog(runs_root: str | Path) -> Iterator[RunCatalog]:
    """Context-manager helper for short-lived CLI usage."""
    cat = RunCatalog(runs_root)
    try:
        yield cat
    finally:
        cat.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_catalog(row: sqlite3.Row) -> CatalogRow:
    tags_json = row["tags_json"]
    summary_json = row["summary_json"]
    return CatalogRow(
        run_id=row["run_id"],
        path=row["path"],
        started_utc=datetime.fromisoformat(row["started_utc"]),
        ended_utc=datetime.fromisoformat(row["ended_utc"]) if row["ended_utc"] else None,
        operator_id=row["operator_id"],
        sample_id=row["sample_id"],
        procedure=row["procedure"],
        capa_version=row["capa_version"],
        capa_git_sha=row["capa_git_sha"],
        run_status=_validate_run_status(row["run_status"]),
        bundle_status=_validate_bundle_status(row["bundle_status"]),
        schema_version=int(row["schema_version"]),
        integrity_status=_validate_integrity_status(row["integrity_status"]),
        tags=tuple(json.loads(tags_json)) if tags_json else (),
        summary=json.loads(summary_json) if summary_json else {},
    )


_VALID_RUN_STATUS: frozenset[str] = frozenset({"running", "completed", "aborted", "crashed"})
_VALID_BUNDLE_STATUS: frozenset[str] = frozenset(
    {"open", "finalizing", "finalized_unverified", "sealed", "verification_failed"}
)
_VALID_INTEGRITY_STATUS: frozenset[str] = frozenset({"unknown", "ok", "mismatch", "partial"})


def _validate_run_status(value: str) -> RunStatus:
    if value not in _VALID_RUN_STATUS:
        raise CatalogError(f"invalid run_status in catalog: {value!r}")
    return value  # type: ignore[return-value]


def _validate_bundle_status(value: str) -> BundleStatus:
    if value not in _VALID_BUNDLE_STATUS:
        raise CatalogError(f"invalid bundle_status in catalog: {value!r}")
    return value  # type: ignore[return-value]


def _validate_integrity_status(value: str) -> IntegrityStatus:
    if value not in _VALID_INTEGRITY_STATUS:
        raise CatalogError(f"invalid integrity_status in catalog: {value!r}")
    return value  # type: ignore[return-value]


def _serialize_queue_health(manifest: BundleManifest) -> dict[str, dict[str, float]]:
    """Pull the manifest's ``queue_health`` dict into a JSON-friendly shape.

    Each :class:`QueueHealthEntry` already serializes via ``model_dump``.
    """
    out: dict[str, dict[str, float]] = {}
    for name, entry in manifest.queue_health.items():
        dump = entry.model_dump()
        # All fields on QueueHealthEntry (and its allowed extras) are floats.
        out[name] = {k: float(v) for k, v in dump.items() if isinstance(v, int | float)}
    return out


# Ensure the Literal import is honored for downstream casts.
_ = Literal


__all__ = [
    "CATALOG_FILENAME",
    "CatalogError",
    "CatalogRow",
    "RunCatalog",
    "open_catalog",
]
