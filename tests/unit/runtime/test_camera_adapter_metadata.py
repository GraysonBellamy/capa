""":class:`CameraDeviceAdapter.camera_metadata` unit tests.

Exercises the capability-style probe forwarding: cameras that expose
``snapshot_metadata`` return a typed :class:`WebcamMetadata`; cameras
that don't (FLIR sim today, plus any future IR adapter) return ``None``.
The wrapper itself never reads camera attributes directly — it's all
``getattr``-probed so a new camera adapter doesn't have to touch this
file to opt in.
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from capa.devices.camera.base import (
    CameraCapability,
    CameraSpec,
)
from capa.devices.camera.metadata import UvcRangeMetadata, WebcamMetadata
from capa.devices.sim.flir_ir_sim import FlirIrSim
from capa.runtime.camera_adapter import CameraDeviceAdapter, _ClockProxy, make_camera_adapter

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


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


class _FakeWebcam:
    """Stand-in that satisfies the probe contract without driving PyAV.

    The wrapper only cares about ``snapshot_metadata`` for this path;
    every other Camera Protocol method stays unimplemented because the
    test never opens or streams the camera.
    """

    def __init__(self, spec: CameraSpec, metadata: WebcamMetadata) -> None:
        self.spec = spec
        self.kind = "visible"
        self.resource_id = f"fake:{spec.name}"
        self.capabilities = frozenset({CameraCapability.LIVE_PREVIEW})
        self._metadata = metadata

    def snapshot_metadata(self) -> WebcamMetadata:
        return self._metadata


def _sample_metadata() -> WebcamMetadata:
    return WebcamMetadata(
        supported_resolutions=((640, 480), (1280, 720)),
        resolution_hint=(1280, 720),
        resolution_fps_caps=MappingProxyType({(640, 480): 30.0, (1280, 720): 30.0}),
        uvc_ranges=MappingProxyType(
            {
                "set_exposure": UvcRangeMetadata(
                    minimum=-13,
                    maximum=-1,
                    step=1,
                    default=-6,
                    current=-6,
                )
            }
        ),
    )


class TestCameraMetadata:
    def test_returns_none_when_camera_has_no_snapshot_method(self) -> None:
        # The FLIR sim is the canonical "no metadata surface" case: an
        # IR camera with no UVC ranges and no dshow probe to enumerate.
        wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=_ir_spec())
        assert wrapper.camera_metadata() is None

    def test_returns_snapshot_for_webcam_shaped_camera(self) -> None:
        spec = _vis_spec()
        meta = _sample_metadata()
        proxy = _ClockProxy()
        wrapper = CameraDeviceAdapter(
            camera=_FakeWebcam(spec=spec, metadata=meta),  # type: ignore[arg-type]
            spec=spec,
            clock_proxy=proxy,
        )
        out = wrapper.camera_metadata()
        assert out is meta

    def test_returns_none_when_snapshot_returns_wrong_type(self) -> None:
        # Defensive: a misbehaving camera that returns a dict from
        # snapshot_metadata gets coerced to None rather than poisoning
        # the cross-loop transfer. Keeps a future buggy plugin from
        # crashing the UI slot that consumes the result.
        class _BadWebcam:
            spec = _vis_spec()
            kind = "visible"
            resource_id = "fake:bad"
            capabilities: frozenset[CameraCapability] = frozenset()

            def snapshot_metadata(self) -> object:
                return {"not": "a WebcamMetadata"}

        spec = _vis_spec()
        wrapper = CameraDeviceAdapter(
            camera=_BadWebcam(),  # type: ignore[arg-type]
            spec=spec,
            clock_proxy=_ClockProxy(),
        )
        assert wrapper.camera_metadata() is None
