"""Camera Protocol + FlirIrSim fixture.

* :class:`CameraSpec` validation (``kind`` enforcement, name uniqueness in
  ``HardwareProfile``).
* :class:`FlirIrSim` lifecycle (open → start_recording → pump → stop → close).
* On-disk file shape — header bytes, frame records, finalized frame count.
* :func:`extract_frame_index` round-trips ``(frame_idx, t_mono_ns)``.
* Selection rules (serial mismatch, model_hint mismatch).
"""

from __future__ import annotations

import itertools
import struct
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from capa.core.clock import RunClock
from capa.core.errors import AdapterError, ConfigError
from capa.devices.camera.base import (
    CameraCapability,
    CameraEvent,
    CameraSpec,
    FrameReceipt,
)
from capa.devices.sim.flir_ir_sim import (
    HEADER_SIZE,
    SIM_MAGIC,
    FlirIrSim,
    extract_frame_index,
)
from capa.experiment.config import HardwareProfile

pytestmark = pytest.mark.anyio


def _spec(name: str = "ir_cam0", **overrides: Any) -> CameraSpec:
    base: dict[str, Any] = {
        "name": name,
        "adapter": "capa.devices.sim.flir_ir_sim",
        "kind": "ir",
    }
    base.update(overrides)
    return CameraSpec.model_validate(base)


def _make_sim(tmp_path: Path, **kwargs: Any) -> FlirIrSim:
    spec = _spec()
    clock = RunClock.now()
    return FlirIrSim(spec=spec, clock=clock, **kwargs)


class TestCameraSpec:
    def test_minimal_ir_spec(self) -> None:
        spec = _spec()
        assert spec.name == "ir_cam0"
        assert spec.kind == "ir"
        assert spec.on_failure == "warn"

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(Exception):
            CameraSpec.model_validate(
                {
                    "name": "x",
                    "adapter": "y",
                    "kind": "ir",
                    "unknown": True,
                }
            )

    def test_estimated_bps_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            CameraSpec.model_validate(
                {
                    "name": "x",
                    "adapter": "y",
                    "kind": "ir",
                    "estimated_bps": 0,
                }
            )


class TestHardwareProfileCameras:
    def test_cameras_default_empty(self) -> None:
        hp = HardwareProfile(name="rig")
        assert hp.cameras == ()
        assert hp.camera_names() == ()

    def test_camera_name_collisions_rejected(self) -> None:
        with pytest.raises(ConfigError, match="duplicate camera names"):
            HardwareProfile(
                name="rig",
                cameras=(_spec(name="cam0"), _spec(name="cam0")),
            )

    def test_camera_device_namespace_collision_rejected(self) -> None:
        from capa.experiment.config import DeviceConfig

        with pytest.raises(ConfigError, match="camera and device names overlap"):
            HardwareProfile(
                name="rig",
                devices=(DeviceConfig(name="ir_cam0", adapter="capa.devices.sim.watlow_sim"),),
                cameras=(_spec(name="ir_cam0"),),
            )


class TestFlirIrSimLifecycle:
    async def test_open_close_idempotent(self, tmp_path: Path) -> None:
        sim = _make_sim(tmp_path)
        info1 = await sim.open()
        info2 = await sim.open()
        assert info1 == info2
        assert info1.adapter == "flir_ir_sim"
        assert info1.serial == "SIM-IR-0001"
        await sim.close()
        await sim.close()  # idempotent

    async def test_serial_mismatch_rejected(self) -> None:
        spec = _spec(serial="DOES-NOT-EXIST")
        sim = FlirIrSim(spec=spec, clock=RunClock.now())
        with pytest.raises(AdapterError, match="requested serial"):
            await sim.open()

    async def test_model_hint_mismatch_rejected(self) -> None:
        spec = _spec(model_hint="FLIR T1020")  # not the sim's "FLIR-SIM"
        sim = FlirIrSim(spec=spec, clock=RunClock.now())
        with pytest.raises(AdapterError, match="model_hint"):
            await sim.open()

    async def test_start_recording_requires_open(self, tmp_path: Path) -> None:
        sim = _make_sim(tmp_path)
        with pytest.raises(AdapterError, match="requires open"):
            await sim.start_recording(tmp_path / "ir_cam0.csq")

    async def test_close_stops_recording(self, tmp_path: Path) -> None:
        sim = _make_sim(tmp_path, fps=30)
        await sim.open()
        await sim.start_recording(tmp_path / "ir_cam0.csq")
        await sim.pump_one_frame()
        # Skip stop_recording — close() should drive it.
        await sim.close()
        # File is closed and frame count patched in.
        path = tmp_path / "ir_cam0.csq"
        assert path.exists()
        with open(path, "rb") as fp:
            assert fp.read(len(SIM_MAGIC)) == SIM_MAGIC
            assert struct.unpack("<I", fp.read(4))[0] == 1


class TestFlirIrSimFileShape:
    async def test_header_layout(self, tmp_path: Path) -> None:
        sim = _make_sim(tmp_path, fps=10, width=64, height=48)
        await sim.open()
        path = tmp_path / "video" / "ir_cam0.csq"
        await sim.start_recording(path)
        await sim.stop_recording()

        with open(path, "rb") as fp:
            data = fp.read()
        assert data[: len(SIM_MAGIC)] == SIM_MAGIC
        frame_count = struct.unpack("<I", data[12:16])[0]
        width = struct.unpack("<I", data[16:20])[0]
        height = struct.unpack("<I", data[20:24])[0]
        fps = struct.unpack("<I", data[24:28])[0]
        assert frame_count == 0
        assert (width, height, fps) == (64, 48, 10)

    async def test_meta_sidecar_written(self, tmp_path: Path) -> None:
        sim = _make_sim(tmp_path, fps=30)
        await sim.open()
        await sim.start_recording(tmp_path / "ir_cam0.csq")
        await sim.pump_one_frame()
        await sim.pump_one_frame()
        await sim.stop_recording()

        meta_path = tmp_path / "ir_cam0.csq.meta.json"
        assert meta_path.exists()
        body = meta_path.read_text(encoding="utf-8")
        assert '"frame_count": 2' in body
        assert '"final": true' in body

    async def test_pump_writes_deterministic_payload(self, tmp_path: Path) -> None:
        sim = _make_sim(tmp_path, fps=30, frame_payload_bytes=8)
        await sim.open()
        await sim.start_recording(tmp_path / "ir_cam0.csq")
        await sim.pump_one_frame()
        await sim.pump_one_frame()
        await sim.stop_recording()

        with open(tmp_path / "ir_cam0.csq", "rb") as fp:
            fp.seek(HEADER_SIZE)
            # Two frames of (16-byte header + 8-byte payload).
            for expected_idx in range(2):
                idx, t_mono_ns, payload_size = struct.unpack("<IqI", fp.read(16))
                payload = fp.read(payload_size)
                assert idx == expected_idx
                assert t_mono_ns >= 0
                assert payload_size == 8
                seed = expected_idx & 0xFF
                assert payload == bytes((seed + i) & 0xFF for i in range(8))


class TestExtractFrameIndex:
    async def test_round_trip(self, tmp_path: Path) -> None:
        sim = _make_sim(tmp_path, fps=10, frame_payload_bytes=32)
        await sim.open()
        path = tmp_path / "ir_cam0.csq"
        await sim.start_recording(path)
        for _ in range(5):
            await sim.pump_one_frame()
        await sim.stop_recording()

        index = extract_frame_index(path)
        assert [row[0] for row in index] == [0, 1, 2, 3, 4]
        # Evenly spaced at 100ms (1/10 fps) from a known anchor.
        gaps = [b - a for (_, a), (_, b) in itertools.pairwise(index)]
        assert all(abs(gap - 100_000_000) < 1_000 for gap in gaps)

    async def test_rejects_non_sim_file(self, tmp_path: Path) -> None:
        bogus = tmp_path / "foo.csq"
        bogus.write_bytes(b"FFF\x00..." + b"\x00" * 64)  # real-FFF magic prefix
        with pytest.raises(AdapterError, match="not a capa IR sim file"):
            extract_frame_index(bogus)


class TestFlirIrSimStreams:
    async def test_frame_stream_yields_receipts(self, tmp_path: Path) -> None:
        sim = _make_sim(tmp_path, fps=30, frame_payload_bytes=16)
        await sim.open()
        await sim.start_recording(tmp_path / "ir_cam0.csq")

        receipts: list[FrameReceipt] = []

        async def drain() -> None:
            async for r in sim.frame_stream():
                receipts.append(r)

        # Drive a few frames, then close to terminate the iterator.
        import anyio

        async with anyio.create_task_group() as tg:
            tg.start_soon(drain)
            for _ in range(3):
                await sim.pump_one_frame()
            await sim.close()

        assert [r.frame_idx for r in receipts] == [0, 1, 2]
        assert all(r.name == "ir_cam0" for r in receipts)
        assert all(isinstance(r.t_utc, datetime) and r.t_utc.tzinfo == UTC for r in receipts)

    async def test_event_stream_records_start_stop(self, tmp_path: Path) -> None:
        sim = _make_sim(tmp_path, fps=30)
        await sim.open()

        events: list[CameraEvent] = []

        async def drain() -> None:
            async for ev in sim.event_stream():
                events.append(ev)

        import anyio

        async with anyio.create_task_group() as tg:
            tg.start_soon(drain)
            await sim.start_recording(tmp_path / "ir_cam0.csq")
            await sim.stop_recording()
            await sim.close()

        kinds = [ev.kind for ev in events]
        assert "recording_started" in kinds
        assert "recording_stopped" in kinds


class TestFlirIrSimCapabilities:
    def test_capabilities_include_radiometric_and_palette(self) -> None:
        sim = _make_sim(tmp_path=Path("/tmp"))
        assert CameraCapability.RADIOMETRIC in sim.capabilities
        assert CameraCapability.PALETTE in sim.capabilities
        # The sim now emits JPEG-decodable preview frames so the preview
        # dock / webcam card actually paint pixels in integration tests.
        # See tests/unit/test_camera_flir_ir_sim_preview.py.
        assert CameraCapability.LIVE_PREVIEW in sim.capabilities
