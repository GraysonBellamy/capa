""":class:`CameraDeviceAdapter` preview-lifecycle unit tests.

Exercises the four new methods (``start/stop_preview_channel``,
``start/stop_idle_preview_source``) with a fake camera so the two cancel
scopes can be observed in isolation from the worker state machine.

Regression guard from rev.1 of the migration plan: cancelling the idle
source must NOT cancel the long-lived channel drainer.
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
        self.kind = "ir"
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

    async def command(self, cmd: object) -> object:
        raise NotImplementedError


class _PumpedFakeCamera(_BaseFakeCamera):
    """Webcam-shape: has start_preview / stop_preview / run_preview_pump."""

    def __init__(self, spec: CameraSpec, *, pump_failures: int = 0) -> None:
        super().__init__(spec, capabilities=frozenset({CameraCapability.LIVE_PREVIEW}))
        self.start_preview_called = 0
        self.stop_preview_called = 0
        self.pump_started = asyncio.Event()
        self.pump_cancelled = False
        self.pump_attempts = 0
        self._pump_failures_remaining = pump_failures
        self._frames_emitted: list[bytes] = []
        self._preview_buf: asyncio.Queue[bytes] = asyncio.Queue()

    async def start_preview(self) -> None:
        self.start_preview_called += 1

    async def stop_preview(self) -> None:
        self.stop_preview_called += 1

    async def run_preview_pump(self) -> None:
        self.pump_attempts += 1
        if self._pump_failures_remaining > 0:
            self._pump_failures_remaining -= 1
            raise RuntimeError("synthetic av.open transient I/O error")
        self.pump_started.set()
        try:
            # Push a single frame to prove the pump ran, then idle.
            await self._preview_buf.put(b"pumped")
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.pump_cancelled = True
            raise

    async def preview_stream(self) -> AsyncIterator[bytes]:
        while True:
            jpeg = await self._preview_buf.get()
            self._frames_emitted.append(jpeg)
            yield jpeg


class _PumplessFakeCamera(_BaseFakeCamera):
    """IR-sim shape: has LIVE_PREVIEW but no run_preview_pump / start_preview."""

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
    return CameraDeviceAdapter(camera=camera, spec=camera.spec, clock_proxy=_ClockProxy())


class TestStartPreviewChannel:
    async def test_noops_without_live_preview_capability(self) -> None:
        cam = _NoPreviewFakeCamera(_spec())
        wrapper = _make_wrapper(cam)
        bridge = _make_bridge()
        await wrapper.start_preview_channel(bridge)
        assert wrapper._channel_task is None  # type: ignore[attr-defined]
        # Stop is a no-op too.
        await wrapper.stop_preview_channel()

    async def test_starts_drainer_with_capability(self) -> None:
        cam = _PumplessFakeCamera(_spec())
        wrapper = _make_wrapper(cam)
        bridge = _make_bridge()
        await wrapper.start_preview_channel(bridge)
        assert wrapper._channel_task is not None  # type: ignore[attr-defined]
        await wrapper.stop_preview_channel()
        assert wrapper._channel_task is None  # type: ignore[attr-defined]

    async def test_second_start_is_noop_when_already_running(self) -> None:
        cam = _PumplessFakeCamera(_spec())
        wrapper = _make_wrapper(cam)
        bridge = _make_bridge()
        await wrapper.start_preview_channel(bridge)
        first_task = wrapper._channel_task  # type: ignore[attr-defined]
        # Second start: would normally trigger bridge.attach_producer
        # again (which would raise). The idempotency guard prevents that.
        await wrapper.start_preview_channel(bridge)
        assert wrapper._channel_task is first_task  # type: ignore[attr-defined]
        await wrapper.stop_preview_channel()


class TestStartIdlePreviewSource:
    async def test_noop_when_camera_has_no_pump(self) -> None:
        cam = _PumplessFakeCamera(_spec())
        wrapper = _make_wrapper(cam)
        await wrapper.start_idle_preview_source()
        assert wrapper._source_task is None  # type: ignore[attr-defined]
        await wrapper.stop_idle_preview_source()  # still no-op

    async def test_calls_start_preview_then_spawns_pump(self) -> None:
        cam = _PumpedFakeCamera(_spec())
        wrapper = _make_wrapper(cam)
        await wrapper.start_idle_preview_source()
        assert cam.start_preview_called == 1
        await asyncio.wait_for(cam.pump_started.wait(), timeout=1.0)
        # Stop the source to clean up the long-lived pump task.
        await wrapper.stop_idle_preview_source()
        assert cam.stop_preview_called == 1
        assert cam.pump_cancelled is True

    async def test_stop_releases_input_container(self) -> None:
        cam = _PumpedFakeCamera(_spec())
        wrapper = _make_wrapper(cam)
        await wrapper.start_idle_preview_source()
        await asyncio.wait_for(cam.pump_started.wait(), timeout=1.0)
        await wrapper.stop_idle_preview_source()
        # After stop returns, the pump task is gone and stop_preview was
        # awaited. The recording pump can now safely claim the container.
        assert wrapper._source_task is None  # type: ignore[attr-defined]
        assert cam.stop_preview_called == 1

    async def test_second_start_is_noop_when_already_running(self) -> None:
        cam = _PumpedFakeCamera(_spec())
        wrapper = _make_wrapper(cam)
        await wrapper.start_idle_preview_source()
        await asyncio.wait_for(cam.pump_started.wait(), timeout=1.0)
        await wrapper.start_idle_preview_source()
        # start_preview was NOT called a second time.
        assert cam.start_preview_called == 1
        await wrapper.stop_idle_preview_source()

    async def test_retries_pump_when_input_container_held(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: post-disarm on Windows the DirectShow filter
        graph holds the camera for several seconds after the recording
        pump released it. The wrapper's source task must retry
        ``run_preview_pump`` with backoff so preview comes back
        instead of stalling on the first transient I/O failure.
        """
        # Squash the backoff for fast test execution.
        from capa.runtime import camera_adapter as ca

        monkeypatch.setattr(ca, "_IDLE_SOURCE_BACKOFF_S", 0.01)

        cam = _PumpedFakeCamera(_spec(), pump_failures=2)
        wrapper = _make_wrapper(cam)
        await wrapper.start_idle_preview_source()
        # Pump should succeed on the third attempt.
        await asyncio.wait_for(cam.pump_started.wait(), timeout=2.0)
        assert cam.pump_attempts == 3
        await wrapper.stop_idle_preview_source()

    async def test_gives_up_after_max_attempts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If every attempt fails (camera truly held by another process)
        the source task exits cleanly without re-raising. The wrapper
        logs a warning; the channel drainer stays alive.
        """
        from capa.runtime import camera_adapter as ca

        monkeypatch.setattr(ca, "_IDLE_SOURCE_BACKOFF_S", 0.01)
        monkeypatch.setattr(ca, "_IDLE_SOURCE_MAX_ATTEMPTS", 2)

        cam = _PumpedFakeCamera(_spec(), pump_failures=99)
        wrapper = _make_wrapper(cam)
        await wrapper.start_idle_preview_source()
        # Wait for the source task to give up.
        for _ in range(100):
            if wrapper._source_task is not None and wrapper._source_task.done():
                break
            await asyncio.sleep(0.01)
        assert wrapper._source_task is not None
        assert wrapper._source_task.done()
        # No exception escaped — task completed via the give-up branch.
        assert wrapper._source_task.exception() is None
        assert cam.pump_attempts == 2
        await wrapper.stop_idle_preview_source()


class _LongLivedPumpFakeCamera(_BaseFakeCamera):
    """Camera with the new unified-pump surface: declares
    ``start_input_pump`` / ``stop_input_pump`` so the wrapper drives the
    pump across its open / close lifecycle instead of the legacy IDLE-
    only ``run_preview_pump``.
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


class TestUnifiedInputPumpHandoff:
    """The wrapper's :meth:`open` calls ``camera.start_input_pump`` when
    the camera advertises it (new visible-cam pump model). The
    ``start_idle_preview_source`` hook is then a no-op for these
    cameras — the pump runs across runs already.
    """

    async def test_open_starts_input_pump_when_camera_has_one(self) -> None:
        cam = _LongLivedPumpFakeCamera(_spec())
        wrapper = _make_wrapper(cam)
        await wrapper.open()
        assert cam.input_pump_started == 1
        # No idle source spawned — the long-lived pump replaces it.
        await wrapper.start_idle_preview_source()
        assert wrapper._source_task is None  # type: ignore[attr-defined]

    async def test_open_is_noop_for_cameras_without_input_pump(self) -> None:
        cam = _PumplessFakeCamera(_spec())
        wrapper = _make_wrapper(cam)
        # No ``start_input_pump`` to invoke — open just delegates and
        # returns without spawning anything.
        await wrapper.open()


class TestScopeIndependence:
    """Regression guard: rev-1 of the plan conflated the channel and the
    source into one scope, so stopping one killed the other. The rewrite
    keeps them independent."""

    async def test_cancelling_idle_source_does_not_cancel_channel(self) -> None:
        cam = _PumpedFakeCamera(_spec())
        wrapper = _make_wrapper(cam)
        bridge = _make_bridge()
        await wrapper.start_preview_channel(bridge)
        await wrapper.start_idle_preview_source()
        await asyncio.wait_for(cam.pump_started.wait(), timeout=1.0)

        channel_task = wrapper._channel_task  # type: ignore[attr-defined]
        assert channel_task is not None

        await wrapper.stop_idle_preview_source()

        # The channel drainer is still alive.
        assert not channel_task.done()
        assert wrapper._channel_task is channel_task  # type: ignore[attr-defined]

        # Clean up.
        await wrapper.stop_preview_channel()
        assert channel_task.done()
