"""Tests for :class:`DataBus` loop-affinity (migration doc §3.10 / §3.11 #7).

Each :class:`DataBus` is pinned to exactly one asyncio loop. Its subscription
queues are :class:`BoundedQueue`s that bind to whatever loop creates them;
publishing from a different loop's task corrupts those queues silently.

Phase 2 introduces a runtime assertion: every :meth:`publish` /
:meth:`publish_nowait` call asserts the running loop matches the bus's
owning loop. The first call captures the owning loop; :meth:`bind_loop`
lets the conductor pin eagerly so a misconfigured subscriber fails at bind
time rather than first publish.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime

import pytest

from capa.core.databus import DataBus, DataBusLoopError
from capa.devices.records import ChannelSample, DeviceEvent


def _sample(value: float = 1.0) -> ChannelSample:
    return ChannelSample(channel="ch", t_mono_ns=1, t_mono_s=1e-9, value=value, unit="V")


def _event() -> DeviceEvent:
    return DeviceEvent(
        adapter="a",
        device="d",
        t_mono_ns=1,
        t_utc=datetime.now(UTC),
        kind="info",
        message="hi",
    )


class TestLazyBinding:
    @pytest.mark.anyio
    async def test_first_publish_captures_owning_loop(self) -> None:
        bus = DataBus()
        assert bus.owning_loop is None
        await bus.publish(_sample())
        assert bus.owning_loop is asyncio.get_running_loop()

    @pytest.mark.anyio
    async def test_first_publish_nowait_captures_owning_loop(self) -> None:
        bus = DataBus()
        bus.publish_nowait(_sample())
        assert bus.owning_loop is asyncio.get_running_loop()

    @pytest.mark.anyio
    async def test_subsequent_publishes_on_same_loop_pass(self) -> None:
        bus = DataBus()
        sub = bus.subscribe_all("s")
        for _ in range(5):
            await bus.publish(_sample())
        assert sub.queue.depth >= 1


class TestExplicitBinding:
    @pytest.mark.anyio
    async def test_bind_loop_pins_eagerly(self) -> None:
        loop = asyncio.get_running_loop()
        bus = DataBus()
        bus.bind_loop(loop)
        assert bus.owning_loop is loop
        # Publish on the same loop succeeds.
        await bus.publish(_sample())

    @pytest.mark.anyio
    async def test_bind_loop_same_loop_is_noop(self) -> None:
        loop = asyncio.get_running_loop()
        bus = DataBus()
        bus.bind_loop(loop)
        bus.bind_loop(loop)  # idempotent
        assert bus.owning_loop is loop

    @pytest.mark.anyio
    async def test_bind_loop_to_different_loop_raises(self) -> None:
        loop = asyncio.get_running_loop()
        other = asyncio.new_event_loop()
        try:
            bus = DataBus()
            bus.bind_loop(loop)
            with pytest.raises(DataBusLoopError, match="already bound"):
                bus.bind_loop(other)
        finally:
            other.close()


class TestWrongLoopRejection:
    @pytest.mark.anyio
    async def test_publish_from_different_loop_raises(self) -> None:
        """The bus is bound on the main test loop; spawning a sibling
        thread with its own loop and publishing into the same bus from
        there must raise :class:`DataBusLoopError`."""
        bus = DataBus()
        # Bind to the current (test) loop.
        bus.bind_loop(asyncio.get_running_loop())

        captured: dict[str, BaseException | None] = {"exc": None}

        def _other_thread() -> None:
            other = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(other)

                async def _publish() -> None:
                    try:
                        await bus.publish(_sample())
                    except BaseException as exc:
                        captured["exc"] = exc

                other.run_until_complete(_publish())
            finally:
                other.close()

        t = threading.Thread(target=_other_thread)
        t.start()
        t.join(timeout=2.0)
        assert not t.is_alive()
        assert isinstance(captured["exc"], DataBusLoopError)
        assert "wrong loop" in str(captured["exc"])

    @pytest.mark.anyio
    async def test_publish_nowait_from_different_loop_raises(self) -> None:
        bus = DataBus()
        bus.bind_loop(asyncio.get_running_loop())

        captured: dict[str, BaseException | None] = {"exc": None}

        def _other_thread() -> None:
            other = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(other)

                async def _publish() -> None:
                    try:
                        bus.publish_nowait(_event())
                    except BaseException as exc:
                        captured["exc"] = exc

                other.run_until_complete(_publish())
            finally:
                other.close()

        t = threading.Thread(target=_other_thread)
        t.start()
        t.join(timeout=2.0)
        assert isinstance(captured["exc"], DataBusLoopError)

    def test_publish_nowait_outside_any_loop_raises(self) -> None:
        """Calling ``publish_nowait`` from a non-loop thread is a violation
        even if the bus is unbound — the underlying queue mutation requires
        a running loop to be safe."""
        bus = DataBus()
        with pytest.raises(DataBusLoopError, match="no loop is currently running"):
            bus.publish_nowait(_sample())


class TestClosedBus:
    @pytest.mark.anyio
    async def test_publish_on_closed_bus_skips_check(self) -> None:
        """A closed bus is a no-op for both publish paths; the loop check
        is bypassed since there's no queue to corrupt."""
        bus = DataBus()
        bus.close()
        # Should not raise even though the bus has no owning loop yet.
        await bus.publish(_sample())
        bus.publish_nowait(_sample())
