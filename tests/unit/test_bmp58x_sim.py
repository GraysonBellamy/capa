"""Sim BMP58x adapter: row shape, channel samples and oversampling state."""

from __future__ import annotations

from typing import Any

import pytest

from capa.channels.calibration import Identity
from capa.channels.spec import Bmp58xReading, ChannelKind, ChannelSpec
from capa.core.errors import AdapterError
from capa.devices.adapter import DeviceCommand
from capa.devices.bmp58x import BMP58X_SETTINGS, bmp58x_row
from capa.devices.records import ChannelSample, SourceRecord
from capa.devices.registry import ADAPTERS
from capa.devices.sim._signals import Constant, Ramp
from capa.devices.sim.bmp58x_sim import Bmp58xSim
from tests._adapter_helpers import make_start_ctx

pytestmark = pytest.mark.anyio


def _channels() -> list[ChannelSpec]:
    return [
        ChannelSpec(
            name="ambient.pressure",
            kind=ChannelKind.PRESSURE,
            source=Bmp58xReading(device="baro", field="pressure"),
            unit="Pa",
            derived_unit="Pa",
            calibration=Identity(input_unit="Pa", output_unit="Pa"),
        ),
        ChannelSpec(
            name="ambient.baro_temperature",
            kind=ChannelKind.TEMPERATURE,
            source=Bmp58xReading(device="baro", field="temperature"),
            unit="degC",
            derived_unit="degC",
            calibration=Identity(input_unit="degC", output_unit="degC"),
        ),
    ]


def _split(emissions: list[Any]) -> tuple[list[SourceRecord], list[ChannelSample]]:
    return (
        [e for e in emissions if isinstance(e, SourceRecord)],
        [e for e in emissions if isinstance(e, ChannelSample)],
    )


def _command(kind: str, **payload: Any) -> DeviceCommand:
    return DeviceCommand(kind=kind, payload=payload, issued_by="op", confirmed_by="op")


class TestBmp58xSim:
    async def test_defaults_give_a_steady_lab(self) -> None:
        sim = Bmp58xSim(name="baro")
        sim.configure_channels(_channels())
        await sim.open()
        await sim.start(make_start_ctx())
        records, samples = _split(sim.tick_once())
        assert len(records) == 1 and len(samples) == 2
        record = records[0]
        assert record.adapter == "bmp58x" and record.shape == "wide_row"
        assert record.row["pressure_pa"] == pytest.approx(101_325.0, abs=0.02)
        assert record.row["temperature_c"] == pytest.approx(22.0, abs=1e-4)
        assert set(record.row) == set(
            bmp58x_row(None, pressure_oversampling=64, temperature_oversampling=4, latency_s=0.0)
        )
        by_channel = {s.channel: s for s in samples}
        assert by_channel["ambient.pressure"].value == pytest.approx(101_325.0, abs=0.02)
        assert by_channel["ambient.pressure"].unit == "Pa"
        assert all(s.source_record_id == record.record_id for s in samples)

    async def test_from_params_builds_signals(self) -> None:
        sim = Bmp58xSim.from_params(
            name="baro",
            pressure_signal={
                "kind": "ramp",
                "start": 100_000.0,
                "end": 101_000.0,
                "duration_s": 10.0,
            },
            temperature_signal={"kind": "constant", "value": 21.0},
            tick_period_s=0.5,
            pressure_oversampling=128,
        )
        assert isinstance(sim.pressure_signal, Ramp)
        assert isinstance(sim.temperature_signal, Constant)
        assert sim.pressure_oversampling == 128
        assert sim.expected_emission_rate_hz == 2.0
        with pytest.raises(AdapterError, match="not one of"):
            Bmp58xSim.from_params(name="baro", pressure_oversampling=3)

    async def test_stream_yields_until_stopped(self) -> None:
        sim = Bmp58xSim(name="baro", tick_period_s=0.01)
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

    async def test_oversampling_setting_reads_back(self) -> None:
        sim = Bmp58xSim(name="baro")
        assert await sim.read_state_snapshot() is None
        await sim.open()
        result = await sim.command(_command("set_oversampling", pressure=16))
        assert result.accepted and "pressure=16x temperature=4x" in result.detail
        snapshot = await sim.read_state_snapshot()
        assert snapshot is not None
        assert (snapshot.pressure_oversampling, snapshot.temperature_oversampling) == (16, 4)
        await sim.start(make_start_ctx())
        assert sim.tick_once()[0].row["pressure_oversampling"] == 16  # type: ignore[union-attr]
        with pytest.raises(AdapterError, match="not one of"):
            await sim.command(_command("set_oversampling", temperature=5))
        with pytest.raises(AdapterError, match="needs 'pressure'"):
            await sim.command(_command("set_oversampling"))

    async def test_gates_and_unknown_commands(self) -> None:
        sim = Bmp58xSim(name="baro")
        await sim.open()
        refused = await sim.command(DeviceCommand(kind="soft_reset", issued_by="op"))
        assert refused.accepted is False
        ok = await sim.command(_command("soft_reset"))
        assert ok.accepted and "BMP580/BMP581" in ok.detail
        with pytest.raises(AdapterError, match="unknown command kind"):
            await sim.command(_command("levitate"))

    async def test_read_once(self) -> None:
        sim = Bmp58xSim(name="baro", pressure_signal=Constant(99_000.0))
        with pytest.raises(AdapterError, match="not open"):
            await sim.read_once()
        await sim.open()
        measurement = await sim.read_once()
        assert measurement.pressure_pa == pytest.approx(99_000.0, abs=0.02)
        assert sim.last_measurement is measurement

    def test_descriptor_mirrors_the_real_adapter(self) -> None:
        descriptor = ADAPTERS["capa.devices.sim.bmp58x_sim"]
        real = ADAPTERS["capa.devices.bmp58x"]
        assert descriptor.family == "sim"
        assert descriptor.supported_binding_sources == real.supported_binding_sources
        assert descriptor.channel_templates == real.channel_templates
        assert descriptor.settings is BMP58X_SETTINGS
