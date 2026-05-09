"""Regression test for the camera-only engine deadlock (hardware-day §6).

Previously: with ``hardware.devices=()`` the engine's ``producer_queue``
was never closed because no producer task ever entered (closing the
queue is the sentinel that lets the fan-out task exit). The fan-out
blocked indefinitely on receive and the run never sealed.

Fix landed at [src/capa/experiment/engine.py:838-843](src/capa/experiment/engine.py#L838-L843)
— close the queue immediately when ``producers_alive.value == 0`` after
starting tasks. This test would have caught the original bug; it locks
in the fix.
"""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from capa.devices.camera.base import CameraSpec
from capa.experiment.config import (
    CalibrationSetRef,
    ExperimentConfig,
    HardwareProfile,
    OperatorRef,
    ProcedureRef,
    SampleInfo,
)
from capa.experiment.engine import ExperimentEngine

pytestmark = pytest.mark.anyio


def _camera_only_config() -> ExperimentConfig:
    return ExperimentConfig(
        hardware=HardwareProfile(
            name="camera-only",
            devices=(),
            channels=(),
            cameras=(
                CameraSpec.model_validate(
                    {
                        "name": "ir_cam0",
                        "adapter": "capa.devices.sim.flir_ir_sim",
                        "kind": "ir",
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
        operator=OperatorRef(id="op"),
        sample=SampleInfo(id="CAM-ONLY-1"),
    )


async def test_engine_seals_with_only_cameras_no_devices(tmp_path: Path) -> None:
    """A camera-only run must seal cleanly. Without the §6 fix this
    deadlocks; with it the fan-out short-circuits on the empty producer
    set and the engine reaches SEALED inside its normal time budget.
    """
    config = _camera_only_config()
    engine = ExperimentEngine()

    # Generous timeout: the run is configured for 0.3 s; the engine
    # adds preflight + finalize on top. 10 s is well past any realistic
    # legitimate runtime — anything longer means the deadlock has
    # regressed.
    with anyio.move_on_after(10.0) as scope:
        result = await engine.run(
            config,
            runs_root=tmp_path,
            configure_logging_for_bundle=False,
        )

    assert not scope.cancelled_caught, "engine.run timed out — camera-only deadlock has regressed"

    assert result.run_status == "completed", result.exit_reason
    assert result.bundle_status == "sealed", result.exit_reason
    assert result.bundle_path is not None

    # Camera produced at least one frame — proves the fan-out path was
    # actually exercised, not just bypassed entirely.
    frames = result.bundle_path / "video" / "ir_cam0.frames.parquet"
    assert frames.is_file()


async def test_camera_event_callback_receives_lifecycle_events(tmp_path: Path) -> None:
    """End-to-end smoke test for the ``camera_event_callback`` fanout.

    The IR sim emits ``recording_started`` and ``recording_stopped`` as
    part of its normal lifecycle. With a callback registered on the
    engine, both events must reach the UI side (``events.sqlite`` keeps
    receiving them too — no behavior change there)."""
    from capa.devices.camera.base import CameraEvent

    config = _camera_only_config()
    engine = ExperimentEngine()
    received: list[CameraEvent] = []
    engine.camera_event_callback = received.append

    result = await engine.run(
        config,
        runs_root=tmp_path,
        configure_logging_for_bundle=False,
    )

    assert result.bundle_status == "sealed", result.exit_reason

    # Lifecycle events reached the callback. The exact order is
    # ``recording_started`` first, ``recording_stopped`` last; other
    # adapter-emitted events may or may not appear in between.
    kinds = [event.kind for event in received]
    assert "recording_started" in kinds
    assert "recording_stopped" in kinds
    assert kinds.index("recording_started") < kinds.index("recording_stopped")

    # Every event is keyed to the ir_cam0 spec; the dock relies on this
    # for routing.
    assert all(event.name == "ir_cam0" for event in received)
