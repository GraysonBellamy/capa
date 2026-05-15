""":class:`RunContext` — the per-run state installed into every worker on arm.

Each :class:`Conductor` builds one :class:`RunContext` at run start and
passes the same instance to every worker via :meth:`Worker.arm`. The
context is **immutable** after construction — workers read fields off it
but never mutate.

Why a frozen dataclass over passing four arguments to ``arm()``:

1. "The run context" is a single conceptual object — bundling reads
   naturally.
2. The :class:`Conductor` passes the same context to several subsystems
   (workers, drain tasks, procedure runner, watchdog). Bundling means
   no plumbing churn when adding a new consumer.
3. Tests construct a context once and re-use it across multiple
   arm/disarm cycles in the same run, which mirrors how real runs work.

Tests use the in-package fakes from
``tests/integration/runtime/fakes.py``; production wires real
:class:`WriterRef` / :class:`BundleRef` implementations via
:class:`Conductor`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from datetime import datetime

    from capa.core.clock import RunClock
    from capa.devices.camera.base import FrameReceipt
    from capa.devices.records import DeviceEmission


@runtime_checkable
class WriterRef(Protocol):
    """Sync facade over the per-run writer thread.

    The real :class:`Conductor` supplies a wrapper that hands emissions
    to :class:`~capa.storage.writer_thread.WriterThread`'s inbox; tests
    supply a fake that records calls into a list.

    Methods are async because the writer-thread inbox is bounded — the caller
    awaits when the inbox is full. That backpressure is the canonical
    saturation trigger.
    """

    async def submit(self, emission: DeviceEmission) -> None:
        """Hand one emission to the writer inbox; await if full."""
        ...

    async def write_event(self, *, kind: str, message: str, metadata: dict[str, Any]) -> None:
        """Record a structured event into ``events.sqlite``.

        Used by the worker to record adapter errors and hard-stop attempts.
        Severity defaults to the ref's configured value (``"info"`` for
        workers); source attribution is the ref's configured source.
        """
        ...

    async def record_frame(self, receipt: FrameReceipt) -> None:
        """Hand one :class:`FrameReceipt` to the frame-index sink.

        Used by the conductor's drain task when an emission is a frame
        receipt. The writer thread wraps it in a :class:`FrameItem`
        internally; callers pass the receipt unwrapped.
        """
        ...

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
        """Record a camera-originated event into ``events.sqlite``.

        Distinct from :meth:`write_event` because camera events carry
        their own timestamps and severity (from the
        :class:`~capa.devices.camera.base.CameraEvent`), and need a
        ``camera:<name>`` source attribution rather than the ref's default.
        Mirrors the camera_task drain semantics in
        ``cameras.py:_drain_events`` so bundle parity holds.
        """
        ...


@runtime_checkable
class BundleRef(Protocol):
    """Opaque handle to the on-disk run bundle.

    The worker doesn't directly write files — that's the writer thread's job
    — but it sometimes needs to know the bundle root (e.g. to emit a path
    into an event). Tests carry a stub; production wires the real
    :class:`~capa.storage.bundle.RunBundleWriter`'s root path through here.
    """

    @property
    def root(self) -> object:
        """Path-like root of the run bundle. Read-only.

        Typed as ``object`` rather than ``pathlib.Path`` so test fakes can
        return ``None`` without lying about the type.
        """
        ...


@dataclass(frozen=True, slots=True)
class RunContext:
    """The per-run state installed into workers via :meth:`Worker.arm`.

    Frozen because every field is logically a constant for the run's
    duration; making mutation impossible at the type level removes a class
    of bug ("which thread last wrote the writer ref?").

    The :class:`RunClock` is the single timestamp authority for the run.
    Workers stamp ``t_bridge_put_ns`` against this clock immediately
    before crossing the outbound bridge; the consumer side reads
    ``time.monotonic_ns()`` and the difference is observed as bridge
    latency.
    """

    run_id: str
    """Stable run identifier. Matches the bundle directory name."""

    clock: RunClock
    """Single :class:`~capa.core.clock.RunClock` instance for this run.
    Used by workers to stamp emission monotonic offsets before bridge put.
    Constructed by the :class:`Conductor` at run-arm; never reused across
    runs (a new run gets a new clock and a new monotonic zero)."""

    writer: WriterRef
    """Sync facade over the writer thread. Workers submit emissions and
    record adapter-side events here. Tests pass a fake; production wires
    the real :class:`~capa.storage.writer_thread.WriterThread`."""

    bundle: BundleRef
    """Opaque handle to the run bundle. Carried for symmetry with how the
    writer thread receives it today — workers read it only to include the
    root path in diagnostic events."""


__all__ = [
    "BundleRef",
    "RunContext",
    "WriterRef",
]
