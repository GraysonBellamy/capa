"""P4 Stage A-D outcome gate — end-to-end camera run.

Drives :class:`ExperimentEngine` with a CAPA hardware profile that includes
both a sim IR camera and a (push-mode) webcam. Asserts:

* The run finalizes cleanly with ``run_status="completed"`` and
  ``bundle_status="sealed"``.
* The IR sim's ``.csq`` and ``.csq.meta.json`` land in ``video/``.
* ``video/<name>.frames.parquet`` exists with the right frame count for
  the IR sim (the webcam runs in pure push-mode here so we drive it from
  the test side).
* ``manifest.cameras[]`` is populated with both entries; finalize
  refreshes ``frames_path``, ``frame_count``, ``meta_path``, and
  ``started_mono_ns_offset``.
* ``manifest.sha256`` covers every camera artifact (vendor file + meta
  sidecar + frames parquet).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from capa.channels.calibration import Identity
from capa.channels.spec import ChannelKind, ChannelSpec, WatlowParameter
from capa.devices.camera.base import CameraSpec
from capa.experiment.config import (
    CalibrationSetRef,
    DeviceConfig,
    ExperimentConfig,
    HardwareProfile,
    OperatorRef,
    ProcedureRef,
    SampleInfo,
)
from capa.experiment.engine import ExperimentEngine
from capa.storage.integrity import verify
from capa.storage.manifest import BundleManifest

pytestmark = pytest.mark.anyio


def _config() -> ExperimentConfig:
    """Minimal hardware profile: one Watlow sim (so the engine spins through
    a normal producer task path) plus one IR sim camera. We deliberately
    omit the webcam — exercising visible cameras end-to-end through the
    engine requires a v4l2 device we don't have in CI; the WebcamAdapter is
    covered by its own unit tests in :mod:`tests.unit.test_camera_webcam`.
    """
    return ExperimentConfig(
        hardware=HardwareProfile(
            name="p4-camera",
            devices=(
                DeviceConfig(
                    name="heater",
                    adapter="capa.devices.sim.watlow_sim",
                    params={
                        "signals": {
                            ("process_value", 1): {
                                "kind": "constant",
                                "value": 400.0,
                            }
                        },
                        "parameter_units": {"process_value": "degC"},
                    },
                ),
            ),
            channels=(
                ChannelSpec(
                    name="heater.pv",
                    kind=ChannelKind.PROCESS_VAR,
                    source=WatlowParameter(device="heater", parameter="process_value", instance=1),
                    unit="degC",
                    derived_unit="degC",
                    calibration=Identity(input_unit="degC", output_unit="degC"),
                ),
            ),
            cameras=(
                CameraSpec.model_validate(
                    {
                        "name": "ir_cam0",
                        "adapter": "capa.devices.sim.flir_ir_sim",
                        "kind": "ir",
                        # Sim records 32-byte payloads at 30 Hz ≈ 960 B/s.
                        # Override the default 4 MB/s estimate so the
                        # disk-space preflight reflects the test's actual
                        # write rate.
                        "estimated_bps": 4096,
                        "params": {
                            "fps": 30,
                            "width": 64,
                            "height": 48,
                            "frame_payload_bytes": 32,
                        },
                    }
                ),
            ),
        ),
        method=None,
        procedure=ProcedureRef(
            id="capa.builtin.free_run",
            version=None,
            config={"duration_s": 0.3},
        ),
        calibration_set=CalibrationSetRef(name="default"),
        operator=OperatorRef(id="abr"),
        sample=SampleInfo(id="P4-1"),
    )


async def test_camera_engine_seals_with_ir_sim(tmp_path: Path) -> None:
    config = _config()
    engine = ExperimentEngine()
    result = await engine.run(
        config,
        runs_root=tmp_path,
        configure_logging_for_bundle=False,
    )
    assert result.run_status == "completed", result.exit_reason
    assert result.bundle_status == "sealed", result.exit_reason
    assert result.bundle_path is not None
    bundle = result.bundle_path

    # IR artifacts land under video/.
    csq = bundle / "video" / "ir_cam0.csq"
    meta = bundle / "video" / "ir_cam0.csq.meta.json"
    frames = bundle / "video" / "ir_cam0.frames.parquet"
    assert csq.is_file(), list((bundle / "video").iterdir())
    assert meta.is_file()
    assert frames.is_file()

    # Frame index is non-empty and structurally consistent with the .csq.
    table = pq.read_table(frames)
    assert table.num_rows > 0
    ts = table.column("t_mono_ns").to_pylist()
    assert ts == sorted(ts)
    sidecar = json.loads(meta.read_text(encoding="utf-8"))
    assert sidecar["frame_count"] == table.num_rows
    assert sidecar["final"] is True

    # Manifest cameras[] block reflects the live state of the IR camera.
    manifest = BundleManifest.read(bundle / "manifest.json")
    assert len(manifest.cameras) == 1
    cam = manifest.cameras[0]
    assert cam.name == "ir_cam0"
    assert cam.kind == "ir"
    assert cam.adapter == "capa.devices.sim.flir_ir_sim"
    assert cam.frames_path == "video/ir_cam0.frames.parquet"
    assert cam.meta_path == "video/ir_cam0.csq.meta.json"
    assert cam.frame_count == table.num_rows

    # Integrity manifest covers every camera artifact. Re-verify from the
    # storage layer *before* opening events.sqlite — sqlite3.connect can
    # leave transient ``-wal`` / ``-shm`` sidecars that the integrity walk
    # would (correctly) flag as "extra" relative to ``manifest.sha256``.
    digest_text = (bundle / "manifest.sha256").read_text(encoding="utf-8")
    assert "video/ir_cam0.csq" in digest_text
    assert "video/ir_cam0.csq.meta.json" in digest_text
    assert "video/ir_cam0.frames.parquet" in digest_text
    assert verify(bundle).status == "ok"

    # Recording start/stop events made it into the audit trail.
    with sqlite3.connect(bundle / "events.sqlite") as conn:
        kinds = [row[0] for row in conn.execute("SELECT kind FROM events").fetchall()]
    assert "camera.recording_started" in kinds
    assert "camera.recording_stopped" in kinds
