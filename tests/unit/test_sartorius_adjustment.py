"""The balance's internal calibration, end to end through sartoriuslib.

Drives :class:`SartoriusAdapter` against a real :class:`sartoriuslib.Balance`
on a :class:`FakeTransport` scripted by :class:`AdjustmentSimulator`, which
replays the timeline measured on the rig's MSE1203S (off-scale readings,
then a fake stable 1300 g, then 0 g, ~24.4 s in all) at a chosen speed.

Covers the ``internal_adjust`` command waiting for the balance, each way a
calibration can end, ``abort_adjustment``, detaching on ``close``, and the
stream withholding mass samples while the balance adjusts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import anyio
import pytest
from sartoriuslib import Balance, open_device
from sartoriuslib.protocol.base import ProtocolKind
from sartoriuslib.protocol.xbpi import build_command
from sartoriuslib.testing import (
    AdjustmentSimulator,
    FakeTransport,
    build_identify_script,
    build_metrology_script,
)

from capa.channels.calibration import Identity
from capa.channels.spec import ChannelKind, ChannelSpec, SartoriusReading
from capa.core.errors import AdapterError
from capa.devices.records import ChannelSample, DeviceEvent, SourceRecord
from capa.devices.sartorius import BalanceAdjustmentError, SartoriusAdapter
from tests._adapter_helpers import make_start_ctx

pytestmark = pytest.mark.anyio

TX_ADJUST = build_command(0x28, b"\x21\x78")
TX_ABORT = build_command(0x29)
NEW_STAMP = datetime(2026, 11, 27, 15, 56, 49)
"""The completion time :class:`AdjustmentSimulator` writes for its first run."""
NET_VALUE = 12.345


async def _adapter(
    sim: AdjustmentSimulator, **params: Any
) -> tuple[SartoriusAdapter, FakeTransport]:
    transport = FakeTransport({**build_identify_script(), **build_metrology_script()})
    sim.install(transport)

    async def factory() -> Balance:
        return await open_device(transport, protocol=ProtocolKind.XBPI, timeout=0.05)

    adapter = SartoriusAdapter(name="balance", port="fake://sim", balance_factory=factory, **params)
    adapter.configure_channels(
        [
            ChannelSpec(
                name="balance.mass",
                kind=ChannelKind.MASS,
                source=SartoriusReading(device="balance", field="value"),
                unit="g",
                derived_unit="g",
                calibration=Identity(input_unit="g", output_unit="g"),
            )
        ]
    )
    await adapter.open()
    return adapter, transport


async def _calibrate(adapter: SartoriusAdapter) -> Any:
    return await adapter.internal_adjust(issued_by="opA", confirmed_by="opA")


async def _until(condition: Any, *, timeout_s: float = 5.0) -> None:
    with anyio.fail_after(timeout_s):
        while not condition():
            await anyio.sleep(0.005)


class _Outcome:
    """What a command run in a background task returned or raised."""

    def __init__(self) -> None:
        self.result: Any = None
        self.error: BaseException | None = None
        self.done = anyio.Event()

    async def run(self, adapter: SartoriusAdapter) -> None:
        try:
            self.result = await _calibrate(adapter)
        except Exception as exc:
            self.error = exc
        finally:
            self.done.set()


class TestInternalCalibration:
    async def test_waits_for_the_balance_and_reports_the_new_record(self) -> None:
        sim = AdjustmentSimulator(speed=200.0)
        adapter, transport = await _adapter(sim)
        try:
            result = await _calibrate(adapter)
            assert result.accepted
            assert result.detail.startswith("internal calibration done in")
            assert result.detail.endswith("at 26.9 °C")
            # The command returned after the balance finished, not at its ACK.
            assert sim.completed == 1
            assert not sim.running
            snapshot = await adapter.read_state_snapshot()
            assert snapshot is not None
            assert snapshot.cal_timestamp == NEW_STAMP
            assert snapshot.cal_temperature_c == pytest.approx(26.91, abs=1e-3)
            assert transport.writes.count(TX_ADJUST) == 1
        finally:
            await adapter.close()

    async def test_waits_for_an_isocal_already_running_instead_of_starting_one(self) -> None:
        sim = AdjustmentSimulator(speed=200.0)
        adapter, transport = await _adapter(sim)
        try:
            sim.start()  # the balance began an isoCAL on its own
            result = await _calibrate(adapter)
            assert result.accepted
            assert "already started it" in result.detail
            assert TX_ADJUST not in transport.writes
            assert sim.completed == 1
        finally:
            await adapter.close()

    async def test_cancel_aborts_and_keeps_the_previous_calibration(self) -> None:
        # 24 s of adjust in ~2.4 s real time: room to abort mid-run.
        sim = AdjustmentSimulator(speed=10.0)
        adapter, transport = await _adapter(sim)
        before = sim.cal_record_body
        outcome = _Outcome()
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(outcome.run, adapter)
                await _until(lambda: sim.running)
                abort = await adapter.abort_adjustment(issued_by="opA", confirmed_by="opA")
                assert abort.accepted
                assert abort.detail.startswith("abort sent")
                await outcome.done.wait()
            assert isinstance(outcome.error, BalanceAdjustmentError)
            assert outcome.error.outcome == "aborted"
            assert sim.aborted == 1
            assert sim.cal_record_body == before
            assert transport.writes.count(TX_ABORT) == 1
        finally:
            await adapter.close()

    async def test_abort_with_nothing_running_is_accepted(self) -> None:
        sim = AdjustmentSimulator(speed=200.0)
        adapter, _ = await _adapter(sim)
        try:
            result = await adapter.abort_adjustment(issued_by="opA", confirmed_by="opA")
            assert result.accepted
            assert result.detail == "no internal calibration was running"
        finally:
            await adapter.close()

    async def test_timeout_reports_and_leaves_the_balance_running(self) -> None:
        sim = AdjustmentSimulator(speed=200.0, outcome="hang")
        adapter, transport = await _adapter(sim, adjust_timeout_s=0.3)
        try:
            with pytest.raises(BalanceAdjustmentError) as info:
                await _calibrate(adapter)
            assert info.value.outcome == "timeout"
            assert "0.3 s" in str(info.value)
            # A timeout stops the waiting, never the balance.
            assert sim.running
            assert TX_ABORT not in transport.writes
        finally:
            await adapter.close()

    async def test_close_mid_calibration_detaches_without_aborting(self) -> None:
        sim = AdjustmentSimulator(speed=1.0)  # a real-time 24 s calibration
        adapter, transport = await _adapter(sim)
        outcome = _Outcome()
        async with anyio.create_task_group() as tg:
            tg.start_soon(outcome.run, adapter)
            await _until(lambda: sim.running)
            with anyio.fail_after(2.0):
                await adapter.close()
            await outcome.done.wait()
        assert isinstance(outcome.error, AdapterError)
        assert not isinstance(outcome.error, BalanceAdjustmentError)
        assert "stopped waiting" in str(outcome.error)
        # Nothing told the balance to stop: it finishes on its own.
        assert TX_ABORT not in transport.writes
        assert sim.running


async def _record_stream(
    adapter: SartoriusAdapter, emissions: list[Any], stopped: anyio.Event
) -> None:
    async for emission in adapter.stream():
        emissions.append(emission)
    stopped.set()


def _masses(emissions: list[Any]) -> list[float]:
    return [e.value for e in emissions if isinstance(e, ChannelSample)]


def _events(emissions: list[Any]) -> list[DeviceEvent]:
    return [e for e in emissions if isinstance(e, DeviceEvent)]


class TestStreamDuringCalibration:
    async def _stream_around(
        self, adapter: SartoriusAdapter, during: Any, *, settle_s: float = 0.2
    ) -> list[Any]:
        """Stream while ``during()`` runs, with ``settle_s`` of readings on
        each side, and return every emission."""
        emissions: list[Any] = []
        stopped = anyio.Event()
        await adapter.start(make_start_ctx())
        async with anyio.create_task_group() as tg:
            tg.start_soon(_record_stream, adapter, emissions, stopped)
            await anyio.sleep(settle_s)
            await during()
            await anyio.sleep(settle_s)
            await adapter.stop()
            await stopped.wait()
        return emissions

    async def test_withholds_mass_during_a_calibration_from_capa(self) -> None:
        # ~0.5 s of adjust: some 25 readings at 50 Hz.
        sim = AdjustmentSimulator(speed=50.0, net_value=NET_VALUE)
        adapter, _ = await _adapter(sim, rate_hz=50.0)
        try:

            async def calibrate() -> None:
                assert (await _calibrate(adapter)).accepted

            emissions = await self._stream_around(adapter, calibrate)
        finally:
            await adapter.close()
        masses = _masses(emissions)
        # Only real pan weights reached the mass channel: no fake 1300 g / 0 g.
        assert masses
        assert masses == [pytest.approx(NET_VALUE)] * len(masses)
        events = _events(emissions)
        assert [e.kind for e in events] == ["adjusting", "adjusting_ended"]
        assert events[0].metadata["started_by"] == "capa"
        assert events[1].metadata["withheld_readings"] > 5
        # The native rows are all kept, fake readings included.
        rows = [e.row for e in emissions if isinstance(e, SourceRecord)]
        assert any(row.get("value") == 1300.0 for row in rows)

    async def test_withholds_mass_during_an_isocal_the_balance_starts(self) -> None:
        sim = AdjustmentSimulator(speed=50.0, net_value=NET_VALUE)
        adapter, _ = await _adapter(sim, rate_hz=50.0)
        try:

            async def isocal() -> None:
                sim.start()  # no command in flight: only the readings say so
                await _until(lambda: sim.completed == 1)

            emissions = await self._stream_around(adapter, isocal)
        finally:
            await adapter.close()
        masses = _masses(emissions)
        assert masses
        assert masses == [pytest.approx(NET_VALUE)] * len(masses)
        events = _events(emissions)
        assert [e.kind for e in events] == ["adjusting", "adjusting_ended"]
        assert events[0].metadata["started_by"] == "balance"
        assert events[0].severity == "warning"
        assert events[1].t_mono_ns > events[0].t_mono_ns
