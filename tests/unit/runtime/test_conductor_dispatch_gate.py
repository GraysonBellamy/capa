"""Unit tests for :meth:`Conductor._dispatch_emission` filtering behaviour.

Pins the core recording-filter invariants:

1. DataBus is never filtered (safety / procedure subscribers depend on it).
2. UI mirror is never filtered (operator visibility).
3. SourceRecord passes through unconditionally in v1 (native_device_records="all").

Plus the obvious dispatch-side gates:
- ChannelSample suppressed when its channel name isn't in the plan.
- FrameReceipt suppressed when its camera name isn't in the plan.
- CameraEvent always reaches the writer (diagnostic value).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from capa.devices.camera.base import CameraEvent, FrameReceipt
from capa.devices.records import ChannelSample, SourceRecord
from capa.runtime.conductor import Conductor, ConductorConfig
from capa.runtime.emissions import ProcedureTick
from capa.runtime.recording import ResolvedRecordingPlan
from tests.integration.runtime.conductor.fakes import FakeRunSession
from tests.integration.runtime.fakes import FakeWriterRef

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conductor(plan: ResolvedRecordingPlan) -> Conductor:
    """Construct a :class:`Conductor` and seed its private state.

    The dispatch method only reads ``self._recording_plan`` and the
    optional UI bridge, plus the writer + bus passed as arguments.
    Construction is sync, allocates no threads, opens no I/O — safe to
    do in a unit test with a Mock pool.
    """
    pool = MagicMock(name="WorkerPool")
    session = FakeRunSession()
    conductor = Conductor(pool=pool, session=session, config=ConductorConfig())
    conductor._recording_plan = plan
    return conductor


def _channel_sample(name: str, value: float = 1.0) -> ChannelSample:
    return ChannelSample(
        channel=name,
        t_mono_ns=0,
        t_mono_s=0.0,
        value=value,
        unit="V",
    )


def _frame_receipt(name: str = "ir") -> FrameReceipt:
    return FrameReceipt(
        name=name,
        frame_idx=0,
        t_mono_ns=0,
        t_utc=datetime.now(UTC),
    )


def _camera_event(name: str = "ir") -> CameraEvent:
    return CameraEvent(
        name=name,
        t_mono_ns=0,
        t_utc=datetime.now(UTC),
        kind="status",
        message="started",
        severity="info",
    )


def _source_record(channels: tuple[str, ...]) -> SourceRecord:
    return SourceRecord(
        record_id="rec-1",
        adapter="capa.devices.sim.fake",
        device="dev0",
        shape="wide_row",
        t_mono_ns=0,
        t_utc=datetime.now(UTC),
        row={ch: 1.0 for ch in channels},
    )


@dataclass
class _BusSpy:
    """Captures every :meth:`DataBus.publish` call without running real subscribers."""

    published: list[Any] = field(default_factory=list)

    async def publish(self, emission: Any) -> None:
        self.published.append(emission)


@dataclass
class _UiBridgeSpy:
    """Fake UI bridge that captures every emission for assertion."""

    closed: bool = False
    received: list[Any] = field(default_factory=list)

    def put_nowait(self, emission: Any) -> None:
        self.received.append(emission)


def _install_spies(
    plan: ResolvedRecordingPlan,
) -> tuple[Conductor, FakeWriterRef, _BusSpy, _UiBridgeSpy]:
    cond = _make_conductor(plan)
    writer = FakeWriterRef()
    bus = _BusSpy()
    ui = _UiBridgeSpy()
    cond._ui_bridge = ui  # type: ignore[assignment]
    return cond, writer, bus, ui


# ---------------------------------------------------------------------------
# ChannelSample gating
# ---------------------------------------------------------------------------


class TestChannelSampleGate:
    async def test_recorded_channel_reaches_writer_bus_and_ui(self) -> None:
        cond, writer, bus, ui = _install_spies(
            ResolvedRecordingPlan(
                channel_mode="only",
                recorded_channels=("flux",),
                camera_mode="none",
                source="procedure_default",
            )
        )
        sample = _channel_sample("flux")
        await cond._dispatch_emission(sample, writer=writer, bus=bus)  # type: ignore[arg-type]

        assert writer.submitted == [sample]
        assert bus.published == [sample]
        assert ui.received == [sample]

    async def test_unrecorded_channel_skips_writer_only(self) -> None:
        """Invariant 1+2: bus and UI still see it; only writer is gated."""
        cond, writer, bus, ui = _install_spies(
            ResolvedRecordingPlan(
                channel_mode="only",
                recorded_channels=("flux",),
                camera_mode="none",
                source="procedure_default",
            )
        )
        sample = _channel_sample("balance.mass")
        await cond._dispatch_emission(sample, writer=writer, bus=bus)  # type: ignore[arg-type]

        assert writer.submitted == []
        assert bus.published == [sample]
        assert ui.received == [sample]

    async def test_all_mode_passes_anything(self) -> None:
        cond, writer, bus, _ui = _install_spies(
            ResolvedRecordingPlan(
                channel_mode="all",
                camera_mode="all",
                source="procedure_default",
            )
        )
        sample = _channel_sample("anything")
        await cond._dispatch_emission(sample, writer=writer, bus=bus)  # type: ignore[arg-type]

        assert writer.submitted == [sample]


# ---------------------------------------------------------------------------
# FrameReceipt gating
# ---------------------------------------------------------------------------


class TestFrameReceiptGate:
    async def test_recorded_camera_frame_reaches_writer(self) -> None:
        cond, writer, bus, _ui = _install_spies(
            ResolvedRecordingPlan(
                channel_mode="all",
                camera_mode="all",
                recorded_cameras=("ir",),
                source="procedure_default",
            )
        )
        frame = _frame_receipt("ir")
        await cond._dispatch_emission(frame, writer=writer, bus=bus)  # type: ignore[arg-type]

        assert writer.frames == [frame]

    async def test_suppressed_camera_frame_skips_writer_keeps_ui(self) -> None:
        cond, writer, bus, ui = _install_spies(
            ResolvedRecordingPlan(
                channel_mode="all",
                camera_mode="none",
                source="procedure_default",
            )
        )
        frame = _frame_receipt("ir")
        await cond._dispatch_emission(frame, writer=writer, bus=bus)  # type: ignore[arg-type]

        assert writer.frames == []
        # Frames never go on the bus (no procedure subscribers).
        assert bus.published == []
        # UI still mirrors so a misrouted frame is at least visible.
        assert ui.received == [frame]


# ---------------------------------------------------------------------------
# CameraEvent — never filtered
# ---------------------------------------------------------------------------


class TestCameraEventAlwaysPasses:
    async def test_camera_event_reaches_writer_even_when_camera_suppressed(self) -> None:
        """Invariant 4: camera events are tiny + diagnostic — always recorded."""
        cond, writer, bus, _ui = _install_spies(
            ResolvedRecordingPlan(
                channel_mode="all",
                camera_mode="none",
                source="procedure_default",
            )
        )
        event = _camera_event("ir")
        await cond._dispatch_emission(event, writer=writer, bus=bus)  # type: ignore[arg-type]

        assert len(writer.camera_events) == 1
        assert writer.camera_events[0]["source"] == "camera:ir"


# ---------------------------------------------------------------------------
# SourceRecord — never filtered in v1
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ProcedureTick — UI-only mirror, never lands on disk or the data bus
# ---------------------------------------------------------------------------


class TestProcedureTickPassesThroughUiOnly:
    """Procedure ticks are operator-facing telemetry only.

    Tested separately from the recording-filter invariants because
    ticks short-circuit at the top of ``_dispatch_emission`` —
    they don't carry a channel name and the recording plan has no
    opinion about them.
    """

    async def test_tick_reaches_ui_skips_writer_and_bus(self) -> None:
        cond, writer, bus, ui = _install_spies(
            ResolvedRecordingPlan(
                channel_mode="all",
                camera_mode="all",
                source="procedure_default",
            )
        )
        tick = ProcedureTick(
            procedure_id="capa.builtin.heat_flux_tune",
            t_mono_ns=123,
            payload={"phase": "settle"},
        )
        await cond._dispatch_emission(tick, writer=writer, bus=bus)  # type: ignore[arg-type]

        assert ui.received == [tick]
        assert writer.submitted == []
        assert writer.frames == []
        assert writer.camera_events == []
        assert bus.published == []

    async def test_tick_drops_silently_when_no_ui_bridge(self) -> None:
        """Headless run path: no UI bridge attached, tick is a no-op."""
        cond = _make_conductor(
            ResolvedRecordingPlan(
                channel_mode="all",
                camera_mode="all",
                source="procedure_default",
            )
        )
        writer = FakeWriterRef()
        bus = _BusSpy()
        # Deliberately no _ui_bridge — must not raise.
        tick = ProcedureTick(
            procedure_id="capa.builtin.heat_flux_tune",
            t_mono_ns=0,
            payload={},
        )
        await cond._dispatch_emission(tick, writer=writer, bus=bus)  # type: ignore[arg-type]

        assert writer.submitted == []
        assert bus.published == []

    async def test_procedure_ui_sink_publish_forwards_to_ui_bridge(self) -> None:
        """The sink returned by :meth:`Conductor.procedure_ui_sink`
        forwards :class:`ProcedureTick`\\ s onto the attached UI bridge
        synchronously, without touching the writer or bus."""
        cond, _writer, _bus, ui = _install_spies(
            ResolvedRecordingPlan(
                channel_mode="all",
                camera_mode="all",
                source="procedure_default",
            )
        )
        sink = cond.procedure_ui_sink()
        tick = ProcedureTick(
            procedure_id="capa.builtin.heat_flux_tune",
            t_mono_ns=99,
            payload={"iteration": 4},
        )
        sink.publish(tick)

        assert ui.received == [tick]


class TestSourceRecordPassesThrough:
    async def test_source_record_always_reaches_writer(self) -> None:
        """Invariant 3: native device records pass through unconditionally.

        Plan with no channels recorded — a SourceRecord carrying multiple
        columns still lands in writer.submit because the manifest declares
        ``native_device_records="all"``.
        """
        cond, writer, bus, _ui = _install_spies(
            ResolvedRecordingPlan(
                channel_mode="only",
                recorded_channels=(),
                camera_mode="none",
                source="procedure_default",
            )
        )
        record = _source_record(channels=("a", "b"))
        await cond._dispatch_emission(record, writer=writer, bus=bus)  # type: ignore[arg-type]

        assert writer.submitted == [record]
        assert bus.published == [record]
