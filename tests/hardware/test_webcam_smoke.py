"""Hardware smoke test for the real :class:`WebcamAdapter` (P4 stage B).

Plan §15.4 contract for the visible camera path:

1. Open the V4L2 device named by ``CAPA_TEST_WEBCAM_DEVICE`` and run
   ``run_pump`` for ~5 s.
2. Assert at least a handful of frames arrive; assert the MKV exists and
   contains video.
3. Drive a short headless ``capa run`` and verify the bundle has both
   ``video/<name>.mkv`` and ``video/<name>.frames.parquet`` plus a
   matching ``manifest.json.cameras[0].frame_count``.

Skipped unless ``CAPA_HARDWARE_TESTS=1``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import anyio
import pyarrow.parquet as pq
import pytest

from capa.core.clock import RunClock
from capa.devices.camera.base import CameraSpec
from capa.devices.camera.webcam import WebcamAdapter
from capa.experiment.config import (
    CalibrationSetRef,
    ExperimentConfig,
    HardwareProfile,
    OperatorRef,
    ProcedureRef,
    SampleInfo,
)
from capa.experiment.engine import ExperimentEngine

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("CAPA_HARDWARE_TESTS") != "1",
        reason="CAPA_HARDWARE_TESTS=1 required",
    ),
]


def _webcam_device() -> str:
    device = os.environ.get("CAPA_TEST_WEBCAM_DEVICE")
    if device is None:
        pytest.skip("CAPA_TEST_WEBCAM_DEVICE not set")
    return device


def _input_format() -> str:
    """``CAPA_TEST_WEBCAM_INPUT_FORMAT`` overrides the platform default —
    ``v4l2`` on Linux, ``avfoundation`` on macOS, ``dshow`` on Windows."""
    override = os.environ.get("CAPA_TEST_WEBCAM_INPUT_FORMAT")
    if override:
        return override
    if sys.platform == "win32":
        return "dshow"
    if sys.platform == "darwin":
        return "avfoundation"
    return "v4l2"


def _operator_id() -> str:
    return os.environ.get("CAPA_TEST_WEBCAM_OPERATOR", "hw-test")


def _spec(device: str, name: str = "visible_cam0") -> CameraSpec:
    return CameraSpec.model_validate(
        {
            "name": name,
            "adapter": "capa.devices.camera.webcam",
            "kind": "visible",
            "estimated_bps": 500_000,
            "params": {
                "fps": 30,
                "width": 1280,
                "height": 720,
                "codec": "libx264",
                "pix_fmt": "yuv420p",
                "input_format": _input_format(),
                "input_url": device,
            },
        }
    )


@pytest.fixture(autouse=True)
def _release_dshow_handle_between_tests():
    """Force PyAV refcount cleanup between Windows webcam tests.

    The production ``WebcamAdapter._open_input_with_retry`` now handles the
    DirectShow ``[Errno 5]`` hold-time, so the autouse sleep that previously
    papered over it is gone. We still ``gc.collect()`` on Windows so the
    PyAV ``InputContainer``'s cyclic-collected refs drop promptly — without
    it, the retry budget ticks against an unreleased handle the GC will
    eventually free anyway.
    """
    yield
    if sys.platform == "win32":
        import gc

        gc.collect()


class TestRealWebcam:
    async def test_pump_writes_frames(self, tmp_path: Path) -> None:
        device = _webcam_device()
        clock = RunClock.now()
        spec = _spec(device)
        output = tmp_path / "video" / f"{spec.name}.mkv"
        output.parent.mkdir(parents=True, exist_ok=True)
        cam = WebcamAdapter.from_params(spec=spec, clock=clock, **spec.params)
        await cam.open()
        try:
            await cam.start_recording(output)
            with anyio.move_on_after(5.0):
                await cam.run_pump()
            await cam.stop_recording()
            health = await cam.snapshot()
            assert health.frame_count > 10, "pump produced too few frames"
            assert output.is_file()
            assert output.stat().st_size > 1024
        finally:
            await cam.close()


class TestRealWebcamEngineRun:
    def test_short_freerun_writes_bundle(self, tmp_path: Path) -> None:
        device = _webcam_device()
        spec = _spec(device)
        config = ExperimentConfig(
            hardware=HardwareProfile(
                name="webcam_smoke",
                devices=(),
                channels=(),
                cameras=(spec,),
            ),
            method=None,
            procedure=ProcedureRef(
                id="capa.builtin.free_run",
                version="0.1",
                config={"duration_s": 5.0},
            ),
            calibration_set=CalibrationSetRef(name="default"),
            operator=OperatorRef(id=_operator_id()),
            sample=SampleInfo(id="HW-SMOKE-WEBCAM-001"),
            tags=("hardware", "webcam", "smoke"),
        )

        async def _go() -> Path:
            engine = ExperimentEngine()
            result = await engine.run(
                config,
                runs_root=tmp_path / "runs",
                configure_logging_for_bundle=False,
            )
            assert result.bundle_path is not None
            assert result.run_status == "completed", result.exit_reason
            assert result.bundle_status == "sealed", result.exit_reason
            assert result.integrity_status == "ok", result.exit_reason
            return result.bundle_path

        bundle = anyio.run(_go)
        mkv = bundle / "video" / f"{spec.name}.mkv"
        frames = bundle / "video" / f"{spec.name}.frames.parquet"
        manifest = json.loads((bundle / "manifest.json").read_text())
        assert mkv.is_file() and mkv.stat().st_size > 1024
        assert frames.is_file()
        rows = pq.read_table(frames).num_rows
        assert rows >= 30  # ~1 second × 30 fps minimum
        assert manifest["cameras"][0]["frame_count"] == rows
