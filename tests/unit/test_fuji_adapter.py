"""Unit tests for :class:`capa.devices.fuji.FujiAdapter`.

Most tests drive the real adapter against fujilib's simulated analyzer
(``fujilib.testing.MockAnalyzer`` on an in-process serial pair), so the
analyzer object, its identity and its settings are the library's own. Tests
that need a scripted sequence of polls (an outage, an undecodable value)
replace the library's recorder with one that yields prepared samples built
from ``fujilib.testing.frames``.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import fujilib
import pytest
from fujilib import (
    Analyzer,
    ChannelId,
    FujiConnectionError,
    FujiModbusTimeoutError,
    Gas,
    ProtocolKind,
    ReadingState,
    Sample,
    sample_to_row,
)
from fujilib.registry.units import Unit
from fujilib.sinks.base import row_columns
from fujilib.testing import DEFAULT_ZPA_BANK, MockAnalyzer, mock_transport
from fujilib.testing import frames as build

from capa.channels.calibration import Identity
from capa.channels.spec import ChannelKind, ChannelSpec, FujiChannel
from capa.core.errors import AdapterError
from capa.devices import fuji
from capa.devices.adapter import Capability, DeviceCommand
from capa.devices.fuji import ADAPTER_ID, FujiAdapter, FujiAdapterParams
from capa.devices.records import (
    ChannelSample,
    DeviceEvent,
    DeviceSnapshot,
    SourceRecord,
)
from tests._adapter_helpers import make_start_ctx

pytestmark = pytest.mark.anyio

CHANNEL_MAP = {"CH1": "co2", "CH2": "co", "CH3": "o2"}
CHANNELS = (ChannelId.CH1, ChannelId.CH2, ChannelId.CH3)
ESC_KEY, ENT_KEY, ZERO_KEY = 0x10, 0x20, 0x40


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _channel(
    name: str, channel: str, *, field: str = "value", unit: str = "percent"
) -> ChannelSpec:
    return ChannelSpec(
        name=name,
        kind=ChannelKind.GAS_CONCENTRATION,
        source=FujiChannel(device="analyzer", channel=channel, field=field),  # type: ignore[arg-type]
        unit=unit,
        derived_unit=unit,
        calibration=Identity(input_unit=unit, output_unit=unit),
    )


def _gas_channels() -> list[ChannelSpec]:
    return [
        _channel("gas.co2", "CH1"),
        _channel("gas.co", "CH2"),
        _channel("gas.o2", "CH3"),
        _channel("gas.o2_valid", "CH3", field="valid", unit="dimensionless"),
    ]


@asynccontextmanager
async def _adapter_on(
    mock: MockAnalyzer,
    *,
    channels: list[ChannelSpec] | None = None,
    **params: Any,
) -> AsyncIterator[FujiAdapter]:
    """A :class:`FujiAdapter`, opened, whose analyzer is on a simulated line."""
    async with mock_transport(mock) as (transport, _line):

        async def factory() -> Analyzer:
            return await fujilib.open_device(transport, channel_map=CHANNEL_MAP, timeout=0.25)

        adapter = FujiAdapter(
            name="analyzer",
            port="mock://zp",
            channel_map=CHANNEL_MAP,
            rate_hz=5.0,
            snapshot_period_s=1e6,
            analyzer_factory=factory,
            **params,
        )
        adapter.configure_channels(channels if channels is not None else _gas_channels())
        await adapter.open()
        try:
            yield adapter
        finally:
            await adapter.close()


def _split(
    emissions: list[Any],
) -> tuple[list[SourceRecord], list[ChannelSample], list[DeviceSnapshot], list[DeviceEvent]]:
    return (
        [e for e in emissions if isinstance(e, SourceRecord)],
        [e for e in emissions if isinstance(e, ChannelSample)],
        [e for e in emissions if isinstance(e, DeviceSnapshot)],
        [e for e in emissions if isinstance(e, DeviceEvent)],
    )


async def _drain(adapter: FujiAdapter, *, max_records: int, on_record: Any = None) -> list[Any]:
    emissions: list[Any] = []
    count = 0
    async for emission in adapter.stream():
        emissions.append(emission)
        if isinstance(emission, SourceRecord):
            count += 1
            if on_record is not None:
                on_record(count)
            if count >= max_records:
                await adapter.stop()
    return emissions


def _now_timing(offset_ms: float = 0.0) -> Any:
    return build.timing(offset_ms, origin=datetime.now(UTC), mono_origin_ns=time.monotonic_ns())


def _sample(*readings: Any, offset_ms: float = 0.0, **frame_kwargs: Any) -> Sample:
    frame = build.frame(
        readings or None,
        readings_timing=_now_timing(offset_ms),
        status_timing=_now_timing(offset_ms + 25.0),
        **frame_kwargs,
    )
    return Sample.from_frame(frame, device="analyzer", address=1, channels=CHANNELS)


def _error_sample(error: fujilib.FujiError, offset_ms: float = 0.0) -> Sample:
    return Sample.from_error(
        error,
        device="analyzer",
        address=1,
        protocol=ProtocolKind.MODBUS_RTU,
        timing=_now_timing(offset_ms),
        channels=CHANNELS,
    )


def _script(monkeypatch: pytest.MonkeyPatch, samples: list[Sample]) -> dict[str, Any]:
    """Replace fujilib's recorder with one that yields ``samples``, then idles."""
    seen: dict[str, Any] = {}

    class _Recording:
        def __init__(self) -> None:
            self.stream = self._batches()

        async def _batches(self) -> AsyncIterator[dict[str, Sample]]:
            for sample in samples:
                yield {"analyzer": sample}

    @asynccontextmanager
    async def fake_record(source: Any, **kwargs: Any) -> AsyncIterator[_Recording]:
        seen.update(kwargs, source=source)
        yield _Recording()

    monkeypatch.setattr(fuji, "fuji_record", fake_record)
    return seen


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_engine_kwargs_path(self) -> None:
        adapter = FujiAdapter(name="zpa", port="COM8", channel_map=CHANNEL_MAP, rate_hz=2.0)
        assert adapter.name == "zpa"
        assert adapter.params.rate_hz == 2.0
        assert adapter.params.address == 1
        assert adapter.resource_id == "serial:COM8"

    def test_params_path(self) -> None:
        params = FujiAdapterParams(port="com8", channel_map={"ch3": "O2"})
        adapter = FujiAdapter(name="zpa", params=params)
        assert adapter.params.channel_map == {"CH3": "o2"}
        assert adapter.params.gases() == {ChannelId.CH3: Gas.O2}
        assert adapter.resource_id == "serial:COM8"

    def test_params_and_kwargs_together_are_refused(self) -> None:
        params = FujiAdapterParams(port="COM8", channel_map=CHANNEL_MAP)
        with pytest.raises(TypeError, match="either"):
            FujiAdapter(name="zpa", params=params, port="COM9")

    def test_capabilities(self) -> None:
        on = FujiAdapter(name="zpa", port="COM8", channel_map=CHANNEL_MAP)
        off = FujiAdapter(name="zpa", port="COM8", channel_map=CHANNEL_MAP, auto_reconnect=False)
        assert Capability.READS_PROCESS_VAR in on.capabilities
        assert Capability.SUPPORTS_AUTO_RECONNECT in on.capabilities
        assert Capability.SUPPORTS_AUTO_RECONNECT not in off.capabilities

    def test_emission_rate_counts_the_bound_channels(self) -> None:
        adapter = FujiAdapter(name="analyzer", port="COM8", channel_map=CHANNEL_MAP, rate_hz=2.0)
        assert adapter.expected_emission_rate_hz == 2.0
        adapter.configure_channels(_gas_channels())
        assert adapter.expected_emission_rate_hz == 2.0 * 5

    @pytest.mark.parametrize(
        "params",
        [
            {"channel_map": {}},
            {"channel_map": {"CH13": "o2"}},
            {"channel_map": {"CH1": "helium"}},
            {"channel_map": {"CH1": "unknown"}},
            {"channel_map": CHANNEL_MAP, "address": 0},
            {"channel_map": CHANNEL_MAP, "address": 32},
            {"channel_map": CHANNEL_MAP, "rate_hz": 0},
            {"channel_map": CHANNEL_MAP, "rate_hz": 6},
            {"channel_map": CHANNEL_MAP, "options": ["clock"]},
            {"channel_map": CHANNEL_MAP, "options": ["warp_drive"]},
            {"channel_map": CHANNEL_MAP, "made_up": 1},
        ],
    )
    def test_bad_params_are_refused(self, params: dict[str, Any]) -> None:
        with pytest.raises(Exception):
            FujiAdapterParams(port="COM8", **params)

    def test_options_become_fujilib_flags(self) -> None:
        params = FujiAdapterParams(
            port="COM8", channel_map=CHANNEL_MAP, options=["Auto_Calibration", "auto_zero"]
        )
        assert params.options == ("auto_calibration", "auto_zero")
        assert params.option_flags() == (
            fujilib.Capability.AUTO_CALIBRATION | fujilib.Capability.AUTO_ZERO
        )
        assert FujiAdapterParams(port="COM8", channel_map=CHANNEL_MAP).option_flags() == (
            fujilib.Capability.NONE
        )

    def test_descriptor(self) -> None:
        descriptor = fuji.DESCRIPTOR
        assert descriptor.id == "capa.devices.fuji"
        assert descriptor.family == "fuji"
        assert descriptor.supported_binding_sources == ("fuji_channel",)
        assert descriptor.discoverable
        assert descriptor.handshake_available
        assert [t.id for t in descriptor.channel_templates] == ["fuji.co2", "fuji.co", "fuji.o2"]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_open_identifies_and_reads_the_settings(self) -> None:
        async with _adapter_on(MockAnalyzer(DEFAULT_ZPA_BANK)) as adapter:
            info = adapter.device_info
            assert info is not None
            assert (info.model, info.serial_number) == ("ZPA", "N8A0259T")
            assert adapter.metadata is not None
            await adapter.open()  # idempotent
            assert adapter.device_info is info
        await adapter.close()  # idempotent

    async def test_open_failure_is_an_adapter_error(self) -> None:
        async def factory() -> Analyzer:
            raise FujiConnectionError("no such port")

        adapter = FujiAdapter(
            name="analyzer", port="COM99", channel_map=CHANNEL_MAP, analyzer_factory=factory
        )
        with pytest.raises(AdapterError, match="open failed") as info:
            await adapter.open()
        assert info.value.device == "analyzer"
        assert adapter.device_info is None
        await adapter.close()

    async def test_stream_needs_open_and_start(self) -> None:
        adapter = FujiAdapter(name="analyzer", port="COM8", channel_map=CHANNEL_MAP)
        with pytest.raises(AdapterError, match="requires open"):
            _ = [e async for e in adapter.stream()]
        async with _adapter_on(MockAnalyzer(DEFAULT_ZPA_BANK)) as opened:
            with pytest.raises(AdapterError, match="requires start"):
                _ = [e async for e in opened.stream()]

    async def test_command_gate(self) -> None:
        adapter = FujiAdapter(name="analyzer", port="COM8", channel_map=CHANNEL_MAP)
        unauthorized = await adapter.command(DeviceCommand(kind="anything", issued_by="op"))
        assert not unauthorized.accepted
        assert "unauthorized" in unauthorized.detail
        closed = await adapter.command(
            DeviceCommand(kind="anything", issued_by="op", confirmed_by="op")
        )
        assert not closed.accepted
        assert "not open" in closed.detail
        async with _adapter_on(MockAnalyzer(DEFAULT_ZPA_BANK)) as opened:
            with pytest.raises(AdapterError, match="unknown command kind"):
                await opened.command(
                    DeviceCommand(kind="anything", issued_by="op", confirmed_by="op")
                )


# ---------------------------------------------------------------------------
# Stream: against the simulated analyzer
# ---------------------------------------------------------------------------


class TestStream:
    async def test_records_and_channel_samples(self) -> None:
        async with _adapter_on(MockAnalyzer(DEFAULT_ZPA_BANK)) as adapter:
            await adapter.start(make_start_ctx())
            emissions = await _drain(adapter, max_records=3)
        records, samples, snapshots, events = _split(emissions)

        assert isinstance(emissions[0], DeviceSnapshot)
        assert len(snapshots) == 1
        assert len(records) == 3
        assert events == []
        record = records[0]
        assert (record.adapter, record.device, record.shape) == (ADAPTER_ID, "analyzer", "wide_row")
        assert record.record_id == "fuji:analyzer:1"
        assert record.metadata == {"address": 1}
        assert record.t_mono_ns >= 0
        # The row is fujilib's own: the columns of an offline fujilib recording.
        assert list(record.row) == [c.name for c in row_columns(CHANNELS)]
        assert record.row["error_type"] is None

        by_channel = {s.channel: s for s in samples if s.source_record_id == record.record_id}
        assert set(by_channel) == {"gas.co2", "gas.co", "gas.o2", "gas.o2_valid"}
        o2 = by_channel["gas.o2"]
        assert o2.value == record.row["ch3_value"]
        assert (o2.unit, o2.status, o2.source_field) == ("percent", "ok", "ch3_value")
        assert o2.t_mono_ns == record.t_mono_ns
        valid = by_channel["gas.o2_valid"]
        assert (valid.value, valid.source_field, valid.status) == (1.0, "ch3_valid", "ok")
        assert record.row["ch3_valid"] is True

    async def test_a_recording_without_a_reopenable_port_has_no_reconnect_policy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _script(monkeypatch, [_sample()])
        async with _adapter_on(MockAnalyzer(DEFAULT_ZPA_BANK), overflow="drop_newest") as adapter:
            await adapter.start(make_start_ctx())
            _ = await _drain(adapter, max_records=1)
        # The simulated line is a transport the test supplied, which fujilib
        # cannot reopen; a port opened by name gets the policy.
        assert seen["reconnect"] is None
        assert seen["rate_hz"] == 5.0
        assert seen["overflow"] is fujilib.OverflowPolicy.DROP_NEWEST

    async def test_hold_is_a_status_a_validity_and_an_event(self) -> None:
        mock = MockAnalyzer(DEFAULT_ZPA_BANK)

        def on_record(count: int) -> None:
            if count == 2:
                mock.set_register("status.ch3.hold", 1)
            if count == 5:
                mock.set_register("status.ch3.hold", 0)

        async with _adapter_on(mock) as adapter:
            await adapter.start(make_start_ctx())
            emissions = await _drain(adapter, max_records=8, on_record=on_record)
        _records, samples, _snapshots, events = _split(emissions)

        statuses = [s.status for s in samples if s.channel == "gas.o2"]
        assert statuses[0] == "ok"
        assert "hold" in statuses
        assert statuses[-1] == "ok"
        validity = [s.value for s in samples if s.channel == "gas.o2_valid"]
        assert validity[0] == 1.0
        assert 0.0 in validity
        assert validity[-1] == 1.0
        assert {s.status for s in samples if s.channel == "gas.co2"} == {"ok"}
        holds = [e for e in events if e.kind == "hold"]
        assert [(e.metadata["active"], e.metadata["channels"], e.severity) for e in holds] == [
            (True, "CH3", "warning"),
            (False, "", "info"),
        ]

    async def test_an_instrument_error_is_reported_once_each_way(self) -> None:
        mock = MockAnalyzer(DEFAULT_ZPA_BANK)

        def on_record(count: int) -> None:
            if count == 2:
                mock.set_register("status.instrument_error", 1)
                mock.set_register("error.e1.active", 1)
            if count == 4:
                mock.set_register("status.instrument_error", 0)
                mock.set_register("error.e1.active", 0)

        async with _adapter_on(mock) as adapter:
            await adapter.start(make_start_ctx())
            emissions = await _drain(adapter, max_records=7, on_record=on_record)
        _records, samples, _snapshots, events = _split(emissions)
        errors = [e for e in events if e.kind == "instrument_error"]
        assert [(e.metadata["active"], e.metadata["errors"], e.severity) for e in errors] == [
            (True, "1", "error"),
            (False, "", "info"),
        ]
        assert "analyzer_error" in {s.status for s in samples if s.channel == "gas.o2"}

    async def test_a_unit_mismatch_quarantines_the_channel_once(self) -> None:
        channels = [_channel("gas.co2", "CH1", unit="ppm"), _channel("gas.o2", "CH3")]
        async with _adapter_on(MockAnalyzer(DEFAULT_ZPA_BANK), channels=channels) as adapter:
            await adapter.start(make_start_ctx())
            emissions = await _drain(adapter, max_records=3)
            _records, samples, _snapshots, events = _split(emissions)
            assert {s.channel for s in samples} == {"gas.o2"}
            mismatches = [e for e in events if e.kind == "unit_mismatch"]
            assert len(mismatches) == 1
            event = mismatches[0]
            assert event.severity == "error"
            assert event.metadata == {
                "channel": "gas.co2",
                "analyzer_channel": "CH1",
                "declared_unit": "ppm",
                "wire_unit": "vol%",
            }
            # A new run checks again.
            await adapter.start(make_start_ctx())
            again = await _drain(adapter, max_records=1)
        assert len([e for e in again if isinstance(e, DeviceEvent)]) == 1

    async def test_a_calibration_at_the_panel_is_an_event_with_its_record(self) -> None:
        mock = MockAnalyzer(dataclasses.replace(DEFAULT_ZPA_BANK, panel_channels=(1, 2, 3)))
        keys = {2: ZERO_KEY, 4: ENT_KEY, 6: ESC_KEY}

        def on_record(count: int) -> None:
            if count in keys:
                mock.press(keys[count])

        async with _adapter_on(mock) as adapter:
            await adapter.start(make_start_ctx())
            emissions = await _drain(adapter, max_records=10, on_record=on_record)
        _records, samples, _snapshots, events = _split(emissions)
        calibrations = [e for e in events if e.kind == "calibration"]
        assert len(calibrations) == 1
        event = calibrations[0]
        assert (event.metadata["kind"], event.metadata["outcome"]) == ("zero", "cancelled")
        assert event.severity == "info"
        record = event.metadata["record"]
        assert record["format"] == "fujilib-calibration/1"
        assert record["source"] == "panel"
        assert record["analyzer"]["serial_number"] == "N8A0259T"
        # While the channel waited for its zero gas, its readings said so.
        assert "calibrating" in {s.status for s in samples}

    async def test_label_disagreement_is_a_warning_not_a_quarantine(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ = _script(monkeypatch, [_sample()])
        async with _adapter_on(MockAnalyzer(DEFAULT_ZPA_BANK)) as adapter:
            info = adapter.device_info
            assert info is not None
            # The bench unit's type code does not list O2 on CH3; suggest CO there.
            channels = tuple(
                dataclasses.replace(c, suggested_gas=Gas.CO) if c.channel is ChannelId.CH3 else c
                for c in info.channels
            )
            adapter._device_info = dataclasses.replace(info, channels=channels)
            await adapter.start(make_start_ctx())
            emissions = await _drain(adapter, max_records=1)
        _records, samples, _snapshots, events = _split(emissions)
        warnings = [e for e in events if e.kind == "label_disagreement"]
        assert [(e.metadata["channel"], e.metadata["asserted"], e.severity) for e in warnings] == [
            ("CH3", "o2", "warning")
        ]
        assert "gas.o2" in {s.channel for s in samples}


# ---------------------------------------------------------------------------
# Stream: scripted polls
# ---------------------------------------------------------------------------


class TestScriptedStream:
    async def test_a_failed_poll_is_a_record_without_samples(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        timeout = FujiModbusTimeoutError("no reply")
        script = [
            _sample(),
            _error_sample(timeout, 1000.0),
            _error_sample(timeout, 2000.0),
            _sample(offset_ms=3000.0),
        ]
        _ = _script(monkeypatch, script)
        async with _adapter_on(MockAnalyzer(DEFAULT_ZPA_BANK)) as adapter:
            await adapter.start(make_start_ctx())
            emissions = await _drain(adapter, max_records=4)
            state = adapter.watchdog_state()
        records, samples, _snapshots, events = _split(emissions)

        assert len(records) == 4
        # Error rows keep the keys of a good row, so the device-records schema holds.
        assert [list(r.row) for r in records] == [list(records[0].row)] * 4
        assert records[1].row["error_type"] == "fujilib.errors.FujiModbusTimeoutError"
        assert records[1].row["ch3_value"] is None
        by_record = {r.record_id: 0 for r in records}
        for sample in samples:
            assert sample.source_record_id is not None
            by_record[sample.source_record_id] += 1
        assert list(by_record.values()) == [4, 0, 0, 4]

        kinds = [e.kind for e in events]
        assert kinds == ["comm_lost", "comm_restored"]
        lost, restored = events
        assert (lost.severity, lost.metadata["error_type"]) == ("warning", "FujiModbusTimeoutError")
        assert restored.metadata["failed_polls"] == 2
        assert restored.metadata["outage_s"] == pytest.approx(2.0, abs=0.2)
        # Only good polls count as emissions for the staleness check.
        assert state.last_t_mono_ns == records[3].t_mono_ns

    async def test_a_failed_poll_ends_the_stream_without_auto_reconnect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ = _script(monkeypatch, [_sample(), _error_sample(FujiModbusTimeoutError("no reply"))])
        async with _adapter_on(MockAnalyzer(DEFAULT_ZPA_BANK), auto_reconnect=False) as adapter:
            await adapter.start(make_start_ctx())
            emissions: list[Any] = []
            with pytest.raises(AdapterError, match="auto_reconnect is disabled"):
                async for emission in adapter.stream():
                    emissions.append(emission)
        records, _samples, _snapshots, _events = _split(emissions)
        assert len(records) == 2  # the error row is still delivered

    async def test_a_recorder_failure_is_an_adapter_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        @asynccontextmanager
        async def failing_record(source: Any, **kwargs: Any) -> AsyncIterator[Any]:
            del source, kwargs
            raise FujiConnectionError("the port is gone")
            yield

        monkeypatch.setattr(fuji, "fuji_record", failing_record)
        async with _adapter_on(MockAnalyzer(DEFAULT_ZPA_BANK)) as adapter:
            await adapter.start(make_start_ctx())
            with pytest.raises(AdapterError, match="stream failed") as info:
                _ = [e async for e in adapter.stream()]
        assert isinstance(info.value.__cause__, FujiConnectionError)

    async def test_values_that_do_not_decode_and_unknown_validity_yield_no_sample(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        co2 = build.reading(ChannelId.CH1, Gas.CO2, 5, 2)
        co = build.reading(ChannelId.CH2, Gas.CO, 7, 3)
        o2 = dataclasses.replace(
            build.reading(ChannelId.CH3, Gas.O2, 2095, 2), value=None, state=ReadingState.UNKNOWN
        )
        _ = _script(monkeypatch, [_sample(co2, co, o2)])
        async with _adapter_on(MockAnalyzer(DEFAULT_ZPA_BANK)) as adapter:
            await adapter.start(make_start_ctx())
            emissions = await _drain(adapter, max_records=1)
        records, samples, _snapshots, _events = _split(emissions)
        assert {s.channel for s in samples} == {"gas.co2", "gas.co"}
        assert records[0].row["ch3_value"] is None
        assert records[0].row["ch3_valid"] is None

    async def test_settling_and_other_states_travel_as_the_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        co2 = build.reading(ChannelId.CH1, Gas.CO2, 5, 2, state=ReadingState.SETTLING)
        co = build.reading(ChannelId.CH2, Gas.CO, 7, 3, state=ReadingState.CALIBRATING)
        o2 = build.reading(ChannelId.CH3, Gas.O2, 2095, 2, state=ReadingState.SETTLING)
        _ = _script(monkeypatch, [_sample(co2, co, o2)])
        async with _adapter_on(MockAnalyzer(DEFAULT_ZPA_BANK)) as adapter:
            await adapter.start(make_start_ctx())
            emissions = await _drain(adapter, max_records=1)
        _records, samples, _snapshots, _events = _split(emissions)
        by_channel = {s.channel: s for s in samples}
        assert by_channel["gas.co2"].status == "settling"
        assert by_channel["gas.co"].status == "calibrating"
        assert (by_channel["gas.o2"].value, by_channel["gas.o2"].status) == (20.95, "settling")
        assert by_channel["gas.o2_valid"].value == 0.0

    async def test_a_ppm_reading_matches_a_ppm_channel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        co = build.reading(ChannelId.CH2, Gas.CO, 125, 0, unit=Unit.PPM)
        _ = _script(monkeypatch, [_sample(build.reading(ChannelId.CH1, Gas.CO2, 5, 2), co)])
        channels = [_channel("gas.co", "CH2", unit="ppm"), _channel("gas.co2", "CH1")]
        async with _adapter_on(MockAnalyzer(DEFAULT_ZPA_BANK), channels=channels) as adapter:
            await adapter.start(make_start_ctx())
            emissions = await _drain(adapter, max_records=1)
        _records, samples, _snapshots, events = _split(emissions)
        assert {s.channel: (s.value, s.unit) for s in samples} == {
            "gas.co": (125.0, "ppm"),
            "gas.co2": (0.05, "percent"),
        }
        assert events == []

    async def test_a_channel_missing_from_the_frame_yields_no_sample(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ = _script(monkeypatch, [_sample(build.reading(ChannelId.CH1, Gas.CO2, 5, 2))])
        async with _adapter_on(MockAnalyzer(DEFAULT_ZPA_BANK)) as adapter:
            await adapter.start(make_start_ctx())
            emissions = await _drain(adapter, max_records=1)
        _records, samples, _snapshots, _events = _split(emissions)
        assert {s.channel for s in samples} == {"gas.co2"}

    async def test_the_row_matches_fujilibs_own(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sample = _sample()
        _ = _script(monkeypatch, [sample])
        async with _adapter_on(MockAnalyzer(DEFAULT_ZPA_BANK)) as adapter:
            ctx = make_start_ctx()
            await adapter.start(ctx)
            emissions = await _drain(adapter, max_records=1)
        records, _samples, _snapshots, _events = _split(emissions)
        assert records[0].row == sample_to_row(sample)
        assert records[0].t_mono_ns == sample.t_mono_ns - ctx.clock.started_mono_ns
        assert records[0].t_utc == sample.t_utc


# ---------------------------------------------------------------------------
# Snapshot and health
# ---------------------------------------------------------------------------


class TestSnapshot:
    async def test_fields(self) -> None:
        async with _adapter_on(MockAnalyzer(DEFAULT_ZPA_BANK)) as adapter:
            snap = await adapter.snapshot()
        fields = snap.fields
        assert (snap.adapter, snap.device, snap.health) == (ADAPTER_ID, "analyzer", "ok")
        assert fields["model"] == "ZPA"
        assert fields["serial"] == "N8A0259T"
        assert fields["type_code"] == "ZPACBJY1MPFYYYYYY2DEYAYAY0"
        assert fields["channel_map"] == "CH1=co2, CH2=co, CH3=o2"
        assert fields["address"] == 1
        assert fields["channel_count"] == 4
        assert fields["connected"] is True
        assert fields["settling_until"] is None
        # The settings in force, flattened: what a run needs to interpret its data.
        assert fields["response_time_o2_s"] == 15
        assert fields["ch3_unit"] == "vol%"
        assert fields["ch3_range"] in (1, 2)
        assert isinstance(fields["ch3_full_scale"], float)
        assert "ch1_range1_span_gas" in fields
        assert fields["output_hold"] in (True, False)
        assert isinstance(fields["metadata_captured_at"], str)
        for value in fields.values():
            assert value is None or isinstance(value, float | int | str | bool)

    async def test_health_follows_the_lifecycle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter = FujiAdapter(name="analyzer", port="COM8", channel_map=CHANNEL_MAP)
        assert (await adapter.snapshot()).health == "down"
        _ = _script(monkeypatch, [_sample()])
        async with _adapter_on(MockAnalyzer(DEFAULT_ZPA_BANK)) as opened:
            assert (await opened.snapshot()).health == "ok"
            await opened.start(make_start_ctx())
            emissions = await _drain(opened, max_records=1)
        _records, _samples, snapshots, _events = _split(emissions)
        assert snapshots[0].health == "ok"
        assert (await opened.snapshot()).health == "down"

    async def test_settings_that_cannot_be_read_at_run_start_are_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ = _script(monkeypatch, [_sample()])
        async with _adapter_on(MockAnalyzer(DEFAULT_ZPA_BANK)) as adapter:
            before = adapter.metadata
            analyzer = adapter._analyzer
            assert analyzer is not None

            async def fail() -> Any:
                raise FujiModbusTimeoutError("no reply")

            monkeypatch.setattr(analyzer, "read_metadata", fail)
            await adapter.start(make_start_ctx())
            emissions = await _drain(adapter, max_records=1)
            assert adapter.metadata is before
        _records, _samples, _snapshots, events = _split(emissions)
        assert [(e.kind, e.severity) for e in events] == [("metadata_stale", "warning")]


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------


class TestHandshake:
    async def test_summary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async with mock_transport(MockAnalyzer(DEFAULT_ZPA_BANK)) as (transport, _line):
            real_open = fujilib.open_device
            seen: dict[str, Any] = {}

            async def fake_open(port: str, **kwargs: Any) -> Analyzer:
                seen.update(kwargs, port=port)
                return await real_open(transport, channel_map=kwargs["channel_map"], timeout=0.25)

            monkeypatch.setattr(fujilib, "open_device", fake_open)
            line = await fuji.handshake({"port": "COM8", "channel_map": CHANNEL_MAP})
        assert line == (
            "fuji model=ZPA serial=N8A0259T type_code=ZPACBJY1MPFYYYYYY2DEYAYAY0 station=1 "
            "channels=[CH1=co2, CH2=co, CH3=o2]"
        )
        assert (seen["port"], seen["address"], seen["timeout"]) == ("COM8", 1, 0.5)

    async def test_failure_is_an_adapter_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_open(port: str, **kwargs: Any) -> Analyzer:
            del port, kwargs
            raise FujiConnectionError("no such port")

        monkeypatch.setattr(fujilib, "open_device", fake_open)
        with pytest.raises(AdapterError, match="handshake failed at COM8"):
            await fuji.handshake({"port": "COM8", "channel_map": CHANNEL_MAP})
