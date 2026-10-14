"""Sim SHT4x adapter: row shape, channel samples, precision and heater state."""

from __future__ import annotations

from typing import Any

import pytest

from capa.channels.calibration import Identity
from capa.channels.spec import ChannelKind, ChannelSpec, Sht4xReading
from capa.core.errors import AdapterError
from capa.devices.adapter import Capability, DeviceCommand
from capa.devices.records import ChannelSample, DeviceEvent, SourceRecord
from capa.devices.registry import ADAPTERS
from capa.devices.sht4x import SHT4X_SETTINGS, sht4x_row
from capa.devices.sim._signals import Constant, Ramp
from capa.devices.sim.sht4x_sim import Sht4xSim
from tests._adapter_helpers import make_start_ctx

pytestmark = pytest.mark.anyio


def _channels() -> list[ChannelSpec]:
    return [
        ChannelSpec(
            name="ambient.temperature",
            kind=ChannelKind.TEMPERATURE,
            source=Sht4xReading(device="ambient", field="temperature"),
            unit="degC",
            derived_unit="degC",
            calibration=Identity(input_unit="degC", output_unit="degC"),
        ),
        ChannelSpec(
            name="ambient.humidity",
            kind=ChannelKind.HUMIDITY,
            source=Sht4xReading(device="ambient", field="humidity"),
            unit="percent",
            derived_unit="percent",
            calibration=Identity(input_unit="percent", output_unit="percent"),
        ),
    ]


def _split(
    emissions: list[Any],
) -> tuple[list[SourceRecord], list[ChannelSample], list[DeviceEvent]]:
    return (
        [e for e in emissions if isinstance(e, SourceRecord)],
        [e for e in emissions if isinstance(e, ChannelSample)],
        [e for e in emissions if isinstance(e, DeviceEvent)],
    )


class TestSht4xSim:
    async def test_defaults_give_a_steady_lab(self) -> None:
        sim = Sht4xSim(name="ambient")
        sim.configure_channels(_channels())
        await sim.open()
        await sim.start(make_start_ctx())
        records, samples, _ = _split(sim.tick_once())
        assert len(records) == 1 and len(samples) == 2
        record = records[0]
        assert record.adapter == "sht4x" and record.shape == "wide_row"
        assert record.row["temperature_c"] == pytest.approx(22.0, abs=0.01)
        assert record.row["humidity_pct"] == pytest.approx(50.0, abs=0.01)
        assert set(record.row) == set(sht4x_row(None, precision="high", latency_s=0.0))
        by_channel = {s.channel: s for s in samples}
        assert by_channel["ambient.temperature"].value == pytest.approx(22.0, abs=0.01)
        assert by_channel["ambient.humidity"].unit == "percent"
        assert all(s.source_record_id == record.record_id for s in samples)

    async def test_from_params_builds_signals(self) -> None:
        sim = Sht4xSim.from_params(
            name="ambient",
            temperature_signal={"kind": "ramp", "start": 20.0, "end": 30.0, "duration_s": 10.0},
            humidity_signal={"kind": "constant", "value": 35.0},
            tick_period_s=0.5,
            precision="medium",
        )
        assert isinstance(sim.temperature_signal, Ramp)
        assert isinstance(sim.humidity_signal, Constant)
        assert sim.precision == "medium"
        assert sim.expected_emission_rate_hz == 2.0
        with pytest.raises(AdapterError, match="unknown precision"):
            Sht4xSim.from_params(name="ambient", precision="ultra")

    async def test_stream_yields_until_stopped(self) -> None:
        sim = Sht4xSim(name="ambient", tick_period_s=0.01)
        await sim.open()
        await sim.start(make_start_ctx())
        seen = 0
        async for emission in sim.stream():
            if isinstance(emission, SourceRecord):
                seen += 1
                if seen == 3:
                    await sim.stop()
        assert seen == 3
        assert (await sim.snapshot()).health == "down"

    async def test_precision_setting_reads_back(self) -> None:
        sim = Sht4xSim(name="ambient")
        assert await sim.read_state_snapshot() is None
        await sim.open()
        result = await sim.command(
            DeviceCommand(
                kind="set_precision",
                payload={"precision": "LOW"},
                issued_by="op",
                confirmed_by="op",
            )
        )
        assert result.accepted
        snapshot = await sim.read_state_snapshot()
        assert snapshot is not None and snapshot.precision == "low"
        await sim.start(make_start_ctx())
        assert sim.tick_once()[0].row["precision"] == "low"  # type: ignore[union-attr]
        with pytest.raises(AdapterError, match="unknown precision"):
            await sim.command(
                DeviceCommand(
                    kind="set_precision",
                    payload={"precision": "ultra"},
                    issued_by="op",
                    confirmed_by="op",
                )
            )

    async def test_heater_pulse_bumps_reading_and_holds_off(self) -> None:
        sim = Sht4xSim(name="ambient")
        await sim.open()
        await sim.start(make_start_ctx())
        pulse = DeviceCommand(
            kind="heater_pulse",
            payload={"power_mw": 110, "duration_s": 1.0},
            issued_by="op",
            confirmed_by="op",
        )
        result = await sim.command(pulse)
        assert result.accepted and "end_of_pulse=27.0" in result.detail
        again = await sim.command(pulse)
        assert again.accepted is False and "hold-off" in again.detail
        records, _, events = _split(sim.tick_once())
        assert records[0].row["temperature_c"] > 24.0  # type: ignore[operator]
        assert [e.kind for e in events] == ["heater_pulse"]
        snapshot = await sim.read_state_snapshot()
        assert snapshot is not None and snapshot.heater_pulses == 1
        with pytest.raises(AdapterError, match="no 50 mW"):
            await sim.command(
                DeviceCommand(
                    kind="heater_pulse",
                    payload={"power_mw": 50, "duration_s": 1.0},
                    issued_by="op",
                    confirmed_by="op",
                )
            )

    async def test_gates_and_unknown_commands(self) -> None:
        sim = Sht4xSim(name="ambient")
        await sim.open()
        refused = await sim.command(DeviceCommand(kind="soft_reset", issued_by="op"))
        assert refused.accepted is False
        ok = await sim.command(DeviceCommand(kind="soft_reset", issued_by="op", confirmed_by="op"))
        assert ok.accepted and sim.sensor_serial in ok.detail
        with pytest.raises(AdapterError, match="unknown command kind"):
            await sim.command(DeviceCommand(kind="levitate", issued_by="op", confirmed_by="op"))

    async def test_read_once(self) -> None:
        sim = Sht4xSim(name="ambient", humidity_signal=Constant(30.0))
        with pytest.raises(AdapterError, match="not open"):
            await sim.read_once()
        await sim.open()
        measurement = await sim.read_once()
        assert measurement.humidity_pct == pytest.approx(30.0, abs=0.01)
        assert sim.last_measurement is measurement

    def test_descriptor_mirrors_the_real_adapter(self) -> None:
        descriptor = ADAPTERS["capa.devices.sim.sht4x_sim"]
        real = ADAPTERS["capa.devices.sht4x"]
        assert descriptor.family == "sim"
        assert descriptor.supported_binding_sources == real.supported_binding_sources
        assert descriptor.channel_templates == real.channel_templates
        assert descriptor.settings is SHT4X_SETTINGS
        assert Capability.HAS_SENSOR_HEATER in Sht4xSim(name="x").capabilities
