"""Unit tests for :class:`capa.devices.sartorius.SartoriusAdapter` (P2).

Drives the real adapter against an in-process ``StubBalance`` that duck-types
:class:`sartoriuslib.devices.balance.Balance`'s public surface.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from sartoriuslib.devices.models import Reading
from sartoriuslib.errors import SartoriusError
from sartoriuslib.protocol.base import ProtocolKind
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
    COLD_OPEN_RETRY_ATTEMPTS,
    SartoriusAdapter,
    SartoriusAdapterParams,
)

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Stub Balance — duck-types Balance for tests
# ---------------------------------------------------------------------------


class StubBalance:
    """Minimal duck-type of :class:`sartoriuslib.devices.balance.Balance`.

    The adapter only calls ``poll()``, ``tare()``, ``zero()``, and ``close()``.
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
        balance_factory=factory,  # type: ignore[arg-type]
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
            await adapter.start()
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
            await adapter.start()
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
            await adapter.start()
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
# Watchdog state
# ---------------------------------------------------------------------------


class TestWatchdog:
    async def test_watchdog_state_after_streaming(self) -> None:
        adapter, _ = _make_adapter(rate_hz=50.0)
        await adapter.open()
        try:
            await adapter.start()
            await _drain(adapter, max_records=1)
            state = adapter.watchdog_state()
            assert state.last_t_mono_ns is not None
            far_future = (state.last_t_mono_ns or 0) + 10 * state.expected_period_ns
            assert state.is_silent(now_t_mono_ns=far_future)
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
# Cold-open retry (hardware-day §3.4)
# ---------------------------------------------------------------------------


class TestColdOpenRetry:
    """``_build_balance`` must absorb the well-known ``frame too short``
    cold-open race after a fresh USB plug, but never retry past genuinely
    fatal ``SartoriusError`` shapes (checksum, timeout, bad device id)."""

    async def test_retries_past_frame_too_short(self) -> None:
        stub = StubBalance(value=1.5)
        attempts = 0

        async def factory() -> Any:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise SartoriusError("frame too short: got 1 bytes (min 4)")
            return stub

        adapter = SartoriusAdapter(
            name="balance",
            port="fake://stub",
            balance_factory=factory,  # type: ignore[arg-type]
        )
        await adapter.open()
        try:
            assert attempts == 2
            assert adapter._cold_open_retry_count == 1
            assert adapter.device_info is stub.info
        finally:
            await adapter.close()

    async def test_retries_past_got_zero_bytes(self) -> None:
        stub = StubBalance(value=1.5)
        attempts = 0

        async def factory() -> Any:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise SartoriusError("read failed: got 0 bytes")
            return stub

        adapter = SartoriusAdapter(
            name="balance",
            port="fake://stub",
            balance_factory=factory,  # type: ignore[arg-type]
        )
        await adapter.open()
        try:
            assert attempts == 2
        finally:
            await adapter.close()

    async def test_non_cold_open_error_raises_immediately(self) -> None:
        attempts = 0

        async def factory() -> Any:
            nonlocal attempts
            attempts += 1
            raise SartoriusError("checksum mismatch on frame 17")

        adapter = SartoriusAdapter(
            name="balance",
            port="fake://stub",
            balance_factory=factory,  # type: ignore[arg-type]
        )
        with pytest.raises(AdapterError, match="checksum mismatch"):
            await adapter.open()
        assert attempts == 1  # no retry
        assert adapter._cold_open_retry_count == 0

    async def test_exhausted_retries_raise_last_cold_open_error(self) -> None:
        attempts = 0

        async def factory() -> Any:
            nonlocal attempts
            attempts += 1
            raise SartoriusError(f"frame too short: attempt {attempts}")

        adapter = SartoriusAdapter(
            name="balance",
            port="fake://stub",
            balance_factory=factory,  # type: ignore[arg-type]
        )
        with pytest.raises(AdapterError, match="frame too short"):
            await adapter.open()
        assert attempts == COLD_OPEN_RETRY_ATTEMPTS
        assert adapter._cold_open_retry_count == COLD_OPEN_RETRY_ATTEMPTS
