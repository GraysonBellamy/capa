"""Conductor-specific test fakes built on top of
:mod:`tests.integration.runtime.fakes`.

The :class:`FakeRunSession` lets integration tests drive the conductor
through its full lifecycle without opening a real bundle on disk. The
production :class:`RealRunSession` plugs in the real
:class:`RunBundleWriter` + :class:`WriterThread`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from capa.core.clock import RunClock
from capa.runtime.conductor import RunOutcome
from capa.runtime.runcontext import RunContext
from capa.runtime.saturation import WriterSaturationSource
from tests.integration.runtime.fakes import FakeBundleRef, FakeWriterRef


@dataclass
class FakeWriterSaturation:
    """In-memory writer-saturation signal.

    Tests advance ``last_accept_monotonic_ns`` directly to simulate either
    a healthy drain or a wedged write. ``depth`` and the timestamp are
    independent levers so a test can isolate either trip condition.
    """

    last_accept_monotonic_ns: int = 0
    depth: int = 0


@dataclass
class FakeRunSession:
    """In-memory :class:`RunSession` for conductor integration tests.

    Counts ``open`` / ``close`` calls and records the eventual outcome so
    tests can assert that the conductor closed the session with the right
    label. The :class:`FakeWriterRef` aggregates all submissions and
    events, making it trivial to assert what reached the writer.

    Construction is fully sync — :meth:`open` just bundles the prebuilt
    pieces into a :class:`RunContext`. Tests that want a session that
    fails at open pass ``open_raises``.
    """

    run_id: str = "test-run-0001"
    bundle_path: Path | None = Path("/tmp/test-bundle")
    clock: RunClock = field(default_factory=RunClock.now)
    writer_ref: FakeWriterRef = field(default_factory=FakeWriterRef)
    bundle_ref: FakeBundleRef = field(default_factory=FakeBundleRef)
    saturation_source: WriterSaturationSource | None = None
    open_raises: BaseException | None = None
    close_raises: BaseException | None = None

    open_calls: int = 0
    close_calls: int = 0
    outcome: RunOutcome | None = None
    exit_reason: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    async def open(self) -> RunContext:
        with self._lock:
            self.open_calls += 1
        if self.open_raises is not None:
            raise self.open_raises
        return RunContext(
            run_id=self.run_id,
            clock=self.clock,
            writer=self.writer_ref,
            bundle=self.bundle_ref,
        )

    def set_outcome(self, outcome: RunOutcome, exit_reason: str | None) -> None:
        with self._lock:
            self.outcome = outcome
            self.exit_reason = exit_reason

    async def close(self) -> None:
        with self._lock:
            self.close_calls += 1
        if self.close_raises is not None:
            raise self.close_raises


def make_fake_session(**kwargs: Any) -> FakeRunSession:
    """Build a default-configured :class:`FakeRunSession`."""
    return FakeRunSession(**kwargs)


__all__ = [
    "FakeRunSession",
    "FakeWriterSaturation",
    "make_fake_session",
]
