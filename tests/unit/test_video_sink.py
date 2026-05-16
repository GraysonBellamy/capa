"""FramesSink + RunBundleWriter cameras-block + finalize rewrite.

Validates that:

* :class:`FramesSink` writes a per-camera in-flight Arrow IPC stream with
  the locked schema and the right column types.
* :meth:`RunBundleWriter.record_frame` lazily creates a FramesSink and
  routes receipts by camera name.
* The arm-time manifest seeds ``cameras[]`` from
  :attr:`HardwareProfile.cameras` (no frame_count yet, no frames_path).
* :func:`finalize_in_place` rewrites ``<name>.frames.in-flight.arrows`` to
  ``<name>.frames.parquet`` with row-group consolidation, refreshes the
  manifest's ``cameras[]`` entries with frame counts + final paths, and
  the integrity walker hashes the new files.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from capa.core.clock import RunClock
from capa.devices.camera.base import CameraSpec, FrameReceipt
from capa.devices.sim.flir_ir_sim import FlirIrSim
from capa.experiment.config import (
    CalibrationSetRef,
    ExperimentConfig,
    HardwareProfile,
    OperatorRef,
    ProcedureRef,
    SampleInfo,
)
from capa.storage._ipc import read_recoverable
from capa.storage.bundle import RunBundleWriter
from capa.storage.manifest import BundleManifest
from capa.storage.video_sink import FramesSink, final_path, in_flight_path


def _ir_spec(name: str = "ir_cam0") -> CameraSpec:
    return CameraSpec.model_validate(
        {
            "name": name,
            "adapter": "capa.devices.sim.flir_ir_sim",
            "kind": "ir",
        }
    )


def _vis_spec(name: str = "visible_cam0") -> CameraSpec:
    return CameraSpec.model_validate(
        {
            "name": name,
            "adapter": "capa.devices.camera.webcam",
            "kind": "visible",
        }
    )


def _config(*camera_specs: CameraSpec) -> ExperimentConfig:
    return ExperimentConfig(
        hardware=HardwareProfile(name="rig", cameras=camera_specs),
        method=None,
        procedure=ProcedureRef(id="capa.builtin.free_run", version="0.1"),
        calibration_set=CalibrationSetRef(name="default"),
        operator=OperatorRef(id="abr"),
        sample=SampleInfo(id="S001"),
    )


class TestFramesSinkSchema:
    def test_writes_in_flight_stream(self, tmp_path: Path) -> None:
        sink = FramesSink(tmp_path, camera="ir_cam0")
        for i in range(5):
            sink.write(
                FrameReceipt(
                    name="ir_cam0",
                    frame_idx=i,
                    t_mono_ns=i * 1_000_000,
                    t_utc=datetime(2026, 5, 7, 12, 0, i, tzinfo=UTC),
                    capture_latency_s=0.001,
                )
            )
        sink.close()

        path = in_flight_path(tmp_path, "ir_cam0")
        assert path.is_file()
        table = read_recoverable(path)
        assert table is not None
        assert table.num_rows == 5
        assert table.column_names == [
            "frame_idx",
            "t_mono_ns",
            "t_utc",
            "capture_latency_s",
            "camera",
        ]
        assert table.column("frame_idx").to_pylist() == [0, 1, 2, 3, 4]

    def test_close_idempotent(self, tmp_path: Path) -> None:
        sink = FramesSink(tmp_path, camera="ir_cam0")
        sink.close()
        sink.close()  # no exception

    def test_rejects_wrong_camera(self, tmp_path: Path) -> None:
        sink = FramesSink(tmp_path, camera="ir_cam0")
        wrong = FrameReceipt(
            name="other_cam",
            frame_idx=0,
            t_mono_ns=0,
            t_utc=datetime(2026, 1, 1, tzinfo=UTC),
        )
        with pytest.raises(Exception, match="received receipt for"):
            sink.write(wrong)
        sink.close()


class TestRunBundleWriterCameras:
    def test_arm_seeds_manifest_cameras(self, tmp_path: Path) -> None:
        config = _config(_ir_spec("ir_cam0"), _vis_spec("visible_cam0"))
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        writer = RunBundleWriter(
            config,
            runs_root=runs_root,
            run_id="run01",
        )
        repo_root = Path(__file__).resolve().parents[2]
        writer.open(repo_root=repo_root, lockfile_source=repo_root / "uv.lock")

        manifest = BundleManifest.read(writer.bundle_path / "manifest.json")
        cams = {c.name: c for c in manifest.cameras}
        assert set(cams.keys()) == {"ir_cam0", "visible_cam0"}
        assert cams["ir_cam0"].kind == "ir"
        assert cams["ir_cam0"].output_path == "video/ir_cam0.csq"
        assert cams["visible_cam0"].kind == "visible"
        assert cams["visible_cam0"].output_path == "video/visible_cam0.mkv"
        # No frames recorded yet; frames_path stays None.
        assert cams["ir_cam0"].frames_path is None
        assert cams["ir_cam0"].frame_count == 0

        writer.close_sinks()

    def test_record_frame_creates_per_camera_sink(self, tmp_path: Path) -> None:
        config = _config(_ir_spec(), _vis_spec())
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        writer = RunBundleWriter(config, runs_root=runs_root, run_id="run02")
        repo_root = Path(__file__).resolve().parents[2]
        writer.open(repo_root=repo_root, lockfile_source=repo_root / "uv.lock")

        for i in range(3):
            writer.record_frame(
                FrameReceipt(
                    name="ir_cam0",
                    frame_idx=i,
                    t_mono_ns=i * 100_000,
                    t_utc=datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC),
                )
            )
        writer.record_frame(
            FrameReceipt(
                name="visible_cam0",
                frame_idx=0,
                t_mono_ns=50_000,
                t_utc=datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC),
            )
        )
        writer.close_sinks()

        ir_path = in_flight_path(writer.bundle_path, "ir_cam0")
        vis_path = in_flight_path(writer.bundle_path, "visible_cam0")
        assert ir_path.is_file()
        assert vis_path.is_file()
        ir_table = read_recoverable(ir_path)
        vis_table = read_recoverable(vis_path)
        assert ir_table is not None and ir_table.num_rows == 3
        assert vis_table is not None and vis_table.num_rows == 1


class TestFinalizeRewrites:
    def test_rewrites_frames_and_refreshes_manifest(self, tmp_path: Path) -> None:
        # Run a sim camera through the bundle writer end-to-end + finalize.
        config = _config(_ir_spec("ir_cam0"))
        runs_root = tmp_path / "runs"
        runs_root.mkdir()

        clock = RunClock.now()
        spec = config.hardware.cameras[0]
        sim = FlirIrSim(spec=spec, clock=clock, fps=10, frame_payload_bytes=32)

        writer = RunBundleWriter(
            config,
            runs_root=runs_root,
            run_id="run03",
            started_mono_ns_anchor=clock.started_mono_ns,
        )
        repo_root = Path(__file__).resolve().parents[2]
        writer.open(repo_root=repo_root, lockfile_source=repo_root / "uv.lock")

        async def drive() -> list[FrameReceipt]:
            await sim.open()
            csq_path = writer.bundle_path / "video" / "ir_cam0.csq"
            await sim.start_recording(csq_path)
            receipts = []
            for _ in range(7):
                receipts.append(await sim.pump_one_frame())
            await sim.stop_recording()
            await sim.close()
            return receipts

        receipts = asyncio.run(drive())
        for r in receipts:
            writer.record_frame(r)

        result = writer.finalize(run_status="completed")
        assert result.integrity.status == "ok"

        # Final frames.parquet exists; in-flight gone.
        final = final_path(writer.bundle_path, "ir_cam0")
        inflight = in_flight_path(writer.bundle_path, "ir_cam0")
        assert final.is_file()
        assert not inflight.exists()
        table = pq.read_table(final)
        assert table.num_rows == 7
        # Sorted by t_mono_ns post-rewrite ().
        ts = table.column("t_mono_ns").to_pylist()
        assert ts == sorted(ts)

        # Manifest cameras[] refreshed.
        manifest = BundleManifest.read(writer.bundle_path / "manifest.json")
        cam = manifest.cameras[0]
        assert cam.frames_path == "video/ir_cam0.frames.parquet"
        assert cam.frame_count == 7
        assert cam.meta_path == "video/ir_cam0.csq.meta.json"
        # Started offset came from the meta sidecar.
        assert cam.started_mono_ns_offset > 0

    def test_finalize_skips_camera_with_no_frames(self, tmp_path: Path) -> None:
        config = _config(_ir_spec("ir_cam0"))
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        writer = RunBundleWriter(config, runs_root=runs_root, run_id="run04")
        repo_root = Path(__file__).resolve().parents[2]
        writer.open(repo_root=repo_root, lockfile_source=repo_root / "uv.lock")

        # No frames recorded.
        result = writer.finalize(run_status="completed")
        assert result.integrity.status == "ok"

        manifest = BundleManifest.read(writer.bundle_path / "manifest.json")
        cam = manifest.cameras[0]
        assert cam.frames_path is None
        assert cam.frame_count == 0
        # The seeded entry still survives so the operator's intent is recorded.
        assert cam.name == "ir_cam0"
