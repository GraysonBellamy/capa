from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from capa.storage.manifest import (
    BundleManifest,
    CapaBlock,
    DataShape,
    DataShapeChannelSamples,
    DataShapeRecord,
    LockfileBlock,
    OperatorBlock,
    PlatformBlock,
    ProcedureBlock,
    PythonBlock,
    SampleBlock,
    is_legal_finalize_combination,
)


def _minimal_manifest(**overrides: object) -> BundleManifest:
    base = dict(
        run_id="2026-05-07_120000_TEST",
        started_utc=datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC),
        started_mono_ns_anchor=1_000_000_000,
        operator=OperatorBlock(id="abr"),
        sample=SampleBlock(id="SPEC-1"),
        procedure=ProcedureBlock(id="capa.builtin.free_run"),
        capa=CapaBlock(version="0.7.3"),
        python=PythonBlock(version="3.13.0", implementation="CPython", executable="/usr/bin/py"),
        platform=PlatformBlock(os="Linux-7.0.3-1", machine="x86_64", node="rig01"),
        lockfile=LockfileBlock(path=None, sha256=None),
    )
    base.update(overrides)
    return BundleManifest(**base)


class TestRoundTrip:
    def test_minimal_round_trips(self, tmp_path: Path) -> None:
        m = _minimal_manifest()
        out = tmp_path / "manifest.json"
        m.write(out)
        m2 = BundleManifest.read(out)
        assert m2.run_id == m.run_id
        assert m2.started_utc == m.started_utc
        assert m2.run_status == "running"
        assert m2.bundle_status == "open"
        assert m2.bundle_schema_version == 2

    def test_atomic_write_no_partial(self, tmp_path: Path) -> None:
        m = _minimal_manifest()
        target = tmp_path / "manifest.json"
        m.write(target)
        # No leftover .tmp files
        assert not (tmp_path / "manifest.json.tmp").exists()
        assert target.is_file()

    def test_data_shape_serializes(self, tmp_path: Path) -> None:
        m = _minimal_manifest(
            data_shape=DataShape(
                channel_samples=DataShapeChannelSamples(path="scalars.parquet"),
                device_records=(
                    DataShapeRecord(
                        adapter="watlow",
                        path="device_records/watlow.parquet",
                        layout="long_row",
                    ),
                ),
            )
        )
        out = tmp_path / "manifest.json"
        m.write(out)
        m2 = BundleManifest.read(out)
        assert m2.data_shape.channel_samples is not None
        assert m2.data_shape.channel_samples.path == "scalars.parquet"
        assert m2.data_shape.device_records[0].adapter == "watlow"

    def test_extra_keys_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.json"
        path.write_bytes(b'{"bundle_schema_version": 1, "totally_made_up": true}')
        with pytest.raises(Exception):
            BundleManifest.read(path)


class TestFinalizeCombination:
    def test_running_plus_open_legal(self) -> None:
        assert is_legal_finalize_combination("running", "open")

    def test_running_plus_sealed_illegal(self) -> None:
        assert not is_legal_finalize_combination("running", "sealed")

    def test_completed_plus_sealed_legal(self) -> None:
        assert is_legal_finalize_combination("completed", "sealed")

    def test_crashed_plus_sealed_legal(self) -> None:
        # crashed runs can still seal.
        assert is_legal_finalize_combination("crashed", "sealed")
