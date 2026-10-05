"""Unit tests for :class:`capa.devices.sartorius.SartoriusAdapter`.

Drives the real adapter against an in-process ``StubBalance`` that duck-types
:class:`sartoriuslib.devices.balance.Balance`'s public surface.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from sartoriuslib.devices.models import CalRecord, ParameterEntry, Reading
from sartoriuslib.errors import SartoriusError
from sartoriuslib.protocol.base import ProtocolKind
from sartoriuslib.registry.aliases import (
    resolve_auto_zero,
    resolve_filter_mode,
    resolve_tare_behavior,
    resolve_unit,
)
from sartoriuslib.registry.modes import (
    AppFilter,
    AutoZeroMode,
    FilterMode,
    StabilityDelay,
    StabilityRange,
    TareBehavior,
)
from sartoriuslib.registry.units import Sign, Unit

from capa.channels.calibration import Identity
from capa.channels.spec import (
    ChannelKind,
    ChannelSpec,
    SartoriusReading,
)
from capa.core.errors import AdapterError
from capa.devices.adapter import Capability as CapaCapability
from capa.devices.records import (
    ChannelSample,
    DeviceSnapshot,
    SourceRecord,
)
from capa.devices.sartorius import (
    ADAPTER_ID,
    SartoriusAdapter,
    SartoriusAdapterParams,
    SartoriusStateSnapshot,
)
from tests._adapter_helpers import make_start_ctx

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Stub Balance — duck-types Balance for tests
# ---------------------------------------------------------------------------


class StubBalance:
    """Minimal duck-type of :class:`sartoriuslib.devices.balance.Balance`.

    Records every public-API call the adapter makes so tests can assert
    dispatch behaviour, including the new parameter-config / internal-cal
    surface.
    """

    def __init__(
        self,
        *,
        value: float = 1.234,
        stable: bool = True,
        overload: bool = False,
        underload: bool = False,
    ) -> None:
        self.value = value
        self.stable = stable
        self.overload = overload
        self.underload = underload
        self.poll_calls = 0
        self.tare_calls = 0
        self.zero_calls = 0
        self.close_calls = 0
        self.raise_on_poll: BaseException | None = None
        # Extended-control surface trackers (manual control panel
        # pathway). Each list captures the *kwargs the adapter forwarded* so
        # tests can assert both payload mapping and ``confirm=True`` pass-through.
        self.internal_adjust_calls: list[dict[str, Any]] = []
        self.set_filter_mode_calls: list[dict[str, Any]] = []
        self.set_display_unit_calls: list[dict[str, Any]] = []
        self.set_auto_zero_calls: list[dict[str, Any]] = []
        self.set_isocal_mode_calls: list[dict[str, Any]] = []
        self.set_tare_behavior_calls: list[dict[str, Any]] = []
        self.save_menu_calls: list[dict[str, Any]] = []
        self.reload_menu_calls: list[dict[str, Any]] = []
        self.write_parameter_calls: list[dict[str, Any]] = []
        self.last_cal_record_calls = 0
        # Menu state reported by the typed getters; a getter whose name is in
        # ``raise_on_get`` raises instead (parameter missing on this family).
        self.filter_mode = FilterMode.STABLE
        self.auto_zero = AutoZeroMode.ON
        self.display_unit = Unit.G
        self.tare_behavior = TareBehavior.WITH_STABILITY
        # Raw parameter table (p-index → wire byte) for the menu entries
        # sartoriuslib has no typed accessor for; ``raise_on_get`` names
        # them ``"p<index>"``.
        self.parameters: dict[int, int] = {
            2: AppFilter.FINAL_READING,
            3: StabilityRange.ACCURATE,
            4: StabilityDelay.SHORT,
        }
        self.cal_record = CalRecord(
            temperature_celsius=22.4,
            signature=b"\x01" * 9,
            counters=b"\x00\x02\x00",
            padding=0,
            raw=b"",
        )
        self.raise_on_get: set[str] = set()

        info = MagicMock()
        info.model = "MSE1203S"
        info.serial = "SN-BAL-001"
        info.manufacturer = "Sartorius"
        info.firmware = "v1.2.3"
        info.family = MagicMock()
        info.family.value = "MSE"
        info.protocol = ProtocolKind.XBPI
        info.software = "fw1"
        self.info = info
        # The adapter reads ``balance.session.recoverable_error_count`` for
        # health derivation under the unified API.
        session = MagicMock()
        session.recoverable_error_count = 0
        self.session = session

    async def snapshot(self) -> Any:
        # The adapter calls ``balance.snapshot()`` for identity+health.
        snap = MagicMock()
        snap.recoverable_error_count = self.session.recoverable_error_count
        snap.family = self.info.family
        snap.protocol = MagicMock()
        snap.protocol.value = "xBPI"
        return snap

    async def poll(self) -> Reading:
        self.poll_calls += 1
        if self.raise_on_poll is not None:
            exc = self.raise_on_poll
            self.raise_on_poll = None
            raise exc
        sign = Sign.POSITIVE if self.value > 0 else Sign.NEGATIVE if self.value < 0 else Sign.ZERO
        return Reading(
            value=self.value,
            unit=Unit.G,
            sign=sign,
            stable=self.stable,
            overload=self.overload,
            underload=self.underload,
            decimals=3,
            sequence=self.poll_calls,
            status_flags={},
            protocol=ProtocolKind.XBPI,
            received_at=datetime.now(UTC),
            monotonic_ns=time.monotonic_ns(),
            raw=b"",
        )

    async def tare(self) -> None:
        self.tare_calls += 1

    async def zero(self) -> None:
        self.zero_calls += 1

    async def internal_adjust(self, *, cal_type: int | None = None, confirm: bool = False) -> None:
        self.internal_adjust_calls.append({"cal_type": cal_type, "confirm": confirm})

    async def set_filter_mode(self, mode: Any, *, confirm: bool = False) -> None:
        self.set_filter_mode_calls.append({"mode": mode, "confirm": confirm})

    async def set_display_unit(self, unit: Any, *, confirm: bool = False) -> None:
        self.set_display_unit_calls.append({"unit": unit, "confirm": confirm})

    async def set_auto_zero(self, mode: Any, *, confirm: bool = False) -> None:
        self.set_auto_zero_calls.append({"mode": mode, "confirm": confirm})

    async def set_isocal_mode(self, mode: Any, *, confirm: bool = False) -> None:
        self.set_isocal_mode_calls.append({"mode": mode, "confirm": confirm})

    async def set_tare_behavior(self, mode: Any, *, confirm: bool = False) -> None:
        self.set_tare_behavior_calls.append({"mode": mode, "confirm": confirm})

    async def save_menu(self, *, confirm: bool = False) -> None:
        self.save_menu_calls.append({"confirm": confirm})

    async def reload_menu(self, *, confirm: bool = False) -> None:
        self.reload_menu_calls.append({"confirm": confirm})

    async def last_cal_record(self) -> CalRecord:
        self.last_cal_record_calls += 1
        self._maybe_raise("last_cal_record")
        return self.cal_record

    async def get_filter_mode(self) -> FilterMode:
        self._maybe_raise("filter_mode")
        return self.filter_mode

    async def get_auto_zero(self) -> AutoZeroMode:
        self._maybe_raise("auto_zero")
        return self.auto_zero

    async def get_display_unit(self) -> Unit:
        self._maybe_raise("display_unit")
        return self.display_unit

    async def get_tare_behavior(self) -> TareBehavior:
        self._maybe_raise("tare_behavior")
        return self.tare_behavior

    async def read_parameter(self, index: int) -> ParameterEntry:
        self._maybe_raise(f"p{index}")
        return ParameterEntry(index=index, current=self.parameters[index], max=6, raw=b"")

    async def write_parameter(self, index: int, value: int, *, confirm: bool = False) -> None:
        self.write_parameter_calls.append({"index": index, "value": value, "confirm": confirm})
        self.parameters[index] = value

    def _maybe_raise(self, what: str) -> None:
        if what in self.raise_on_get:
            raise SartoriusError(f"{what} not supported")

    async def close(self) -> None:
        self.close_calls += 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _channels_for_balance() -> list[ChannelSpec]:
    return [
        ChannelSpec(
            name="balance.mass",
            kind=ChannelKind.MASS,
            source=SartoriusReading(device="balance", field="value"),
            unit="g",
            derived_unit="g",
            calibration=Identity(input_unit="g", output_unit="g"),
        ),
    ]


def _make_adapter(
    *,
    name: str = "balance",
    rate_hz: float = 50.0,
    snapshot_period_s: float = 1e6,
    auto_reconnect: bool = True,
    stable: bool = True,
    value: float = 1.234,
    overload: bool = False,
) -> tuple[SartoriusAdapter, StubBalance]:
    stub = StubBalance(value=value, stable=stable, overload=overload)

    async def factory() -> Any:
        return stub

    adapter = SartoriusAdapter(
        name=name,
        port="fake://stub",
        rate_hz=rate_hz,
        snapshot_period_s=snapshot_period_s,
        auto_reconnect=auto_reconnect,
        balance_factory=factory,
    )
    adapter.configure_channels(_channels_for_balance())
    return adapter, stub


def _split(
    emissions: list[Any],
) -> tuple[list[SourceRecord], list[ChannelSample], list[DeviceSnapshot]]:
    return (
        [e for e in emissions if isinstance(e, SourceRecord)],
        [e for e in emissions if isinstance(e, ChannelSample)],
        [e for e in emissions if isinstance(e, DeviceSnapshot)],
    )


async def _drain(adapter: SartoriusAdapter, *, max_records: int) -> list[Any]:
    emissions: list[Any] = []
    record_count = 0
    async for emission in adapter.stream():
        emissions.append(emission)
        if isinstance(emission, SourceRecord):
            record_count += 1
            if record_count >= max_records:
                await adapter.stop()
    return emissions


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_engine_kwargs_path(self) -> None:
        adapter = SartoriusAdapter(name="bal", port="/dev/ttyUSB0", rate_hz=5.0)
        assert adapter.name == "bal"
        assert adapter.params.rate_hz == 5.0

    def test_capabilities(self) -> None:
        a = SartoriusAdapter(name="bal", port="/dev/null", auto_reconnect=False)
        assert CapaCapability.HAS_TARE in a.capabilities
        assert CapaCapability.HAS_ZERO in a.capabilities
        assert CapaCapability.EMITS_STABILITY_FLAG in a.capabilities
        assert CapaCapability.SUPPORTS_AUTO_RECONNECT not in a.capabilities

    def test_extra_forbidden(self) -> None:
        with pytest.raises(Exception):
            SartoriusAdapterParams(port="/dev/null", made_up=1)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_open_caches_info(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            assert adapter.device_info is stub.info
        finally:
            await adapter.close()
            assert stub.close_calls == 1


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


class TestStream:
    async def test_emits_record_and_channel_sample(self) -> None:
        adapter, _ = _make_adapter(value=2.5)
        await adapter.open()
        try:
            await adapter.start(make_start_ctx())
            emissions = await _drain(adapter, max_records=2)
        finally:
            await adapter.close()
        records, samples, snapshots = _split(emissions)
        assert len(records) >= 2
        assert all(r.shape == "single_value_row" for r in records)
        assert all(r.adapter == ADAPTER_ID for r in records)
        # one sample per record (single channel)
        per_tick = len(samples) // len(records)
        assert per_tick == 1
        for s in samples:
            assert s.channel == "balance.mass"
            assert s.value == pytest.approx(2.5)
            assert s.status == "ok"
        # initial DeviceSnapshot before first record
        assert snapshots and snapshots[0].health == "ok"

    async def test_unstable_status_propagated(self) -> None:
        adapter, _ = _make_adapter(value=2.5, stable=False)
        await adapter.open()
        try:
            await adapter.start(make_start_ctx())
            emissions = await _drain(adapter, max_records=2)
        finally:
            await adapter.close()
        _, samples, _ = _split(emissions)
        assert samples
        assert all(s.status == "settling" for s in samples)

    async def test_overload_propagated(self) -> None:
        adapter, _ = _make_adapter(value=999.0, overload=True)
        await adapter.open()
        try:
            await adapter.start(make_start_ctx())
            emissions = await _drain(adapter, max_records=2)
        finally:
            await adapter.close()
        _, samples, _ = _split(emissions)
        assert samples
        assert all(s.status == "overload" for s in samples)


# ---------------------------------------------------------------------------
# Authorization gate
# ---------------------------------------------------------------------------


class TestAuthorization:
    async def test_tare_without_auth_refused(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            result = await adapter.tare(issued_by="alice")
            assert result.accepted is False
            assert stub.tare_calls == 0  # never reached the device
        finally:
            await adapter.close()

    async def test_tare_with_manual_confirm(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            result = await adapter.tare(issued_by="alice", confirmed_by="alice")
            assert result.accepted is True
            assert stub.tare_calls == 1
        finally:
            await adapter.close()

    async def test_zero_with_auth(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            result = await adapter.zero(issued_by="alice", authorization_id="run-1")
            assert result.accepted is True
            assert stub.zero_calls == 1
        finally:
            await adapter.close()


# ---------------------------------------------------------------------------
# Extended control surface — internal cal, parameter writes, EEPROM persist
# ---------------------------------------------------------------------------


class TestControlSurfaceCapabilities:
    def test_advertises_internal_cal_and_parameter_config(self) -> None:
        a = SartoriusAdapter(name="bal", port="/dev/null", auto_reconnect=False)
        assert CapaCapability.HAS_INTERNAL_CAL in a.capabilities
        assert CapaCapability.HAS_PARAMETER_CONFIG in a.capabilities


class TestInternalAdjust:
    async def test_default_cal_type_passes_none(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            result = await adapter.internal_adjust(issued_by="alice", confirmed_by="alice")
            assert result.accepted is True
            assert len(stub.internal_adjust_calls) == 1
            assert stub.internal_adjust_calls[0]["cal_type"] is None
            # CAPA's authorization gate covers the library's confirm gate.
            assert stub.internal_adjust_calls[0]["confirm"] is True
        finally:
            await adapter.close()

    async def test_custom_cal_type(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            result = await adapter.internal_adjust(
                issued_by="alice", cal_type=0x71, confirmed_by="alice"
            )
            assert result.accepted is True
            assert stub.internal_adjust_calls[0]["cal_type"] == 0x71
        finally:
            await adapter.close()

    async def test_refused_without_authorization(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            result = await adapter.internal_adjust(issued_by="alice")
            assert result.accepted is False
            # Library never reached.
            assert stub.internal_adjust_calls == []
        finally:
            await adapter.close()


class TestParameterWrites:
    async def test_set_filter_mode_forwards_payload(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            result = await adapter.set_filter_mode(
                "very stable", issued_by="alice", confirmed_by="alice"
            )
            assert result.accepted is True
            assert stub.set_filter_mode_calls == [{"mode": "very stable", "confirm": True}]
        finally:
            await adapter.close()

    async def test_set_display_unit_int_payload(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            result = await adapter.set_display_unit(2, issued_by="alice", authorization_id="run-1")
            assert result.accepted is True
            assert stub.set_display_unit_calls == [{"unit": 2, "confirm": True}]
        finally:
            await adapter.close()

    async def test_set_auto_zero(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            result = await adapter.set_auto_zero("off", issued_by="alice", authorization_id="run-1")
            assert result.accepted is True
            assert stub.set_auto_zero_calls == [{"mode": "off", "confirm": True}]
        finally:
            await adapter.close()


class TestRawMenuParameterWrites:
    """p02–p04 have no typed sartoriuslib accessor; the adapter encodes them."""

    async def _send(self, adapter: SartoriusAdapter, kind: str, mode: object) -> Any:
        from capa.devices.adapter import DeviceCommand

        return await adapter.command(
            DeviceCommand(
                kind=kind, payload={"mode": mode}, issued_by="alice", confirmed_by="alice"
            )
        )

    @pytest.mark.parametrize(
        ("kind", "mode", "index", "wire"),
        [
            ("set_app_filter", "filling", 2, AppFilter.FILLING),
            ("set_stability_range", "very accurate", 3, StabilityRange.VERY_ACCURATE),
            ("set_stability_range", "MAX_FAST", 3, StabilityRange.MAX_FAST),
            ("set_stability_delay", 4, 4, StabilityDelay.LONG),
        ],
    )
    async def test_writes_encoded_parameter(
        self, kind: str, mode: object, index: int, wire: int
    ) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            result = await self._send(adapter, kind, mode)
            assert result.accepted is True
            assert stub.write_parameter_calls == [{"index": index, "value": wire, "confirm": True}]
        finally:
            await adapter.close()

    @pytest.mark.parametrize("mode", ["sticky", 0, 42])
    async def test_unknown_mode_is_refused_before_the_wire(self, mode: object) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            with pytest.raises(AdapterError, match="expected one of"):
                await self._send(adapter, "set_stability_delay", mode)
            assert stub.write_parameter_calls == []
        finally:
            await adapter.close()

    async def test_write_reads_back(self) -> None:
        adapter, _ = _make_adapter()
        await adapter.open()
        try:
            await self._send(adapter, "set_stability_range", "fast")
            snapshot = await adapter.read_state_snapshot()
            assert snapshot is not None
            assert snapshot.stability_range == "fast"
        finally:
            await adapter.close()


class TestMenuPersistence:
    async def test_save_menu(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            result = await adapter.save_menu(issued_by="alice", confirmed_by="alice")
            assert result.accepted is True
            assert stub.save_menu_calls == [{"confirm": True}]
        finally:
            await adapter.close()

    async def test_reload_menu(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            result = await adapter.reload_menu(issued_by="alice", confirmed_by="alice")
            assert result.accepted is True
            assert stub.reload_menu_calls == [{"confirm": True}]
        finally:
            await adapter.close()


class TestReadOnlyHelpers:
    async def test_read_last_cal_record_no_auth_required(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            # Pure read — no authorization needed (mirrors ``read_mass``).
            cal = await adapter.read_last_cal_record()
            assert cal is not None
            assert stub.last_cal_record_calls == 1
        finally:
            await adapter.close()

    async def test_read_last_cal_record_requires_open(self) -> None:
        adapter = SartoriusAdapter(name="bal", port="/dev/null")
        with pytest.raises(AdapterError, match="requires open"):
            await adapter.read_last_cal_record()


class TestReadStateSnapshot:
    async def test_reports_menu_settings_and_last_cal(self) -> None:
        adapter, _ = _make_adapter()
        await adapter.open()
        try:
            assert await adapter.read_state_snapshot() == SartoriusStateSnapshot(
                filter_mode="stable",
                app_filter="final reading",
                stability_range="accurate",
                stability_delay="short",
                auto_zero="on",
                display_unit="g",
                tare_behavior="with stability",
                cal_temperature_c=22.4,
                cal_on_record=True,
            )
        finally:
            await adapter.close()

    async def test_unsupported_and_unknown_values_are_none(self) -> None:
        adapter, stub = _make_adapter()
        stub.raise_on_get = {"tare_behavior", "last_cal_record", "p3"}
        stub.filter_mode = FilterMode.UNKNOWN
        stub.parameters[4] = 99  # a byte sartoriuslib doesn't model
        await adapter.open()
        try:
            snapshot = await adapter.read_state_snapshot()
            assert snapshot is not None
            assert snapshot.filter_mode is None
            assert snapshot.stability_range is None
            assert snapshot.stability_delay is None
            assert snapshot.app_filter == "final reading"
            assert snapshot.tare_behavior is None
            assert snapshot.cal_temperature_c is None
            assert snapshot.cal_on_record is None
            # One failed read doesn't hide the rest.
            assert snapshot.auto_zero == "on"
            assert snapshot.display_unit == "g"
        finally:
            await adapter.close()

    async def test_cold_boot_cal_record_is_not_on_record(self) -> None:
        adapter, stub = _make_adapter()
        stub.cal_record = CalRecord(
            temperature_celsius=21.0,
            signature=bytes(9),
            counters=bytes(3),
            padding=0,
            raw=b"",
        )
        await adapter.open()
        try:
            snapshot = await adapter.read_state_snapshot()
            assert snapshot is not None
            assert snapshot.cal_on_record is False
        finally:
            await adapter.close()

    async def test_returns_none_before_open(self) -> None:
        adapter = SartoriusAdapter(name="bal", port="/dev/null")
        assert await adapter.read_state_snapshot() is None


def test_balance_card_choices_resolve_and_match_snapshot_labels() -> None:
    """Every value the balance card offers must be one sartoriuslib accepts,
    and every mode the adapter can report must be one the card offers."""
    from capa.devices.sartorius import _encode_mode, _mode_label
    from capa.ui.manual.cards.balance import (
        APP_FILTERS,
        AUTO_ZERO_MODES,
        DISPLAY_UNITS,
        FILTER_MODES,
        STABILITY_DELAYS,
        STABILITY_RANGES,
        TARE_BEHAVIORS,
    )

    for choices, resolve in (
        (FILTER_MODES, resolve_filter_mode),
        (AUTO_ZERO_MODES, resolve_auto_zero),
        (DISPLAY_UNITS, resolve_unit),
        (TARE_BEHAVIORS, resolve_tare_behavior),
    ):
        for choice in choices:
            resolve(choice)
    for choices, index in ((APP_FILTERS, 2), (STABILITY_RANGES, 3), (STABILITY_DELAYS, 4)):
        for choice in choices:
            _encode_mode(index, choice)
    for choices, enum in (
        (FILTER_MODES, FilterMode),
        (APP_FILTERS, AppFilter),
        (STABILITY_RANGES, StabilityRange),
        (STABILITY_DELAYS, StabilityDelay),
        (AUTO_ZERO_MODES, AutoZeroMode),
        (TARE_BEHAVIORS, TareBehavior),
    ):
        reported = {_mode_label(member) for member in enum} - {None}
        assert reported == set(choices)


class TestUnknownCommandKind:
    async def test_unknown_kind_raises(self) -> None:
        from capa.devices.adapter import DeviceCommand

        adapter, _ = _make_adapter()
        await adapter.open()
        try:
            with pytest.raises(AdapterError, match="unknown command kind"):
                await adapter.command(
                    DeviceCommand(
                        kind="not_a_real_verb",
                        payload={},
                        issued_by="alice",
                        confirmed_by="alice",
                    )
                )
        finally:
            await adapter.close()


# ---------------------------------------------------------------------------
# Watchdog state
# ---------------------------------------------------------------------------


class TestWatchdog:
    async def test_watchdog_state_after_streaming(self) -> None:
        """Time-math assertion only; ``_drain`` stops the adapter so we
        rebuild the state with ``lifecycle_state="running"`` to avoid the
        new clean-shutdown grace path."""
        from capa.devices._helpers import WatchdogState

        adapter, _ = _make_adapter(rate_hz=50.0)
        await adapter.open()
        try:
            await adapter.start(make_start_ctx())
            await _drain(adapter, max_records=1)
            live = adapter.watchdog_state()
            assert live.last_t_mono_ns is not None
            running = WatchdogState(
                device=live.device,
                last_t_mono_ns=live.last_t_mono_ns,
                expected_period_ns=live.expected_period_ns,
                lifecycle_state="running",
            )
            far_future = (running.last_t_mono_ns or 0) + 10 * running.expected_period_ns
            assert running.is_silent(now_t_mono_ns=far_future)
        finally:
            await adapter.close()


# ---------------------------------------------------------------------------
# Stream lifecycle errors
# ---------------------------------------------------------------------------


class TestStreamLifecycle:
    async def test_stream_requires_start(self) -> None:
        adapter, _ = _make_adapter()
        await adapter.open()
        try:
            with pytest.raises(AdapterError, match="requires start"):
                async for _e in adapter.stream():
                    pass
        finally:
            await adapter.close()


# ---------------------------------------------------------------------------
# Cold-open behavior — under the unified API
# ---------------------------------------------------------------------------


class TestColdOpen:
    """Under the unified API, ``sartoriuslib.open_device`` swallows the
    well-known cold-open first-byte race internally (frame underrun /
    0-byte read) with a bounded retry. The adapter no longer carries a
    retry loop: a single ``open_device`` call either succeeds or surfaces
    the post-retry error as an :class:`AdapterError`."""

    async def test_open_calls_balance_factory_exactly_once(self) -> None:
        stub = StubBalance(value=1.5)
        attempts = 0

        async def factory() -> Any:
            nonlocal attempts
            attempts += 1
            return stub

        adapter = SartoriusAdapter(
            name="balance",
            port="fake://stub",
            balance_factory=factory,
        )
        await adapter.open()
        try:
            assert attempts == 1
            assert adapter.device_info is stub.info
        finally:
            await adapter.close()

    async def test_open_failure_surfaces_as_adapter_error(self) -> None:
        """A ``SartoriusError`` from ``open_device`` (post the lib's own
        internal retry) must surface as an :class:`AdapterError` — no
        adapter-side retry loop swallows it."""
        attempts = 0

        async def factory() -> Any:
            nonlocal attempts
            attempts += 1
            raise SartoriusError("checksum mismatch on frame 17")

        adapter = SartoriusAdapter(
            name="balance",
            port="fake://stub",
            balance_factory=factory,
        )
        with pytest.raises(AdapterError, match="checksum mismatch"):
            await adapter.open()
        assert attempts == 1  # no adapter-side retry
