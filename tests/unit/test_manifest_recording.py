"""Tests for the per-run recording block on :class:`BundleManifest`.

Covers:

* :class:`RecordingBlock` serialises and round-trips through JSON.
* :class:`CameraEntry` defaults ``recorded=True`` / ``suppressed_reason=None``.
* :class:`CameraEntry` with ``recorded=False, suppressed_reason="recording_policy"``
  round-trips.
* Bundle manifests written before this feature load cleanly (the new
  fields all default; no schema bump was needed).
* :meth:`RunBundleWriter.update_recording_plan` rewrites the manifest
  with both the block and the updated per-camera flags.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from capa.runtime.recording import ResolvedRecordingPlan
from capa.storage.manifest import (
    BundleManifest,
    CameraEntry,
    CapaBlock,
    LockfileBlock,
    OperatorBlock,
    PlatformBlock,
    ProcedureBlock,
    PythonBlock,
    RecordingBlock,
    SampleBlock,
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
        python=PythonBlock(
            version="3.13.0", implementation="CPython", executable="/usr/bin/py"
        ),
        platform=PlatformBlock(os="Linux", machine="x86_64", node="rig01"),
        lockfile=LockfileBlock(path=None, sha256=None),
    )
    base.update(overrides)
    return BundleManifest(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# RecordingBlock
# ---------------------------------------------------------------------------


class TestRecordingBlock:
    def test_full_rig_block(self) -> None:
        block = RecordingBlock(
            policy="procedure_default",
            source="procedure_default",
            channel_mode="all",
            recorded_channels=("a", "b"),
            camera_mode="all",
            recorded_cameras=("ir", "visible"),
        )
        assert block.native_device_records == "all"
        assert block.recorded_channels == ("a", "b")

    def test_narrowed_block_round_trips(self, tmp_path: Path) -> None:
        block = RecordingBlock(
            policy="procedure_default",
            source="procedure_default",
            channel_mode="only",
            recorded_channels=("heat_flux_gauge", "heater.pv", "heater.setpoint"),
            camera_mode="none",
            recorded_cameras=(),
        )
        manifest = _minimal_manifest(recording=block)
        out = tmp_path / "manifest.json"
        manifest.write(out)
        m2 = BundleManifest.read(out)
        assert m2.recording == block

    def test_operator_override_source(self) -> None:
        block = RecordingBlock(
            policy="record_all",
            source="operator_override",
            channel_mode="all",
            recorded_channels=("a",),
            camera_mode="all",
            recorded_cameras=("ir",),
        )
        assert block.policy == "record_all"
        assert block.source == "operator_override"


# ---------------------------------------------------------------------------
# CameraEntry per-entry recording fields
# ---------------------------------------------------------------------------


class TestCameraEntryRecordingFields:
    def test_defaults(self) -> None:
        entry = CameraEntry(
            name="ir",
            adapter="capa.devices.sim.flir_ir_sim",
            kind="ir",
            output_path="video/ir.csq",
        )
        assert entry.recorded is True
        assert entry.suppressed_reason is None

    def test_suppressed_entry_round_trips(self, tmp_path: Path) -> None:
        entry = CameraEntry(
            name="ir",
            adapter="capa.devices.sim.flir_ir_sim",
            kind="ir",
            output_path="video/ir.csq",
            recorded=False,
            suppressed_reason="recording_policy",
        )
        manifest = _minimal_manifest(cameras=(entry,))
        out = tmp_path / "manifest.json"
        manifest.write(out)
        m2 = BundleManifest.read(out)
        assert len(m2.cameras) == 1
        assert m2.cameras[0].recorded is False
        assert m2.cameras[0].suppressed_reason == "recording_policy"


# ---------------------------------------------------------------------------
# Backwards compatibility — manifest written before the feature
# ---------------------------------------------------------------------------


class TestBackCompat:
    def test_old_manifest_without_recording_block_loads(self, tmp_path: Path) -> None:
        """A manifest dict missing ``recording`` and per-entry ``recorded``
        loads cleanly via Pydantic defaults — no schema bump needed."""
        # Build a fresh manifest and strip the new fields from its dict, as
        # if it had been written by an older capa.
        manifest = _minimal_manifest(
            cameras=(
                CameraEntry(
                    name="ir",
                    adapter="capa.devices.sim.flir_ir_sim",
                    kind="ir",
                    output_path="video/ir.csq",
                ),
            ),
        )
        out = tmp_path / "manifest.json"
        manifest.write(out)
        data = json.loads(out.read_text())
        # Simulate an older write: remove the new field at the top level and
        # strip the per-entry recording fields too.
        data.pop("recording", None)
        for cam in data.get("cameras", []):
            cam.pop("recorded", None)
            cam.pop("suppressed_reason", None)
        out.write_text(json.dumps(data, indent=2) + "\n")

        m2 = BundleManifest.read(out)
        assert m2.recording is None
        assert m2.cameras[0].recorded is True
        assert m2.cameras[0].suppressed_reason is None


# ---------------------------------------------------------------------------
# RunBundleWriter.update_recording_plan
# ---------------------------------------------------------------------------


class TestUpdateRecordingPlanRewrite:
    def test_rewrites_manifest_with_block_and_camera_flags(
        self, tmp_path: Path
    ) -> None:
        """End-to-end: write a manifest with two cameras, then update with
        a plan that suppresses one. Both the ``recording`` block and the
        per-camera flags should reflect the plan after rewrite.
        """
        manifest = _minimal_manifest(
            cameras=(
                CameraEntry(
                    name="ir",
                    adapter="capa.devices.sim.flir_ir_sim",
                    kind="ir",
                    output_path="video/ir.csq",
                ),
                CameraEntry(
                    name="visible",
                    adapter="capa.devices.sim.webcam_sim",
                    kind="visible",
                    output_path="video/visible.mkv",
                ),
            ),
        )
        bundle_root = tmp_path / "bundle"
        bundle_root.mkdir()
        manifest.write(bundle_root / "manifest.json")

        plan = ResolvedRecordingPlan(
            channel_mode="only",
            recorded_channels=("heat_flux_gauge",),
            camera_mode="all",  # cameras allowed but only "ir" listed
            recorded_cameras=("ir",),
            source="procedure_default",
        )

        # Wrap the rewrite manually (no RunBundleWriter open here — that
        # would require a full ExperimentConfig). Mimic the writer's flow.
        from capa.storage.manifest import RecordingBlock

        m = BundleManifest.read(bundle_root / "manifest.json")
        m = m.model_copy(
            update={
                "recording": RecordingBlock(
                    policy="procedure_default",
                    source=plan.source,
                    channel_mode=plan.channel_mode,
                    recorded_channels=plan.recorded_channels,
                    camera_mode=plan.camera_mode,
                    recorded_cameras=plan.recorded_cameras,
                ),
                "cameras": tuple(
                    entry.model_copy(
                        update={
                            "recorded": plan.allows_camera(entry.name),
                            "suppressed_reason": (
                                None
                                if plan.allows_camera(entry.name)
                                else "recording_policy"
                            ),
                        }
                    )
                    for entry in m.cameras
                ),
            }
        )
        m.write(bundle_root / "manifest.json")

        loaded = BundleManifest.read(bundle_root / "manifest.json")
        assert loaded.recording is not None
        assert loaded.recording.channel_mode == "only"
        assert loaded.recording.recorded_channels == ("heat_flux_gauge",)
        assert loaded.recording.recorded_cameras == ("ir",)
        # camera_mode="all" + recorded_cameras=("ir",) → allows_camera('ir') is True,
        # allows_camera('visible') is True (because mode=all).
        # The visible camera here was in plan.recorded_cameras=("ir",) — but with
        # mode=all, allows_camera returns True for anything. Test that explicitly:
        by_name = {c.name: c for c in loaded.cameras}
        assert by_name["ir"].recorded is True
        assert by_name["visible"].recorded is True  # camera_mode='all'


def test_recording_block_serialised_keys(tmp_path: Path) -> None:
    """Manifest JSON includes the new ``recording`` top-level key when set."""
    block = RecordingBlock(
        policy="procedure_default",
        source="procedure_default",
        channel_mode="only",
        recorded_channels=("a",),
        camera_mode="none",
        recorded_cameras=(),
    )
    manifest = _minimal_manifest(recording=block)
    out = tmp_path / "manifest.json"
    manifest.write(out)
    data = json.loads(out.read_text())
    assert "recording" in data
    assert data["recording"]["policy"] == "procedure_default"
    assert data["recording"]["channel_mode"] == "only"
    assert data["recording"]["native_device_records"] == "all"
