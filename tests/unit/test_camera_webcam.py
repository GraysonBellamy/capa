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

    async def test_push_before_recording_rejected(self) -> None:
        cam = _make()
        await cam.open()
        with pytest.raises(AdapterError, match="requires start_recording"):
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
            stream = next(s for s in container.streams if s.type == "video")
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
        monkeypatch.setattr(webcam_module.sys, "platform", "linux")
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
        monkeypatch.setattr(webcam_module.sys, "platform", "linux")
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
        monkeypatch.setattr(webcam_module.sys, "platform", "darwin")
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

        async def _spy(func, *args, **kwargs):
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


class TestStopTimeRaceGuard:
    """Hardware-day 2026-05-09 PM re-validation: every clean stop emitted a
    misleading ``engine.camera.pump_failed: push_frame requires
    start_recording()`` because the pump's last in-flight frame raced with
    ``close()`` flipping ``_recording=False``. ``run_pump`` must observe the
    flag between decode and ``push_frame`` and exit before invoking the
    precondition guard on a non-recording adapter.
    """

    async def test_run_pump_does_not_call_push_frame_after_stop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cam = _make(codec="mpeg4")
        await cam.open()
        await cam.start_recording(tmp_path / "v.mkv")

        # Synthetic decode loop. Frame 3's decode flips ``_recording`` to
        # False mid-tick — before the fix, run_pump would proceed to
        # push_frame and the precondition guard would raise.
        decoded = 0

        def fake_advance(_decoder: object) -> object | None:
            nonlocal decoded
            decoded += 1
            if decoded == 3:
                cam._recording = False
                return object()
            if decoded > 3:
                return None
            return object()

        def fake_reformat(_frame: object) -> np.ndarray:
            return _solid_frame(64, 48, (10, 0, 0))

        class _FakeStream:
            type = "video"

        class _FakeContainer:
            streams = (_FakeStream(),)

            def decode(self, _stream: object) -> object:
                return object()

            def close(self) -> None: ...

        monkeypatch.setattr(
            "capa.devices.camera.webcam.av.open",
            lambda *_a, **_kw: _FakeContainer(),
        )
        monkeypatch.setattr("capa.devices.camera.webcam._advance_decoder", fake_advance)
        monkeypatch.setattr("capa.devices.camera.webcam._reformat_to_rgb24", fake_reformat)

        push_calls_recording_state: list[bool] = []
        original_push = WebcamAdapter.push_frame

        async def tracked_push(
            self_: WebcamAdapter,
            frame: np.ndarray,
            *,
            capture_latency_s: float = 0.0,
        ) -> FrameReceipt | None:
            push_calls_recording_state.append(self_._recording)
            return await original_push(self_, frame, capture_latency_s=capture_latency_s)

        monkeypatch.setattr(WebcamAdapter, "push_frame", tracked_push)

        # No exception must propagate — that's the regression.
        await cam.run_pump()

        # Frames 1 + 2 reached push_frame; frame 3's decode flipped the
        # flag and the new gate broke the loop before push_frame fired.
        assert decoded == 3
        assert push_calls_recording_state == [True, True], (
            "stop-time race regressed: push_frame called with "
            f"_recording state history {push_calls_recording_state}"
        )

    async def test_run_pump_breaks_after_reformat_when_recording_flips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Second gate: ``_recording`` may also flip during the reformat
        worker thread (CPU-heavy on real frames). The post-reformat check
        must catch that too.
        """
        cam = _make(codec="mpeg4")
        await cam.open()
        await cam.start_recording(tmp_path / "v.mkv")

        decoded = 0

        def fake_advance(_decoder: object) -> object | None:
            nonlocal decoded
            decoded += 1
            if decoded > 2:
                return None
            return object()

        reformatted = 0

        def fake_reformat(_frame: object) -> np.ndarray:
            nonlocal reformatted
            reformatted += 1
            if reformatted == 2:
                # Flip the flag inside reformat — exercises the post-reformat
                # gate that wraps push_frame.
                cam._recording = False
            return _solid_frame(64, 48, (10, 0, 0))

        class _FakeStream:
            type = "video"

        class _FakeContainer:
            streams = (_FakeStream(),)

            def decode(self, _stream: object) -> object:
                return object()

            def close(self) -> None: ...

        monkeypatch.setattr(
            "capa.devices.camera.webcam.av.open",
            lambda *_a, **_kw: _FakeContainer(),
        )
        monkeypatch.setattr("capa.devices.camera.webcam._advance_decoder", fake_advance)
        monkeypatch.setattr("capa.devices.camera.webcam._reformat_to_rgb24", fake_reformat)

        push_calls_recording_state: list[bool] = []
        original_push = WebcamAdapter.push_frame

        async def tracked_push(
            self_: WebcamAdapter,
            frame: np.ndarray,
            *,
            capture_latency_s: float = 0.0,
        ) -> FrameReceipt | None:
            push_calls_recording_state.append(self_._recording)
            return await original_push(self_, frame, capture_latency_s=capture_latency_s)

        monkeypatch.setattr(WebcamAdapter, "push_frame", tracked_push)

        await cam.run_pump()

        # Only the first frame reached push_frame; the second's reformat
        # flipped the flag and the post-reformat gate broke the loop.
        assert push_calls_recording_state == [True]


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
