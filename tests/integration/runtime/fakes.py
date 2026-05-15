"""Shared test doubles for :mod:`capa.runtime` integration tests.

The fakes here implement just enough of the production protocols to drive
:class:`Worker` end-to-end against a controllable surface. They are
intentionally narrow — they don't try to be drop-in replacements for the
real adapters / writer / bundle. The point is to isolate the worker state
machine and the cancellation shield from real hardware behaviour so each
test can stage exactly one scenario.

Conventions:

* Every fake exposes an inspection surface (``call_log``, counters,
  ``triggered``-style events) so assertions can target what happened rather
  than ducking around opaque side effects.
* No fake has a thread of its own; all of them schedule purely as asyncio
  coroutines and run on whichever loop the worker's runner installs.
* Fakes are *not* exported from :mod:`capa.runtime` — they live with the
  tests so a production code path can never depend on them by accident.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from capa.core.clock import RunClock
from capa.devices.adapter import (
    AdapterLifecycle,
    Capability,
    CommandResult,
    DeviceCommand,
)
from capa.devices.camera.base import FrameReceipt
from capa.devices.records import DeviceEmission, DeviceSnapshot
from capa.runtime.runcontext import BundleRef, RunContext, WriterRef

# ---------------------------------------------------------------------------
# Writer / bundle stubs (satisfy WriterRef / BundleRef protocols).
# ---------------------------------------------------------------------------


@dataclass
class FakeBundleRef:
    """Bare BundleRef. Carries a stable root tag for assertions."""

    _root: object = "fake-bundle-root"

    @property
    def root(self) -> object:
        return self._root


@dataclass
class FakeWriterRef:
    """In-memory WriterRef.

    ``submitted`` collects every successful :meth:`submit` payload in
    arrival order. ``events`` collects every :meth:`write_event` payload.
    Tests assert against these directly.

    Set ``submit_raises`` / ``write_event_raises`` to inject failures
    (e.g. simulate a writer-thread inbox slowing or refusing).
    """

    submitted: list[DeviceEmission] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    frames: list[FrameReceipt] = field(default_factory=list)
    camera_events: list[dict[str, Any]] = field(default_factory=list)
    submit_raises: BaseException | None = None
    write_event_raises: BaseException | None = None
    record_frame_raises: BaseException | None = None
    write_camera_event_raises: BaseException | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    async def submit(self, emission: DeviceEmission) -> None:
        if self.submit_raises is not None:
            raise self.submit_raises
        with self._lock:
            self.submitted.append(emission)

    async def write_event(self, *, kind: str, message: str, metadata: dict[str, Any]) -> None:
        if self.write_event_raises is not None:
            raise self.write_event_raises
        with self._lock:
            self.events.append({"kind": kind, "message": message, "metadata": metadata})

    async def record_frame(self, receipt: FrameReceipt) -> None:
        if self.record_frame_raises is not None:
            raise self.record_frame_raises
        with self._lock:
            self.frames.append(receipt)

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
        if self.write_camera_event_raises is not None:
            raise self.write_camera_event_raises
        with self._lock:
            self.camera_events.append(
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


def make_run_context(
    *,
    run_id: str = "test-run-0001",
    writer: FakeWriterRef | None = None,
    bundle: FakeBundleRef | None = None,
    clock: RunClock | None = None,
) -> RunContext:
    """Build a :class:`RunContext` against test fakes."""
    return RunContext(
        run_id=run_id,
        clock=clock or RunClock.now(),
        writer=writer or FakeWriterRef(),
        bundle=bundle or FakeBundleRef(),
    )


# Verify the protocols (best-effort runtime check used by tests).
assert isinstance(FakeWriterRef(), WriterRef)
assert isinstance(FakeBundleRef(), BundleRef)


# ---------------------------------------------------------------------------
# Fake adapter — fine-grained control over open/start/stream/command.
# ---------------------------------------------------------------------------


@dataclass
class FakeAdapter:
    """Minimal :class:`DeviceAdapter` impl for worker tests.

    Each lifecycle method records a call in ``call_log`` (timestamped to
    monotonic ns so test assertions can verify ordering across adapters).
    Streaming yields a configurable sequence of :class:`DeviceSnapshot`
    emissions, one per :attr:`tick_period_s` interval, until :meth:`stop`
    flips the lifecycle or the requested ``emit_limit`` is reached.

    The ``command`` method honours ``command_delay_s`` and
    ``command_raises`` for cancellation-shield testing — a slow command
    that completes despite caller cancellation is the canonical scenario.
    """

    name: str
    resource_id: str = "sim:fake"
    capabilities: frozenset[Capability] = field(
        default_factory=lambda: frozenset({Capability.HAS_SETPOINT})
    )

    # Stream behaviour
    tick_period_s: float = 0.01
    emit_limit: int | None = None  # None means stream until stop()
    stream_raises: BaseException | None = None  # raise after N=raise_after emits
    raise_after: int = 0  # only meaningful if stream_raises set

    # Command behaviour
    command_delay_s: float = 0.0
    command_raises: BaseException | None = None
    command_results: deque[CommandResult] | None = None  # cycled per call
    # Records every `command(cmd)` payload — used by shield tests to assert
    # the worker-side coroutine completed even after caller cancellation.
    commands_completed: list[DeviceCommand] = field(default_factory=list)

    # Snapshot / health
    healthy: bool = True

    # Lifecycle + counters
    _lifecycle: AdapterLifecycle = field(default_factory=AdapterLifecycle)
    call_log: list[tuple[str, int]] = field(default_factory=list)  # (verb, t_mono_ns)
    open_calls: int = 0
    close_calls: int = 0
    start_calls: int = 0
    stop_calls: int = 0
    stream_calls: int = 0
    _seq: int = 0
    _open_raises: BaseException | None = None
    _start_raises: BaseException | None = None
    _stop_raises: BaseException | None = None
    _clock: RunClock | None = None

    expected_emission_rate_hz: float = 100.0  # for queue sizing if asked

    # ----- adapter Protocol -----

    async def open(self) -> None:
        import time as _t

        self.call_log.append(("open", _t.monotonic_ns()))
        self.open_calls += 1
        if self._open_raises is not None:
            raise self._open_raises
        self._lifecycle.open()

    async def close(self) -> None:
        import time as _t

        self.call_log.append(("close", _t.monotonic_ns()))
        self.close_calls += 1
        self._lifecycle.close()

    async def start(self, clock: RunClock | None = None) -> None:
        import time as _t

        self.call_log.append(("start", _t.monotonic_ns()))
        self.start_calls += 1
        self._clock = clock
        if self._start_raises is not None:
            raise self._start_raises
        self._lifecycle.start()

    async def stop(self) -> None:
        import time as _t

        self.call_log.append(("stop", _t.monotonic_ns()))
        self.stop_calls += 1
        if self._stop_raises is not None:
            raise self._stop_raises
        self._lifecycle.stop()

    async def stream(self) -> AsyncIterator[DeviceEmission]:
        import time as _t

        self.call_log.append(("stream_enter", _t.monotonic_ns()))
        self.stream_calls += 1
        try:
            emitted = 0
            while self._lifecycle.state == "running":
                if self.emit_limit is not None and emitted >= self.emit_limit:
                    return
                if self.stream_raises is not None and emitted >= self.raise_after:
                    raise self.stream_raises
                self._seq += 1
                yield self._make_snapshot()
                emitted += 1
                await asyncio.sleep(self.tick_period_s)
        finally:
            self.call_log.append(("stream_exit", _t.monotonic_ns()))

    async def snapshot(self) -> DeviceEmission:
        return self._make_snapshot()

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        if self.command_delay_s > 0:
            await asyncio.sleep(self.command_delay_s)
        if self.command_raises is not None:
            raise self.command_raises
        # Record AFTER the delay so cancel-during-delay tests can verify
        # the coroutine actually completed despite caller cancellation.
        self.commands_completed.append(cmd)
        if self.command_results is not None and self.command_results:
            return self.command_results.popleft()
        return CommandResult(
            accepted=True,
            detail=f"fake ack {cmd.kind}",
            t_mono_ns=0,
            t_utc=datetime.now(UTC),
        )

    # ----- helpers -----

    def _make_snapshot(self) -> DeviceSnapshot:
        clock = self._clock or RunClock.now()
        return DeviceSnapshot(
            adapter="fake",
            device=self.name,
            t_mono_ns=clock.t_mono_ns(),
            t_utc=datetime.now(UTC),
            healthy=self.healthy,
            fields={"seq": self._seq, "state": self._lifecycle.state},
        )


def fake_command(**overrides: Any) -> DeviceCommand:
    """Build a minimal DeviceCommand for tests.

    Defaults to an authorized-by-arm command so the adapter's auth check
    (when present) accepts it.
    """
    base = dict(
        kind="set_setpoint",
        target="setpoint:1",
        payload={"value": 100.0},
        issued_by="test",
        authorization_id="auth-test",
        confirmed_by=None,
    )
    base.update(overrides)
    return DeviceCommand(**base)


# ---------------------------------------------------------------------------
# Convenience adapters that exercise specific edge cases.
# ---------------------------------------------------------------------------


def make_fake_adapter(name: str = "fake", **kwargs: Any) -> FakeAdapter:
    """Build a default-configured FakeAdapter."""
    return FakeAdapter(name=name, **kwargs)


def make_open_failing_adapter(name: str = "fail-open") -> FakeAdapter:
    """Adapter whose :meth:`open` raises — used for pool-rollback tests."""
    a = FakeAdapter(name=name)
    a._open_raises = RuntimeError(f"{name}: cannot open")
    return a


def make_stuck_adapter(name: str = "stuck", grace_buster_s: float = 30.0) -> FakeAdapter:
    """Adapter whose stream() doesn't honour stop() promptly.

    The lifecycle goes "open"→"running"→"open" on stop(), but stream's
    loop guard re-checks state *after* a long sleep — so a worker disarm
    with a small grace will see the stream task still running on grace
    expiry and produce DisarmResult.FORCED.
    """
    a = FakeAdapter(name=name, tick_period_s=grace_buster_s)
    return a


@dataclass
class HangingCloseAdapter:
    """Adapter whose :meth:`close` hangs forever.

    Used by shutdown-bounds tests to verify that
    :meth:`Worker._close_all_impl` wraps each ``adapter.close()`` in
    ``asyncio.wait_for`` and surfaces the timeout as a result-error
    rather than wedging the worker thread.
    """

    name: str
    resource_id: str = "sim:hanging-close"
    capabilities: frozenset[Capability] = field(
        default_factory=lambda: frozenset({Capability.HAS_SETPOINT})
    )
    open_calls: int = 0
    close_calls: int = 0
    start_calls: int = 0
    stop_calls: int = 0
    expected_emission_rate_hz: float = 100.0
    _lifecycle: AdapterLifecycle = field(default_factory=AdapterLifecycle)

    async def open(self) -> None:
        self.open_calls += 1
        self._lifecycle.open()

    async def close(self) -> None:
        self.close_calls += 1
        # Hang until cancelled (or the deadline fires above us).
        await asyncio.Event().wait()

    async def start(self, clock: RunClock | None = None) -> None:
        self.start_calls += 1
        self._lifecycle.start()

    async def stop(self) -> None:
        self.stop_calls += 1
        self._lifecycle.stop()

    async def stream(self) -> AsyncIterator[DeviceEmission]:
        if False:  # pragma: no cover - never iterated; satisfies type checker
            yield  # type: ignore[unreachable]

    async def snapshot(self) -> DeviceEmission:
        return DeviceSnapshot(
            adapter="hanging-close",
            device=self.name,
            t_mono_ns=0,
            t_utc=datetime.now(UTC),
            healthy=True,
            fields={},
        )

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        return CommandResult(
            accepted=True, detail="hanging-close ack", t_mono_ns=0, t_utc=datetime.now(UTC)
        )


@dataclass
class HangingStopAdapter:
    """Adapter whose :meth:`stop` hangs forever.

    Used by disarm-bounds tests for the ``adapter_stop_grace_s`` wrap-
    in-``asyncio.wait_for`` behavior. The stream yields one snapshot
    immediately so SAMPLING is reachable, then awaits a future that the
    test never resolves so the stream task is still pending when disarm
    runs.
    """

    name: str
    resource_id: str = "sim:hanging-stop"
    capabilities: frozenset[Capability] = field(
        default_factory=lambda: frozenset({Capability.HAS_SETPOINT})
    )
    open_calls: int = 0
    close_calls: int = 0
    start_calls: int = 0
    stop_calls: int = 0
    expected_emission_rate_hz: float = 100.0
    _lifecycle: AdapterLifecycle = field(default_factory=AdapterLifecycle)
    _stream_park: asyncio.Event | None = None

    async def open(self) -> None:
        self.open_calls += 1
        self._lifecycle.open()

    async def close(self) -> None:
        self.close_calls += 1
        self._lifecycle.close()

    async def start(self, clock: RunClock | None = None) -> None:
        self.start_calls += 1
        self._lifecycle.start()

    async def stop(self) -> None:
        self.stop_calls += 1
        # Hang past the adapter_stop_grace_s. We never flip lifecycle,
        # but the disarm event still fires so the test can validate the
        # bound landed.
        await asyncio.Event().wait()

    async def stream(self) -> AsyncIterator[DeviceEmission]:
        # Yield one snapshot so SAMPLING is reachable and the stream
        # task isn't immediately done, then park.
        yield DeviceSnapshot(
            adapter="hanging-stop",
            device=self.name,
            t_mono_ns=0,
            t_utc=datetime.now(UTC),
            healthy=True,
            fields={},
        )
        self._stream_park = asyncio.Event()
        await self._stream_park.wait()

    async def snapshot(self) -> DeviceEmission:
        return DeviceSnapshot(
            adapter="hanging-stop",
            device=self.name,
            t_mono_ns=0,
            t_utc=datetime.now(UTC),
            healthy=True,
            fields={},
        )

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        return CommandResult(
            accepted=True, detail="hanging-stop ack", t_mono_ns=0, t_utc=datetime.now(UTC)
        )


@dataclass
class CancelIgnoringStreamAdapter:
    """Adapter whose stream() catches :class:`asyncio.CancelledError`.

    Used by the secondary-bounded-gather test. The stream loops on a
    short sleep; when cancelled, the ``except`` clause re-enters a long
    sleep that itself ignores cancellation (the canonical "vendor
    finally block taking forever" pattern). The forced-cancel grace
    ``stream_cancel_grace_s`` bound is what keeps disarm from wedging.
    """

    name: str
    resource_id: str = "sim:cancel-ignorer"
    capabilities: frozenset[Capability] = field(
        default_factory=lambda: frozenset({Capability.HAS_SETPOINT})
    )
    open_calls: int = 0
    close_calls: int = 0
    start_calls: int = 0
    stop_calls: int = 0
    expected_emission_rate_hz: float = 100.0
    _lifecycle: AdapterLifecycle = field(default_factory=AdapterLifecycle)
    cancel_swallow_s: float = 10.0
    """How long the finally block hangs after CancelledError. Picked
    well above the test's ``stream_cancel_grace_s`` so the secondary
    bound is what unwedges disarm."""

    async def open(self) -> None:
        self.open_calls += 1
        self._lifecycle.open()

    async def close(self) -> None:
        self.close_calls += 1
        self._lifecycle.close()

    async def start(self, clock: RunClock | None = None) -> None:
        self.start_calls += 1
        self._lifecycle.start()

    async def stop(self) -> None:
        # Don't actually flip lifecycle — we want the stream task to
        # still be running when disarm's cooperative stop grace fires so
        # forced-cancel cancellation is exercised.
        self.stop_calls += 1

    async def stream(self) -> AsyncIterator[DeviceEmission]:
        try:
            while True:
                yield DeviceSnapshot(
                    adapter="cancel-ignorer",
                    device=self.name,
                    t_mono_ns=0,
                    t_utc=datetime.now(UTC),
                    healthy=True,
                    fields={},
                )
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            # The canonical "swallowed cancel" — vendor finally block
            # that takes much longer than expected. Sleep with shielding
            # so a second cancel still doesn't unwedge it. The forced-cancel
            # secondary bound is what saves us.
            await asyncio.shield(asyncio.sleep(self.cancel_swallow_s))
            raise

    async def snapshot(self) -> DeviceEmission:
        return DeviceSnapshot(
            adapter="cancel-ignorer",
            device=self.name,
            t_mono_ns=0,
            t_utc=datetime.now(UTC),
            healthy=True,
            fields={},
        )

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        return CommandResult(
            accepted=True, detail="cancel-ignorer ack", t_mono_ns=0, t_utc=datetime.now(UTC)
        )


def make_hanging_close_adapter(name: str = "hangs-close") -> HangingCloseAdapter:
    return HangingCloseAdapter(name=name)


def make_hanging_stop_adapter(name: str = "hangs-stop") -> HangingStopAdapter:
    return HangingStopAdapter(name=name)


def make_cancel_ignoring_adapter(
    name: str = "ignores-cancel", *, cancel_swallow_s: float = 10.0
) -> CancelIgnoringStreamAdapter:
    return CancelIgnoringStreamAdapter(name=name, cancel_swallow_s=cancel_swallow_s)


def collect_emissions_from_bridge(bridge: Any, *, max_count: int = 100) -> Sequence[DeviceEmission]:
    """Synchronous-side helper to drain a bridge to a list.

    Used by tests where the bridge consumer side is this thread's loop.
    Returns once either ``max_count`` is reached or the bridge yields its
    end-of-stream sentinel.
    """

    async def _drain() -> list[DeviceEmission]:
        out: list[DeviceEmission] = []
        while len(out) < max_count:
            item = await bridge.get()
            if item is None:
                break
            out.append(item)
        return out

    return asyncio.get_event_loop().run_until_complete(_drain())  # pragma: no cover


__all__ = [
    "FakeAdapter",
    "FakeBundleRef",
    "FakeWriterRef",
    "collect_emissions_from_bridge",
    "fake_command",
    "make_fake_adapter",
    "make_open_failing_adapter",
    "make_run_context",
    "make_stuck_adapter",
]
