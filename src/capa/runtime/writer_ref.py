""":class:`WriterRef` over a real :class:`WriterThread`.

The :class:`Conductor` builds a
:class:`~capa.storage.writer_thread.WriterThread` at run start and installs a
:class:`WriterRef` view of it into every :class:`~capa.runtime.runcontext.RunContext`.
Workers and the conductor's drain tasks call into this view rather than
holding a direct reference to the writer thread.

Two responsibilities the protocol can't carry:

1. **Timestamping events.** The :class:`WriterRef` protocol takes only
   ``kind`` / ``message`` / ``metadata``; the underlying
   :meth:`WriterThread.write_event` requires ``t_mono_ns`` and ``t_utc``.
   :class:`WriterThreadRef` synthesizes them from the run's authoritative
   :class:`~capa.core.clock.RunClock` so every event is stamped against the
   same time origin as adapter emissions.
2. **Attribution.** The writer thread tags every event with ``source``;
   workers attribute to ``"worker"``, the conductor to ``"conductor"``. A
   single :class:`WriterThreadRef` carries a fixed source so callers don't
   re-state it on every call.

Construction is cheap; the conductor builds one ref per attribution-source.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from capa.core.clock import RunClock
    from capa.devices.camera.base import FrameReceipt
    from capa.devices.records import DeviceEmission
    from capa.storage.writer_thread import WriterThread


DEFAULT_EVENT_SOURCE: Final[str] = "worker"
"""Source attribution for events recorded through a WriterRef installed in a
worker's RunContext. The conductor builds its own ref with
``source="conductor"`` for events it records directly."""


@dataclass(frozen=True, slots=True)
class WriterThreadRef:
    """Production :class:`WriterRef` impl backed by a :class:`WriterThread`.

    Frozen because every field is fixed for the run's duration; this also
    makes it safe to share the same instance across threads without locks
    (the underlying :class:`WriterThread` is thread-safe by design).

    :param writer_thread: The per-run writer thread the conductor started.
    :param clock: Run-authoritative monotonic clock — used to stamp events.
    :param source: Attribution string copied verbatim into every event's
        ``source`` field. Conductor convention: ``"worker"`` for worker
        contexts, ``"conductor"`` for the conductor's own use.
    :param severity: Default severity for events recorded through this ref.
        The protocol doesn't expose severity, but most worker-recorded
        events are informational (``"info"``) and adapter-error events
        re-record themselves at ``"error"`` via a separate path if needed.
    """

    writer_thread: WriterThread
    clock: RunClock
    source: str = DEFAULT_EVENT_SOURCE
    severity: str = "info"

    async def submit(self, emission: DeviceEmission) -> None:
        """Forward to :meth:`WriterThread.submit`.

        Backpressure semantics are inherited: if the inbox is full the
        caller awaits inside ``anyio.to_thread.run_sync(submit_blocking)``
        until space frees. Sustained block surfaces as a saturation-monitor
        escalation in the conductor.
        """
        await self.writer_thread.submit(emission)

    async def write_event(
        self,
        *,
        kind: str,
        message: str,
        metadata: dict[str, Any],
    ) -> None:
        """Stamp the event with run-clock time, attribute it to ``source``,
        and submit through the writer thread's event path.

        The protocol's ``metadata`` argument is forwarded as-is; the writer
        thread accepts ``None`` but the protocol guarantees a dict, so we
        pass it directly.
        """
        await self.writer_thread.write_event(
            kind=kind,
            message=message,
            severity=self.severity,
            source=self.source,
            t_mono_ns=self.clock.t_mono_ns(),
            t_utc=datetime.now(UTC),
            metadata=metadata,
        )

    async def record_frame(self, receipt: FrameReceipt) -> None:
        """Forward a frame receipt to the writer thread's frame inbox.

        The conductor's drain task dispatches by emission type —
        :class:`FrameReceipt` lands here, everything else
        on :class:`~capa.devices.records.DeviceEmission` lands on
        :meth:`submit`. The writer thread internally wraps the receipt
        in a :class:`~capa.storage.writer_thread.FrameItem`.
        """
        await self.writer_thread.record_frame(receipt)

    async def write_camera_event(
        self,
        *,
        kind: str,
        message: str,
        severity: str,
        source: str,
        t_mono_ns: int,
        t_utc: datetime,
        metadata: dict[str, Any],
    ) -> None:
        """Forward a camera-originated event with full attribution.

        Unlike :meth:`write_event` (which stamps with the ref's clock and
        attributes to the ref's source), camera events carry their own
        :attr:`CameraEvent.t_mono_ns` / :attr:`t_utc` (captured in the
        camera adapter at event time) and their own ``severity`` /
        ``camera:<name>`` source string.
        """
        await self.writer_thread.write_event(
            kind=kind,
            message=message,
            severity=severity,
            source=source,
            t_mono_ns=t_mono_ns,
            t_utc=t_utc,
            metadata=metadata,
        )


__all__ = [
    "DEFAULT_EVENT_SOURCE",
    "WriterThreadRef",
]
