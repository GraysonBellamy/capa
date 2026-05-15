""":class:`FlirIrSim` preview fixture tests.

The IR sim previously emitted a 64-byte slice of the raw frame payload
onto ``_preview_send``, which is not a JPEG. The dock's ``QImage.fromData``
returned null on those bytes, so the preview integration tests passed
without any tile ever painting — a silent failure.

This module asserts the new fixture:

1. advertises :class:`CameraCapability.LIVE_PREVIEW`, and
2. emits JPEG-decodable previews while recording, and
3. emits NO previews between recordings (the IR shape has no
   between-run pump; the drainer sits idle until the next record).
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from capa.core.clock import RunClock
from capa.devices.camera.base import CameraCapability, CameraSpec
from capa.devices.sim.flir_ir_sim import FlirIrSim

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _spec(name: str = "ir_cam0") -> CameraSpec:
    return CameraSpec.model_validate(
        {
            "name": name,
            "adapter": "capa.devices.sim.flir_ir_sim",
            "kind": "ir",
            "params": {"fps": 30},
        }
    )


class TestPreviewCapability:
    def test_sim_advertises_live_preview_capability(self) -> None:
        assert CameraCapability.LIVE_PREVIEW in FlirIrSim.capabilities


class TestPreviewEmission:
    async def test_emits_jpeg_decodable_preview_during_recording(self, tmp_path: Path) -> None:
        cam = FlirIrSim.from_params(spec=_spec(), clock=RunClock.now())
        await cam.open()
        try:
            output = tmp_path / "ir.csq"
            await cam.start_recording(output)
            try:
                # Drive one frame deterministically.
                await cam.pump_one_frame()
                # The preview stream yields the JPEG.
                preview_iter = cam.preview_stream()
                jpeg = await preview_iter.__anext__()
                assert isinstance(jpeg, bytes) and len(jpeg) > 0
                # Round-trip decode through Pillow proves it is a real JPEG.
                img = Image.open(io.BytesIO(jpeg))
                img.load()
                assert img.size == (64, 48)
                assert img.mode in ("L", "RGB", "YCbCr")
                # Magic bytes for JPEG SOI / APP0.
                assert jpeg[:3] == b"\xff\xd8\xff"
            finally:
                await cam.stop_recording()
        finally:
            await cam.close()

    async def test_emits_no_preview_between_recordings(self, tmp_path: Path) -> None:
        """The IR sim has no between-run pump (no input container to drive).
        Calling ``pump_one_frame`` outside of recording raises, so the
        preview channel stays empty — the drainer sits idle but doesn't
        crash.
        """
        cam = FlirIrSim.from_params(spec=_spec(), clock=RunClock.now())
        await cam.open()
        try:
            # An attempt to pump without recording raises — protecting the
            # preview stream from accidental between-runs frames.
            from capa.core.errors import AdapterError

            with pytest.raises(AdapterError):
                await cam.pump_one_frame()
        finally:
            await cam.close()
