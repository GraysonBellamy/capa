"""Unit tests for :class:`capa.devices.alicat.AlicatAdapter` (P2).

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
        self.gas_calls: list[Any] = []
        self.tare_flow_calls = 0
        self.close_calls = 0
        self.raise_on_poll: BaseException | None = None

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

    async def gas(self, name: Any) -> None:
        self.gas_calls.append(name)

    async def tare_flow(self) -> Any:
        self.tare_flow_calls += 1
        return MagicMock()

    async def close(self) -> None:
        self.close_calls += 1


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
) -> tuple[AlicatAdapter, StubAlicatDevice]:
    stub = StubAlicatDevice(values=values, is_controller=is_controller)

    async def factory() -> Any:
        return stub

    adapter = AlicatAdapter(
        name=name,
        port="fake://stub",
        rate_hz=rate_hz,
        snapshot_period_s=snapshot_period_s,
        auto_reconnect=auto_reconnect,
        device_factory=factory,  # type: ignore[arg-type]
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
        assert CapaCapability.SUPPORTS_AUTO_RECONNECT not in a.capabilities

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
            await adapter.start()
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
            await adapter.start()


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
            assert stub.gas_calls == ["Air"]
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
# Watchdog state
# ---------------------------------------------------------------------------


class TestWatchdog:
    async def test_pre_first_sample_is_silent_false(self) -> None:
        adapter, _ = _make_adapter(rate_hz=10.0)
        await adapter.open()
        await adapter.start()
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
            await adapter.start()
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
