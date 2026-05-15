"""WebcamAdapter (PyAV) — push-mode encoding + frame-index bookkeeping.

Plan §12.3 / P4 Stage B. Uses synthetic numpy frames so tests don't depend
on a real V4L2 device. Pump-mode (live capture) is exercised by the gated
hardware tier (``CAPA_HARDWARE_TESTS=1``); not covered here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import av
import numpy as np
import pytest

from capa.core.clock import RunClock
from capa.core.errors import AdapterError
from capa.devices.camera.base import (
    CameraEvent,
    CameraSpec,
    FrameReceipt,
)
from capa.devices.camera.webcam import (
    DEFAULT_FPS,
    WebcamAdapter,
)

pytestmark = pytest.mark.anyio


def _spec(name: str = "visible_cam0", **overrides: object) -> CameraSpec:
    base: dict[str, object] = {
        "name": name,
        "adapter": "capa.devices.camera.webcam",
        "kind": "visible",
    }
    base.update(overrides)
    return CameraSpec.model_validate(base)


def _make(
    *, fps: float = 30.0, width: int = 64, height: int = 48, codec: str = "mpeg4"
) -> WebcamAdapter:
    """Default to ``mpeg4`` for tests — it's faster and always available
    even on FFmpeg builds without libx264. Production uses ``libx264``."""
    return WebcamAdapter(
        spec=_spec(),
        clock=RunClock.now(),
        fps=fps,
        width=width,
        height=height,
        codec=codec,
        pix_fmt="yuv420p",
    )


def _solid_frame(width: int, height: int, color: tuple[int, int, int]) -> np.ndarray:
    """HxWx3 uint8 RGB frame of a single color."""
    arr = np.zeros((height, width, 3), dtype=np.uint8)
    arr[..., 0] = color[0]
    arr[..., 1] = color[1]
    arr[..., 2] = color[2]
    return arr


class TestWebcamPreviewBetweenRuns:
    """Preview emission is unconditional in the unified-pump design:
    ``push_frame`` produces a 2 Hz preview JPEG whether or not recording
    is active, so the live tile stays current between runs.
    """

    async def test_push_frame_emits_preview_without_recording(self) -> None:
        """``push_frame`` between runs emits a JPEG but no FrameReceipt —
        the live tile updates while the encoder stays idle."""
        cam = _make()
        await cam.open()
        try:
            frame = _solid_frame(64, 48, (255, 0, 0))
            receipt = await cam.push_frame(frame)
            assert receipt is None, "no FrameReceipt when not recording"
            # The JPEG landed on the preview stream.
            stream = cam.preview_stream()
            jpeg_bytes = await stream.__anext__()
            assert isinstance(jpeg_bytes, bytes)
            assert jpeg_bytes[:2] == b"\xff\xd8", f"expected JPEG SOI; got {jpeg_bytes[:2]!r}"
        finally:
            await cam.close()

    async def test_preview_throttle_drops_within_500ms(self) -> None:
        """Two pushes within the 2 Hz window should produce exactly one
        emission. The throttle uses RunClock.t_mono_ns() so a real wall-clock
        race doesn't matter — back-to-back pushes are always inside the
        same nanosecond bucket."""
        cam = _make()
        await cam.open()
        try:
            frame = _solid_frame(64, 48, (255, 0, 0))
            await cam.push_frame(frame)
            await cam.push_frame(frame)  # throttled
            stream = cam.preview_stream()
            # First receive must succeed; second receive must timeout
            # within a short window (no second frame queued).
            import anyio

            jpeg = await stream.__anext__()
            assert jpeg[:2] == b"\xff\xd8"
            with anyio.move_on_after(0.05) as scope:
                await stream.__anext__()
            assert scope.cancelled_caught, (
                "expected throttle to suppress the second preview emission"
            )
        finally:
            await cam.close()

    async def test_start_recording_resets_preview_throttle(self, tmp_path: Path) -> None:
        """``CameraDeviceAdapter`` rebinds the clock proxy onto the
        run's RunClock immediately before calling ``start_recording``.
        ``_last_preview_t_mono_ns`` was previously persisted across that
        rebind, so the throttle check (``t_mono_ns() - last >= 500ms``)
        saw a large negative delta — the new clock's ``t_mono_ns`` was
        near zero but ``last`` held a value in the OLD anchor's units.
        Previews stayed dark for the early part of every run and the
        dock's stale detector tripped.

        Regression: after ``start_recording``, the throttle must be
        reset so the first push_frame's preview encode fires.
        """
        cam = _make()
        await cam.open()
        try:
            # Simulate "idle preview ran for a while" by stamping the
            # throttle with a value far in the future of the new clock
            # we'll install via the wrapper analogue.
            cam._last_preview_t_mono_ns = 30_000_000_000  # 30s in old units

            out = tmp_path / "v.mkv"
            await cam.start_recording(out)
            # Throttle must be reset; first push_frame produces a preview.
            assert cam._last_preview_t_mono_ns is None

            await cam.push_frame(_solid_frame(64, 48, (255, 0, 0)))

            # A JPEG SHOULD be available on the preview stream right away.
            import anyio

            jpeg: bytes | None = None
            with anyio.move_on_after(0.5):
                jpeg = await cam.preview_stream().__anext__()
            assert jpeg is not None, "expected preview JPEG after first recorded frame"
            assert jpeg[:2] == b"\xff\xd8"
            await cam.stop_recording()
        finally:
            await cam.close()

    async def test_preview_keeps_emitting_after_stop_recording(self, tmp_path: Path) -> None:
        """Regression for the bug that motivated unifying the pumps:
        after ``stop_recording``, ``push_frame`` must still emit preview
        frames (no FrameReceipt) so the live tile doesn't freeze on the
        last recorded frame while the operator waits for the next run.
        """
        cam = _make(codec="mpeg4")
        await cam.open()
        await cam.start_recording(tmp_path / "v.mkv")
        r1 = await cam.push_frame(_solid_frame(64, 48, (10, 200, 30)))
        assert r1 is not None
        await cam.stop_recording()

        # Throttle is at 0ns since the recording-mode preview just
        # emitted; reset it so the next push fires its preview.
        cam._last_preview_t_mono_ns = None

        r2 = await cam.push_frame(_solid_frame(64, 48, (200, 10, 30)))
        assert r2 is None, "no FrameReceipt after stop_recording"

        # cam.close() drops the preview send-end so the iterator
        # terminates after draining buffered items. Two JPEGs should
        # be present: one from inside the recording window, one from
        # the preview-only push after.
        await cam.close()

        previews: list[bytes] = []
        async for jpeg in cam.preview_stream():
            previews.append(jpeg)
        assert len(previews) == 2
        for jpeg in previews:
            assert jpeg[:3] == b"\xff\xd8\xff"


class TestWebcamLifecycle:
    async def test_kind_mismatch_rejected(self) -> None:
        ir_spec = CameraSpec.model_validate({"name": "x", "adapter": "y", "kind": "ir"})
        with pytest.raises(AdapterError, match="kind == 'visible'"):
            WebcamAdapter(spec=ir_spec, clock=RunClock.now())

    async def test_open_close_idempotent(self) -> None:
        cam = _make()
        info1 = await cam.open()
        info2 = await cam.open()
        assert info1 == info2
        assert info1.adapter == "webcam"
        await cam.close()
        await cam.close()

    async def test_push_before_open_rejected(self) -> None:
        """``push_frame`` requires ``open()`` so the streams are bound. It
        does NOT require ``start_recording`` — the preview-only path is
        valid between runs and emits a JPEG without a FrameReceipt."""
        cam = _make()
        with pytest.raises(AdapterError, match="requires open"):
            await cam.push_frame(_solid_frame(64, 48, (255, 0, 0)))


class TestWebcamRecording:
    async def test_writes_mkv_with_frames(self, tmp_path: Path) -> None:
        cam = _make(width=64, height=48, codec="mpeg4")
        await cam.open()
        out = tmp_path / "video" / "visible_cam0.mkv"
        await cam.start_recording(out)
        for color in [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]:
            await cam.push_frame(_solid_frame(64, 48, color))
        await cam.stop_recording()
        await cam.close()

        assert out.exists()
        assert out.stat().st_size > 0
        # PyAV must be able to read what we wrote.
        with av.open(str(out)) as container:
            stream = container.streams.video[0]
            assert stream.width == 64
            assert stream.height == 48
            decoded = list(container.decode(stream))
            assert len(decoded) == 4

    async def test_mkv_metadata_has_run_anchor(self, tmp_path: Path) -> None:
        cam = _make(width=64, height=48, codec="mpeg4")
        await cam.open()
        out = tmp_path / "visible_cam0.mkv"
        await cam.start_recording(out)
        await cam.push_frame(_solid_frame(64, 48, (10, 20, 30)))
        await cam.stop_recording()
        await cam.close()

        with av.open(str(out)) as container:
            # MKV uppercases metadata keys on read; compare case-insensitively.
            md = {k.upper(): v for k, v in container.metadata.items()}
            assert md.get("RUN_STARTED_UTC") is not None
            assert md.get("CAMERA_NAME") == "visible_cam0"
            assert md.get("CAPA_CODEC") == "mpeg4"

    async def test_frame_receipt_indexing(self, tmp_path: Path) -> None:
        cam = _make(codec="mpeg4")
        await cam.open()
        await cam.start_recording(tmp_path / "v.mkv")

        receipts: list[FrameReceipt] = []

        async def drain() -> None:
            async for r in cam.frame_stream():
                receipts.append(r)

        import anyio

        async with anyio.create_task_group() as tg:
            tg.start_soon(drain)
            for _ in range(3):
                await cam.push_frame(_solid_frame(64, 48, (1, 2, 3)))
            await cam.stop_recording()
            await cam.close()

        assert [r.frame_idx for r in receipts] == [0, 1, 2]
        # Monotonic timestamps strictly non-decreasing.
        assert all(
            b >= a
            for a, b in zip(
                (r.t_mono_ns for r in receipts),
                (r.t_mono_ns for r in receipts[1:]),
                strict=False,
            )
        )


class TestWebcamFrameValidation:
    async def test_rejects_wrong_dtype(self, tmp_path: Path) -> None:
        cam = _make(codec="mpeg4")
        await cam.open()
        await cam.start_recording(tmp_path / "v.mkv")
        bad = np.zeros((48, 64, 3), dtype=np.float32)
        with pytest.raises(AdapterError, match="HxWx3 uint8"):
            await cam.push_frame(bad)
        await cam.close()

    async def test_rejects_grayscale(self, tmp_path: Path) -> None:
        cam = _make(codec="mpeg4")
        await cam.open()
        await cam.start_recording(tmp_path / "v.mkv")
        bad = np.zeros((48, 64), dtype=np.uint8)
        with pytest.raises(AdapterError, match="HxWx3 uint8"):
            await cam.push_frame(bad)
        await cam.close()


class TestWebcamHealth:
    async def test_snapshot_tracks_frame_count(self, tmp_path: Path) -> None:
        cam = _make(codec="mpeg4")
        await cam.open()
        h0 = await cam.snapshot()
        assert h0.recording is False
        assert h0.frame_count == 0

        await cam.start_recording(tmp_path / "v.mkv")
        await cam.push_frame(_solid_frame(64, 48, (0, 0, 0)))
        await cam.push_frame(_solid_frame(64, 48, (0, 0, 0)))
        h1 = await cam.snapshot()
        assert h1.recording is True
        assert h1.frame_count == 2
        assert h1.last_frame_t_mono_ns is not None

        await cam.stop_recording()
        h2 = await cam.snapshot()
        assert h2.recording is False
        assert h2.frame_count == 2
        assert h2.file_size_bytes > 0
        await cam.close()


class TestWebcamEvents:
    async def test_start_stop_events_emitted(self, tmp_path: Path) -> None:
        cam = _make(codec="mpeg4")
        await cam.open()

        events: list[CameraEvent] = []

        async def drain() -> None:
            async for ev in cam.event_stream():
                events.append(ev)

        import anyio

        async with anyio.create_task_group() as tg:
            tg.start_soon(drain)
            await cam.start_recording(tmp_path / "v.mkv")
            await cam.stop_recording()
            await cam.close()

        kinds = [ev.kind for ev in events]
        assert "recording_started" in kinds
        assert "recording_stopped" in kinds


class TestFromParams:
    def test_from_params_constructor(self) -> None:
        cam = WebcamAdapter.from_params(
            spec=_spec(),
            clock=RunClock.now(),
            fps=15,
            width=320,
            height=240,
            codec="mpeg4",
        )
        assert cam.spec.name == "visible_cam0"


class TestDefaults:
    def test_default_fps_constant(self) -> None:
        assert DEFAULT_FPS == 30


class TestV4L2IdentityProbe:
    """Hardware-day §5: ``manifest.json.cameras[*].identity`` was ``None``
    for real webcams because sysfs metadata was never read. Probe in
    :meth:`open` so the bundle records the actual hardware identity.
    """

    async def test_open_populates_model_and_serial_from_sysfs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from capa.devices.camera import webcam as webcam_module

        monkeypatch.setattr(
            webcam_module,
            "_probe_v4l2_info",
            lambda _path: webcam_module.V4L2Probe(
                card_name="Logitech Webcam C930e",
                serial="E7501BDE",
                bus_info="3-6.2",
            ),
        )
        monkeypatch.setattr("capa.devices.camera.webcam.sys.platform", "linux")
        cam = WebcamAdapter(
            spec=_spec(),
            clock=RunClock.now(),
            input_format="v4l2",
            input_url="/dev/video4",
        )
        info = await cam.open()
        assert info.model == "Logitech Webcam C930e"
        assert info.serial == "E7501BDE"
        await cam.close()

    async def test_probe_failure_leaves_spec_hints(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from capa.devices.camera import webcam as webcam_module

        monkeypatch.setattr(
            webcam_module,
            "_probe_v4l2_info",
            lambda _path: webcam_module.V4L2Probe(card_name=None, serial=None, bus_info=None),
        )
        monkeypatch.setattr("capa.devices.camera.webcam.sys.platform", "linux")
        spec = _spec()
        spec_with_hint = CameraSpec.model_validate(
            {**spec.model_dump(), "model_hint": "fallback-model", "serial": "fallback-serial"}
        )
        cam = WebcamAdapter(
            spec=spec_with_hint,
            clock=RunClock.now(),
            input_format="v4l2",
            input_url="/dev/video4",
        )
        info = await cam.open()
        assert info.model == "fallback-model"
        assert info.serial == "fallback-serial"
        await cam.close()

    async def test_probe_skipped_on_non_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from capa.devices.camera import webcam as webcam_module

        called: list[str] = []

        def _spy(path: str) -> webcam_module.V4L2Probe:
            called.append(path)
            return webcam_module.V4L2Probe(card_name="X", serial="Y", bus_info=None)

        monkeypatch.setattr(webcam_module, "_probe_v4l2_info", _spy)
        monkeypatch.setattr("capa.devices.camera.webcam.sys.platform", "darwin")
        cam = WebcamAdapter(
            spec=_spec(),
            clock=RunClock.now(),
            input_format="avfoundation",
            input_url="default",
        )
        await cam.open()
        await cam.close()
        assert called == []  # probe skipped on non-Linux + non-v4l2

    def test_probe_returns_empty_on_missing_node(self, tmp_path: Path) -> None:
        # Pointing at a path that doesn't exist must not raise.
        from capa.devices.camera.webcam import _probe_v4l2_info

        result = _probe_v4l2_info("/dev/video999")
        assert result.card_name is None
        assert result.serial is None
        assert result.bus_info is None


class TestEncoderFailureGuard:
    """Hardware-day §6: a single ``avcodec_send_packet() returned 22``
    (libx264 EINVAL) at t≈23 s killed the entire camera task and lost
    the recording. The adapter must drop the offending frame, log a
    ``pump_warning`` event, and keep recording.
    """

    async def test_encoder_invaliddata_drops_frame_and_continues(self, tmp_path: Path) -> None:
        cam = _make(codec="mpeg4")
        await cam.open()
        await cam.start_recording(tmp_path / "v.mkv")

        # PyAV's stream.encode and container.mux are Cython attributes
        # (read-only); swap the entire stream + container for in-Python
        # fakes so the second push triggers InvalidDataError without
        # poking at PyAV internals.
        class _FaultyStream:
            def __init__(self) -> None:
                self.calls = 0

            def encode(self, _frame: object) -> list[object]:
                self.calls += 1
                if self.calls == 2:
                    raise av.error.InvalidDataError(22, "Invalid argument")
                return [object()]

        class _NullContainer:
            def __init__(self) -> None:
                self.muxed: list[object] = []

            def mux(self, packet: object) -> None:
                self.muxed.append(packet)

            def close(self) -> None: ...

        cam._output_stream = _FaultyStream()
        cam._output_container = _NullContainer()

        events: list[CameraEvent] = []

        async def drain_events() -> None:
            async for ev in cam.event_stream():
                events.append(ev)

        import anyio

        async with anyio.create_task_group() as tg:
            tg.start_soon(drain_events)
            r1 = await cam.push_frame(_solid_frame(64, 48, (10, 0, 0)))
            r2 = await cam.push_frame(_solid_frame(64, 48, (0, 10, 0)))  # rejected
            r3 = await cam.push_frame(_solid_frame(64, 48, (0, 0, 10)))
            await cam.close()

        # Surviving frames return receipts with contiguous indexes — the
        # dropped frame must NOT advance the index counter or libx264
        # would later reject every frame after the gap.
        assert r1 is not None and r1.frame_idx == 0
        assert r2 is None
        assert r3 is not None and r3.frame_idx == 1

        snap = await _snapshot_health(cam)
        assert snap.frame_count == 2
        assert snap.dropped_frames == 1

        # Operator-visible warning event.
        kinds = [ev.kind for ev in events]
        assert "pump_warning" in kinds
        warning = next(ev for ev in events if ev.kind == "pump_warning")
        assert "InvalidDataError" in warning.message
        assert warning.severity == "warning"


async def _snapshot_health(cam: WebcamAdapter) -> Any:
    """Helper — call snapshot() while the adapter is in any lifecycle state."""
    return await cam.snapshot()


class TestPushFrameOffLoop:
    """Encode + mux must run in a worker thread so the asyncio loop stays free.

    Hardware-day §5.B regression: the visible webcam pump captured at ~14 fps
    instead of 30 because :code:`frame.reformat(...).to_ndarray()` and the
    libx264 encode were running on the asyncio loop.
    """

    async def test_push_frame_runs_encode_in_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cam = _make(codec="mpeg4")
        await cam.open()
        await cam.start_recording(tmp_path / "v.mkv")

        import anyio.to_thread

        original_run_sync = anyio.to_thread.run_sync
        offloaded: list[str] = []

        async def _spy(func: Any, *args: Any, **kwargs: Any) -> Any:
            offloaded.append(getattr(func, "__name__", repr(func)))
            return await original_run_sync(func, *args, **kwargs)

        monkeypatch.setattr(anyio.to_thread, "run_sync", _spy)

        await cam.push_frame(_solid_frame(64, 48, (10, 20, 30)))
        await cam.stop_recording()
        await cam.close()

        assert "_push_frame_sync" in offloaded, (
            f"push_frame must offload encode to a worker thread; "
            f"to_thread.run_sync calls observed: {offloaded}"
        )


class TestInputPumpLifecycle:
    """Unified input pump: ``start_input_pump`` spawns the long-lived
    decode-and-push loop, ``stop_input_pump`` signals it to exit and
    closes the input container. The pump runs across the recording
    boundary — encoding when ``_recording`` is set, preview-only
    otherwise — so the operator's live tile never freezes on the gap
    between runs.
    """

    async def test_start_then_stop_drives_decoder_and_exits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pump opens the input, decodes a few frames into the preview
        stream, then exits cleanly when ``stop_input_pump`` fires."""
        decoded = 0
        pump_container_closed = {"n": 0}

        def fake_advance(_decoder: object) -> object | None:
            nonlocal decoded
            decoded += 1
            if decoded > 3:
                return None
            return object()

        def fake_reformat(_frame: object) -> np.ndarray:
            return _solid_frame(64, 48, (10, 200, 30))

        class _FakeStream:
            type = "video"

        class _FakeContainer:
            streams = (_FakeStream(),)

            def decode(self, _stream: object) -> object:
                return object()

            def close(self) -> None:
                pump_container_closed["n"] += 1

        cam = _make(codec="mpeg4")
        # Real av.open during the dshow/V4L2 probe in cam.open(); the
        # fake replaces it only for the pump's input-side ``_open_input_with_retry``.
        await cam.open()

        monkeypatch.setattr(
            "capa.devices.camera.webcam.av.open",
            lambda *_a, **_kw: _FakeContainer(),
        )
        monkeypatch.setattr("capa.devices.camera.webcam._advance_decoder", fake_advance)
        monkeypatch.setattr("capa.devices.camera.webcam._reformat_to_rgb24", fake_reformat)

        await cam.start_input_pump()

        # Let the pump drain the synthetic decoder.
        import anyio

        previews: list[bytes] = []

        async def _drain_one() -> None:
            async for jpeg in cam.preview_stream():
                previews.append(jpeg)
                if len(previews) >= 1:
                    return

        with anyio.move_on_after(1.0):
            await _drain_one()

        await cam.stop_input_pump()
        await cam.close()

        assert decoded >= 1, "pump never advanced the decoder"
        assert pump_container_closed["n"] == 1, "pump's input container closed exactly once"
        assert len(previews) >= 1
        assert previews[0][:2] == b"\xff\xd8"

    async def test_pump_survives_stop_recording(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bug this whole refactor fixes: after ``stop_recording``
        the pump must continue emitting preview frames so the live tile
        stays current. The pump must NOT close the input container."""
        decoded = 0

        def fake_advance(_decoder: object) -> object | None:
            nonlocal decoded
            decoded += 1
            # Stay alive long enough for the test to exercise both
            # phases (recording, then between-runs). The test stops the
            # pump explicitly via stop_input_pump.
            return object()

        def fake_reformat(_frame: object) -> np.ndarray:
            return _solid_frame(64, 48, (10, 200, 30))

        class _FakeStream:
            type = "video"

        pump_opened = {"n": 0}
        pump_closed = {"n": 0}

        class _FakeContainer:
            streams = (_FakeStream(),)

            def decode(self, _stream: object) -> object:
                return object()

            def close(self) -> None:
                pump_closed["n"] += 1

        def fake_av_open(*_a: object, **_kw: object) -> _FakeContainer:
            pump_opened["n"] += 1
            return _FakeContainer()

        cam = _make(codec="mpeg4")
        # Real av.open for the dshow/V4L2 probe inside cam.open() AND
        # for start_recording's output encoder; the fake replaces it
        # only for the pump's input container open.
        await cam.open()
        await cam.start_recording(tmp_path / "v.mkv")

        monkeypatch.setattr("capa.devices.camera.webcam.av.open", fake_av_open)
        monkeypatch.setattr("capa.devices.camera.webcam._advance_decoder", fake_advance)
        monkeypatch.setattr("capa.devices.camera.webcam._reformat_to_rgb24", fake_reformat)

        await cam.start_input_pump()

        # Wait for the pump to drain at least two frames, then stop recording.
        import anyio

        async def _wait_for_frames(n: int) -> None:
            while decoded < n:
                await anyio.sleep(0.01)

        with anyio.move_on_after(1.0):
            await _wait_for_frames(2)
        frames_at_stop = decoded
        await cam.stop_recording()

        # Critical assertion: the pump task is still running, input
        # container was not closed by stop_recording.
        assert cam._pump_task is not None
        assert not cam._pump_task.done(), "pump must outlive stop_recording"
        assert pump_closed["n"] == 0, "input container must NOT close on stop_recording"
        assert pump_opened["n"] == 1, "input container should open exactly once per pool"

        # Let it run a bit more to verify post-recording pumping works.
        with anyio.move_on_after(1.0):
            await _wait_for_frames(frames_at_stop + 2)

        await cam.stop_input_pump()
        await cam.close()
        assert pump_closed["n"] == 1, "input container closed exactly once at close()"


class TestPreviewEncoding:
    """Preview thumbnails: real JPEG (Pillow) + 2 Hz adapter-side throttle.

    Until this lands the preview stream emitted a ``bytes(frame[:1,:,0][:64])``
    placeholder that no consumer could render. Replacing it with JPEG +
    throttle is the visible-camera half of the §10.2 camera-preview dock.
    """

    async def test_first_preview_is_decodable_jpeg(self, tmp_path: Path) -> None:
        from io import BytesIO

        from PIL import Image

        from capa.devices.camera.webcam import PREVIEW_MAX_WIDTH

        cam = _make(width=640, height=480, codec="mpeg4")
        await cam.open()
        await cam.start_recording(tmp_path / "v.mkv")
        await cam.push_frame(_solid_frame(640, 480, (10, 200, 30)))
        await cam.stop_recording()
        await cam.close()

        previews: list[bytes] = []
        # ``cam.close()`` closed the preview send stream above, so
        # ``async for`` drains every buffered item then exits cleanly.
        async for jpeg in cam.preview_stream():
            previews.append(jpeg)

        assert previews, "no preview emitted on the first frame"
        first = previews[0]
        # Real JPEGs always start with the SOI marker FF D8 FF.
        assert first[:3] == b"\xff\xd8\xff", first[:8]

        img = Image.open(BytesIO(first))
        img.verify()  # raises on corruption
        # Re-open: verify() consumes the file pointer.
        img = Image.open(BytesIO(first))
        # Width-cap honored; aspect preserved.
        assert img.width <= PREVIEW_MAX_WIDTH
        # 640×480 input → 320×240 thumbnail at the 320 px cap.
        assert img.width == PREVIEW_MAX_WIDTH
        assert img.height == round(480 * (PREVIEW_MAX_WIDTH / 640))

    async def test_throttles_consecutive_pushes(self, tmp_path: Path) -> None:
        """Five rapid pushes must NOT produce five previews — the 2 Hz
        cap inside ``_push_frame_sync`` skips encodes inside the 500 ms
        window. Pushing in a tight loop completes in <100 ms on any
        plausible CI box, so all 5 fall inside one window and only the
        first emits a preview.
        """
        cam = _make(width=64, height=48, codec="mpeg4")
        await cam.open()
        await cam.start_recording(tmp_path / "v.mkv")
        for _ in range(5):
            await cam.push_frame(_solid_frame(64, 48, (10, 200, 30)))
        await cam.stop_recording()
        await cam.close()

        previews: list[bytes] = []
        async for jpeg in cam.preview_stream():
            previews.append(jpeg)

        # Strict upper bound: 5 frames → at most 1 preview at 2 Hz; a
        # second would require ≥500 ms between pushes.
        assert len(previews) == 1, len(previews)


class TestOpenInputRetry:
    """``_open_input_with_retry`` recovers from transient ``[Errno 5]``."""

    async def test_transient_errno5_then_success(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Two transient EIO failures, then a working open. Backoff sleeps
        are stubbed so the test runs in milliseconds."""
        from capa.devices.camera import webcam as webcam_mod

        cam = _make(codec="mpeg4")
        await cam.open()

        attempts = {"n": 0}
        sentinel = object()

        def _fake_av_open() -> object:
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise OSError(5, "I/O error: device busy (DirectShow)")
            return sentinel

        sleeps: list[float] = []

        async def _no_sleep(secs: float) -> None:
            sleeps.append(secs)

        # Patch ``av.open`` (called via ``anyio.to_thread.run_sync``) and
        # the backoff to keep the test fast.
        monkeypatch.setattr("capa.devices.camera.webcam.av.open", lambda *a, **kw: _fake_av_open())
        monkeypatch.setattr("capa.devices.camera.webcam.anyio.sleep", _no_sleep)

        result = await cam._open_input_with_retry()
        assert result is sentinel
        assert attempts["n"] == 3
        # First two delays from the schedule should have been used.
        assert sleeps == list(webcam_mod.OPEN_RETRY_DELAYS_S[:2])
        await cam.close()

    async def test_non_transient_errno_propagates_immediately(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ENOENT (missing device) is not transient; surface it on attempt 1."""
        cam = _make(codec="mpeg4")
        await cam.open()

        attempts = {"n": 0}

        def _fake_av_open() -> None:
            attempts["n"] += 1
            raise OSError(2, "No such file or directory")

        monkeypatch.setattr("capa.devices.camera.webcam.av.open", lambda *a, **kw: _fake_av_open())
        with pytest.raises(OSError):
            await cam._open_input_with_retry()
        assert attempts["n"] == 1
        await cam.close()


class TestDshowFormatInfoProbe:
    """``_probe_dshow_format_info_sync`` opens the dshow input with
    ``list_options=true``, captures FFmpeg's log output, and parses the
    ``max s=WxH fps=NN`` tails into a sorted, deduped resolution list
    plus a per-resolution fps cap dict. Failures (PyAV refuses, no
    parseable lines) collapse to ``([], {})`` so callers can fall back
    to a static set and an uncapped fps spinbox.
    """

    def test_returns_empty_when_av_open_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from capa.devices.camera.webcam import _probe_dshow_format_info_sync

        def _boom(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("simulated dshow failure")

        monkeypatch.setattr("capa.devices.camera.webcam.av.open", _boom)
        # No matching log lines means an empty parse result, regardless
        # of whether av.open succeeded or failed.
        result = _probe_dshow_format_info_sync("video=Whatever")
        assert result == ([], {})

    def test_parses_max_lines_into_sorted_unique_pairs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resolution list is sorted by area, deduped across pixel formats;
        the fps dict holds the highest reported max-fps per resolution."""
        from capa.devices.camera import webcam as webcam_mod

        class _FakeContainer:
            def close(self) -> None: ...

        monkeypatch.setattr(webcam_mod.av, "open", lambda *a, **kw: _FakeContainer())

        fake_log_entries = [
            (0, "dshow", "  pixel_format=yuyv422  min s=1920x1080 fps=5 max s=1920x1080 fps=15"),
            (0, "dshow", "  pixel_format=mjpeg    min s=1920x1080 fps=5 max s=1920x1080 fps=30"),
            (0, "dshow", "  pixel_format=mjpeg    min s=640x480 fps=5 max s=640x480 fps=30"),
            (0, "dshow", "  pixel_format=mjpeg    min s=1280x720 fps=5 max s=1280x720 fps=30"),
            (0, "dshow", "  pixel_format=yuyv422  min s=640x480 fps=5 max s=640x480 fps=30"),
            (0, "dshow", "  no resolution here"),
        ]

        class _FakeCapture:
            def __init__(self, *_args: object, **_kwargs: object) -> None: ...

            def __enter__(self) -> list[tuple[int, str, str]]:
                return fake_log_entries

            def __exit__(self, *_args: object) -> None:
                return None

        import av.logging as _av_log

        monkeypatch.setattr(_av_log, "Capture", _FakeCapture)
        monkeypatch.setattr(_av_log, "set_level", lambda _level: None)
        monkeypatch.setattr(_av_log, "get_level", lambda: None)

        resolutions, fps_caps = webcam_mod._probe_dshow_format_info_sync("video=Whatever")
        assert resolutions == [(640, 480), (1280, 720), (1920, 1080)]
        # 1920x1080 had two pixel formats (15 and 30); the higher wins.
        assert fps_caps == {(640, 480): 30.0, (1280, 720): 30.0, (1920, 1080): 30.0}

    def test_falls_back_to_min_when_max_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When a log line has only ``min s=WxH`` (no ``max s=``), the
        resolution still surfaces — fps_caps stays empty for it."""
        from capa.devices.camera import webcam as webcam_mod

        class _FakeContainer:
            def close(self) -> None: ...

        monkeypatch.setattr(webcam_mod.av, "open", lambda *a, **kw: _FakeContainer())

        fake_log_entries = [
            # No "max s=" — only "min s=", fps annotation present but lives
            # on the min side (which we still capture for cap purposes).
            (0, "dshow", "  pixel_format=yuyv422  min s=1280x720 fps=10"),
        ]

        class _FakeCapture:
            def __init__(self, *_args: object, **_kwargs: object) -> None: ...

            def __enter__(self) -> list[tuple[int, str, str]]:
                return fake_log_entries

            def __exit__(self, *_args: object) -> None:
                return None

        import av.logging as _av_log

        monkeypatch.setattr(_av_log, "Capture", _FakeCapture)
        monkeypatch.setattr(_av_log, "set_level", lambda _level: None)
        monkeypatch.setattr(_av_log, "get_level", lambda: None)

        resolutions, fps_caps = webcam_mod._probe_dshow_format_info_sync("video=X")
        assert resolutions == [(1280, 720)]
        assert fps_caps == {(1280, 720): 10.0}
