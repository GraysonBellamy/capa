"""Tests for :class:`capa.storage.catalog.RunCatalog`."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from capa.storage.catalog import CATALOG_FILENAME, CatalogError, RunCatalog
from capa.storage.manifest import (
    BundleManifest,
    CapaBlock,
    LockfileBlock,
    OperatorBlock,
    PlatformBlock,
    ProcedureBlock,
    PythonBlock,
    SampleBlock,
)


def _make_manifest(
    *,
    run_id: str = "R-1",
    operator_id: str = "abr",
    sample_id: str = "S-1",
    procedure_id: str = "capa.builtin.free_run",
    run_status: str = "running",
    bundle_status: str = "open",
    ended_utc: datetime | None = None,
    started_utc: datetime | None = None,
) -> BundleManifest:
    return BundleManifest(
        run_id=run_id,
        started_utc=started_utc or datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC),
        ended_utc=ended_utc,
        started_mono_ns_anchor=0,
        run_status=run_status,
        bundle_status=bundle_status,
        operator=OperatorBlock(id=operator_id, display_name="A. Researcher"),
        sample=SampleBlock(id=sample_id),
        procedure=ProcedureBlock(id=procedure_id, version="0.1"),
        capa=CapaBlock(version="0.0.0", git_sha=None, git_dirty=None),
        python=PythonBlock(version="3.13", implementation="CPython", executable="/x"),
        platform=PlatformBlock(os="Linux", machine="x86_64", node="rig"),
        lockfile=LockfileBlock(path=None, sha256=None),
    )


def test_catalog_initializes_schema(tmp_path: Path) -> None:
    cat = RunCatalog(tmp_path)
    assert (tmp_path / CATALOG_FILENAME).is_file()
    assert cat.list() == []
    cat.close()


def test_insert_and_get(tmp_path: Path) -> None:
    cat = RunCatalog(tmp_path)
    bundle = tmp_path / "R-1"
    bundle.mkdir()
    manifest = _make_manifest()
    cat.insert_run_at_open(manifest, bundle_path=bundle)
    row = cat.get("R-1")
    assert row is not None
    assert row.run_status == "running"
    assert row.bundle_status == "open"
    assert row.operator_id == "abr"
    assert row.sample_id == "S-1"
    cat.close()


def test_update_at_finalize_mirrors_manifest(tmp_path: Path) -> None:
    cat = RunCatalog(tmp_path)
    bundle = tmp_path / "R-2"
    bundle.mkdir()
    cat.insert_run_at_open(_make_manifest(run_id="R-2"), bundle_path=bundle)
    finalized = _make_manifest(
        run_id="R-2",
        run_status="completed",
        bundle_status="sealed",
        ended_utc=datetime(2026, 5, 7, 12, 5, 0, tzinfo=UTC),
    )
    cat.update_at_finalize(finalized, bundle_path=bundle)
    row = cat.get("R-2")
    assert row is not None
    assert row.run_status == "completed"
    assert row.bundle_status == "sealed"
    assert row.ended_utc == datetime(2026, 5, 7, 12, 5, 0, tzinfo=UTC)
    cat.close()


def test_flip_orphans_marks_running_as_crashed(tmp_path: Path) -> None:
    cat = RunCatalog(tmp_path)
    bundle = tmp_path / "R-3"
    bundle.mkdir()
    cat.insert_run_at_open(_make_manifest(run_id="R-3"), bundle_path=bundle)
    affected = cat.flip_orphans()
    assert affected == ["R-3"]
    row = cat.get("R-3")
    assert row is not None and row.run_status == "crashed"
    # Idempotent: second call sees nothing to flip.
    assert cat.flip_orphans() == []
    cat.close()


def test_list_filters(tmp_path: Path) -> None:
    cat = RunCatalog(tmp_path)
    for i, status in enumerate(("running", "completed", "aborted")):
        b = tmp_path / f"R-{i}"
        b.mkdir()
        cat.insert_run_at_open(
            _make_manifest(
                run_id=f"R-{i}",
                run_status=status,
                bundle_status="open" if status == "running" else "sealed",
            ),
            bundle_path=b,
        )
    completed = cat.list(run_status="completed")
    assert [r.run_id for r in completed] == ["R-1"]
    sealed = cat.list(bundle_status="sealed")
    assert {r.run_id for r in sealed} == {"R-1", "R-2"}
    cat.close()


def test_verify_one_unknown_run_raises(tmp_path: Path) -> None:
    cat = RunCatalog(tmp_path)
    with pytest.raises(CatalogError):
        cat.verify_one("does-not-exist")
    cat.close()


def test_rebuild_from_disk_drops_missing(tmp_path: Path) -> None:
    cat = RunCatalog(tmp_path)
    # Pre-existing row whose bundle directory is gone.
    ghost = tmp_path / "ghost"
    ghost.mkdir()
    cat.insert_run_at_open(_make_manifest(run_id="ghost"), bundle_path=ghost)
    ghost.rmdir()  # remove the directory; row remains in catalog
    n = cat.rebuild_from_disk()
    assert n == 0
    assert cat.get("ghost") is None
    cat.close()
