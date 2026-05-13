"""Tests for :class:`capa.runtime.writer_ref.WriterThreadRef`.

The ref must:

1. Satisfy the :class:`WriterRef` protocol (verified via ``isinstance``).
2. Forward emission submissions through to the underlying writer thread.
3. Synthesize ``t_mono_ns`` and ``t_utc`` from the run clock on every
   ``write_event``, and tag the event with the configured ``source``.
4. Propagate writer-thread errors verbatim (no swallowing).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest

from capa.core.clock import RunClock
from capa.devices.records import DeviceSnapshot
from capa.runtime.runcontext import WriterRef
from capa.runtime.writer_ref import DEFAULT_EVENT_SOURCE, WriterThreadRef

pytestmark = pytest.mark.anyio


@dataclass
class _RecordingWriterThread:
    """Stand-in for the real WriterThread: records every call without
    touching disk or spawning a thread."""

    submitted: list[Any] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    submit_raises: BaseException | None = None
    write_event_raises: BaseException | None = None

    async def submit(self, item: Any) -> None:
        if self.submit_raises is not None:
            raise self.submit_raises
        self.submitted.append(item)

    async def write_event(
        self,
        *,
        kind: str,
        message: str,
        severity: str,
        source: str,
        t_mono_ns: int,
        t_utc: datetime,
        metadata: dict[str, Any] | None,
    ) -> None:
        if self.write_event_raises is not None:
            raise self.write_event_raises
        self.events.append(
            {
                "kind": kind,
                "message": message,
                "severity": severity,
                "source": source,
                "t_mono_ns": t_mono_ns,
                "t_utc": t_utc,
                "metadata": metadata,
            }
        )


def _snapshot() -> DeviceSnapshot:
    return DeviceSnapshot(
        adapter="test",
        device="dev",
        t_mono_ns=0,
        t_utc=datetime.now().astimezone(),
        healthy=True,
    )


class TestWriterRefProtocol:
    def test_satisfies_writer_ref_protocol(self) -> None:
        ref = WriterThreadRef(
            writer_thread=_RecordingWriterThread(),  # type: ignore[arg-type]
            clock=RunClock.now(),
        )
        assert isinstance(ref, WriterRef)


class TestSubmit:
    async def test_submit_forwards_emission(self) -> None:
        rec = _RecordingWriterThread()
        ref = WriterThreadRef(writer_thread=rec, clock=RunClock.now())  # type: ignore[arg-type]
        snap = _snapshot()
        await ref.submit(snap)
        assert rec.submitted == [snap]

    async def test_submit_propagates_writer_errors(self) -> None:
        rec = _RecordingWriterThread(submit_raises=RuntimeError("inbox dead"))
        ref = WriterThreadRef(writer_thread=rec, clock=RunClock.now())  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="inbox dead"):
            await ref.submit(_snapshot())


class TestWriteEvent:
    async def test_write_event_uses_run_clock_for_t_mono_ns(self) -> None:
        clock = RunClock.now()
        rec = _RecordingWriterThread()
        ref = WriterThreadRef(writer_thread=rec, clock=clock)  # type: ignore[arg-type]
        await ref.write_event(
            kind="worker_adapter_error",
            message="kaboom",
            metadata={"resource_id": "serial:COM6"},
        )
        assert len(rec.events) == 1
        ev = rec.events[0]
        # Stamp must be a real monotonic-ns reading from THIS clock.
        assert isinstance(ev["t_mono_ns"], int)
        assert ev["t_mono_ns"] >= 0
        # The clock should be the authoritative source: the recorded stamp
        # must lie between two readings of the same clock taken either side.
        before = clock.t_mono_ns()
        await ref.write_event(kind="k", message="m", metadata={})
        after = clock.t_mono_ns()
        second = rec.events[1]["t_mono_ns"]
        assert before <= second <= after

    async def test_write_event_attribution_default_is_worker(self) -> None:
        rec = _RecordingWriterThread()
        ref = WriterThreadRef(writer_thread=rec, clock=RunClock.now())  # type: ignore[arg-type]
        await ref.write_event(kind="k", message="m", metadata={})
        assert rec.events[0]["source"] == DEFAULT_EVENT_SOURCE == "worker"

    async def test_write_event_attribution_override(self) -> None:
        rec = _RecordingWriterThread()
        ref = WriterThreadRef(
            writer_thread=rec,  # type: ignore[arg-type]
            clock=RunClock.now(),
            source="conductor",
        )
        await ref.write_event(kind="k", message="m", metadata={})
        assert rec.events[0]["source"] == "conductor"

    async def test_write_event_severity_default(self) -> None:
        rec = _RecordingWriterThread()
        ref = WriterThreadRef(writer_thread=rec, clock=RunClock.now())  # type: ignore[arg-type]
        await ref.write_event(kind="k", message="m", metadata={})
        assert rec.events[0]["severity"] == "info"

    async def test_write_event_severity_override(self) -> None:
        rec = _RecordingWriterThread()
        ref = WriterThreadRef(
            writer_thread=rec,  # type: ignore[arg-type]
            clock=RunClock.now(),
            severity="error",
        )
        await ref.write_event(kind="k", message="m", metadata={})
        assert rec.events[0]["severity"] == "error"

    async def test_write_event_passes_metadata_through(self) -> None:
        rec = _RecordingWriterThread()
        ref = WriterThreadRef(writer_thread=rec, clock=RunClock.now())  # type: ignore[arg-type]
        meta = {"resource_id": "serial:COM6", "adapter": "heater"}
        await ref.write_event(kind="k", message="m", metadata=meta)
        assert rec.events[0]["metadata"] == meta

    async def test_write_event_propagates_writer_errors(self) -> None:
        rec = _RecordingWriterThread(write_event_raises=RuntimeError("sqlite locked"))
        ref = WriterThreadRef(writer_thread=rec, clock=RunClock.now())  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="sqlite locked"):
            await ref.write_event(kind="k", message="m", metadata={})


class TestImmutability:
    def test_is_frozen(self) -> None:
        ref = WriterThreadRef(
            writer_thread=_RecordingWriterThread(),  # type: ignore[arg-type]
            clock=RunClock.now(),
        )
        with pytest.raises((AttributeError, TypeError)):
            ref.source = "other"  # type: ignore[misc]
