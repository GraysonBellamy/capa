""":class:`CameraDeviceAdapter.camera_metadata` and
:meth:`~CameraDeviceAdapter.read_state_snapshot` unit tests.

Exercises the capability-style probe forwarding: cameras that expose
``snapshot_metadata`` return a typed :class:`WebcamMetadata`; cameras
that don't (FLIR sim today, plus any future IR adapter) return ``None``.
A webcam's :class:`WebcamStateSnapshot` read-back is forwarded likewise.
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
    WebcamControlState,
    WebcamStateSnapshot,
)
from capa.devices.camera.metadata import WebcamMetadata
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


class _DetailWebcam:
    """Stand-in that records ``set_preview_detail`` calls."""

    spec = _vis_spec()
    kind = "visible"
    resource_id = "fake:detail"
    capabilities: frozenset[CameraCapability] = frozenset()

    def __init__(self) -> None:
        self.calls: list[bool] = []

    def set_preview_detail(self, enabled: bool) -> None:
        self.calls.append(enabled)


class TestSetPreviewDetail:
    def test_forwards_to_a_camera_with_a_detail_mode(self) -> None:
        camera = _DetailWebcam()
        wrapper = CameraDeviceAdapter(
            camera=camera,  # type: ignore[arg-type]
            spec=_vis_spec(),
            clock_proxy=_ClockProxy(),
        )
        assert wrapper.set_preview_detail(True) is True
        assert wrapper.set_preview_detail(False) is True
        assert camera.calls == [True, False]

    def test_false_for_a_camera_without_one(self) -> None:
        wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=_ir_spec())
        assert wrapper.set_preview_detail(True) is False


class _ReadingWebcam:
    """Stand-in whose ``read_state_snapshot`` returns whatever it's given."""

    spec = _vis_spec()
    kind = "visible"
    resource_id = "fake:reading"
    capabilities: frozenset[CameraCapability] = frozenset()

    def __init__(self, snapshot: object) -> None:
        self._snapshot = snapshot

    async def read_state_snapshot(self) -> object:
        return self._snapshot


class TestReadStateSnapshot:
    async def test_forwards_a_webcam_snapshot(self) -> None:
        snapshot = WebcamStateSnapshot(
            controls={"zoom": WebcamControlState(value=265, minimum=100, maximum=500, step=1)}
        )
        wrapper = CameraDeviceAdapter(
            camera=_ReadingWebcam(snapshot),  # type: ignore[arg-type]
            spec=_vis_spec(),
            clock_proxy=_ClockProxy(),
        )
        assert await wrapper.read_state_snapshot() is snapshot

    async def test_drops_a_read_back_of_another_type(self) -> None:
        wrapper = CameraDeviceAdapter(
            camera=_ReadingWebcam({"zoom": 265}),  # type: ignore[arg-type]
            spec=_vis_spec(),
            clock_proxy=_ClockProxy(),
        )
        assert await wrapper.read_state_snapshot() is None
