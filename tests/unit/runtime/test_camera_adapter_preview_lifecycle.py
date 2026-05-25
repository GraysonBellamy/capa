""":class:`CameraDeviceAdapter` preview-lifecycle unit tests.

Exercises ``start/stop_preview_channel`` against fake cameras so the
drainer task lifecycle can be observed in isolation from the worker
state machine.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Literal, cast

import pytest

from capa.devices.adapter import CommandResult, DeviceCommand
from capa.devices.camera.base import (
    Camera,
    CameraCapability,
    CameraEvent,
    CameraHealth,
    CameraInfo,
    CameraSpec,
    FrameReceipt,
)
from capa.runtime.bridge import BridgePolicy, ThreadBridge
from capa.runtime.camera_adapter import CameraDeviceAdapter, _ClockProxy
from capa.runtime.preview import PreviewFrame

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _spec(name: str = "cam0") -> CameraSpec:
    return CameraSpec.model_validate(
        {
            "name": name,
            "adapter": "capa.devices.sim.flir_ir_sim",
            "kind": "ir",
        }
    )


class _BaseFakeCamera:
    """Common surface for the lifecycle fakes."""

    def __init__(self, spec: CameraSpec, *, capabilities: frozenset[CameraCapability]) -> None:
        self.spec = spec
        self.kind: Literal["visible", "ir"] = "ir"
        self.resource_id = f"fake:{spec.name}"
        self.capabilities = capabilities
        self._opened = False
        self._closed_evt = asyncio.Event()

    async def discover(self) -> tuple[CameraInfo, ...]:
        return ()

    async def open(self) -> CameraInfo:
        self._opened = True
        return CameraInfo.model_validate(
            {"adapter": "fake", "name": self.spec.name, "transport": "loopback"}
        )

    async def close(self) -> None:
        self._closed_evt.set()

    async def start_recording(self, output_path: object) -> None:
        pass

    async def stop_recording(self) -> None:
        pass

    async def snapshot(self) -> CameraHealth:
        raise NotImplementedError

    def frame_stream(self) -> AsyncIterator[FrameReceipt]:
        raise NotImplementedError

    def event_stream(self) -> AsyncIterator[CameraEvent]:
        raise NotImplementedError

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        raise NotImplementedError


class _PumplessFakeCamera(_BaseFakeCamera):
    """IR-sim shape: has LIVE_PREVIEW; preview_stream blocks until cancelled."""

    def __init__(self, spec: CameraSpec) -> None:
        super().__init__(spec, capabilities=frozenset({CameraCapability.LIVE_PREVIEW}))

    async def preview_stream(self) -> AsyncIterator[bytes]:
        await asyncio.Event().wait()
        yield b""  # never reached


class _NoPreviewFakeCamera(_BaseFakeCamera):
    """No LIVE_PREVIEW capability — start_preview_channel must no-op."""

    def __init__(self, spec: CameraSpec) -> None:
        super().__init__(spec, capabilities=frozenset())

    async def preview_stream(self) -> AsyncIterator[bytes]:
        await asyncio.Event().wait()
        yield b""


def _make_bridge(name: str = "preview") -> ThreadBridge[PreviewFrame]:
    loop = asyncio.get_running_loop()
    bridge: ThreadBridge[PreviewFrame] = ThreadBridge(
        name=name,
        capacity=4,
        consumer_loop=loop,
        policy=BridgePolicy.DROP_OLDEST,
    )
    bridge.attach_consumer()
    return bridge


def _make_wrapper(camera: _BaseFakeCamera) -> CameraDeviceAdapter:
    return CameraDeviceAdapter(
        camera=cast(Camera, camera), spec=camera.spec, clock_proxy=_ClockProxy()
    )


class TestStartPreviewChannel:
    async def test_noops_without_live_preview_capability(self) -> None:
        cam = _NoPreviewFakeCamera(_spec())
        wrapper = _make_wrapper(cam)
        bridge = _make_bridge()
        await wrapper.start_preview_channel(bridge)
        assert wrapper._channel_task is None
        # Stop is a no-op too.
        await wrapper.stop_preview_channel()

    async def test_starts_drainer_with_capability(self) -> None:
        cam = _PumplessFakeCamera(_spec())
        wrapper = _make_wrapper(cam)
        bridge = _make_bridge()
        await wrapper.start_preview_channel(bridge)
        assert wrapper._channel_task is not None
        await wrapper.stop_preview_channel()
        assert wrapper._channel_task is None

    async def test_second_start_is_noop_when_already_running(self) -> None:
        cam = _PumplessFakeCamera(_spec())
        wrapper = _make_wrapper(cam)
        bridge = _make_bridge()
        await wrapper.start_preview_channel(bridge)
        first_task = wrapper._channel_task
        # Second start: would normally trigger bridge.attach_producer
        # again (which would raise). The idempotency guard prevents that.
        await wrapper.start_preview_channel(bridge)
        assert wrapper._channel_task is first_task
        await wrapper.stop_preview_channel()


class _LongLivedPumpFakeCamera(_BaseFakeCamera):
    """Camera with the unified-pump surface: declares
    ``start_input_pump`` / ``stop_input_pump`` so the wrapper drives the
    pump across its open / close lifecycle.
    """

    def __init__(self, spec: CameraSpec) -> None:
        super().__init__(spec, capabilities=frozenset({CameraCapability.LIVE_PREVIEW}))
        self.input_pump_started = 0
        self.input_pump_stopped = 0

    async def start_input_pump(self) -> None:
        self.input_pump_started += 1

    async def stop_input_pump(self) -> None:
        self.input_pump_stopped += 1

    async def preview_stream(self) -> AsyncIterator[bytes]:
        await asyncio.Event().wait()
        yield b""


class TestUnifiedInputPump:
    """The wrapper's :meth:`open` calls ``camera.start_input_pump`` when
    the camera advertises it. Cameras without it (IR sim, FLIR Atlas)
    drive their per-run pump from :meth:`stream` instead.
    """

    async def test_open_starts_input_pump_when_camera_has_one(self) -> None:
        cam = _LongLivedPumpFakeCamera(_spec())
        wrapper = _make_wrapper(cam)
        await wrapper.open()
        assert cam.input_pump_started == 1

    async def test_open_is_noop_for_cameras_without_input_pump(self) -> None:
        cam = _PumplessFakeCamera(_spec())
        wrapper = _make_wrapper(cam)
        # No ``start_input_pump`` to invoke — open just delegates and
        # returns without spawning anything.
        await wrapper.open()
