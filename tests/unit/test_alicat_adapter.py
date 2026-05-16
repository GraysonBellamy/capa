"""Unit tests for :class:`capa.devices.alicat.AlicatAdapter`.

Drives the real adapter against an in-process ``StubAlicatDevice`` that
duck-types :class:`alicatlib.devices.base.Device`'s public surface. The stub
emits canned :class:`alicatlib.DataFrame`\\ s under ``poll()`` (the
:class:`alicatlib.streaming.recorder.PollSource` shape the recorder needs)
and records every typed-method call so tests can assert command dispatch.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any
from unittest.mock import MagicMock

import pytest
from alicatlib.devices.data_frame import (
    DataFrame,
    DataFrameFormat,
    DataFrameFormatFlavor,
)
from alicatlib.devices.flow_controller import FlowController

from capa.channels.calibration import Identity
from capa.channels.spec import (
    AlicatFrameField,
    ChannelKind,
    ChannelSpec,
)
from capa.core.errors import AdapterError
from capa.devices.adapter import Capability as CapaCapability
from capa.devices.alicat import (
    ADAPTER_ID,
    AlicatAdapter,
    AlicatAdapterParams,
)
from capa.devices.records import (
    ChannelSample,
    DeviceSnapshot,
    SourceRecord,
)
from tests._adapter_helpers import make_start_ctx

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Stub Device — duck-types alicatlib.devices.base.Device for tests
# ---------------------------------------------------------------------------


_EMPTY_FORMAT = DataFrameFormat(fields=(), flavor=DataFrameFormatFlavor.DEFAULT)


class StubAlicatDevice:
    """Minimal duck-type of :class:`alicatlib.devices.base.Device`.

    The adapter only calls ``poll()``, ``setpoint(...)``, ``gas(...)``,
    ``tare_flow()`` / ``tare_*_pressure()``, and ``close()``. We expose those
    plus ``info`` so :meth:`AlicatAdapter.update_capabilities_from_device`
    can probe.
    """

    def __init__(
        self,
        *,
        values: dict[str, float] | None = None,
        info: Any | None = None,
        is_controller: bool = True,
    ) -> None:
        self._values = values or {"Mass_Flow": 12.5, "Abs_Press": 100.0}
        self.info = info or _make_device_info()
        self.is_controller = is_controller
        self.poll_calls = 0
        self.setpoint_calls: list[dict[str, Any]] = []
        self.gas_calls: list[dict[str, Any]] = []
        self.tare_flow_calls = 0
        self.close_calls = 0
        self.raise_on_poll: BaseException | None = None
        # Extended-control surface trackers — every recorded call captures
        # the kwargs the adapter forwarded so tests can assert payload mapping.
        self.engineering_units_calls: list[dict[str, Any]] = []
        self.zero_band_calls: list[dict[str, Any]] = []
        self.stp_ntp_pressure_calls: list[dict[str, Any]] = []
        self.stp_ntp_temperature_calls: list[dict[str, Any]] = []
        self.tare_gauge_pressure_calls = 0
        self.tare_absolute_pressure_calls = 0
        self.power_up_tare_calls: list[Any] = []
        self.blink_display_calls: list[Any] = []
        self.lock_display_calls = 0
        self.unlock_display_calls = 0
        self.totalizer_reset_calls: list[dict[str, Any]] = []
        self.totalizer_reset_peak_calls: list[dict[str, Any]] = []
        self.totalizer_save_calls: list[dict[str, Any]] = []
        self.gas_list_calls = 0
        # Controller-only trackers (used by ``StubAlicatController`` only)
        self.setpoint_source_calls: list[dict[str, Any]] = []
        self.loop_control_variable_calls: list[Any] = []
        self.ramp_rate_calls: list[dict[str, Any]] = []
        self.deadband_limit_calls: list[dict[str, Any]] = []
        self.auto_tare_calls: list[dict[str, Any]] = []
        self.hold_valves_calls = 0
        self.hold_valves_closed_calls: list[dict[str, Any]] = []
        self.cancel_valve_hold_calls = 0

    async def poll(self) -> DataFrame:
        self.poll_calls += 1
        if self.raise_on_poll is not None:
            exc = self.raise_on_poll
            self.raise_on_poll = None
            raise exc
        return DataFrame(
            unit_id="A",
            format=_EMPTY_FORMAT,
            values=MappingProxyType(dict(self._values)),
            values_by_statistic=MappingProxyType({}),
            status=frozenset(),
            received_at=datetime.now(UTC),
            monotonic_ns=time.monotonic_ns(),
        )

    async def setpoint(self, value: float | None = None, unit: Any = None) -> Any:
        self.setpoint_calls.append({"value": value, "unit": unit})
        # Return a minimal SetpointState-like object
        state = MagicMock()
        state.current = value
        state.requested = value
        return state

    async def gas(self, name: Any, *, save: bool = False) -> None:
        self.gas_calls.append({"gas": name, "save": save})

    async def tare_flow(self) -> Any:
        self.tare_flow_calls += 1
        return MagicMock()

    async def tare_absolute_pressure(self) -> Any:
        self.tare_absolute_pressure_calls += 1
        return MagicMock()

    async def tare_gauge_pressure(self) -> Any:
        self.tare_gauge_pressure_calls += 1
        return MagicMock()

    async def engineering_units(
        self,
        statistic: Any,
        unit: Any = None,
        *,
        apply_to_group: bool = False,
        override_special_rules: bool = False,
    ) -> Any:
        self.engineering_units_calls.append(
            {
                "statistic": statistic,
                "unit": unit,
                "apply_to_group": apply_to_group,
                "override_special_rules": override_special_rules,
            }
        )
        return MagicMock()

    async def zero_band(self, zero_band: float | None = None) -> Any:
        self.zero_band_calls.append({"zero_band": zero_band})
        return MagicMock()

    async def stp_ntp_pressure(
        self, mode: Any, pressure: float | None = None, unit_code: int | None = None
    ) -> Any:
        self.stp_ntp_pressure_calls.append(
            {"mode": mode, "pressure": pressure, "unit_code": unit_code}
        )
        return MagicMock()

    async def stp_ntp_temperature(
        self, mode: Any, temperature: float | None = None, unit_code: int | None = None
    ) -> Any:
        self.stp_ntp_temperature_calls.append(
            {"mode": mode, "temperature": temperature, "unit_code": unit_code}
        )
        return MagicMock()

    async def power_up_tare(self, enable: bool | None = None) -> Any:
        self.power_up_tare_calls.append(enable)
        return MagicMock()

    async def blink_display(self, duration_s: int | None = None) -> Any:
        self.blink_display_calls.append(duration_s)
        return MagicMock()

    async def lock_display(self) -> Any:
        self.lock_display_calls += 1
        return MagicMock()

    async def unlock_display(self) -> Any:
        self.unlock_display_calls += 1
        return MagicMock()

    async def totalizer_reset(self, totalizer: Any, *, confirm: bool = False) -> Any:
        self.totalizer_reset_calls.append({"totalizer": totalizer, "confirm": confirm})
        return MagicMock()

    async def totalizer_reset_peak(self, totalizer: Any, *, confirm: bool = False) -> Any:
        self.totalizer_reset_peak_calls.append({"totalizer": totalizer, "confirm": confirm})
        return MagicMock()

    async def totalizer_save(self, enable: bool | None = None, *, save: bool | None = None) -> Any:
        self.totalizer_save_calls.append({"enable": enable, "save": save})
        return MagicMock()

    async def gas_list(self) -> Any:
        self.gas_list_calls += 1
        return {1: "Air", 2: "N2"}

    async def close(self) -> None:
        self.close_calls += 1


class StubAlicatController(StubAlicatDevice, FlowController):  # type: ignore[misc]
    """Controller flavor of :class:`StubAlicatDevice`.

    Inherits :class:`FlowController` so the adapter's
    ``_require_controller`` ``isinstance`` check passes, but skips
    :class:`Device.__init__` entirely (it requires a real
    :class:`Session`). Adds the controller-only methods the adapter
    dispatches to.

    ``info`` is a ``@property`` on :class:`Device` — we shadow it with our
    own descriptor instead of trying to set the attribute.
    """

    def __init__(
        self,
        *,
        values: dict[str, float] | None = None,
        info: Any | None = None,
    ) -> None:
        # ``StubAlicatDevice.__init__`` writes to ``self.info``, which is a
        # read-only property on :class:`Device`. Stash on a private attr and
        # let the override below expose it as ``info``.
        self._stub_info = info or _make_device_info()
        StubAlicatDevice.__init__(self, values=values, info=self._stub_info, is_controller=True)
        # Deliberately do NOT call FlowController.__init__ / Device.__init__:
        # both require a Session we don't have.

    @property
    def info(self) -> Any:
        return self._stub_info

    @info.setter
    def info(self, value: Any) -> None:
        self._stub_info = value

    async def setpoint_source(self, mode: str | None = None, *, save: bool | None = None) -> str:
        self.setpoint_source_calls.append({"mode": mode, "save": save})
        return mode if mode is not None else "S"

    async def loop_control_variable(self, variable: Any = None) -> Any:
        self.loop_control_variable_calls.append(variable)
        return MagicMock()

    async def ramp_rate(self, max_ramp: float | None = None, time_unit: Any = None) -> Any:
        self.ramp_rate_calls.append({"max_ramp": max_ramp, "time_unit": time_unit})
        return MagicMock()

    async def deadband_limit(
        self, deadband: float | None = None, *, save: bool | None = None
    ) -> Any:
        self.deadband_limit_calls.append({"deadband": deadband, "save": save})
        return MagicMock()

    async def auto_tare(self, enable: bool | None = None, delay_s: float | None = None) -> Any:
        self.auto_tare_calls.append({"enable": enable, "delay_s": delay_s})
        return MagicMock()

    async def hold_valves(self) -> Any:
        self.hold_valves_calls += 1
        return MagicMock()

    async def hold_valves_closed(self, *, confirm: bool = False) -> Any:
        self.hold_valves_closed_calls.append({"confirm": confirm})
        return MagicMock()

    async def cancel_valve_hold(self) -> Any:
        self.cancel_valve_hold_calls += 1
        return MagicMock()


def _make_device_info() -> Any:
    """Synthesize an alicatlib :class:`DeviceInfo` for tests.

    We use ``MagicMock`` rather than constructing the real frozen dataclass
    because the constructor needs ~12 fields and we only test that the
    adapter calls ``.model``, ``.serial``, ``.firmware``, ``.kind``,
    ``.media`` on it.
    """
    info = MagicMock()
    info.model = "MC-100SCCM-D"
    info.serial = "SN-TEST"
    info.firmware = "10v05"
    info.kind = MagicMock()
    info.kind.value = "flow_controller"
    info.media = "gas"
    return info


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _channels_for_mfc() -> list[ChannelSpec]:
    return [
        ChannelSpec(
            name="mfc.flow",
            kind=ChannelKind.MFC_FLOW,
            source=AlicatFrameField(device="mfc", field="Mass_Flow"),
            unit="mL/min",
            derived_unit="mL/min",
            calibration=Identity(input_unit="mL/min", output_unit="mL/min"),
        ),
        ChannelSpec(
            name="mfc.pressure",
            kind=ChannelKind.PROCESS_VAR,
            source=AlicatFrameField(device="mfc", field="Abs_Press"),
            unit="kPa",
            derived_unit="kPa",
            calibration=Identity(input_unit="kPa", output_unit="kPa"),
        ),
    ]


def _make_adapter(
    *,
    name: str = "mfc",
    rate_hz: float = 50.0,  # max for AlicatAdapterParams
    snapshot_period_s: float = 1e6,
    auto_reconnect: bool = True,
    is_controller: bool = True,
    values: dict[str, float] | None = None,
    controller_stub: bool = False,
) -> tuple[AlicatAdapter, StubAlicatDevice]:
    """Build an adapter wired to a stub.

    ``controller_stub=True`` returns a :class:`StubAlicatController` (a real
    :class:`FlowController` subclass) so tests of controller-only commands
    can pass the adapter's ``_require_controller`` check.
    """
    stub: StubAlicatDevice
    if controller_stub:
        stub = StubAlicatController(values=values)
    else:
        stub = StubAlicatDevice(values=values, is_controller=is_controller)

    async def factory() -> Any:
        return stub

    adapter = AlicatAdapter(
        name=name,
        port="fake://stub",
        rate_hz=rate_hz,
        snapshot_period_s=snapshot_period_s,
        auto_reconnect=auto_reconnect,
        device_factory=factory,
    )
    adapter.configure_channels(_channels_for_mfc())
    return adapter, stub


def _split(
    emissions: list[Any],
) -> tuple[list[SourceRecord], list[ChannelSample], list[DeviceSnapshot]]:
    return (
        [e for e in emissions if isinstance(e, SourceRecord)],
        [e for e in emissions if isinstance(e, ChannelSample)],
        [e for e in emissions if isinstance(e, DeviceSnapshot)],
    )


async def _drain(adapter: AlicatAdapter, *, max_records: int) -> list[Any]:
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
# Params + construction
# ---------------------------------------------------------------------------


class TestParams:
    def test_defaults(self) -> None:
        p = AlicatAdapterParams(port="/dev/ttyUSB0")
        assert p.unit_id == "A"
        assert p.baudrate == 19200
        assert p.rate_hz == 2.0
        assert p.auto_reconnect is True

    def test_extra_forbidden(self) -> None:
        with pytest.raises(Exception):
            AlicatAdapterParams(port="/dev/null", made_up=1)  # type: ignore[call-arg]


class TestConstruction:
    def test_engine_kwargs_path(self) -> None:
        adapter = AlicatAdapter(name="mfc", port="/dev/ttyUSB0", unit_id="B", rate_hz=5.0)
        assert adapter.name == "mfc"
        assert adapter.params.unit_id == "B"
        assert adapter.params.rate_hz == 5.0

    def test_explicit_params_path(self) -> None:
        params = AlicatAdapterParams(port="/dev/ttyUSB0")
        adapter = AlicatAdapter(name="mfc", params=params)
        assert adapter.params is params

    def test_rejects_both_params_and_kwargs(self) -> None:
        params = AlicatAdapterParams(port="/dev/ttyUSB0")
        with pytest.raises(TypeError):
            AlicatAdapter(name="mfc", params=params, port="/dev/ttyUSB1")

    def test_baseline_capabilities(self) -> None:
        a = AlicatAdapter(name="mfc", port="/dev/null", auto_reconnect=False)
        assert CapaCapability.HAS_TARE in a.capabilities
        assert CapaCapability.HAS_GAS_SELECT in a.capabilities
        assert CapaCapability.READS_PROCESS_VAR in a.capabilities
        assert CapaCapability.HAS_PARAMETER_CONFIG in a.capabilities
        assert CapaCapability.HAS_DISPLAY_CONTROL in a.capabilities
        assert CapaCapability.HAS_TOTALIZER in a.capabilities
        assert CapaCapability.SUPPORTS_AUTO_RECONNECT not in a.capabilities
        # Controller-only flags are added at ``open()`` once the device kind
        # is known. Pre-open the adapter advertises the meter-safe baseline.
        assert CapaCapability.HAS_SETPOINT not in a.capabilities
        assert CapaCapability.HAS_VALVE_HOLD not in a.capabilities

    def test_auto_reconnect_advertised(self) -> None:
        a = AlicatAdapter(name="mfc", port="/dev/null", auto_reconnect=True)
        assert CapaCapability.SUPPORTS_AUTO_RECONNECT in a.capabilities


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_open_caches_info_and_advertises_setpoint(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            assert adapter.device_info is stub.info
            # FlowController detection: stub doesn't subclass FlowController, so
            # HAS_SETPOINT remains absent. The adapter is conservative — only
            # advertises HAS_SETPOINT when isinstance() succeeds.
            assert CapaCapability.HAS_SETPOINT not in adapter.capabilities
        finally:
            await adapter.close()
            assert stub.close_calls == 1

    async def test_open_idempotent(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        await adapter.open()
        await adapter.close()
        # Second open returned early; first close still closes once.
        assert stub.close_calls == 1


# ---------------------------------------------------------------------------
# Streaming — SourceRecord + ChannelSample shape
# ---------------------------------------------------------------------------


class TestStream:
    async def test_emits_record_and_channel_samples(self) -> None:
        adapter, _stub = _make_adapter(rate_hz=50.0)
        await adapter.open()
        try:
            await adapter.start(make_start_ctx())
            emissions = await _drain(adapter, max_records=2)
        finally:
            await adapter.close()
        records, samples, snapshots = _split(emissions)
        assert len(records) >= 2
        # Initial snapshot before any data, plus per-tick records:
        assert snapshots
        assert snapshots[0].adapter == ADAPTER_ID
        # Each record is wide_row, carrying every Alicat field via sample_to_row
        for r in records:
            assert r.shape == "wide_row"
            assert r.adapter == ADAPTER_ID
            assert r.device == "mfc"
            assert "Mass_Flow" in r.row
            assert "Abs_Press" in r.row
        # Two channels declared → two ChannelSamples per tick.
        per_record = len(samples) // len(records) if records else 0
        assert per_record == 2
        flow_samples = [s for s in samples if s.channel == "mfc.flow"]
        pressure_samples = [s for s in samples if s.channel == "mfc.pressure"]
        assert flow_samples and pressure_samples
        assert flow_samples[0].value == pytest.approx(12.5)
        assert pressure_samples[0].value == pytest.approx(100.0)
        # source_record_id must back-pointer into the records.
        record_ids = {r.record_id for r in records}
        for s in samples:
            assert s.source_record_id in record_ids

    async def test_stream_requires_start(self) -> None:
        adapter, _ = _make_adapter()
        await adapter.open()
        try:
            with pytest.raises(AdapterError, match="requires start"):
                async for _emission in adapter.stream():
                    pass
        finally:
            await adapter.close()

    async def test_start_requires_open(self) -> None:
        adapter, _ = _make_adapter()
        # AdapterLifecycle enforces open-before-start at the lifecycle level.
        with pytest.raises(RuntimeError, match="must be open"):
            await adapter.start(make_start_ctx())


# ---------------------------------------------------------------------------
# Authorization gate
# ---------------------------------------------------------------------------


class TestAuthorization:
    async def test_command_without_auth_refused(self) -> None:
        adapter, _ = _make_adapter()
        await adapter.open()
        try:
            # No authorization_id, no confirmed_by → refused at the boundary,
            # *before* the adapter reaches the underlying Device.
            result = await adapter.set_setpoint(
                value=50.0,
                issued_by="alice",
            )
            assert result.accepted is False
            assert "unauthorized" in result.detail.lower()
        finally:
            await adapter.close()

    async def test_command_with_auth_dispatches(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            # Stub isn't a controller subclass, so set_setpoint will fall
            # through to the AdapterError path. Use ``set_gas`` instead —
            # it's available on every alicat Device.
            result = await adapter.set_gas(
                "Air",
                issued_by="alice",
                authorization_id="run-2026-05-07-1500",
            )
            assert result.accepted is True
            assert stub.gas_calls == [{"gas": "Air", "save": False}]
        finally:
            await adapter.close()

    async def test_command_with_manual_confirm_dispatches(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            result = await adapter.tare_flow(
                issued_by="alice",
                confirmed_by="alice",  # manual confirm path
            )
            assert result.accepted is True
            assert stub.tare_flow_calls == 1
        finally:
            await adapter.close()


# ---------------------------------------------------------------------------
# Extended control surface — units, references, display, valve hold, totalizer
# ---------------------------------------------------------------------------


class TestControllerCapabilityFromOpen:
    async def test_controller_stub_advertises_setpoint_and_valve_hold(self) -> None:
        adapter, _ = _make_adapter(controller_stub=True)
        await adapter.open()
        try:
            assert CapaCapability.HAS_SETPOINT in adapter.capabilities
            assert CapaCapability.HAS_VALVE_HOLD in adapter.capabilities
        finally:
            await adapter.close()


class TestEngineeringConfig:
    async def test_set_units_forwards_payload(self) -> None:
        from capa.devices.adapter import DeviceCommand

        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            result = await adapter.command(
                DeviceCommand(
                    kind="set_units",
                    payload={
                        "statistic": "MASS_FLOW",
                        "unit": "SCCM",
                        "apply_to_group": True,
                    },
                    issued_by="alice",
                    confirmed_by="alice",
                )
            )
            assert result.accepted is True
            assert stub.engineering_units_calls == [
                {
                    "statistic": "MASS_FLOW",
                    "unit": "SCCM",
                    "apply_to_group": True,
                    "override_special_rules": False,
                }
            ]
        finally:
            await adapter.close()

    async def test_typed_set_units_helper(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            result = await adapter.set_units(
                "MASS_FLOW",
                "SCCM",
                issued_by="alice",
                confirmed_by="alice",
            )
            assert result.accepted is True
            assert stub.engineering_units_calls[0]["statistic"] == "MASS_FLOW"
            assert stub.engineering_units_calls[0]["unit"] == "SCCM"
        finally:
            await adapter.close()

    async def test_set_zero_band(self) -> None:
        from capa.devices.adapter import DeviceCommand

        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            result = await adapter.command(
                DeviceCommand(
                    kind="set_zero_band",
                    payload={"zero_band": 0.5},
                    issued_by="alice",
                    confirmed_by="alice",
                )
            )
            assert result.accepted is True
            assert stub.zero_band_calls == [{"zero_band": 0.5}]
        finally:
            await adapter.close()

    async def test_set_stp_pressure(self) -> None:
        from capa.devices.adapter import DeviceCommand

        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            result = await adapter.command(
                DeviceCommand(
                    kind="set_stp_pressure",
                    payload={"mode": "S", "pressure": 101.325},
                    issued_by="alice",
                    confirmed_by="alice",
                )
            )
            assert result.accepted is True
            assert len(stub.stp_ntp_pressure_calls) == 1
            call = stub.stp_ntp_pressure_calls[0]
            assert call["pressure"] == pytest.approx(101.325)
            assert call["unit_code"] is None
        finally:
            await adapter.close()


class TestDisplay:
    async def test_blink_display(self) -> None:
        from capa.devices.adapter import DeviceCommand

        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            result = await adapter.command(
                DeviceCommand(
                    kind="blink_display",
                    payload={"duration_s": 3},
                    issued_by="alice",
                    confirmed_by="alice",
                )
            )
            assert result.accepted is True
            assert stub.blink_display_calls == [3]
        finally:
            await adapter.close()

    async def test_lock_and_unlock(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            r1 = await adapter.lock_display(issued_by="alice", confirmed_by="alice")
            r2 = await adapter.unlock_display(issued_by="alice", confirmed_by="alice")
            assert r1.accepted is True and r2.accepted is True
            assert stub.lock_display_calls == 1
            assert stub.unlock_display_calls == 1
        finally:
            await adapter.close()


class TestValveHold:
    async def test_hold_valves_requires_controller(self) -> None:
        # Meter stub: hold_valves should error out at the dispatch.
        adapter, _ = _make_adapter()
        await adapter.open()
        try:
            with pytest.raises(AdapterError, match="requires a controller"):
                await adapter.hold_valves(issued_by="alice", confirmed_by="alice")
        finally:
            await adapter.close()

    async def test_hold_valves_dispatches_on_controller(self) -> None:
        adapter, stub = _make_adapter(controller_stub=True)
        await adapter.open()
        try:
            result = await adapter.hold_valves(issued_by="alice", confirmed_by="alice")
            assert result.accepted is True
            assert isinstance(stub, StubAlicatController)
            assert stub.hold_valves_calls == 1
        finally:
            await adapter.close()

    async def test_hold_valves_closed_passes_library_confirm(self) -> None:
        adapter, stub = _make_adapter(controller_stub=True)
        await adapter.open()
        try:
            result = await adapter.hold_valves_closed(issued_by="alice", confirmed_by="alice")
            assert result.accepted is True
            assert isinstance(stub, StubAlicatController)
            # CAPA's authorization gate covers the library's confirm gate;
            # the adapter passes ``confirm=True`` down so the library doesn't
            # double-prompt.
            assert stub.hold_valves_closed_calls == [{"confirm": True}]
        finally:
            await adapter.close()

    async def test_cancel_valve_hold(self) -> None:
        adapter, stub = _make_adapter(controller_stub=True)
        await adapter.open()
        try:
            result = await adapter.cancel_valve_hold(issued_by="alice", confirmed_by="alice")
            assert result.accepted is True
            assert isinstance(stub, StubAlicatController)
            assert stub.cancel_valve_hold_calls == 1
        finally:
            await adapter.close()


class TestTotalizer:
    async def test_totalizer_reset_passes_library_confirm(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            result = await adapter.totalizer_reset(issued_by="alice", confirmed_by="alice")
            assert result.accepted is True
            # Default totalizer is FIRST(=1)
            assert len(stub.totalizer_reset_calls) == 1
            call = stub.totalizer_reset_calls[0]
            assert int(call["totalizer"]) == 1
            assert call["confirm"] is True
        finally:
            await adapter.close()

    async def test_totalizer_reset_explicit_id(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            result = await adapter.totalizer_reset(
                issued_by="alice",
                totalizer=2,
                confirmed_by="alice",
            )
            assert result.accepted is True
            assert int(stub.totalizer_reset_calls[0]["totalizer"]) == 2
        finally:
            await adapter.close()


class TestControllerOnlyVerbs:
    async def test_set_setpoint_source_dispatches(self) -> None:
        from capa.devices.adapter import DeviceCommand

        adapter, stub = _make_adapter(controller_stub=True)
        await adapter.open()
        try:
            result = await adapter.command(
                DeviceCommand(
                    kind="set_setpoint_source",
                    payload={"mode": "S", "save": True},
                    issued_by="alice",
                    confirmed_by="alice",
                )
            )
            assert result.accepted is True
            assert isinstance(stub, StubAlicatController)
            assert stub.setpoint_source_calls == [{"mode": "S", "save": True}]
        finally:
            await adapter.close()

    async def test_set_ramp_rate_string_time_unit(self) -> None:
        from capa.devices.adapter import DeviceCommand

        adapter, stub = _make_adapter(controller_stub=True)
        await adapter.open()
        try:
            result = await adapter.command(
                DeviceCommand(
                    kind="set_ramp_rate",
                    payload={"max_ramp": 5.0, "time_unit": "second"},
                    issued_by="alice",
                    confirmed_by="alice",
                )
            )
            assert result.accepted is True
            assert isinstance(stub, StubAlicatController)
            from alicatlib.devices.models import TimeUnit

            call = stub.ramp_rate_calls[0]
            assert call["max_ramp"] == pytest.approx(5.0)
            assert call["time_unit"] == TimeUnit.SECOND
        finally:
            await adapter.close()

    async def test_set_deadband_propagates_save(self) -> None:
        from capa.devices.adapter import DeviceCommand

        adapter, stub = _make_adapter(controller_stub=True)
        await adapter.open()
        try:
            result = await adapter.command(
                DeviceCommand(
                    kind="set_deadband",
                    payload={"deadband": 0.1, "save": False},
                    issued_by="alice",
                    confirmed_by="alice",
                )
            )
            assert result.accepted is True
            assert isinstance(stub, StubAlicatController)
            assert stub.deadband_limit_calls == [{"deadband": 0.1, "save": False}]
        finally:
            await adapter.close()


class TestReadOnlyHelpers:
    async def test_read_gas_list(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            gases = await adapter.read_gas_list()
            assert dict(gases) == {1: "Air", 2: "N2"}
            assert stub.gas_list_calls == 1
        finally:
            await adapter.close()

    async def test_read_gas_list_requires_open(self) -> None:
        adapter = AlicatAdapter(name="mfc", port="/dev/null")
        with pytest.raises(AdapterError, match="requires open"):
            await adapter.read_gas_list()


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
    async def test_pre_first_sample_is_silent_false(self) -> None:
        adapter, _ = _make_adapter(rate_hz=10.0)
        await adapter.open()
        await adapter.start(make_start_ctx())
        # No sample has been marked yet; tracker reports None → not silent.
        state = adapter.watchdog_state()
        assert state.last_t_mono_ns is None
        assert not state.is_silent(now_t_mono_ns=10**18)
        await adapter.close()

    async def test_after_streaming_marked(self) -> None:
        """Verify the time-based silence math in isolation. ``_drain`` calls
        ``adapter.stop()`` before returning, so the live adapter's
        ``lifecycle_state`` is ``"open"`` and the new grace logic correctly
        suppresses silence; we rebuild the state with ``"running"`` so the
        elapsed-time arithmetic can be asserted on its own."""
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
            # Right after the most-recent sample, age is tiny → not silent.
            now_ns = (running.last_t_mono_ns or 0) + 10  # 10 ns later
            assert not running.is_silent(now_t_mono_ns=now_ns)
            # Far future → silent.
            far_future = (running.last_t_mono_ns or 0) + 10 * running.expected_period_ns
            assert running.is_silent(now_t_mono_ns=far_future)
        finally:
            await adapter.close()
