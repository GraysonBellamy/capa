"""Unit tests for :class:`capa.devices.bmp58x.Bmp58xAdapter`.

Drives the real adapter and the real :class:`Mcp2221I2cBus` framing
against the register-map :class:`tests.fixtures.fake_mcp2221.FakeBmp58x`
on a scripted bridge, so the sensor protocol, the forced-conversion
handshake and the adapter's emission shapes are all exercised without
hardware. The last class puts an SHT4x and a BMP58x on one bridge.
"""

from __future__ import annotations

import itertools
from typing import Any, ClassVar

import anyio
import pytest

import capa.devices.bmp58x as bmp58x_module
from capa.channels.calibration import Identity
from capa.channels.spec import Bmp58xReading, ChannelKind, ChannelSpec, Sht4xReading
from capa.core.errors import AdapterError
from capa.devices._mcp2221 import BUS_POOL, BridgeInfo, BusPool, Mcp2221Error, Mcp2221I2cBus
from capa.devices.adapter import Capability, DeviceCommand
from capa.devices.bmp58x import (
    ADAPTER_ID,
    BMP58X_SETTINGS,
    Bmp58xAdapter,
    Bmp58xAdapterParams,
    Bmp58xError,
    Bmp58xSettings,
    Bmp58xStateSnapshot,
    Measurement,
    bmp58x_row,
    conversion_time_s,
    convert_pressure,
    convert_temperature,
    decode_data,
    discover,
    encode_data,
    handshake,
    osr_code,
    osr_config,
)
from capa.devices.records import ChannelSample, DeviceEvent, DeviceSnapshot, SourceRecord
from capa.devices.registry import ADAPTERS
from capa.devices.settings import plan_settings
from capa.devices.sht4x import Sht4xAdapter, Sht4xAdapterParams
from tests._adapter_helpers import make_start_ctx
from tests.fixtures.fake_mcp2221 import (
    FakeBmp58x,
    FakeMcp2221Hid,
    FakeSht4xHid,
    opener_for,
)

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _channels(device: str = "baro") -> list[ChannelSpec]:
    return [
        ChannelSpec(
            name="ambient.pressure",
            kind=ChannelKind.PRESSURE,
            source=Bmp58xReading(device=device, field="pressure"),
            unit="Pa",
            derived_unit="Pa",
            calibration=Identity(input_unit="Pa", output_unit="Pa"),
        ),
        ChannelSpec(
            name="ambient.baro_temperature",
            kind=ChannelKind.TEMPERATURE,
            source=Bmp58xReading(device=device, field="temperature"),
            unit="degC",
            derived_unit="degC",
            calibration=Identity(input_unit="degC", output_unit="degC"),
        ),
    ]


def _bridge(
    sensor: FakeBmp58x | None = None, address: int = 0x47
) -> tuple[FakeMcp2221Hid, FakeBmp58x]:
    bridge = FakeMcp2221Hid(present=frozenset())
    sensor = sensor if sensor is not None else FakeBmp58x()
    bridge.attach(address, sensor)
    return bridge, sensor


def _factory(bridge: FakeMcp2221Hid) -> Any:
    opener = opener_for(bridge)

    def bus_factory(parsed: Any) -> Mcp2221I2cBus:
        return Mcp2221I2cBus(
            serial=parsed.serial,
            i2c_speed_hz=parsed.i2c_speed_hz,
            opener=opener,
            transfer_timeout_s=0.02,
        )

    return bus_factory


def _make(
    *,
    bridge: FakeMcp2221Hid | None = None,
    sensor: FakeBmp58x | None = None,
    bind: bool = True,
    pool: BusPool | None = None,
    **params: Any,
) -> tuple[Bmp58xAdapter, FakeBmp58x, FakeMcp2221Hid]:
    if bridge is None:
        bridge, sensor = _bridge(sensor)
    assert sensor is not None
    params.setdefault("rate_hz", 10.0)
    params.setdefault("pressure_oversampling", 1)
    params.setdefault("temperature_oversampling", 1)
    adapter = Bmp58xAdapter(
        name="baro",
        bus_factory=_factory(bridge),
        bus_pool=pool if pool is not None else BusPool(),
        **params,
    )
    if bind:
        adapter.configure_channels(_channels())
    return adapter, sensor, bridge


def _split(
    emissions: list[Any],
) -> tuple[list[SourceRecord], list[ChannelSample], list[DeviceSnapshot], list[DeviceEvent]]:
    return (
        [e for e in emissions if isinstance(e, SourceRecord)],
        [e for e in emissions if isinstance(e, ChannelSample)],
        [e for e in emissions if isinstance(e, DeviceSnapshot)],
        [e for e in emissions if isinstance(e, DeviceEvent)],
    )


async def _drain(adapter: Any, *, max_records: int) -> list[Any]:
    emissions: list[Any] = []
    records = 0
    async for emission in adapter.stream():
        emissions.append(emission)
        if isinstance(emission, SourceRecord):
            records += 1
            if records >= max_records:
                await adapter.stop()
    return emissions


async def _drain_until_good_after_error(adapter: Bmp58xAdapter, on_error: Any) -> list[Any]:
    """Stream until a good record follows a failed one; ``on_error`` runs at the failure."""
    emissions: list[Any] = []
    seen_error = False
    async for emission in adapter.stream():
        emissions.append(emission)
        if isinstance(emission, SourceRecord):
            if emission.row["error_type"] is not None:
                if not seen_error:
                    on_error()
                seen_error = True
            elif seen_error:
                await adapter.stop()
    return emissions


# ---------------------------------------------------------------------------
# Protocol helpers
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_oversampling_codes(self) -> None:
        assert [osr_code(n) for n in (1, 2, 4, 8, 16, 32, 64, 128)] == list(range(8))
        with pytest.raises(ValueError, match="not one of"):
            osr_code(3)
        # Pressure enabled, 64x pressure (code 6) and 4x temperature (code 2).
        assert osr_config(64, 4) == 0x40 | (6 << 3) | 2

    def test_conversion_time_grows_with_oversampling(self) -> None:
        assert conversion_time_s(1, 1) < conversion_time_s(64, 4) < conversion_time_s(128, 8)
        # Close to what the rig's BMP581 needed.
        assert 0.035 < conversion_time_s(64, 4) < 0.055
        assert 0.075 < conversion_time_s(128, 8) < 0.1

    def test_data_round_trip_and_scaling(self) -> None:
        payload = encode_data(-12.25, 100_828.125)
        t_raw, p_raw = decode_data(payload)
        assert convert_temperature(t_raw) == pytest.approx(-12.25, abs=1 / 65536)
        assert convert_pressure(p_raw) == pytest.approx(100_828.125, abs=1 / 64)
        # Rig capture: 7f7f7f... is the reset pattern, 130.6 kPa when decoded.
        assert convert_pressure(decode_data(bytes([0x7F] * 6))[1]) == pytest.approx(130_557.98)
        with pytest.raises(Bmp58xError, match="expected 6 data bytes"):
            decode_data(b"\x00" * 5)

    def test_out_of_range_pressure_is_a_fault(self) -> None:
        with pytest.raises(Bmp58xError, match="outside the sensor's"):
            Measurement.from_payload(
                bytes([0x7F] * 6),
                pressure_oversampling=1,
                temperature_oversampling=1,
                latency_s=0.0,
            )

    def test_row_shape_is_stable_across_good_and_failed_polls(self) -> None:
        good = bmp58x_row(
            Measurement.from_payload(
                encode_data(20.0, 101_000.0),
                pressure_oversampling=64,
                temperature_oversampling=4,
                latency_s=0.05,
            ),
            pressure_oversampling=64,
            temperature_oversampling=4,
            latency_s=0.05,
        )
        bad = bmp58x_row(
            None,
            pressure_oversampling=64,
            temperature_oversampling=4,
            latency_s=0.05,
            error=Bmp58xError("boom"),
        )
        assert set(good) == set(bad)
        assert good["pressure_pa"] == pytest.approx(101_000.0)
        assert good["temperature_c"] == pytest.approx(20.0)
        assert bad["pressure_pa"] is None
        assert bad["error_type"] == "Bmp58xError"
        assert bad["error_message"] == "boom"
        assert bad["pressure_oversampling"] == 64


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_engine_kwargs_path(self) -> None:
        adapter = Bmp58xAdapter(name="baro", rate_hz=2.0, pressure_oversampling=32)
        assert adapter.params.rate_hz == 2.0
        assert adapter.oversampling == (32, 4)
        assert adapter.params.i2c_address == 0x47
        assert adapter.resource_id == "hid:mcp2221:any"

    def test_shares_a_resource_id_with_an_sht4x_on_the_same_bridge(self) -> None:
        assert Bmp58xAdapter(name="b").resource_id == Sht4xAdapter(name="s").resource_id
        assert (
            Bmp58xAdapter(name="b", serial="0006774894").resource_id
            == Sht4xAdapter(name="s", serial="0006774894").resource_id
            == "hid:mcp2221:0006774894"
        )

    def test_params_and_kwargs_are_exclusive(self) -> None:
        with pytest.raises(TypeError, match="not both"):
            Bmp58xAdapter(name="b", params=Bmp58xAdapterParams(), rate_hz=1.0)

    def test_capabilities(self) -> None:
        adapter = Bmp58xAdapter(name="b")
        assert Capability.HAS_PARAMETER_CONFIG in adapter.capabilities
        assert Capability.SUPPORTS_AUTO_RECONNECT in adapter.capabilities
        assert Capability.HAS_SENSOR_HEATER not in adapter.capabilities
        without = Bmp58xAdapter(name="b", auto_reconnect=False)
        assert Capability.SUPPORTS_AUTO_RECONNECT not in without.capabilities

    @pytest.mark.parametrize(
        "bad",
        [
            {"made_up": 1},
            {"rate_hz": 11.0},
            {"rate_hz": 0.0},
            {"i2c_address": 0x44},
            {"pressure_oversampling": 3},
            {"temperature_oversampling": 256},
            {"i2c_speed_hz": 1_000_000},
        ],
    )
    def test_params_validation(self, bad: dict[str, Any]) -> None:
        with pytest.raises(Exception):
            Bmp58xAdapterParams(**bad)

    def test_expected_emission_rate_counts_bound_channels(self) -> None:
        adapter, _, _ = _make(rate_hz=2.0)
        assert adapter.expected_emission_rate_hz == 2.0 * 3
        bare, _, _ = _make(rate_hz=2.0, bind=False)
        assert bare.expected_emission_rate_hz == 2.0

    def test_descriptor_registered(self) -> None:
        descriptor = ADAPTERS["capa.devices.bmp58x"]
        assert descriptor.family == "bmp58x"
        assert descriptor.supported_binding_sources == ("bmp58x_reading",)
        assert descriptor.settings is BMP58X_SETTINGS
        assert {t.id for t in descriptor.channel_templates} == {
            "bmp58x.ambient_pressure",
            "bmp58x.ambient_temperature",
        }
        assert descriptor.discoverable and descriptor.handshake_available


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_open_resets_checks_and_configures(self) -> None:
        adapter, sensor, bridge = _make(pressure_oversampling=64, temperature_oversampling=4)
        await adapter.open()
        assert sensor.resets == 1
        assert adapter.chip == "BMP580/BMP581"
        assert sensor.registers[0x15] == 0x01  # data-ready source on
        assert sensor.registers[0x36] == osr_config(64, 4)
        assert sensor.registers[0x37] == 0xF0  # deep standby off, standby
        assert sensor.registers[0x27] == 0x00  # power-on flag consumed
        assert (await adapter.snapshot()).fields["bridge_revision"] == "A6 / 1.2"
        await adapter.open()  # idempotent
        assert sensor.resets == 1
        await adapter.close()
        assert bridge.closed
        await adapter.close()  # idempotent

    async def test_bmp585_is_recognised(self) -> None:
        adapter, _, _ = _make(sensor=FakeBmp58x(chip_id=0x51))
        await adapter.open()
        assert adapter.chip == "BMP585"

    async def test_wrong_chip_fails_and_releases_the_bridge(self) -> None:
        pool = BusPool()
        adapter, _, bridge = _make(sensor=FakeBmp58x(chip_id=0x58), pool=pool)
        with pytest.raises(AdapterError, match="chip id 0x58, not a BMP58x"):
            await adapter.open()
        assert bridge.closed
        assert pool.open_keys() == []
        assert await adapter.read_state_snapshot() is None

    async def test_nvm_fault_fails_open(self) -> None:
        class NvmFault(FakeBmp58x):
            RESET_REGISTERS: ClassVar[dict[int, int]] = {
                **FakeBmp58x.RESET_REGISTERS,
                0x28: 0x06,  # NVM ready, but with the error bit
            }

        adapter, _, _ = _make(sensor=NvmFault())
        with pytest.raises(AdapterError, match="NVM fault"):
            await adapter.open()

    async def test_configuration_that_does_not_stick_fails_open(self) -> None:
        sensor = FakeBmp58x()
        sensor.reject_config = True
        adapter, _, _ = _make(sensor=sensor)
        with pytest.raises(AdapterError, match="did not take its configuration"):
            await adapter.open()

    async def test_absent_sensor_fails_open(self) -> None:
        bridge, _ = _bridge(address=0x47)
        adapter, _, _ = _make(bridge=bridge, sensor=FakeBmp58x(), i2c_address=0x46)
        with pytest.raises(AdapterError, match=r"open failed.*no acknowledge"):
            await adapter.open()
        assert bridge.engine_state == 0x00

    async def test_stream_requires_open_and_start(self) -> None:
        adapter, _, _ = _make()
        with pytest.raises(AdapterError, match="requires open"):
            async for _ in adapter.stream():
                pass
        await adapter.open()
        with pytest.raises(AdapterError, match="requires start"):
            async for _ in adapter.stream():
                pass


# ---------------------------------------------------------------------------
# Stream
# ---------------------------------------------------------------------------


class TestStream:
    async def test_records_and_channel_samples(self) -> None:
        adapter, sensor, _ = _make()
        sensor.pressure_pa, sensor.temperature_c = 100_808.5, 19.45
        await adapter.open()
        ctx = make_start_ctx()
        await adapter.start(ctx)
        emissions = await _drain(adapter, max_records=3)
        records, samples, snapshots, _ = _split(emissions)
        assert isinstance(emissions[0], DeviceSnapshot)
        assert len(records) == 3
        assert len(samples) == 6
        first = records[0]
        assert first.adapter == ADAPTER_ID and first.shape == "wide_row"
        assert first.row["pressure_pa"] == pytest.approx(100_808.5, abs=0.02)
        assert first.row["temperature_c"] == pytest.approx(19.45, abs=1e-4)
        assert first.row["pressure_oversampling"] == 1
        assert first.row["error_type"] is None
        assert first.metadata == {"chip": "BMP580/BMP581", "revision": "0x32", "i2c_address": 0x47}
        assert first.t_utc == ctx.clock.to_wall_ns(first.t_mono_ns)
        by_channel = {s.channel: s for s in samples[:2]}
        assert by_channel["ambient.pressure"].value == pytest.approx(100_808.5, abs=0.02)
        assert by_channel["ambient.pressure"].unit == "Pa"
        assert by_channel["ambient.baro_temperature"].source_field == "temperature"
        assert all(s.source_record_id == first.record_id for s in samples[:2])
        assert sensor.conversions == 3
        assert snapshots[0].health == "ok"
        assert adapter.last_measurement is not None

    async def test_paces_at_rate_hz(self) -> None:
        adapter, _, _ = _make(rate_hz=10.0)
        await adapter.open()
        await adapter.start(make_start_ctx())
        records, *_ = _split(await _drain(adapter, max_records=4))
        gaps = [(b.t_mono_ns - a.t_mono_ns) / 1e9 for a, b in itertools.pairwise(records)]
        assert all(0.08 < gap < 0.25 for gap in gaps), gaps

    async def test_polls_data_ready_until_it_shows(self) -> None:
        adapter, sensor, _ = _make()
        sensor.ready_after_polls = 3
        await adapter.open()
        measurement = await adapter.read_once()
        assert measurement.pressure_pa == pytest.approx(sensor.pressure_pa, abs=0.02)

    async def test_no_data_ready_is_a_failed_poll_then_reconfigures(self) -> None:
        adapter, sensor, _ = _make()
        await adapter.open()
        await adapter.start(make_start_ctx())
        sensor.never_ready = True

        def heal() -> None:
            sensor.never_ready = False

        emissions = await _drain_until_good_after_error(adapter, heal)
        records, samples, _, _ = _split(emissions)
        bad = next(r for r in records if r.row["error_type"])
        assert bad.row["error_type"] == "Bmp58xError"
        assert "no data ready" in str(bad.row["error_message"])
        assert bad.row["pressure_pa"] is None
        assert records[-1].row["error_type"] is None
        assert sensor.resets == 2  # open, then the reconfigure after the failure
        assert (await adapter.snapshot()).fields["recoverable_errors"] == 1
        assert len(samples) == 2 * sum(1 for r in records if r.row["error_type"] is None)

    async def test_power_on_reset_mid_run_is_reported_and_reconfigured(self) -> None:
        adapter, sensor, _ = _make()
        await adapter.open()
        await adapter.start(make_start_ctx())
        sensor.power_on_reset_next = True
        emissions = await _drain_until_good_after_error(adapter, lambda: None)
        records, *_ = _split(emissions)
        assert "power-on reset" in str(records[0].row["error_message"])
        assert records[-1].row["error_type"] is None
        assert sensor.registers[0x36] == osr_config(1, 1)  # configuration restored

    async def test_failed_poll_without_auto_reconnect_ends_stream(self) -> None:
        adapter, sensor, _ = _make(auto_reconnect=False)
        await adapter.open()
        await adapter.start(make_start_ctx())
        sensor.never_ready = True
        with pytest.raises(AdapterError, match="auto_reconnect is disabled"):
            async for _ in adapter.stream():
                pass

    async def test_bridge_failure_reopens_and_reports(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmp58x_module, "_RECONNECT_BACKOFF_S", 0.01)
        adapter, _, bridge = _make()
        await adapter.open()
        await adapter.start(make_start_ctx())
        bridge.fail_hid = OSError("unplugged")

        def replug() -> None:
            bridge.fail_hid = None

        emissions = await _drain_until_good_after_error(adapter, replug)
        records, _, _, events = _split(emissions)
        assert records[0].row["error_type"] == "Mcp2221Error"
        assert records[-1].row["error_type"] is None
        assert [e.kind for e in events] == ["reconnect"]
        assert bridge.closed  # the reopen closed the dead handle first


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


class TestCommands:
    async def test_unauthorized_rejected(self) -> None:
        adapter, _, _ = _make()
        await adapter.open()
        result = await adapter.command(DeviceCommand(kind="soft_reset", issued_by="op"))
        assert result.accepted is False
        assert "unauthorized" in result.detail

    async def test_not_open_rejected(self) -> None:
        adapter, _, _ = _make()
        result = await adapter.set_oversampling(pressure=8, issued_by="op", confirmed_by="op")
        assert result.accepted is False
        assert "not open" in result.detail

    async def test_set_oversampling_writes_the_sensor(self) -> None:
        adapter, sensor, _ = _make()
        await adapter.open()
        result = await adapter.set_oversampling(pressure=16, issued_by="op", confirmed_by="op")
        assert result.accepted
        assert "pressure=16x temperature=1x" in result.detail
        assert sensor.registers[0x36] == osr_config(16, 1)
        measurement = await adapter.read_once()
        assert (measurement.pressure_oversampling, measurement.temperature_oversampling) == (16, 1)
        await adapter.set_oversampling(temperature=8, issued_by="op", confirmed_by="op")
        assert adapter.oversampling == (16, 8)
        snapshot = await adapter.read_state_snapshot()
        assert snapshot is not None and snapshot.temperature_oversampling == 8

    async def test_set_oversampling_validation(self) -> None:
        adapter, sensor, _ = _make()
        await adapter.open()
        with pytest.raises(AdapterError, match="needs 'pressure' and/or 'temperature'"):
            await adapter.set_oversampling(issued_by="op", confirmed_by="op")
        with pytest.raises(AdapterError, match="not one of"):
            await adapter.set_oversampling(pressure=3, issued_by="op", confirmed_by="op")
        assert adapter.oversampling == (1, 1)
        assert sensor.registers[0x36] == osr_config(1, 1)

    async def test_failed_oversampling_write_keeps_the_old_setting(self) -> None:
        adapter, sensor, _ = _make()
        await adapter.open()
        sensor.reject_config = True
        with pytest.raises(AdapterError, match="did not take its configuration"):
            await adapter.set_oversampling(pressure=32, issued_by="op", confirmed_by="op")
        assert adapter.oversampling == (1, 1)

    async def test_soft_reset_and_unknown_kind(self) -> None:
        adapter, sensor, _ = _make()
        await adapter.open()
        result = await adapter.soft_reset(issued_by="op", confirmed_by="op")
        assert result.accepted and "BMP580/BMP581" in result.detail
        assert sensor.resets == 2
        assert sensor.registers[0x36] == osr_config(1, 1)
        with pytest.raises(AdapterError, match="unknown command kind"):
            await adapter.command(DeviceCommand(kind="levitate", issued_by="op", confirmed_by="op"))

    async def test_command_failure_surfaces_as_adapter_error(self) -> None:
        adapter, _, bridge = _make()
        await adapter.open()
        bridge.fail_hid = OSError("gone")
        with pytest.raises(AdapterError, match="command 'soft_reset' failed"):
            await adapter.soft_reset(issued_by="op", confirmed_by="op")


# ---------------------------------------------------------------------------
# Read-back and declarative settings
# ---------------------------------------------------------------------------


class TestReadback:
    async def test_state_snapshot(self) -> None:
        adapter, sensor, _ = _make()
        assert await adapter.read_state_snapshot() is None
        await adapter.open()
        assert await adapter.read_state_snapshot() == Bmp58xStateSnapshot(
            pressure_oversampling=1,
            temperature_oversampling=1,
            chip="BMP580/BMP581",
            revision="0x32",
            bridge_serial=None,
            bridge_firmware="A6 / 1.2",
        )
        sensor.pressure_pa = 99_500.0
        await adapter.read_once()
        snapshot = await adapter.read_state_snapshot()
        assert snapshot is not None
        assert snapshot.last_pressure_pa == pytest.approx(99_500.0, abs=0.02)

    async def test_read_once_requires_open_and_wraps_errors(self) -> None:
        adapter, sensor, _ = _make()
        with pytest.raises(AdapterError, match="not open"):
            await adapter.read_once()
        await adapter.open()
        sensor.never_ready = True
        with pytest.raises(AdapterError, match="read failed"):
            await adapter.read_once()

    def test_settings_plan_changes_only_what_differs(self) -> None:
        snapshot = Bmp58xStateSnapshot(pressure_oversampling=64, temperature_oversampling=4)
        changes, issues = plan_settings(
            BMP58X_SETTINGS,
            Bmp58xSettings(pressure_oversampling=128, temperature_oversampling=4),
            snapshot,
        )
        assert not issues
        assert [(c.kind, dict(c.payload)) for c in changes] == [
            ("set_oversampling", {"pressure": 128})
        ]
        changes, _ = plan_settings(BMP58X_SETTINGS, Bmp58xSettings(), snapshot)
        assert changes == ()


# ---------------------------------------------------------------------------
# Handshake and discovery hooks
# ---------------------------------------------------------------------------


class TestHooks:
    async def test_handshake_summary(self) -> None:
        bridge, _ = _bridge(FakeBmp58x(pressure_pa=100_808.4, temperature_c=19.45))
        summary = await handshake({}, bus_factory=_factory(bridge), bus_pool=BusPool())
        assert BUS_POOL.open_keys() == []
        assert summary.startswith(
            "bmp58x chip=BMP580/BMP581 rev=0x32 addr=0x47 bridge=MCP2221 rev=A6 / 1.2"
        )
        assert "P=100808.4Pa" in summary
        assert bridge.closed

    async def test_discover_reads_the_chip_id_without_resetting(self) -> None:
        bridge = FakeSht4xHid()  # an SHT4x at 0x44 as well
        sensor = FakeBmp58x(chip_id=0x51)
        bridge.attach(0x46, sensor)
        opener = opener_for(bridge)
        bridges = [BridgeInfo(path=b"hid-path-1", serial=None, product="MCP2221", manufacturer="M")]
        rows = await discover(bridges=lambda: bridges, opener=opener)
        assert rows == [
            {
                "adapter": ADAPTER_ID,
                "serial": None,
                "path": "hid-path-1",
                "i2c_address": 0x46,
                "chip": "BMP585",
            }
        ]
        assert sensor.resets == 0
        assert bridge.closed

    async def test_discover_skips_bridges_it_cannot_open(self) -> None:
        opener = opener_for(FakeMcp2221Hid())
        opener.fail_with = Mcp2221Error("held by another process")
        bridges = [BridgeInfo(path=b"p", serial=None, product=None, manufacturer=None)]
        assert await discover(bridges=lambda: bridges, opener=opener) == []


# ---------------------------------------------------------------------------
# An SHT4x and a BMP58x on one bridge
# ---------------------------------------------------------------------------


class TestSharedBridge:
    async def test_both_sensors_stream_over_one_open_bridge(self) -> None:
        bridge = FakeSht4xHid(temperature_c=19.9, humidity_pct=62.2)
        sensor = FakeBmp58x(pressure_pa=100_809.0, temperature_c=19.5)
        bridge.attach(0x47, sensor)
        opener = opener_for(bridge)
        pool = BusPool()

        def bus_factory(parsed: Any) -> Mcp2221I2cBus:
            return Mcp2221I2cBus(
                i2c_speed_hz=parsed.i2c_speed_hz, opener=opener, transfer_timeout_s=0.02
            )

        sht = Sht4xAdapter(
            name="ambient",
            params=Sht4xAdapterParams(rate_hz=10.0),
            bus_factory=bus_factory,
            bus_pool=pool,
        )
        sht.configure_channels(
            [
                ChannelSpec(
                    name="ambient.temperature",
                    kind=ChannelKind.TEMPERATURE,
                    source=Sht4xReading(device="ambient", field="temperature"),
                    unit="degC",
                    derived_unit="degC",
                    calibration=Identity(input_unit="degC", output_unit="degC"),
                )
            ]
        )
        baro = Bmp58xAdapter(
            name="baro",
            params=Bmp58xAdapterParams(
                rate_hz=10.0, pressure_oversampling=1, temperature_oversampling=1
            ),
            bus_factory=bus_factory,
            bus_pool=pool,
        )
        baro.configure_channels(_channels())
        await sht.open()
        await baro.open()
        assert len(opener.selectors) == 1  # one HID handle for both sensors
        assert pool.open_keys() == ["hid:mcp2221:any"]

        ctx = make_start_ctx()
        await sht.start(ctx)
        await baro.start(ctx)
        results: dict[str, list[Any]] = {}

        async def run(adapter: Any) -> None:
            results[adapter.name] = await _drain(adapter, max_records=5)

        async with anyio.create_task_group() as tg:
            tg.start_soon(run, sht)
            tg.start_soon(run, baro)

        sht_records, *_ = _split(results["ambient"])
        baro_records, *_ = _split(results["baro"])
        assert all(r.row["error_type"] is None for r in sht_records + baro_records)
        assert all(r.row["temperature_c"] == pytest.approx(19.9, abs=0.01) for r in sht_records)
        assert all(r.row["pressure_pa"] == pytest.approx(100_809.0, abs=0.02) for r in baro_records)

        await sht.close()
        assert not bridge.closed  # the barometer still holds the bridge
        assert (await baro.read_once()).pressure_pa == pytest.approx(100_809.0, abs=0.02)
        await baro.close()
        assert bridge.closed
        assert pool.open_keys() == []

    async def test_a_second_bus_speed_on_the_bridge_is_refused(self) -> None:
        bridge = FakeSht4xHid()
        bridge.attach(0x47, FakeBmp58x())
        pool = BusPool()
        sht = Sht4xAdapter(
            name="ambient",
            params=Sht4xAdapterParams(i2c_speed_hz=100_000),
            bus_factory=_factory(bridge),
            bus_pool=pool,
        )
        baro = Bmp58xAdapter(
            name="baro",
            params=Bmp58xAdapterParams(i2c_speed_hz=400_000),
            bus_factory=_factory(bridge),
            bus_pool=pool,
        )
        await sht.open()
        with pytest.raises(AdapterError, match="same i2c_speed_hz"):
            await baro.open()
        await sht.close()
        assert pool.open_keys() == []
