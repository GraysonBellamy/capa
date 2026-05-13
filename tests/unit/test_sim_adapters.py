"""Sim adapter behaviour: SourceRecord shape, ChannelSample derivation,
lifecycle, and command authorization gates.
"""

from __future__ import annotations

from typing import Any

import pytest

from capa.channels.calibration import Identity, LinearTwoPoint, UncertaintySpec
from capa.channels.spec import (
    AlicatFrameField,
    ChannelKind,
    ChannelSpec,
    NIDAQReadingField,
    SartoriusReading,
    WatlowParameter,
)
from capa.devices.adapter import DeviceCommand
from capa.devices.records import ChannelSample, SourceRecord
from capa.devices.sim._signals import Constant, Ramp, Sine
from capa.devices.sim.alicat_sim import AlicatSim
from capa.devices.sim.nidaq_block_sim import NIDAQBlockSim
from capa.devices.sim.nidaq_polled_sim import NIDAQPolledSim
from capa.devices.sim.sartorius_sim import SartoriusSim
from capa.devices.sim.watlow_sim import WatlowSim

pytestmark = pytest.mark.anyio


def _split(emissions: list[Any]) -> tuple[list[SourceRecord], list[ChannelSample]]:
    return (
        [e for e in emissions if isinstance(e, SourceRecord)],
        [e for e in emissions if isinstance(e, ChannelSample)],
    )


class TestWatlowSim:
    def _make(self) -> tuple[WatlowSim, list[ChannelSpec]]:
        sim = WatlowSim(
            name="heater",
            signals={
                ("process_value", 1): Constant(400.0),
                ("setpoint", 1): Constant(400.0),
            },
            parameter_units={"process_value": "degC", "setpoint": "degC"},
        )
        channels = [
            ChannelSpec(
                name="heater.pv",
                kind=ChannelKind.PROCESS_VAR,
                source=WatlowParameter(device="heater", parameter="process_value", instance=1),
                unit="degC",
                derived_unit="degC",
                calibration=Identity(input_unit="degC", output_unit="degC"),
            ),
            ChannelSpec(
                name="heater.setpoint",
                kind=ChannelKind.SETPOINT,
                source=WatlowParameter(device="heater", parameter="setpoint", instance=1),
                unit="degC",
                derived_unit="degC",
                calibration=Identity(input_unit="degC", output_unit="degC"),
            ),
        ]
        sim.configure_channels(channels)
        return sim, channels

    async def test_native_row_layout(self) -> None:
        sim, _ = self._make()
        await sim.open()
        await sim.start()
        records, samples = _split(sim.tick_once())
        # one record + one sample per (parameter, instance)
        assert len(records) == 2
        assert len(samples) == 2
        for r in records:
            # watlowlib.sample_to_row layout
            assert {
                "device",
                "address",
                "protocol",
                "parameter",
                "parameter_id",
                "instance",
                "value",
                "unit",
                "requested_at",
                "received_at",
                "midpoint_at",
                "latency_s",
            } <= set(r.row.keys())
            assert r.shape == "long_row"
            assert r.adapter == "watlow"

    async def test_channel_samples_link_to_records(self) -> None:
        sim, _channels = self._make()
        await sim.open()
        await sim.start()
        records, samples = _split(sim.tick_once())
        record_ids = {r.record_id for r in records}
        for s in samples:
            assert s.source_record_id in record_ids
            assert s.unit == "degC"
            assert s.value == 400.0

    async def test_tick_first_marks_only_first_record_of_batch(self) -> None:
        # The diagnostics dock reads ``metadata["tick_first"]`` to count
        # one poll per acquisition tick rather than one per polled
        # parameter. Two parameters per tick → first record True, second
        # record False. Without this, a 1 Hz heater with 2 params reports
        # ~thousands of Hz because per-parameter yields are sub-millisecond.
        sim, _ = self._make()
        await sim.open()
        await sim.start()
        records, _ = _split(sim.tick_once())
        assert len(records) == 2
        assert records[0].metadata["tick_first"] is True
        assert records[1].metadata["tick_first"] is False

    async def test_command_unauthorized_rejected(self) -> None:
        sim, _ = self._make()
        await sim.open()
        await sim.start()
        result = await sim.command(
            DeviceCommand(kind="set_setpoint", target="setpoint:1", issued_by="abr")
        )
        assert result.accepted is False

    async def test_command_with_authorization_accepted(self) -> None:
        sim, _ = self._make()
        await sim.open()
        await sim.start()
        result = await sim.command(
            DeviceCommand(
                kind="set_setpoint",
                target="setpoint:1",
                issued_by="abr",
                authorization_id="run-123",
            )
        )
        assert result.accepted is True

    async def test_lifecycle(self) -> None:
        sim, _ = self._make()
        # tick_once before start raises
        with pytest.raises(Exception):
            sim.tick_once()
        await sim.open()
        await sim.start()
        assert sim._lifecycle.state == "running"
        await sim.stop()
        await sim.close()


class TestAlicatSim:
    async def test_wide_row_layout(self) -> None:
        sim = AlicatSim(
            name="air_mfc",
            signals={"Mass_Flow": Constant(50.0), "Abs_Press": Constant(101.3)},
            static_fields={"Mix_Gas": "Air"},
        )
        channel = ChannelSpec(
            name="MFC_air.flow",
            kind=ChannelKind.MFC_FLOW,
            source=AlicatFrameField(device="air_mfc", field="Mass_Flow"),
            unit="slpm",
            derived_unit="slpm",
            calibration=Identity(input_unit="slpm", output_unit="slpm"),
        )
        sim.configure_channels([channel])
        await sim.open()
        await sim.start()
        records, samples = _split(sim.tick_once())
        assert len(records) == 1
        rec = records[0]
        assert rec.shape == "wide_row"
        assert rec.adapter == "alicat"
        # Underscored field names from DataFrame.as_dict
        assert "Mass_Flow" in rec.row
        assert "Abs_Press" in rec.row
        assert rec.row.get("Mix_Gas") == "Air"
        # status column always present (alicatlib semantics)
        assert "status" in rec.row
        # Sample matches binding
        assert len(samples) == 1
        assert samples[0].channel == "MFC_air.flow"
        assert samples[0].source_field == "Mass_Flow"
        assert samples[0].value == 50.0


class TestSartoriusSim:
    async def test_single_value_row(self) -> None:
        sim = SartoriusSim(
            name="balance",
            mass_signal=Ramp(start=25.0, end=20.0, duration_s=300),
            stable_after_s=0.0,
        )
        channel = ChannelSpec(
            name="balance.mass",
            kind=ChannelKind.MASS,
            source=SartoriusReading(device="balance", field="value"),
            unit="g",
            derived_unit="g",
            calibration=Identity(input_unit="g", output_unit="g"),
        )
        sim.configure_channels([channel])
        await sim.open()
        await sim.start()
        records, samples = _split(sim.tick_once())
        rec = records[0]
        assert rec.shape == "single_value_row"
        assert rec.adapter == "sartorius"
        # sartoriuslib sample_to_row layout (Reading.as_dict keys + sample-level
        # timing + error fields).
        assert {
            "device",
            "value",
            "unit",
            "stable",
            "overload",
            "underload",
            "decimals",
            "sequence",
            "protocol",
            "raw",
            "error_type",
            "error_message",
        } <= set(rec.row.keys())
        assert len(samples) == 1
        assert samples[0].source_field == "value"


class TestNIDAQPolledSim:
    async def test_wide_row_with_unit_columns(self) -> None:
        sim = NIDAQPolledSim(
            name="cdaq1",
            task="tc_task",
            signals={"TC_top_1": Constant(800.0), "TC_top_2": Constant(810.0)},
            units={"TC_top_1": "K", "TC_top_2": "K"},
        )
        channel = ChannelSpec(
            name="TC_top_1",
            kind=ChannelKind.THERMOCOUPLE,
            source=NIDAQReadingField(device="cdaq1", task="tc_task", field="TC_top_1"),
            unit="K",
            derived_unit="K",
            calibration=Identity(input_unit="K", output_unit="K"),
        )
        sim.configure_channels([channel])
        await sim.open()
        await sim.start()
        records, samples = _split(sim.tick_once())
        rec = records[0]
        assert rec.adapter == "nidaq_polled"
        assert rec.shape == "wide_row"
        # nidaqlib reading_to_row: one column per channel + <ch>_unit columns
        assert "TC_top_1" in rec.row
        assert "TC_top_1_unit" in rec.row
        assert "TC_top_2" in rec.row
        assert "TC_top_2_unit" in rec.row
        assert rec.row["TC_top_1_unit"] == "K"
        assert len(samples) == 1
        assert samples[0].source_field == "TC_top_1"
        assert samples[0].value == 800.0

    async def test_calibration_applied(self) -> None:
        # Channel with a non-identity calibration: 0..10 mV -> 0..100 kW/m^2
        sim = NIDAQPolledSim(
            name="cdaq1",
            task="tc_task",
            signals={"AI_HFG": Constant(5.0)},
            units={"AI_HFG": "mV"},
        )
        channel = ChannelSpec(
            name="heat_flux",
            kind=ChannelKind.ANALOG_IN,
            source=NIDAQReadingField(device="cdaq1", task="tc_task", field="AI_HFG"),
            unit="mV",
            derived_unit="kW/m^2",
            keep_raw=True,
            calibration=LinearTwoPoint(
                input_unit="mV",
                output_unit="kW/m^2",
                ref_low_raw=0.0,
                ref_low_value=0.0,
                ref_high_raw=10.0,
                ref_high_value=100.0,
                uncertainty=UncertaintySpec(kind="relative", value=0.03, coverage_factor=2),
            ),
        )
        sim.configure_channels([channel])
        await sim.open()
        await sim.start()
        _, samples = _split(sim.tick_once())
        s = samples[0]
        assert s.value == pytest.approx(50.0)
        assert s.raw == 5.0  # keep_raw=True
        assert s.unit == "kW/m^2"
        # 50 * 0.03 * 2 = 3.0
        assert s.uncertainty == pytest.approx(3.0)


class TestNIDAQBlockSim:
    async def test_emits_block_record_only(self) -> None:
        sim = NIDAQBlockSim(
            name="cdaq1",
            task="ai_block",
            sample_rate_hz=10000.0,
            block_size=100,
            signals={"AI0": Sine(amplitude=1, frequency_hz=50)},
        )
        await sim.open()
        await sim.start()
        emissions = sim.tick_once()
        records, samples = _split(emissions)
        # P0a: block adapter does NOT emit per-sample ChannelSamples.
        assert len(samples) == 0
        assert len(records) == 1
        rec = records[0]
        assert rec.shape == "block"
        assert rec.row == {}
        assert rec.block_ref is not None
        assert rec.metadata["samples_per_channel"] == 100
        assert rec.metadata["sample_rate_hz"] == 10000.0
        # Block index increments
        assert rec.metadata["block_index"] == 0
        block = sim.emitted_blocks[0]
        assert block.data.shape == (1, 100)
        assert block.first_sample_index == 0

    async def test_block_index_advances(self) -> None:
        sim = NIDAQBlockSim(
            name="cdaq1",
            task="ai_block",
            sample_rate_hz=10000.0,
            block_size=100,
            signals={"AI0": Constant(1.0)},
        )
        await sim.open()
        await sim.start()
        sim.tick_once()
        sim.tick_once()
        block0, block1 = sim.emitted_blocks[:2]
        assert block0.first_sample_index == 0
        assert block1.first_sample_index == 100
        assert block0.block_index == 0
        assert block1.block_index == 1
