"""Engine-side preview wiring — ``_drain_preview`` + the ``LIVE_PREVIEW``
gate in :func:`capa.experiment.cameras.camera_task`, plus the
``camera_event_callback`` follow-up that fans :class:`CameraEvent` out
to the camera-preview dock.

The webcam adapter's preview encoding is unit-tested in
:mod:`tests.unit.test_camera_webcam`. These tests cover the engine's
*forwarding* contract.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from capa.devices.camera.base import (
    CameraCapability,
    CameraEvent,
    CameraSpec,
    FrameReceipt,
)
from capa.experiment.cameras import _drain_events, _drain_preview

pytestmark = pytest.mark.anyio


def _spec(name: str = "fake_cam") -> CameraSpec:
    return CameraSpec.model_validate(
        {
            "name": name,
            "adapter": "capa.devices.camera.webcam",
            "kind": "visible",
        }
    )


class _FakeCamera:
    """Just enough of the :class:`Camera` Protocol surface to drive
    :func:`_drain_preview`. We don't go through the engine's full
    ``camera_task`` here — that's covered by the IR-sim integration."""

    kind = "visible"

    def __init__(
        self,
        *,
        spec: CameraSpec,
        previews: list[bytes],
        capabilities: frozenset[CameraCapability] = frozenset({CameraCapability.LIVE_PREVIEW}),
    ) -> None:
        self.spec = spec
        self.capabilities = capabilities
        self._previews = previews

    async def preview_stream(self) -> AsyncIterator[bytes]:
        for jpeg in self._previews:
            yield jpeg

    # The Protocol covers more methods — none of them are touched by
    # ``_drain_preview`` so a stub-by-omission works.

    async def frame_stream(self) -> AsyncIterator[FrameReceipt]:  # pragma: no cover
        if False:
            yield  # type: ignore[unreachable]

    async def event_stream(self) -> AsyncIterator[CameraEvent]:  # pragma: no cover
        if False:
            yield  # type: ignore[unreachable]


class TestDrainPreviewForwarding:
    async def test_callback_receives_each_preview(self) -> None:
        cam = _FakeCamera(spec=_spec(), previews=[b"jpeg-a", b"jpeg-b", b"jpeg-c"])
        seen: list[tuple[str, bytes]] = []

        await _drain_preview(
            cam,  # type: ignore[arg-type]
            lambda name, jpeg: seen.append((name, jpeg)),
            structlog.get_logger("test"),
        )

        assert seen == [
            ("fake_cam", b"jpeg-a"),
            ("fake_cam", b"jpeg-b"),
            ("fake_cam", b"jpeg-c"),
        ]

    async def test_drain_swallows_callback_exceptions(self) -> None:
        """A flaky preview consumer must never escalate into a recording
        failure — ``_drain_preview`` logs and continues."""
        cam = _FakeCamera(spec=_spec(), previews=[b"jpeg-a", b"jpeg-b"])
        calls: list[bytes] = []

        def cb(_name: str, jpeg: bytes) -> None:
            calls.append(jpeg)
            if len(calls) == 1:
                raise RuntimeError("ui blew up")

        # Must not raise.
        await _drain_preview(
            cam,  # type: ignore[arg-type]
            cb,
            structlog.get_logger("test"),
        )

        # Both items processed; the failure on item 1 didn't kill the loop.
        assert calls == [b"jpeg-a", b"jpeg-b"]


class TestLivePreviewGate:
    """The gate lives in :func:`camera_task`; surfacing it as a separate
    test would require spinning a writer + bundle. Instead assert the
    contract directly via :class:`CameraCapability` membership: the IR
    sim does not declare ``LIVE_PREVIEW``, so the engine skips its drain;
    the webcam adapter does, so the engine spins one up."""

    def test_ir_sim_does_not_advertise_live_preview(self) -> None:
        from capa.devices.sim.flir_ir_sim import FlirIrSim

        assert CameraCapability.LIVE_PREVIEW not in FlirIrSim.capabilities

    def test_webcam_advertises_live_preview(self) -> None:
        # WebcamAdapter.capabilities is now an instance attribute (the
        # set is augmented at open() with duvc-ctl-probed UVC controls).
        # Construct a stub instance to read the baseline set.
        from capa.core.clock import RunClock
        from capa.devices.camera.base import CameraSpec
        from capa.devices.camera.webcam import WebcamAdapter

        spec = CameraSpec.model_validate(
            {
                "name": "x",
                "adapter": "capa.devices.camera.webcam",
                "kind": "visible",
            }
        )
        cam = WebcamAdapter(spec=spec, clock=RunClock.now())
        assert CameraCapability.LIVE_PREVIEW in cam.capabilities


class TestPreviewCallbackOnEngine:
    """Smoke test: setting ``engine.preview_callback`` round-trips into
    the cameras module's ``camera_task`` invocation. Pure attribute
    plumbing — running a real engine here is overkill; the integration
    tests already prove the camera-task code path."""

    def test_preview_callback_attribute_round_trips(self) -> None:
        from capa.experiment.engine import ExperimentEngine

        engine = ExperimentEngine()
        assert engine.preview_callback is None

        captured: list[tuple[str, bytes]] = []

        def cb(name: str, jpeg: bytes) -> None:
            captured.append((name, jpeg))

        engine.preview_callback = cb
        assert engine.preview_callback is cb

        engine.preview_callback = None
        assert engine.preview_callback is None

    def test_camera_event_callback_attribute_round_trips(self) -> None:
        from capa.experiment.engine import ExperimentEngine

        engine = ExperimentEngine()
        assert engine.camera_event_callback is None

        captured: list[CameraEvent] = []

        def cb(event: CameraEvent) -> None:
            captured.append(event)

        engine.camera_event_callback = cb
        assert engine.camera_event_callback is cb

        engine.camera_event_callback = None
        assert engine.camera_event_callback is None


def _camera_event(name: str, kind: str, severity: str = "info") -> CameraEvent:
    return CameraEvent(
        name=name,
        t_mono_ns=0,
        t_utc=datetime.now(UTC),
        kind=kind,
        message="",
        severity=severity,
    )


class _FakeEventCamera:
    """Minimal Camera-like surface for :func:`_drain_events`. The real
    :func:`camera_task` wraps this drain task plus 3 others; here we
    only care about the events fanout, so the rest of the Protocol is
    stubbed."""

    kind = "visible"
    capabilities: frozenset[CameraCapability] = frozenset()

    def __init__(self, *, spec: CameraSpec, events: list[CameraEvent]) -> None:
        self.spec = spec
        self._events = events

    async def event_stream(self) -> AsyncIterator[CameraEvent]:
        for ev in self._events:
            yield ev

    async def frame_stream(self) -> AsyncIterator[FrameReceipt]:  # pragma: no cover
        if False:
            yield  # type: ignore[unreachable]

    async def preview_stream(self) -> AsyncIterator[bytes]:  # pragma: no cover
        if False:
            yield  # type: ignore[unreachable]


class TestDrainEventsCallback:
    """``_drain_events`` writes to ``events.sqlite`` AND fans events to
    the optional ``camera_event_callback``. The callback is the live UI
    side channel; ``events.sqlite`` remains the durable record."""

    async def test_callback_invoked_for_every_event(self) -> None:
        spec = CameraSpec.model_validate(
            {
                "name": "visible_cam0",
                "adapter": "capa.devices.camera.webcam",
                "kind": "visible",
            }
        )
        events_in = [
            _camera_event("visible_cam0", "recording_started"),
            _camera_event("visible_cam0", "pump_warning", severity="warning"),
            _camera_event("visible_cam0", "pump_failed", severity="error"),
            _camera_event("visible_cam0", "recording_stopped"),
        ]
        cam = _FakeEventCamera(spec=spec, events=events_in)

        # ``writer_thread.write_event`` is what the durable side awaits; we
        # just need an awaitable that accepts arbitrary kwargs.
        writer = MagicMock()
        writer.write_event = AsyncMock()

        from capa.core.clock import RunClock

        clock = RunClock.now()
        seen: list[CameraEvent] = []

        await _drain_events(
            cam,  # type: ignore[arg-type]
            writer,
            clock,
            spec,
            None,  # on_failure_callback
            seen.append,  # camera_event_callback
            structlog.get_logger("test"),
        )

        # Durable write happened for every event …
        assert writer.write_event.call_count == len(events_in)
        # … and the callback fanout received every one.
        assert [e.kind for e in seen] == [e.kind for e in events_in]

    async def test_callback_is_optional(self) -> None:
        """``None`` callback must remain a clean no-op — the durable
        writer side keeps working."""
        spec = CameraSpec.model_validate(
            {
                "name": "visible_cam0",
                "adapter": "capa.devices.camera.webcam",
                "kind": "visible",
            }
        )
        cam = _FakeEventCamera(
            spec=spec, events=[_camera_event("visible_cam0", "recording_started")]
        )
        writer = MagicMock()
        writer.write_event = AsyncMock()

        from capa.core.clock import RunClock

        await _drain_events(
            cam,  # type: ignore[arg-type]
            writer,
            RunClock.now(),
            spec,
            None,
            None,  # camera_event_callback omitted
            structlog.get_logger("test"),
        )

        assert writer.write_event.call_count == 1

    async def test_callback_exception_does_not_kill_drain(self) -> None:
        """Same robustness contract as ``_drain_preview``: a flaky UI
        consumer must not escalate into a recording failure. The drain
        logs and keeps going."""
        spec = CameraSpec.model_validate(
            {
                "name": "visible_cam0",
                "adapter": "capa.devices.camera.webcam",
                "kind": "visible",
            }
        )
        events_in = [
            _camera_event("visible_cam0", "recording_started"),
            _camera_event("visible_cam0", "recording_stopped"),
        ]
        cam = _FakeEventCamera(spec=spec, events=events_in)
        writer = MagicMock()
        writer.write_event = AsyncMock()

        seen: list[str] = []

        def cb(event: CameraEvent) -> None:
            seen.append(event.kind)
            if len(seen) == 1:
                raise RuntimeError("ui blew up")

        from capa.core.clock import RunClock

        # Must not raise.
        await _drain_events(
            cam,  # type: ignore[arg-type]
            writer,
            RunClock.now(),
            spec,
            None,
            cb,
            structlog.get_logger("test"),
        )

        # Both events processed; the failure on event 1 didn't kill the loop.
        assert seen == ["recording_started", "recording_stopped"]


# Keep a strict-mode-friendly reference to MagicMock so ruff doesn't
# flag the conditional import path above.
_ = (Any, MagicMock)
