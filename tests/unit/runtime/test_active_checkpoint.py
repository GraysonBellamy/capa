"""Tests for the active-bundle checkpoint module.

Covers the atomic-write recipe, the dataclass round-trip, and the
:func:`recover_active_bundle_checkpoint` helper's branches
(absent / live owner / dead owner / missing bundle).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from capa.runtime.recovery import (
    CHECKPOINT_FILENAME,
    ActiveCheckpoint,
    delete_active_checkpoint,
    read_active_checkpoint,
    recover_active_bundle_checkpoint,
    write_active_checkpoint,
)
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


def _sample_checkpoint(bundle_path: Path) -> ActiveCheckpoint:
    now = datetime.now(UTC)
    return ActiveCheckpoint(
        pid=12345,
        run_id="2026-05-13_120000_sample",
        bundle_path=bundle_path,
        config_path=Path("configs/sample.toml"),
        started_utc=now,
        last_update_utc=now,
    )


class TestRoundTrip:
    def test_to_json_then_from_json_preserves_fields(self, tmp_path: Path) -> None:
        ck = _sample_checkpoint(tmp_path / "bundle")
        roundtripped = ActiveCheckpoint.from_json(ck.to_json())
        assert roundtripped == ck

    def test_config_path_none_round_trips(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        ck = ActiveCheckpoint(
            pid=1,
            run_id="r",
            bundle_path=tmp_path / "b",
            config_path=None,
            started_utc=now,
            last_update_utc=now,
        )
        assert ActiveCheckpoint.from_json(ck.to_json()) == ck


class TestWriteRead:
    def test_write_creates_runs_root(self, tmp_path: Path) -> None:
        runs_root = tmp_path / "runs"
        # runs_root does not exist yet — write_active_checkpoint mkdirs.
        ck = _sample_checkpoint(runs_root / "bundle")
        write_active_checkpoint(runs_root, ck)
        assert (runs_root / CHECKPOINT_FILENAME).is_file()

    def test_read_returns_payload(self, tmp_path: Path) -> None:
        ck = _sample_checkpoint(tmp_path / "bundle")
        write_active_checkpoint(tmp_path, ck)
        out = read_active_checkpoint(tmp_path)
        assert out == ck

    def test_read_returns_none_when_absent(self, tmp_path: Path) -> None:
        assert read_active_checkpoint(tmp_path) is None

    def test_read_returns_none_on_corrupt_json(self, tmp_path: Path) -> None:
        (tmp_path / CHECKPOINT_FILENAME).write_text("{this is not valid json")
        assert read_active_checkpoint(tmp_path) is None

    def test_delete_is_idempotent(self, tmp_path: Path) -> None:
        # Delete on an absent checkpoint must not raise.
        delete_active_checkpoint(tmp_path)
        ck = _sample_checkpoint(tmp_path / "bundle")
        write_active_checkpoint(tmp_path, ck)
        delete_active_checkpoint(tmp_path)
        assert not (tmp_path / CHECKPOINT_FILENAME).exists()
        # Second delete is also a no-op.
        delete_active_checkpoint(tmp_path)


class TestAtomicWrite:
    """Torn write must leave either prior valid JSON or new valid
    JSON, never a partial file."""

    def test_failed_write_preserves_existing_file(self, tmp_path: Path) -> None:
        ck1 = _sample_checkpoint(tmp_path / "bundle1")
        write_active_checkpoint(tmp_path, ck1)
        original_bytes = (tmp_path / CHECKPOINT_FILENAME).read_bytes()

        # Simulate a torn write: ``os.write`` raises mid-write. With the
        # temp+replace recipe, the destination file should still be the
        # ORIGINAL one because os.replace was never called.
        ck2_path = tmp_path / "bundle2"
        ck2 = ActiveCheckpoint(
            pid=99999,
            run_id="other",
            bundle_path=ck2_path,
            config_path=None,
            started_utc=datetime.now(UTC),
            last_update_utc=datetime.now(UTC),
        )

        real_write = os.write

        def _exploding_write(fd: int, data: bytes) -> int:
            if b'"other"' in data:
                raise OSError("simulated torn write")
            return real_write(fd, data)

        with (
            patch("capa.runtime.recovery.os.write", side_effect=_exploding_write),
            pytest.raises(OSError, match="simulated torn write"),
        ):
            write_active_checkpoint(tmp_path, ck2)

        # Destination unchanged.
        assert (tmp_path / CHECKPOINT_FILENAME).read_bytes() == original_bytes

    def test_replace_makes_swap_atomic(self, tmp_path: Path) -> None:
        # Once os.replace succeeds, the new file is observable; before that
        # the old file is observable. There's no in-between.
        ck1 = _sample_checkpoint(tmp_path / "bundle1")
        write_active_checkpoint(tmp_path, ck1)
        ck2 = _sample_checkpoint(tmp_path / "bundle2")
        write_active_checkpoint(tmp_path, ck2)
        out = read_active_checkpoint(tmp_path)
        assert out is not None
        assert out.bundle_path == ck2.bundle_path


# ---------------------------------------------------------------------------
# Recovery helper
# ---------------------------------------------------------------------------


class TestRecovery:
    def test_absent_checkpoint_is_noop(self, tmp_path: Path) -> None:
        result = recover_active_bundle_checkpoint(tmp_path)
        assert result.status == "absent"
        assert result.checkpoint is None

    def test_live_owner_is_left_alone(self, tmp_path: Path) -> None:
        # Use this test's PID — guaranteed alive while the test runs.
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        # Write a minimal manifest so the helper doesn't take the
        # missing_bundle branch.
        _write_running_manifest(bundle)

        now = datetime.now(UTC)
        ck = ActiveCheckpoint(
            pid=os.getpid(),
            run_id="r",
            bundle_path=bundle,
            config_path=None,
            started_utc=now,
            last_update_utc=now,
        )
        write_active_checkpoint(tmp_path, ck)
        result = recover_active_bundle_checkpoint(tmp_path)
        assert result.status == "live_owner"
        # Checkpoint must still exist — we don't reconcile a live owner.
        assert (tmp_path / CHECKPOINT_FILENAME).exists()
        # Manifest must be unmodified.
        manifest = json.loads((bundle / "manifest.json").read_text())
        assert manifest["run_status"] == "running"

    def test_dead_owner_reconciles_manifest_and_clears_checkpoint(self, tmp_path: Path) -> None:
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        _write_running_manifest(bundle)

        # PID 1 on Windows is the System idle process; on POSIX it's
        # init/launchd. Patch the liveness check to return False so the
        # test isn't platform-sensitive.
        now = datetime.now(UTC)
        ck = ActiveCheckpoint(
            pid=2147483640,  # an absurdly large PID that won't exist
            run_id="r",
            bundle_path=bundle,
            config_path=None,
            started_utc=now,
            last_update_utc=now,
        )
        write_active_checkpoint(tmp_path, ck)

        with patch("capa.runtime.recovery._pid_is_alive", return_value=False):
            result = recover_active_bundle_checkpoint(tmp_path)

        assert result.status == "reconciled"
        assert result.checkpoint == ck
        # Checkpoint cleared.
        assert not (tmp_path / CHECKPOINT_FILENAME).exists()
        # Manifest flipped to crashed with a recovery exit reason.
        manifest = json.loads((bundle / "manifest.json").read_text())
        assert manifest["run_status"] == "crashed"
        assert "recovered after hard exit" in (manifest.get("exit_reason") or "")

    def test_missing_bundle_clears_checkpoint(self, tmp_path: Path) -> None:
        bundle = tmp_path / "gone"
        # bundle is intentionally not created — simulates an operator who
        # deleted the run dir before launching capa again.
        now = datetime.now(UTC)
        ck = ActiveCheckpoint(
            pid=2147483640,
            run_id="r",
            bundle_path=bundle,
            config_path=None,
            started_utc=now,
            last_update_utc=now,
        )
        write_active_checkpoint(tmp_path, ck)
        with patch("capa.runtime.recovery._pid_is_alive", return_value=False):
            result = recover_active_bundle_checkpoint(tmp_path)
        assert result.status == "missing_bundle"
        assert not (tmp_path / CHECKPOINT_FILENAME).exists()

    def test_already_finalized_manifest_is_left_alone(self, tmp_path: Path) -> None:
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        _write_running_manifest(bundle, run_status="completed", bundle_status="sealed")
        now = datetime.now(UTC)
        ck = ActiveCheckpoint(
            pid=2147483640,
            run_id="r",
            bundle_path=bundle,
            config_path=None,
            started_utc=now,
            last_update_utc=now,
        )
        write_active_checkpoint(tmp_path, ck)
        with patch("capa.runtime.recovery._pid_is_alive", return_value=False):
            result = recover_active_bundle_checkpoint(tmp_path)
        # Checkpoint still gets cleared (we don't need it any more), but
        # the manifest's status stays "completed" — we only flip
        # "running" rows.
        assert result.status == "reconciled"
        assert not (tmp_path / CHECKPOINT_FILENAME).exists()
        manifest = json.loads((bundle / "manifest.json").read_text())
        assert manifest["run_status"] == "completed"


def _write_running_manifest(
    bundle: Path,
    *,
    run_status: str = "running",
    bundle_status: str = "open",
) -> None:
    """Write a minimal valid manifest.json (same pattern as
    test_storage_manifest._minimal_manifest)."""
    manifest = BundleManifest(
        run_id="2026-05-13_120000_TEST",
        started_utc=datetime.now(UTC),
        started_mono_ns_anchor=0,
        operator=OperatorBlock(id="op"),
        sample=SampleBlock(id="SPEC-1"),
        procedure=ProcedureBlock(id="capa.builtin.free_run"),
        capa=CapaBlock(version="0.0.0"),
        python=PythonBlock(version="3.13", implementation="CPython", executable="py"),
        platform=PlatformBlock(os="test", machine="test", node="test"),
        lockfile=LockfileBlock(path=None, sha256=None),
        run_status=run_status,  # type: ignore[arg-type]
        bundle_status=bundle_status,  # type: ignore[arg-type]
    )
    manifest.write(bundle / "manifest.json")
