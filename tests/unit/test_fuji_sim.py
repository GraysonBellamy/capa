"""The simulated Fuji analyzer: record shape, parity with the real adapter,
channel samples, commands and the simulated calibration.
"""

from __future__ import annotations

from typing import Any

import fujilib
import pytest
from fujilib import ChannelId
from fujilib.sinks.base import row_columns
from fujilib.testing import DEFAULT_ZPA_BANK, MockAnalyzer, mock_transport

from capa.channels.calibration import Identity
from capa.channels.spec import ChannelKind, ChannelSpec, FujiChannel
from capa.core.errors import AdapterError
from capa.devices.adapter import Capability, DeviceCommand
from capa.devices.fuji import FujiAdapter, FujiStateSnapshot
from capa.devices.records import ChannelSample, SourceRecord
from capa.devices.registry import get_descriptor
from capa.devices.sim._signals import Constant, Step
from capa.devices.sim.fuji_sim import ADAPTER_ID, FujiSim
from tests._adapter_helpers import make_start_ctx

pytestmark = pytest.mark.anyio

CHANNELS = (ChannelId.CH1, ChannelId.CH2, ChannelId.CH3)


def _channel(name: str, channel: str, *, field: str = "value") -> ChannelSpec:
    unit = "percent" if field == "value" else "dimensionless"
    return ChannelSpec(
        name=name,
        kind=ChannelKind.GAS_CONCENTRATION,
        source=FujiChannel(device="analyzer", channel=channel, field=field),  # type: ignore[arg-type]
        unit=unit,
        derived_unit=unit,
        calibration=Identity(input_unit=unit, output_unit=unit),
    )


def _specs() -> list[ChannelSpec]:
    return [
        _channel("gas.co2", "CH1"),
        _channel("gas.o2", "CH3"),
        _channel("gas.o2_valid", "CH3", field="valid"),
    ]


def _split(emissions: list[Any]) -> tuple[list[SourceRecord], list[ChannelSample]]:
    return (
        [e for e in emissions if isinstance(e, SourceRecord)],
        [e for e in emissions if isinstance(e, ChannelSample)],
    )


def _person(kind: str, /, **payload: Any) -> DeviceCommand:
    return DeviceCommand(kind=kind, payload=payload, issued_by="op", confirmed_by="op")


def _run(kind: str, /, **payload: Any) -> DeviceCommand:
    return DeviceCommand(kind=kind, payload=payload, issued_by="op", authorization_id="run-1")


async def _started(**kwargs: Any) -> FujiSim:
    sim = FujiSim(name="analyzer", **kwargs)
    sim.configure_channels(_specs())
    await sim.open()
    await sim.start(make_start_ctx())
    return sim


class TestRecords:
    async def test_wide_row_and_channel_samples(self) -> None:
        sim = await _started(signals={"CH3": Constant(20.5), "CH1": Constant(1.25)})
        records, samples = _split(sim.tick_once())
        record = records[0]
        assert (record.adapter, record.device, record.shape) == (ADAPTER_ID, "analyzer", "wide_row")
        assert record.record_id == "fuji:analyzer:1"
        assert list(record.row) == [c.name for c in row_columns(CHANNELS)]
        assert (record.row["ch1_value"], record.row["ch3_value"]) == (1.25, 20.5)
        assert record.row["ch2_gas"] == "co"
        assert record.t_mono_ns >= 0
        by_channel = {s.channel: s for s in samples}
        assert by_channel["gas.o2"].value == 20.5
        assert (by_channel["gas.o2"].status, by_channel["gas.o2"].source_field) == (
            "ok",
            "ch3_value",
        )
        assert by_channel["gas.o2_valid"].value == 1.0
        assert sim.expected_emission_rate_hz == 1.0 * 4
        assert sim.resource_id == "sim:analyzer"

    async def test_rows_have_the_real_adapters_keys(self) -> None:
        sim = await _started()
        sim_record = _split(sim.tick_once())[0][0]

        async with mock_transport(MockAnalyzer(DEFAULT_ZPA_BANK)) as (transport, _line):

            async def factory() -> fujilib.Analyzer:
                return await fujilib.open_device(
                    transport, channel_map=sim.channel_map, timeout=0.25
                )

            real = FujiAdapter(
                name="analyzer",
                port="mock://zp",
                channel_map=sim.channel_map,
                rate_hz=5.0,
                analyzer_factory=factory,
            )
            await real.open()
            await real.start(make_start_ctx())
            real_record = None
            async for emission in real.stream():
                if isinstance(emission, SourceRecord):
                    real_record = emission
                    await real.stop()
            await real.close()
        assert real_record is not None
        # One records file and one schema per adapter id: sim and real must agree.
        assert sim_record.adapter == real_record.adapter
        assert list(sim_record.row) == list(real_record.row)
        assert {k: type(v) for k, v in sim_record.row.items() if v is not None} == {
            k: type(v) for k, v in real_record.row.items() if v is not None
        }

    async def test_hold_marks_status_and_validity(self) -> None:
        sim = await _started(hold_from_s=0.0)
        records, samples = _split(sim.tick_once())
        by_channel = {s.channel: s for s in samples}
        assert by_channel["gas.o2"].status == "hold"
        assert by_channel["gas.o2_valid"].value == 0.0
        assert records[0].row["ch3_hold"] is True

    async def test_signals_follow_run_time(self) -> None:
        sim = await _started(signals={"CH3": Step(before=20.95, after=15.0, at_s=1e9)})
        records, _samples = _split(sim.tick_once())
        assert records[0].row["ch3_value"] == 20.95

    async def test_stream_needs_start(self) -> None:
        sim = FujiSim(name="analyzer")
        with pytest.raises(AdapterError):
            _ = [e async for e in sim.stream()]
        with pytest.raises(AdapterError):
            sim.tick_once()

    async def test_stream_ticks_until_stopped(self) -> None:
        sim = await _started(tick_period_s=0.01)
        seen = 0
        async for emission in sim.stream():
            if isinstance(emission, SourceRecord):
                seen += 1
                if seen == 3:
                    await sim.stop()
        assert seen == 3
        assert (await sim.snapshot()).health == "down"
        await sim.close()

    def test_from_params_and_descriptor(self) -> None:
        sim = FujiSim.from_params(
            name="analyzer",
            channel_map={"CH1": "o2"},
            signals={"CH1": {"kind": "constant", "value": 20.0}},
            tick_period_s=0.5,
        )
        assert sim.channel_map == {"CH1": "o2"}
        assert sim.signals["CH1"](0.0) == 20.0
        assert FujiSim.from_params(name="a").channel_map == {"CH1": "co2", "CH2": "co", "CH3": "o2"}
        descriptor = get_descriptor("capa.devices.sim.fuji_sim")
        assert descriptor is not None
        assert descriptor.family == "sim"
        assert descriptor.supported_binding_sources == ("fuji_channel",)
        assert Capability.HAS_GAS_CALIBRATION in sim.capabilities


class TestCommands:
    async def test_gate(self) -> None:
        sim = await _started()
        unauthorized = await sim.command(DeviceCommand(kind="set_output_hold", issued_by="op"))
        assert not unauthorized.accepted

    async def test_settings_are_mirrored(self) -> None:
        sim = await _started()
        results = [
            await sim.command(_run("set_response_time", target="o2", seconds=5)),
            await sim.command(_run("set_response_time", target="CH1", seconds=7)),
            await sim.command(_run("set_output_hold", enabled=True)),
            await sim.command(_run("set_hold_mode", mode="setting")),
            await sim.command(_run("set_range", channel="CH3", range=2)),
            await sim.command(_run("return_to_measurement")),
        ]
        assert all(r.accepted for r in results), [r.detail for r in results]
        snap = await sim.snapshot()
        assert snap.fields["response_time_o2_s"] == 5
        assert snap.fields["response_time_ndir1_s"] == 7
        assert snap.fields["output_hold"] is True
        assert snap.fields["hold_mode"] == "setting"
        assert snap.fields["ch3_range"] == 2

    async def test_a_calibration_gas_needs_a_person(self) -> None:
        sim = await _started()
        payload = {"channel": "CH3", "range": 1, "kind": "span", "value": 20.0, "unit": "vol%"}
        by_run = await sim.command(_run("set_calibration_gas", **payload))
        by_person = await sim.command(_person("set_calibration_gas", **payload))
        assert not by_run.accepted
        assert by_person.accepted
        assert (await sim.snapshot()).fields["ch3_range1_span_gas"] == 20.0

    async def test_bad_payloads_are_refused(self) -> None:
        sim = await _started()
        missing = await sim.command(_run("set_response_time", target="o2"))
        unmapped = await sim.command(_run("set_range", channel="CH5", range=1))
        assert not missing.accepted
        assert not unmapped.accepted


class TestCalibration:
    async def test_a_zero_reaches_the_wait_step_settles_and_completes(self) -> None:
        sim = await _started(signals={"CH3": Constant(0.3)}, settle_s=0.0)
        begun = await sim.command(
            _person("calibration_begin", channel="CH3", kind="zero", gas_value=0.0)
        )
        assert begun.accepted, begun.detail
        waiting = await sim.read_state_snapshot()
        assert isinstance(waiting, FujiStateSnapshot)
        assert (waiting.calibration.state, waiting.calibration.steady) == ("waiting", True)
        assert {r.channel: r.state for r in waiting.readings}["CH3"] == "calibrating"

        committed = await sim.command(_person("calibration_commit"))
        assert committed.accepted, committed.detail
        assert sim.calibration.outcome == "completed"
        # The calibration moved the reading onto the gas.
        records, _samples = _split(sim.tick_once())
        assert records[0].row["ch3_value"] == 0.0
        assert records[0].row["ch3_state"] == "ok"

    async def test_commit_waits_for_the_gas_to_settle(self) -> None:
        sim = await _started(settle_s=1e6)
        _ = await sim.command(
            _person("calibration_begin", channel="CH3", kind="zero", gas_value=0.0)
        )
        early = await sim.command(_person("calibration_commit"))
        cancelled = await sim.command(_run("calibration_cancel"))
        again = await sim.command(_run("calibration_cancel"))
        assert not early.accepted
        assert "not steady" in early.detail
        assert cancelled.accepted
        assert sim.calibration.outcome == "cancelled"
        assert not again.accepted

    async def test_the_plan_reads_the_gas_setting_and_begins_nothing(self) -> None:
        sim = await _started()
        plan = await sim.command(_run("calibration_plan", channel="CH3", kind="span"))
        bad = await sim.command(_run("calibration_plan", channel="CH3", kind="both"))
        assert plan.accepted
        assert plan.detail == "calibration_plan: span of CH3: CH3 range 1 against 20.95 vol%"
        assert not bad.accepted
        assert sim.calibration.state == "idle"

    async def test_a_run_starting_cancels_a_calibration_left_waiting(self) -> None:
        sim = await _started(settle_s=1e6)
        await sim.stop()
        begun = await sim.command(
            _person("calibration_begin", channel="CH3", kind="zero", gas_value=0.0)
        )
        assert begun.accepted
        await sim.start(make_start_ctx())
        assert (sim.calibration.state, sim.calibration.outcome) == ("ended", "cancelled")
        records, _samples = _split(sim.tick_once())
        assert records[0].row["ch3_state"] == "ok"

    async def test_what_is_refused(self) -> None:
        sim = await _started()
        by_run = await sim.command(
            _run("calibration_begin", channel="CH3", kind="zero", gas_value=0.0)
        )
        wrong_gas = await sim.command(
            _person("calibration_begin", channel="CH3", kind="span", gas_value=5.0)
        )
        wrong_kind = await sim.command(
            _person("calibration_begin", channel="CH3", kind="both", gas_value=0.0)
        )
        no_run = await sim.command(_person("calibration_commit"))
        assert [r.accepted for r in (by_run, wrong_gas, wrong_kind, no_run)] == [False] * 4
        assert "not CH3's span-gas setting" in wrong_gas.detail
        first = await sim.command(
            _person("calibration_begin", channel="CH3", kind="zero", gas_value=0.0)
        )
        second = await sim.command(
            _person("calibration_begin", channel="CH1", kind="zero", gas_value=0.0)
        )
        assert first.accepted
        assert not second.accepted
