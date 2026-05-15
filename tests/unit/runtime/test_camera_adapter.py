""":class:`CameraDeviceAdapter` unit tests.

Drives the wrapper against :class:`FlirIrSim` because it's deterministic
(every frame synthesized in-process, no I/O), exercises both source
streams (frames + events), and has a ``run_pump`` so the multiplexer's
pump path is covered. Hardware tests against the webcam adapter live
under ``tests/hardware/``.

Covers camera unification: the wrapper produces DeviceEmissions over a
Camera and the emission-type dispatch routes frames and events correctly.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from capa.core.clock import RunClock
from capa.devices.adapter import AdapterStartContext, CommandResult, DeviceCommand
from capa.devices.camera.base import (
    CameraEvent,
    CameraHealth,
    CameraSpec,
    FrameReceipt,
)
from capa.devices.records import DeviceSnapshot
from capa.devices.sim.flir_ir_sim import FlirIrSim
from capa.runtime.camera_adapter import (
    CameraDeviceAdapter,
    _ClockProxy,
    make_camera_adapter,
)

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _start_ctx(
    bundle_root: Path,
    *,
    run_id: str = "test-run",
    clock: RunClock | None = None,
) -> AdapterStartContext:
    """Build an :class:`AdapterStartContext` whose bundle root is a real tmp dir.

    The wrapper's :meth:`start` computes its output_path from
    ``ctx.bundle_root``; tests need a real directory so the
    :meth:`Path.mkdir(parents=True, exist_ok=True)` call in
    :meth:`_resolve_output_path` succeeds.
    """
    return AdapterStartContext(
        clock=clock if clock is not None else RunClock.now(),
        run_id=run_id,
        bundle_root=bundle_root,
    )


# ---------------------------------------------------------------------------
# _ClockProxy
# ---------------------------------------------------------------------------


class TestClockProxy:
    def test_proxy_starts_with_now_clock(self) -> None:
        proxy = _ClockProxy()
        # started_mono_ns is non-negative monotonic
        assert proxy.started_mono_ns >= 0
        # t_mono_ns returns ~0 immediately after construction
        # (proxy.now()'s anchor is "now"; t_mono_ns = now - anchor)
        assert proxy.t_mono_ns() < 100_000_000  # < 100ms slack

    def test_rebind_swaps_underlying_clock(self) -> None:
        proxy = _ClockProxy()
        original_anchor = proxy.started_mono_ns
        new_clock = RunClock(
            started_mono_ns=original_anchor + 1_000_000_000,
            started_utc=datetime.now(UTC),
        )
        proxy.rebind(new_clock)
        assert proxy.started_mono_ns == new_clock.started_mono_ns
        # t_mono_ns now relative to the new anchor — should be negative
        # (we just claimed a future anchor)
        assert proxy.t_mono_ns() < 0

    def test_proxy_satisfies_clock_duck_type(self) -> None:
        """Cameras call ``.t_mono_ns()`` / ``.to_wall_ns(...)`` /
        ``.started_mono_ns`` — proxy must expose all of them.
        """
        proxy = _ClockProxy()
        assert hasattr(proxy, "t_mono_ns")
        assert hasattr(proxy, "to_wall_ns")
        assert hasattr(proxy, "started_mono_ns")
        assert hasattr(proxy, "started_utc")
        # Calling them doesn't crash
        _ = proxy.t_mono_ns()
        _ = (proxy.t_mono(),)
        _ = proxy.to_wall_ns(0)
        _ = proxy.to_wall(0.0)


# ---------------------------------------------------------------------------
# Construction / factory
# ---------------------------------------------------------------------------


class TestFactory:
    def test_make_camera_adapter_wraps_flir_sim(self) -> None:
        spec = _ir_spec()
        wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=spec)
        assert isinstance(wrapper, CameraDeviceAdapter)
        assert wrapper.name == spec.name
        assert wrapper.resource_id == "sim:ir_cam0"
        assert wrapper.spec is spec
        assert isinstance(wrapper.camera, FlirIrSim)

    def test_make_camera_adapter_rejects_kind_mismatch(self) -> None:
        """The camera's ``kind`` attribute must match ``spec.kind``.

        :class:`FlirIrSim` declares ``kind="ir"`` so feeding it a
        visible-kind spec must fail at construction. We rely on
        ``FlirIrSim.__init__``'s own kind check raising first; the
        factory's post-check is the defensive double-belt.
        """
        spec = _vis_spec("would_be_ir")
        with pytest.raises(Exception):  # AdapterError, ValueError, etc.
            make_camera_adapter(camera_cls=FlirIrSim, spec=spec)

    def test_resource_id_delegates_to_camera(self) -> None:
        wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=_ir_spec("ir_cam7"))
        assert wrapper.resource_id == wrapper.camera.resource_id


# ---------------------------------------------------------------------------
# open / close
# ---------------------------------------------------------------------------


class TestOpenClose:
    async def test_open_then_close_idempotent(self) -> None:
        wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=_ir_spec())
        await wrapper.open()
        await wrapper.open()  # idempotent
        await wrapper.close()
        await wrapper.close()  # idempotent


# ---------------------------------------------------------------------------
# start / stop — output path + clock rebind
# ---------------------------------------------------------------------------


class TestStartStop:
    async def test_start_rebinds_clock_and_creates_output(self, tmp_path: Path) -> None:
        wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=_ir_spec())
        await wrapper.open()
        try:
            ctx = _start_ctx(tmp_path)
            await wrapper.start(ctx)
            try:
                # Output container exists at <bundle>/video/<name>.csq
                expected = tmp_path / "video" / "ir_cam0.csq"
                assert expected.exists()
                # The camera's internal clock is now the run's clock
                # (proxied — same monotonic anchor)
                assert wrapper.camera._clock.started_mono_ns == ctx.clock.started_mono_ns
            finally:
                await wrapper.stop()
        finally:
            await wrapper.close()

    async def test_start_uses_output_root_override(self, tmp_path: Path) -> None:
        custom_root = tmp_path / "external_video_storage"
        spec_dict = _ir_spec().model_dump()
        spec_dict["output_root"] = str(custom_root)
        spec = CameraSpec.model_validate(spec_dict)
        wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=spec)
        await wrapper.open()
        try:
            ctx = _start_ctx(tmp_path, run_id="run-42")
            await wrapper.start(ctx)
            try:
                # With output_root: <override>/<run_id>/video/<name>.csq
                expected = custom_root / "run-42" / "video" / "ir_cam0.csq"
                assert expected.exists()
            finally:
                await wrapper.stop()
        finally:
            await wrapper.close()

    async def test_stop_idempotent_when_not_recording(self) -> None:
        wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=_ir_spec())
        await wrapper.open()
        try:
            # Never called start — stop should be a no-op rather than
            # exploding.
            await wrapper.stop()
        finally:
            await wrapper.close()

    async def test_start_refuses_double_start(self, tmp_path: Path) -> None:
        wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=_ir_spec())
        await wrapper.open()
        try:
            ctx = _start_ctx(tmp_path)
            await wrapper.start(ctx)
            try:
                with pytest.raises(RuntimeError, match="already recording"):
                    await wrapper.start(ctx)
            finally:
                await wrapper.stop()
        finally:
            await wrapper.close()


# ---------------------------------------------------------------------------
# stream() — interleaved frames + events
# ---------------------------------------------------------------------------


class TestStream:
    async def test_stream_yields_frames_and_recording_events(self, tmp_path: Path) -> None:
        """Drive the sim's run_pump for a short window; assert we see
        at least the ``recording_started`` event plus several frames.
        """
        spec_dict = _ir_spec().model_dump()
        # 10 fps gives ~5 frames in 500 ms
        spec_dict["params"] = {"fps": 10}
        spec = CameraSpec.model_validate(spec_dict)
        wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=spec)
        await wrapper.open()
        try:
            ctx = _start_ctx(tmp_path)
            await wrapper.start(ctx)

            frames: list[FrameReceipt] = []
            events: list[CameraEvent] = []

            async def _consume() -> None:
                async for emission in wrapper.stream():
                    if isinstance(emission, FrameReceipt):
                        frames.append(emission)
                    elif isinstance(emission, CameraEvent):
                        events.append(emission)
                    # Bail after a handful of frames so the test is bounded
                    if len(frames) >= 3:
                        return

            await asyncio.wait_for(_consume(), timeout=5.0)
            # We collected at least 3 frames + saw the start event
            assert len(frames) >= 3
            # recording_started fires during start(), so it should have
            # been buffered before stream() began consuming
            assert any(e.kind == "recording_started" for e in events)
            # FrameReceipts carry the camera name + monotonic ts
            for f in frames:
                assert f.name == spec.name
                assert f.t_mono_ns >= 0
        finally:
            # Stop must run regardless of whether _consume returned early
            await wrapper.stop()
            await wrapper.close()

    async def test_stop_terminates_stream_iteration(self, tmp_path: Path) -> None:
        """Calling ``stop()`` from a separate task must cause the
        iterator to exit. This is the worker-disarm path's load-bearing
        property: without it, ``Worker._stream_task`` would hang past
        the disarm grace and the disarm would report ``FORCED``.
        """
        spec_dict = _ir_spec().model_dump()
        spec_dict["params"] = {"fps": 30}  # high enough to keep pump active
        spec = CameraSpec.model_validate(spec_dict)
        wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=spec)
        await wrapper.open()
        try:
            ctx = _start_ctx(tmp_path)
            await wrapper.start(ctx)

            stopped_after_n: list[int] = []

            async def _consume() -> None:
                count = 0
                async for _ in wrapper.stream():
                    count += 1
                stopped_after_n.append(count)

            async def _stop_after_delay() -> None:
                await asyncio.sleep(0.2)
                await wrapper.stop()

            await asyncio.wait_for(
                asyncio.gather(_consume(), _stop_after_delay()),
                timeout=5.0,
            )
            # The consumer exited (iterator returned, list appended).
            assert len(stopped_after_n) == 1
        finally:
            await wrapper.close()

    async def test_stream_refused_before_start(self) -> None:
        wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=_ir_spec())
        await wrapper.open()
        try:
            with pytest.raises(RuntimeError, match="not recording"):
                # The generator coroutine doesn't raise until first
                # __anext__ — explicitly entering and stepping it forces
                # the check to fire.
                ait = wrapper.stream()
                await ait.__anext__()
        finally:
            await wrapper.close()


# ---------------------------------------------------------------------------
# command — passthrough
# ---------------------------------------------------------------------------


class TestCommand:
    async def test_command_delegates_to_camera(self) -> None:
        wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=_ir_spec())
        await wrapper.open()
        try:
            cmd = DeviceCommand(
                kind="trigger_nuc",
                target=None,
                payload={},
                issued_by="test",
                authorization_id="auth-test",
                confirmed_by=None,
            )
            result = await wrapper.command(cmd)
            assert isinstance(result, CommandResult)
            assert result.accepted is True
        finally:
            await wrapper.close()


# ---------------------------------------------------------------------------
# snapshot — CameraHealth → DeviceSnapshot translation
# ---------------------------------------------------------------------------


class TestSnapshot:
    async def test_snapshot_translates_camera_health(self, tmp_path: Path) -> None:
        wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=_ir_spec())
        await wrapper.open()
        try:
            snap = await wrapper.snapshot()
            assert isinstance(snap, DeviceSnapshot)
            assert snap.adapter == "camera"
            assert snap.device == "ir_cam0"
            # Pre-recording: health="ok", recording=False, frame_count=0
            assert snap.health == "ok"
            assert snap.fields["recording"] is False
            assert snap.fields["frame_count"] == 0
        finally:
            await wrapper.close()

    async def test_snapshot_marks_degraded_when_unhealthy(self) -> None:
        """A camera reporting ``healthy=False`` must surface as
        ``health="degraded"`` so the UI status bar shows a warning pill.
        """
        # Use a MagicMock camera so we can stage an unhealthy snapshot
        # without touching the sim's internals.
        mock_cam = MagicMock()
        mock_cam.spec = _ir_spec()
        mock_cam.kind = "ir"
        mock_cam.resource_id = "sim:fake"

        async def _bad_snapshot() -> CameraHealth:
            return CameraHealth(
                name="fake",
                t_mono_ns=1_000,
                t_utc=datetime.now(UTC),
                recording=True,
                frame_count=100,
                file_size_bytes=4096,
                last_frame_t_mono_ns=999,
                healthy=False,
                error="sensor dropout",
                dropped_frames=3,
            )

        mock_cam.snapshot = _bad_snapshot

        wrapper = CameraDeviceAdapter(
            camera=mock_cam,
            spec=mock_cam.spec,
            clock_proxy=_ClockProxy(),
        )
        snap = await wrapper.snapshot()
        assert snap.health == "degraded"
        assert snap.fields["error"] == "sensor dropout"
        assert snap.fields["dropped_frames"] == 3


# ---------------------------------------------------------------------------
# DeviceAdapter Protocol surface — runtime structural check
# ---------------------------------------------------------------------------


class TestProtocolSurface:
    def test_wrapper_has_required_adapter_attributes(self) -> None:
        """The wrapper must expose ``name``, ``capabilities``, and
        ``resource_id`` so :func:`build_workers` validation treats it
        like any other adapter.
        """
        wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=_ir_spec())
        assert hasattr(wrapper, "name")
        assert hasattr(wrapper, "capabilities")
        assert hasattr(wrapper, "resource_id")
        # All Camera capability flags live on a different enum
        # (CameraCapability). The wrapper's adapter capabilities are
        # intentionally empty.
        assert wrapper.capabilities == frozenset()

    def test_wrapper_has_required_adapter_methods(self) -> None:
        wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=_ir_spec())
        for method in ("open", "close", "start", "stop", "stream", "command", "snapshot"):
            assert callable(getattr(wrapper, method, None)), f"missing {method}"
