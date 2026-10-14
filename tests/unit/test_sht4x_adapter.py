"""Unit tests for :class:`capa.devices.sht4x.Sht4xAdapter`.

Drives the real adapter and the real :class:`Mcp2221I2cBus` framing
against the scripted HID handle in :mod:`tests.fixtures.fake_mcp2221`, so
the sensor protocol, the bridge's NACK recovery and the adapter's
emission shapes are all exercised without hardware.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

import capa.devices.sht4x as sht4x_module
from capa.channels.calibration import Identity
from capa.channels.spec import ChannelKind, ChannelSpec, Sht4xReading
from capa.core.errors import AdapterError
from capa.devices._mcp2221 import BridgeInfo, Mcp2221I2cBus
from capa.devices.adapter import Capability, DeviceCommand
from capa.devices.records import ChannelSample, DeviceEvent, DeviceSnapshot, SourceRecord
from capa.devices.registry import ADAPTERS
from capa.devices.settings import plan_settings
from capa.devices.sht4x import (
    ADAPTER_ID,
    SHT4X_SETTINGS,
    Measurement,
    Sht4xAdapter,
    Sht4xAdapterParams,
    Sht4xCrcError,
    Sht4xError,
    Sht4xSettings,
    Sht4xStateSnapshot,
    convert_humidity,
    convert_temperature,
    crc8,
    decode_words,
    discover,
    handshake,
    raw_from_humidity,
    raw_from_temperature,
    sht4x_row,
)
from tests._adapter_helpers import make_start_ctx
from tests.fixtures.fake_mcp2221 import FakeSht4xHid, opener_for, sht4x_payload

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _channels(device: str = "ambient") -> list[ChannelSpec]:
    return [
        ChannelSpec(
            name="ambient.temperature",
            kind=ChannelKind.TEMPERATURE,
            source=Sht4xReading(device=device, field="temperature"),
            unit="degC",
            derived_unit="degC",
            calibration=Identity(input_unit="degC", output_unit="degC"),
        ),
        ChannelSpec(
            name="ambient.humidity",
            kind=ChannelKind.HUMIDITY,
            source=Sht4xReading(device=device, field="humidity"),
            unit="percent",
            derived_unit="percent",
            calibration=Identity(input_unit="percent", output_unit="percent"),
        ),
    ]


def _make(
    *,
    handle: FakeSht4xHid | None = None,
    bind: bool = True,
    **params: Any,
) -> tuple[Sht4xAdapter, FakeSht4xHid]:
    fake = handle if handle is not None else FakeSht4xHid()
    opener = opener_for(fake)

    def bus_factory(parsed: Sht4xAdapterParams) -> Mcp2221I2cBus:
        return Mcp2221I2cBus(
            serial=parsed.serial,
            i2c_speed_hz=parsed.i2c_speed_hz,
            opener=opener,
            transfer_timeout_s=0.02,
        )

    params.setdefault("rate_hz", 10.0)
    adapter = Sht4xAdapter(name="ambient", bus_factory=bus_factory, **params)
    if bind:
        adapter.configure_channels(_channels())
    return adapter, fake


def _split(
    emissions: list[Any],
) -> tuple[list[SourceRecord], list[ChannelSample], list[DeviceSnapshot], list[DeviceEvent]]:
    return (
        [e for e in emissions if isinstance(e, SourceRecord)],
        [e for e in emissions if isinstance(e, ChannelSample)],
        [e for e in emissions if isinstance(e, DeviceSnapshot)],
        [e for e in emissions if isinstance(e, DeviceEvent)],
    )


async def _drain(adapter: Sht4xAdapter, *, max_records: int) -> list[Any]:
    emissions: list[Any] = []
    records = 0
    async for emission in adapter.stream():
        emissions.append(emission)
        if isinstance(emission, SourceRecord):
            records += 1
            if records >= max_records:
                await adapter.stop()
    return emissions


# ---------------------------------------------------------------------------
# Protocol helpers
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_crc8_matches_datasheet_example(self) -> None:
        # Datasheet 4.4: CRC of 0xBEEF is 0x92.
        assert crc8(b"\xbe\xef") == 0x92

    def test_decode_words_round_trip(self) -> None:
        payload = sht4x_payload(21.67, 64.52)
        t_raw, rh_raw = decode_words(payload)
        assert convert_temperature(t_raw) == pytest.approx(21.67, abs=0.01)
        assert convert_humidity(rh_raw) == pytest.approx(64.52, abs=0.01)

    def test_decode_rejects_bad_crc_and_bad_length(self) -> None:
        payload = bytearray(sht4x_payload(20.0, 50.0))
        payload[5] ^= 0x01
        with pytest.raises(Sht4xCrcError, match="checksum"):
            decode_words(bytes(payload))
        with pytest.raises(Sht4xError, match="expected 6 bytes"):
            decode_words(b"\x00\x00")

    def test_humidity_is_clamped_but_raw_keeps_overshoot(self) -> None:
        assert convert_humidity(0) == 0.0
        assert convert_humidity(65535) == 100.0
        assert raw_from_humidity(-6.0) == 0
        assert raw_from_temperature(-45.0) == 0
        assert raw_from_temperature(130.0) == 65535

    def test_row_shape_is_stable_across_good_and_failed_polls(self) -> None:
        good = sht4x_row(
            Measurement(22.0, 50.0, 1, 2, "high", 0.015), precision="high", latency_s=0.015
        )
        bad = sht4x_row(None, precision="high", latency_s=0.02, error=Sht4xError("boom"))
        assert set(good) == set(bad)
        assert good["temperature_c"] == 22.0
        assert bad["temperature_c"] is None
        assert bad["error_type"] == "Sht4xError"
        assert bad["error_message"] == "boom"
        assert good["error_type"] is None


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_engine_kwargs_path(self) -> None:
        adapter = Sht4xAdapter(name="amb", rate_hz=2.0, precision="medium")
        assert adapter.name == "amb"
        assert adapter.params.rate_hz == 2.0
        assert adapter.precision == "medium"
        assert adapter.resource_id == "hid:mcp2221:any"

    def test_params_and_kwargs_are_exclusive(self) -> None:
        with pytest.raises(TypeError, match="not both"):
            Sht4xAdapter(name="amb", params=Sht4xAdapterParams(), rate_hz=1.0)

    def test_capabilities(self) -> None:
        adapter = Sht4xAdapter(name="amb")
        assert Capability.HAS_PARAMETER_CONFIG in adapter.capabilities
        assert Capability.HAS_SENSOR_HEATER in adapter.capabilities
        assert Capability.SUPPORTS_AUTO_RECONNECT in adapter.capabilities
        without = Sht4xAdapter(name="amb", auto_reconnect=False)
        assert Capability.SUPPORTS_AUTO_RECONNECT not in without.capabilities

    @pytest.mark.parametrize(
        "bad",
        [
            {"made_up": 1},
            {"rate_hz": 11.0},
            {"rate_hz": 0.0},
            {"precision": "ultra"},
            {"i2c_address": 0x80},
            {"i2c_speed_hz": 1_000_000},
        ],
    )
    def test_params_validation(self, bad: dict[str, Any]) -> None:
        with pytest.raises(Exception):
            Sht4xAdapterParams(**bad)

    def test_expected_emission_rate_counts_bound_channels(self) -> None:
        adapter, _ = _make(rate_hz=2.0)
        assert adapter.expected_emission_rate_hz == 2.0 * 3
        bare, _ = _make(rate_hz=2.0, bind=False)
        assert bare.expected_emission_rate_hz == 2.0

    def test_descriptor_registered(self) -> None:
        descriptor = ADAPTERS["capa.devices.sht4x"]
        assert descriptor.family == "sht4x"
        assert descriptor.supported_binding_sources == ("sht4x_reading",)
        assert descriptor.settings is SHT4X_SETTINGS
        assert {t.id for t in descriptor.channel_templates} == {
            "sht4x.ambient_temperature",
            "sht4x.ambient_humidity",
        }
        assert descriptor.discoverable and descriptor.handshake_available


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_open_resets_and_identifies(self) -> None:
        adapter, fake = _make()
        await adapter.open()
        assert adapter.sensor_serial == "0x1643e746"
        assert fake.sensor_commands == [0x94, 0x89]
        assert fake.cancels == 1
        assert (await adapter.snapshot()).fields["bridge_revision"] == "A6 / 1.2"
        await adapter.open()  # idempotent
        assert fake.sensor_commands == [0x94, 0x89]
        await adapter.close()
        assert fake.closed
        await adapter.close()  # idempotent

    async def test_open_with_sensor_absent_fails_and_releases_bridge(self) -> None:
        adapter, fake = _make(i2c_address=0x45)
        with pytest.raises(AdapterError, match="open failed"):
            await adapter.open()
        assert fake.closed
        assert await adapter.read_state_snapshot() is None

    async def test_stream_requires_open_and_start(self) -> None:
        adapter, _ = _make()
        with pytest.raises(AdapterError, match="requires open"):
            async for _ in adapter.stream():
                pass
        await adapter.open()
        with pytest.raises(AdapterError, match="requires start"):
            async for _ in adapter.stream():
                pass

    async def test_snapshot_health_follows_lifecycle(self) -> None:
        adapter, _ = _make()
        assert (await adapter.snapshot()).health == "down"
        await adapter.open()
        assert (await adapter.snapshot()).health == "ok"
        assert adapter.watchdog_state().is_silent(now_t_mono_ns=10**12) is False


# ---------------------------------------------------------------------------
# Stream
# ---------------------------------------------------------------------------


class TestStream:
    async def test_records_and_channel_samples(self) -> None:
        adapter, fake = _make(rate_hz=10.0)
        fake.temperature_c, fake.humidity_pct = 23.25, 41.5
        await adapter.open()
        ctx = make_start_ctx()
        await adapter.start(ctx)
        emissions = await _drain(adapter, max_records=3)
        records, samples, snapshots, _events = _split(emissions)
        assert isinstance(emissions[0], DeviceSnapshot)
        assert len(records) == 3
        assert len(samples) == 6
        first = records[0]
        assert first.adapter == ADAPTER_ID and first.shape == "wide_row"
        assert first.row["temperature_c"] == pytest.approx(23.25, abs=0.01)
        assert first.row["humidity_pct"] == pytest.approx(41.5, abs=0.01)
        assert first.row["precision"] == "high"
        assert first.row["error_type"] is None
        assert first.metadata == {"sensor_serial": "0x1643e746", "i2c_address": 0x44}
        assert first.t_utc == ctx.clock.to_wall_ns(first.t_mono_ns)
        by_channel = {s.channel: s for s in samples[:2]}
        assert by_channel["ambient.temperature"].value == pytest.approx(23.25, abs=0.01)
        assert by_channel["ambient.temperature"].unit == "degC"
        assert by_channel["ambient.humidity"].value == pytest.approx(41.5, abs=0.01)
        assert by_channel["ambient.humidity"].source_field == "humidity"
        assert all(s.source_record_id == first.record_id for s in samples[:2])
        assert all(s.t_mono_ns == first.t_mono_ns for s in samples[:2])
        assert fake.sensor_commands.count(0xFD) == 3
        assert snapshots[0].health == "ok"
        assert adapter.last_measurement is not None

    async def test_paces_at_rate_hz(self) -> None:
        adapter, _ = _make(rate_hz=10.0)
        await adapter.open()
        await adapter.start(make_start_ctx())
        records, *_ = _split(await _drain(adapter, max_records=4))
        gaps = [(b.t_mono_ns - a.t_mono_ns) / 1e9 for a, b in itertools.pairwise(records)]
        assert all(0.08 < gap < 0.25 for gap in gaps), gaps

    async def test_crc_failure_keeps_row_drops_samples_and_degrades(self) -> None:
        adapter, fake = _make(rate_hz=10.0)
        await adapter.open()
        await adapter.start(make_start_ctx())
        fake.corrupt_next = True
        emissions: list[Any] = []
        health_after_failure: str | None = None
        async for emission in adapter.stream():
            emissions.append(emission)
            if isinstance(emission, SourceRecord):
                if emission.row["error_type"] is not None:
                    # Still running: the degradation counter shows in the pill.
                    health_after_failure = (await adapter.snapshot()).health
                else:
                    await adapter.stop()
        records, samples, _snapshots, _ = _split(emissions)
        bad, good = records
        assert bad.row["error_type"] == "Sht4xCrcError"
        assert bad.row["temperature_c"] is None
        assert good.row["error_type"] is None
        assert len(samples) == 2  # only the good poll
        assert (await adapter.snapshot()).fields["recoverable_errors"] == 1
        assert health_after_failure == "degraded"

    async def test_busy_sensor_past_retry_budget_is_a_failed_poll_then_recovers(self) -> None:
        adapter, fake = _make(rate_hz=10.0)
        await adapter.open()
        await adapter.start(make_start_ctx())
        fake.nack_reads = 10_000
        emissions: list[Any] = []
        async for emission in adapter.stream():
            emissions.append(emission)
            if isinstance(emission, SourceRecord):
                if emission.row["error_type"] is not None:
                    fake.nack_reads = 0
                else:
                    await adapter.stop()
        records, samples, *_ = _split(emissions)
        assert records[0].row["error_type"] == "Sht4xError"
        assert "did not answer" in str(records[0].row["error_message"])
        assert records[-1].row["error_type"] is None
        assert fake.engine_state == 0x00
        assert len(samples) == 2

    async def test_failed_poll_without_auto_reconnect_ends_stream(self) -> None:
        adapter, fake = _make(rate_hz=10.0, auto_reconnect=False)
        await adapter.open()
        await adapter.start(make_start_ctx())
        fake.corrupt_next = True
        with pytest.raises(AdapterError, match="auto_reconnect is disabled"):
            async for _ in adapter.stream():
                pass

    async def test_bridge_failure_reopens_and_reports(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sht4x_module, "_RECONNECT_BACKOFF_S", 0.01)
        adapter, fake = _make(rate_hz=10.0)
        await adapter.open()
        await adapter.start(make_start_ctx())
        fake.fail_hid = OSError("unplugged")
        emissions: list[Any] = []
        async for emission in adapter.stream():
            emissions.append(emission)
            if isinstance(emission, SourceRecord):
                if emission.row["error_type"] == "Mcp2221Error":
                    fake.fail_hid = None
                else:
                    await adapter.stop()
        records, _, _, events = _split(emissions)
        assert records[0].row["error_type"] == "Mcp2221Error"
        assert records[-1].row["error_type"] is None
        assert [e.kind for e in events] == ["reconnect"]
        assert fake.closed  # the reopen closed the dead handle first
        assert fake.sensor_commands.count(0x89) == 2  # identified again after reopen

    async def test_stop_before_first_tick_yields_only_snapshot(self) -> None:
        adapter, _ = _make(rate_hz=10.0)
        await adapter.open()
        await adapter.start(make_start_ctx())
        await adapter.stop()
        emissions = [e async for e in adapter.stream()]
        assert len(emissions) == 1 and isinstance(emissions[0], DeviceSnapshot)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


class TestCommands:
    async def test_unauthorized_rejected(self) -> None:
        adapter, _ = _make()
        await adapter.open()
        result = await adapter.command(DeviceCommand(kind="soft_reset", issued_by="op"))
        assert result.accepted is False
        assert "unauthorized" in result.detail

    async def test_not_open_rejected(self) -> None:
        adapter, _ = _make()
        result = await adapter.set_precision("low", issued_by="op", confirmed_by="op")
        assert result.accepted is False
        assert "not open" in result.detail

    async def test_set_precision_changes_the_measure_command(self) -> None:
        adapter, fake = _make(rate_hz=10.0)
        await adapter.open()
        result = await adapter.set_precision("LOW", issued_by="op", confirmed_by="op")
        assert result.accepted and "precision='low'" in result.detail
        measurement = await adapter.read_once()
        assert measurement.precision == "low"
        assert fake.sensor_commands[-1] == 0xE0
        snapshot = await adapter.read_state_snapshot()
        assert snapshot is not None and snapshot.precision == "low"
        with pytest.raises(AdapterError, match="unknown precision"):
            await adapter.set_precision("ultra", issued_by="op", confirmed_by="op")

    async def test_heater_pulse_reports_reading_queues_event_and_holds_off(self) -> None:
        adapter, fake = _make(rate_hz=10.0)
        await adapter.open()
        await adapter.start(make_start_ctx())
        result = await adapter.heater_pulse(
            power_mw=20, duration_s=0.1, issued_by="op", confirmed_by="op"
        )
        assert result.accepted
        assert "end_of_pulse=27.50C" in result.detail
        assert fake.sensor_commands[-1] == 0x15
        again = await adapter.heater_pulse(
            power_mw=200, duration_s=1.0, issued_by="op", confirmed_by="op"
        )
        assert again.accepted is False
        assert "duty-cycle hold-off" in again.detail
        emissions = await _drain(adapter, max_records=1)
        _, _, _, events = _split(emissions)
        assert len(events) == 1
        assert events[0].kind == "heater_pulse"
        assert events[0].metadata["power_mw"] == 20
        snapshot = await adapter.read_state_snapshot()
        assert snapshot is not None and snapshot.heater_pulses == 1
        assert snapshot.seconds_since_heater_pulse is not None

    async def test_heater_refused_above_ambient_limit(self) -> None:
        adapter, fake = _make()
        fake.temperature_c = 70.0
        await adapter.open()
        await adapter.read_once()
        result = await adapter.heater_pulse(
            power_mw=20, duration_s=0.1, issued_by="op", confirmed_by="op"
        )
        assert result.accepted is False
        assert "above the 65 °C limit" in result.detail
        assert 0x15 not in fake.sensor_commands

    async def test_heater_payload_validation(self) -> None:
        adapter, _ = _make()
        await adapter.open()
        with pytest.raises(AdapterError, match="no 50 mW"):
            await adapter.heater_pulse(
                power_mw=50, duration_s=0.1, issued_by="op", confirmed_by="op"
            )
        with pytest.raises(AdapterError, match="needs power_mw"):
            await adapter.command(
                DeviceCommand(kind="heater_pulse", payload={}, issued_by="op", confirmed_by="op")
            )

    async def test_soft_reset_and_unknown_kind(self) -> None:
        adapter, fake = _make()
        await adapter.open()
        result = await adapter.soft_reset(issued_by="op", confirmed_by="op")
        assert result.accepted and "0x1643e746" in result.detail
        assert fake.sensor_commands[-2:] == [0x94, 0x89]
        with pytest.raises(AdapterError, match="unknown command kind"):
            await adapter.command(DeviceCommand(kind="levitate", issued_by="op", confirmed_by="op"))

    async def test_command_failure_surfaces_as_adapter_error(self) -> None:
        adapter, fake = _make()
        await adapter.open()
        fake.fail_hid = OSError("gone")
        with pytest.raises(AdapterError, match="command 'soft_reset' failed"):
            await adapter.soft_reset(issued_by="op", confirmed_by="op")


# ---------------------------------------------------------------------------
# Read-back and declarative settings
# ---------------------------------------------------------------------------


class TestReadback:
    async def test_state_snapshot(self) -> None:
        adapter, fake = _make()
        assert await adapter.read_state_snapshot() is None
        await adapter.open()
        snapshot = await adapter.read_state_snapshot()
        assert snapshot == Sht4xStateSnapshot(
            precision="high",
            sensor_serial="0x1643e746",
            bridge_serial=None,
            bridge_firmware="A6 / 1.2",
        )
        fake.temperature_c = 19.0
        await adapter.read_once()
        snapshot = await adapter.read_state_snapshot()
        assert snapshot is not None
        assert snapshot.last_temperature_c == pytest.approx(19.0, abs=0.01)

    async def test_read_once_requires_open_and_wraps_errors(self) -> None:
        adapter, fake = _make()
        with pytest.raises(AdapterError, match="not open"):
            await adapter.read_once()
        await adapter.open()
        fake.corrupt_next = True
        with pytest.raises(AdapterError, match="read failed"):
            await adapter.read_once()

    def test_settings_plan_changes_precision_only_when_it_differs(self) -> None:
        snapshot = Sht4xStateSnapshot(precision="high")
        changes, issues = plan_settings(SHT4X_SETTINGS, Sht4xSettings(precision="low"), snapshot)
        assert not issues
        assert [(c.kind, dict(c.payload)) for c in changes] == [
            ("set_precision", {"precision": "low"})
        ]
        changes, _ = plan_settings(SHT4X_SETTINGS, Sht4xSettings(precision="high"), snapshot)
        assert changes == ()
        changes, _ = plan_settings(SHT4X_SETTINGS, Sht4xSettings(), snapshot)
        assert changes == ()


# ---------------------------------------------------------------------------
# Handshake and discovery hooks
# ---------------------------------------------------------------------------


class TestHooks:
    async def test_handshake_summary(self) -> None:
        fake = FakeSht4xHid(temperature_c=21.85, humidity_pct=64.8)
        opener = opener_for(fake)

        def bus_factory(parsed: Sht4xAdapterParams) -> Mcp2221I2cBus:
            return Mcp2221I2cBus(opener=opener, transfer_timeout_s=0.02)

        summary = await handshake({"rate_hz": 1.0}, bus_factory=bus_factory)
        assert summary.startswith("sht4x serial=0x1643e746 addr=0x44 bridge=MCP2221 rev=A6 / 1.2")
        assert "T=21.85C" in summary
        assert fake.closed

    async def test_discover_probes_every_address_on_every_bridge(self) -> None:
        fake = FakeSht4xHid(address=0x45, serial=0xABCD1234)
        opener = opener_for(fake)
        bridges = [BridgeInfo(path=b"hid-path-1", serial=None, product="MCP2221", manufacturer="M")]
        rows = await discover(bridges=lambda: bridges, opener=opener)
        assert rows == [
            {
                "adapter": ADAPTER_ID,
                "serial": None,
                "path": "hid-path-1",
                "i2c_address": 0x45,
                "sensor_serial": "0xabcd1234",
            }
        ]
        assert opener.selectors[0].path == b"hid-path-1"
        assert fake.closed

    async def test_discover_skips_bridges_it_cannot_open(self) -> None:
        opener = opener_for(FakeSht4xHid())
        opener.fail_with = sht4x_module.Mcp2221Error("held by another process")
        bridges = [BridgeInfo(path=b"p", serial="0006774894", product=None, manufacturer=None)]
        assert await discover(bridges=lambda: bridges, opener=opener) == []
