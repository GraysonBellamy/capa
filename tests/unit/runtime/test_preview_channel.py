""":func:`run_preview_drain` and the preview wire type.

Targets the worker-side drainer in isolation: a fake camera with a
synthetic ``preview_stream`` is paired with a real :class:`ThreadBridge`
on the test loop; the test asserts every frame the camera emits lands on
the bridge with its name and a positive ``t_mono_ns`` stamp.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from capa.devices.camera.base import (
    CameraCapability,
    CameraEvent,
    CameraHealth,
    CameraInfo,
    CameraSpec,
    FrameReceipt,
)
from capa.runtime.bridge import BridgePolicy, ThreadBridge
from capa.runtime.preview import PreviewFrame, run_preview_drain

pytestmark = pytest.mark.anyio


def _spec(name: str = "cam") -> CameraSpec:
    return CameraSpec.model_validate(
        {
            "name": name,
            "adapter": "capa.devices.sim.flir_ir_sim",
            "kind": "ir",
        }
    )


class _FakeCamera:
    """Minimal :class:`Camera` Protocol surface for drainer tests."""

    def __init__(
        self,
        *,
        spec: CameraSpec,
        jpegs: list[bytes],
        raise_on_index: int | None = None,
    ) -> None:
        self.spec = spec
        self.kind = "ir"
        self.resource_id = f"fake:{spec.name}"
        self.capabilities = frozenset({CameraCapability.LIVE_PREVIEW})
        self._jpegs = list(jpegs)
        self._raise_on_index = raise_on_index
        self._closed = asyncio.Event()

    async def discover(self) -> tuple[CameraInfo, ...]:  # pragma: no cover
        return ()

    async def open(self) -> CameraInfo:  # pragma: no cover
        return CameraInfo.model_validate(
            {"adapter": "fake", "name": self.spec.name, "transport": "loopback"}
        )

    async def close(self) -> None:
        self._closed.set()

    async def start_recording(self, output_path: object) -> None:  # pragma: no cover
        pass

    async def stop_recording(self) -> None:  # pragma: no cover
        pass

    async def snapshot(self) -> CameraHealth:  # pragma: no cover
        raise NotImplementedError

    def frame_stream(self) -> AsyncIterator[FrameReceipt]:  # pragma: no cover
        raise NotImplementedError

    def event_stream(self) -> AsyncIterator[CameraEvent]:  # pragma: no cover
        raise NotImplementedError

    async def command(self, cmd: object) -> object:  # pragma: no cover
        raise NotImplementedError

    async def preview_stream(self) -> AsyncIterator[bytes]:
        for idx, jpeg in enumerate(self._jpegs):
            if self._raise_on_index is not None and idx == self._raise_on_index:
                raise RuntimeError("synthetic preview failure")
            yield jpeg
        # After the synthetic frames, block until close() is called so
        # the drainer's "iterator end" path is taken on a deliberate
        # signal rather than racing the test.
        await self._closed.wait()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPreviewFrame:
    def test_dataclass_is_frozen(self) -> None:
        pf = PreviewFrame(name="cam", t_mono_ns=12345, jpeg=b"\x00")
        with pytest.raises(Exception):
            pf.name = "other"  # type: ignore[misc]


class TestRunPreviewDrain:
    async def test_forwards_each_frame_onto_bridge(self) -> None:
        jpegs = [b"jpeg-0", b"jpeg-1", b"jpeg-2"]
        cam = _FakeCamera(spec=_spec(), jpegs=jpegs)
        bridge = _make_bridge()
        drain_task = asyncio.create_task(run_preview_drain(camera=cam, bridge=bridge))
        received: list[PreviewFrame] = []
        try:
            for _ in range(len(jpegs)):
                pf = await asyncio.wait_for(bridge.get(), timeout=1.0)
                assert pf is not None
                received.append(pf)
        finally:
            await cam.close()
            await asyncio.wait_for(drain_task, timeout=1.0)
        assert [pf.jpeg for pf in received] == jpegs
        assert {pf.name for pf in received} == {"cam"}
        assert all(pf.t_mono_ns > 0 for pf in received)

    async def test_exits_when_camera_closes(self) -> None:
        cam = _FakeCamera(spec=_spec(), jpegs=[b"one"])
        bridge = _make_bridge()
        drain_task = asyncio.create_task(run_preview_drain(camera=cam, bridge=bridge))
        # Drain the one frame then trigger close — the drainer's
        # ``async for`` exits on the next iteration.
        pf = await asyncio.wait_for(bridge.get(), timeout=1.0)
        assert pf is not None
        await cam.close()
        await asyncio.wait_for(drain_task, timeout=1.0)
        assert drain_task.done() and drain_task.exception() is None

    async def test_logs_and_returns_when_bridge_closes(self) -> None:
        cam = _FakeCamera(spec=_spec(), jpegs=[b"one", b"two", b"three"])
        bridge = _make_bridge()
        drain_task = asyncio.create_task(run_preview_drain(camera=cam, bridge=bridge))
        # Receive one frame so the loop is mid-stream, then slam the
        # bridge closed. The next put raises ThreadBridgeClosedError;
        # the drainer logs and returns cleanly.
        await asyncio.wait_for(bridge.get(), timeout=1.0)
        bridge.close()
        await cam.close()
        await asyncio.wait_for(drain_task, timeout=1.0)
        assert drain_task.done()
        # No exception escapes — the drainer swallowed the close.
        assert drain_task.exception() is None

    async def test_drain_continues_past_producer_error(self) -> None:
        """A camera-side iterator error stops the drainer (the iterator is
        gone), but the failure is logged not re-raised — the long-lived
        drainer cannot leak a task exception.
        """
        cam = _FakeCamera(spec=_spec(), jpegs=[b"one"], raise_on_index=0)
        bridge = _make_bridge()
        drain_task = asyncio.create_task(run_preview_drain(camera=cam, bridge=bridge))
        await asyncio.wait_for(drain_task, timeout=1.0)
        assert drain_task.exception() is None
