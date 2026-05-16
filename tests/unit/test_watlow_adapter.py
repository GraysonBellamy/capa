"""Unit tests for :class:`capa.devices.watlow.WatlowAdapter`.

We exercise the adapter against an in-process ``StubWatlowController`` that
duck-types :class:`watlowlib.Controller`'s public surface. The stub yields
canned :class:`watlowlib.streaming.Sample`\\ s under ``poll_many`` (the
:class:`watlowlib.streaming.PollSource` Protocol watlowlib's recorder needs)
and records every ``set_setpoint`` / ``write_parameter`` call so the tests
can assert command dispatch.

The stub avoids the byte-level scripting of
:class:`watlowlib.transport.fake.FakeTransport` — the adapter never touches
the wire encoding, so a higher-level fake gives crisper assertions.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from watlowlib import PARAMETERS, Unit
from watlowlib.devices.capability import Capability as WatlowCapability
from watlowlib.devices.models import (
    DeviceHealth,
    DeviceInfo,
    ParameterEntry,
    PartNumber,
    Reading,
)
from watlowlib.errors import (
    ErrorContext,
    WatlowConnectionError,
    WatlowTimeoutError,
)
from watlowlib.protocol.base import ProtocolKind
from watlowlib.registry.families import ControllerFamily
from watlowlib.registry.units import resolve_unit
from watlowlib.streaming.sample import Sample
from watlowlib.transport.base import SerialSettings

from capa.channels.calibration import Identity, LinearTwoPoint, UncertaintySpec
from capa.channels.spec import ChannelKind, ChannelSpec, WatlowParameter
from capa.core.clock import RunClock
from capa.core.errors import AdapterError
from capa.devices.adapter import Capability as CapaCapability
from capa.devices.adapter import DeviceCommand
from capa.devices.records import ChannelSample, DeviceEvent, DeviceSnapshot, SourceRecord
from capa.devices.watlow import ADAPTER_ID, WatlowAdapter, WatlowAdapterParams
from tests._adapter_helpers import make_start_ctx

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Stub controller — duck-types watlowlib.Controller for tests
# ---------------------------------------------------------------------------


def _make_device_info() -> DeviceInfo:
    """Synthesize a plausible PM3 :class:`DeviceInfo` for tests."""
    part = PartNumber(raw="PM3C1AJ-AAAAAAA", family=ControllerFamily.PM)
    return DeviceInfo(
        part_number=part,
        hardware_id=1234,
        firmware_id=5678,
        serial_number="SN-TEST-001",
        family=ControllerFamily.PM,
        protocol=ProtocolKind.STDBUS,
        address=1,
        capabilities=WatlowCapability.NONE,
        serial_settings=SerialSettings(port="fake://test"),
        loops=1,
        health=DeviceHealth.OK,
        configured_protocol=ProtocolKind.STDBUS,
    )


class StubWatlowController:
    """Duck-typed stand-in for :class:`watlowlib.Controller`.

    ``signals`` is keyed by ``(parameter, instance)`` and yields the
    corresponding scalar each time :meth:`poll_many` is called for that pair.
    Failed-poll behavior is simulated via :attr:`raise_on_poll`.
    """

    def __init__(
        self,
        *,
        signals: dict[tuple[str, int], float | int | None],
        info: DeviceInfo | None = None,
        display_unit: Unit | None = Unit.CELSIUS,
    ) -> None:
        self.signals = signals
        self.info = info or _make_device_info()
        self.display_unit: Unit | None = display_unit
        self.set_display_unit_calls: list[dict[str, Any]] = []
        self.aentered = False
        self.aexited = False
        self.set_setpoint_calls: list[dict[str, Any]] = []
        self.write_parameter_calls: list[dict[str, Any]] = []
        self.read_pv_calls = 0
        self.identify_calls = 0
        self.raise_on_poll: BaseException | None = None
        self.raise_on_set_setpoint: BaseException | None = None

    async def __aenter__(self) -> StubWatlowController:
        self.aentered = True
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.aexited = True

    async def close(self) -> None:
        """Mirror of :meth:`watlowlib.Controller.close` for the new unified API."""
        self.aexited = True

    @property
    def session(self) -> Any:
        """The adapter reads ``controller.session.recoverable_error_count``."""
        if not hasattr(self, "_session_proxy"):
            session = MagicMock()
            session.recoverable_error_count = 0
            self._session_proxy = session
        return self._session_proxy

    async def snapshot(self, *, name: str | None = None) -> Any:
        """Mirror of :meth:`Controller.snapshot` — no I/O, derived from info."""
        del name
        snap = MagicMock()
        snap.recoverable_error_count = self.session.recoverable_error_count
        snap.family = self.info.family
        snap.capabilities = self.info.capabilities
        return snap

    async def identify(
        self, *, query_configured_protocol: bool = False, **_kwargs: Any
    ) -> DeviceInfo:
        self.identify_calls += 1
        return self.info

    async def poll_many(
        self,
        parameters: Any,
        *,
        names: Any = None,
        instances: Any = (1,),
    ) -> list[Sample]:
        del names
        if self.raise_on_poll is not None:
            exc = self.raise_on_poll
            self.raise_on_poll = None  # one-shot so retries can succeed
            raise exc
        out: list[Sample] = []
        now = datetime.now(UTC)
        mono = time.monotonic_ns()
        for ident in parameters:
            param = str(ident)
            for inst in instances:
                value = self.signals.get((param, inst), 0.0)
                spec = PARAMETERS.resolve(param)
                out.append(
                    Sample(
                        device="stub",
                        address=1,
                        protocol=ProtocolKind.STDBUS,
                        parameter=param,
                        parameter_id=spec.parameter_id,
                        instance=inst,
                        value=value,
                        unit=resolve_unit(spec.unit_kind, self.display_unit),
                        t_mono_ns=mono,
                        t_utc=now,
                        t_midpoint_mono_ns=None,
                        requested_at=now,
                        received_at=now,
                        latency_s=0.001,
                        raw=b"",
                    )
                )
        return out

    async def set_setpoint(
        self,
        value: float,
        *,
        instance: int = 1,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> Reading:
        del timeout
        self.set_setpoint_calls.append({"value": value, "instance": instance, "confirm": confirm})
        if self.raise_on_set_setpoint is not None:
            exc = self.raise_on_set_setpoint
            self.raise_on_set_setpoint = None
            raise exc
        spec = PARAMETERS.resolve("setpoint")
        return Reading(
            value=value,
            unit=resolve_unit(spec.unit_kind, self.display_unit),
            received_at=datetime.now(UTC),
            monotonic_ns=time.monotonic_ns(),
            raw=b"",
            protocol=ProtocolKind.STDBUS,
        )

    async def write_parameter(
        self,
        name_or_id: str | int,
        value: Any,
        *,
        instance: int = 1,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> ParameterEntry:
        del timeout
        self.write_parameter_calls.append(
            {"name": name_or_id, "value": value, "instance": instance, "confirm": confirm}
        )
        spec = PARAMETERS.resolve(name_or_id)
        return ParameterEntry(spec=spec, instance=instance, value=value, raw=b"")

    async def read_pv(self, *, instance: int = 1, timeout: float | None = None) -> Reading:
        del timeout
        self.read_pv_calls += 1
        value = self.signals.get(("process_value", instance), 0.0)
        spec = PARAMETERS.resolve("process_value")
        return Reading(
            value=float(value) if value is not None else None,
            unit=resolve_unit(spec.unit_kind, self.display_unit),
            received_at=datetime.now(UTC),
            monotonic_ns=time.monotonic_ns(),
            raw=b"",
            protocol=ProtocolKind.STDBUS,
        )

    async def read_setpoint(self, *, instance: int = 1, timeout: float | None = None) -> Reading:
        del timeout
        value = self.signals.get(("setpoint", instance), 0.0)
        spec = PARAMETERS.resolve("setpoint")
        return Reading(
            value=float(value) if value is not None else None,
            unit=resolve_unit(spec.unit_kind, self.display_unit),
            received_at=datetime.now(UTC),
            monotonic_ns=time.monotonic_ns(),
            raw=b"",
            protocol=ProtocolKind.STDBUS,
        )

    async def read_comms_unit_label(self, *, timeout: float | None = None) -> Unit | None:
        del timeout
        return self.display_unit

    async def set_comms_unit_label(
        self,
        unit: Unit | str,
        *,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> Unit | None:
        del timeout
        self.set_display_unit_calls.append({"unit": unit, "confirm": confirm})
        if isinstance(unit, Unit):
            self.display_unit = unit
        else:
            from watlowlib.registry.units import coerce_unit

            self.display_unit = coerce_unit(unit)
        return self.display_unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _channels_for_heater() -> list[ChannelSpec]:
    return [
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


def _make_adapter(
    *,
    name: str = "heater",
    signals: dict[tuple[str, int], float | int | None] | None = None,
    rate_hz: float = 50.0,
    snapshot_period_s: float = 1e6,  # effectively never (only the start-of-stream snapshot)
    auto_reconnect: bool = False,
) -> tuple[WatlowAdapter, StubWatlowController]:
    stub = StubWatlowController(
        signals=signals or {("process_value", 1): 400.0, ("setpoint", 1): 410.0},
    )

    async def factory() -> Any:
        return stub

    adapter = WatlowAdapter(
        name=name,
        port="fake://stub",
        rate_hz=rate_hz,
        snapshot_period_s=snapshot_period_s,
        auto_reconnect=auto_reconnect,
        controller_factory=factory,
    )
    adapter.configure_channels(_channels_for_heater())
    return adapter, stub


def _split(
    emissions: list[Any],
) -> tuple[list[SourceRecord], list[ChannelSample], list[DeviceSnapshot]]:
    return (
        [e for e in emissions if isinstance(e, SourceRecord)],
        [e for e in emissions if isinstance(e, ChannelSample)],
        [e for e in emissions if isinstance(e, DeviceSnapshot)],
    )


async def _drain(adapter: WatlowAdapter, *, max_records: int) -> list[Any]:
    """Iterate the adapter's stream until ``max_records`` SourceRecords landed,
    then signal stop and drain the remainder."""
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
        p = WatlowAdapterParams(port="/dev/ttyUSB0")
        assert p.address == 1
        assert p.protocol == "stdbus"
        assert p.protocol_kind() is ProtocolKind.STDBUS
        assert p.parameters == ("process_value", "setpoint")
        assert p.instances == (1,)
        assert p.identify_on_open is True

    def test_to_serial_settings(self) -> None:
        p = WatlowAdapterParams(
            port="COM3",
            baudrate=9600,
            parity="even",
            stopbits=1,
            bytesize=8,
        )
        s = p.to_serial_settings()
        assert s.port == "COM3"
        assert s.baudrate == 9600

    def test_extra_forbidden(self) -> None:
        with pytest.raises(Exception):
            WatlowAdapterParams(port="/dev/null", made_up_field=42)  # type: ignore[call-arg]


class TestConstruction:
    def test_engine_kwargs_path(self) -> None:
        adapter = WatlowAdapter(
            name="heater",
            port="/dev/ttyUSB0",
            address=2,
            rate_hz=2.5,
        )
        assert adapter.name == "heater"
        assert adapter.params.port == "/dev/ttyUSB0"
        assert adapter.params.address == 2
        assert adapter.params.rate_hz == 2.5

    def test_explicit_params_path(self) -> None:
        params = WatlowAdapterParams(port="/dev/ttyUSB0", address=3)
        adapter = WatlowAdapter(name="heater", params=params)
        assert adapter.params is params

    def test_rejects_both_params_and_kwargs(self) -> None:
        params = WatlowAdapterParams(port="/dev/ttyUSB0")
        with pytest.raises(TypeError):
            WatlowAdapter(name="heater", params=params, port="/dev/ttyUSB1")

    def test_capabilities_advertised(self) -> None:
        adapter = WatlowAdapter(name="heater", port="/dev/ttyUSB0")
        assert CapaCapability.HAS_SETPOINT in adapter.capabilities
        assert CapaCapability.READS_PROCESS_VAR in adapter.capabilities


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_open_runs_identify_and_caches_info(self) -> None:
        # Under the unified API, ``open_device`` returns an *opened*
        # controller — the adapter no longer calls ``__aenter__`` itself.
        # Close goes through ``Controller.close()``.
        adapter, stub = _make_adapter()
        await adapter.open()
        try:
            assert stub.identify_calls == 1
            assert adapter.device_info is not None
            assert adapter.device_info.part_number.raw == "PM3C1AJ-AAAAAAA"
        finally:
            await adapter.close()
            assert stub.aexited is True

    async def test_open_idempotent(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        await adapter.open()
        # Second call is a no-op: only one identify
        assert stub.identify_calls == 1
        await adapter.close()

    async def test_skip_identify(self) -> None:
        stub = StubWatlowController(signals={("process_value", 1): 0.0})

        async def factory() -> Any:
            return stub

        adapter = WatlowAdapter(
            name="heater",
            port="fake://test",
            identify_on_open=False,
            controller_factory=factory,
        )
        await adapter.open()
        assert stub.identify_calls == 0
        assert adapter.device_info is None
        await adapter.close()

    async def test_stream_before_open_raises(self) -> None:
        adapter, _ = _make_adapter()
        with pytest.raises(AdapterError):
            async for _ in adapter.stream():
                pass

    async def test_close_idempotent(self) -> None:
        adapter, _ = _make_adapter()
        await adapter.open()
        await adapter.close()
        await adapter.close()  # no-op


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


class TestStreaming:
    async def test_emits_record_and_samples(self) -> None:
        adapter, _stub = _make_adapter(rate_hz=100.0)
        await adapter.open()
        await adapter.start(make_start_ctx())
        emissions = await _drain(adapter, max_records=4)
        await adapter.close()

        records, samples, _snapshots = _split(emissions)
        # 2 parameters × ≥2 ticks → ≥4 records
        assert len(records) >= 4
        # Per record we expect one ChannelSample (1:1 with parameter)
        assert len(samples) >= 4
        for r in records:
            assert r.adapter == ADAPTER_ID
            assert r.shape == "long_row"
            # watlowlib.sample_to_row schema (unified API)
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
                "t_mono_ns",
                "t_utc",
                "latency_s",
            } <= set(r.row.keys())

    async def test_channel_samples_link_back(self) -> None:
        adapter, _ = _make_adapter(rate_hz=100.0)
        await adapter.open()
        await adapter.start(make_start_ctx())
        emissions = await _drain(adapter, max_records=4)
        await adapter.close()

        records, samples, _ = _split(emissions)
        record_ids = {r.record_id for r in records}
        assert samples  # non-empty
        for s in samples:
            assert s.source_record_id in record_ids
            assert s.source_field in {"process_value", "setpoint"}
            assert s.unit == "degC"

    async def test_t_mono_ns_relative_to_run_clock(self) -> None:
        adapter, _ = _make_adapter(rate_hz=100.0)
        await adapter.open()
        clock = RunClock.now()
        await adapter.start(make_start_ctx(clock=clock))
        emissions = await _drain(adapter, max_records=2)
        await adapter.close()

        records, samples, _ = _split(emissions)
        # Records' t_mono_ns are sample.monotonic_ns - clock.started_mono_ns,
        # which must be small and positive (anchored at start).
        assert records
        for r in records:
            assert 0 <= r.t_mono_ns < int(60e9)  # within 60 s of start
        for s in samples:
            assert 0 <= s.t_mono_ns < int(60e9)

    async def test_calibration_applied(self) -> None:
        # Channel that maps 0..100 V → 0..1000 degC (silly but deterministic).
        channel = ChannelSpec(
            name="heater.pv",
            kind=ChannelKind.PROCESS_VAR,
            source=WatlowParameter(device="heater", parameter="process_value", instance=1),
            unit="V",
            derived_unit="degC",
            keep_raw=True,
            calibration=LinearTwoPoint(
                input_unit="V",
                output_unit="degC",
                ref_low_raw=0.0,
                ref_low_value=0.0,
                ref_high_raw=100.0,
                ref_high_value=1000.0,
                uncertainty=UncertaintySpec(kind="absolute", value=2.0),
            ),
        )
        # Channel models a non-physical 0..100 V → 0..1000 degC mapping; the
        # wire-side unit would clash with that, so the stub is configured to
        # report no unit (display_unit=None) and the adapter's drift check
        # stays silent.
        stub = StubWatlowController(
            signals={("process_value", 1): 50.0},
            display_unit=None,
        )

        async def factory() -> Any:
            return stub

        adapter = WatlowAdapter(
            name="heater",
            port="fake://test",
            parameters=("process_value",),
            rate_hz=100.0,
            snapshot_period_s=1e6,
            controller_factory=factory,
        )
        adapter.configure_channels([channel])
        await adapter.open()
        await adapter.start(make_start_ctx())
        emissions = await _drain(adapter, max_records=2)
        await adapter.close()
        _records, samples, _ = _split(emissions)
        assert samples
        s = samples[0]
        assert s.value == pytest.approx(500.0)  # 50 V → 500 degC
        assert s.raw == 50.0
        assert s.unit == "degC"
        assert s.uncertainty == pytest.approx(2.0)

    async def test_initial_snapshot_emitted(self) -> None:
        adapter, _ = _make_adapter(rate_hz=100.0)
        await adapter.open()
        await adapter.start(make_start_ctx())
        emissions = await _drain(adapter, max_records=2)
        await adapter.close()
        _records, _samples, snapshots = _split(emissions)
        # At minimum, the initial snapshot before the first batch
        assert len(snapshots) >= 1
        snap0 = snapshots[0]
        assert snap0.adapter == ADAPTER_ID
        assert snap0.health == "ok"
        # Identity captured at open() flows through into the snapshot fields
        assert snap0.fields.get("part_number") == "PM3C1AJ-AAAAAAA"
        assert snap0.fields.get("firmware_id") == 5678

    async def test_periodic_snapshot(self) -> None:
        # A tight snapshot cadence so we see more than just the initial.
        stub = StubWatlowController(
            signals={("process_value", 1): 100.0, ("setpoint", 1): 110.0},
        )

        async def factory() -> Any:
            return stub

        adapter = WatlowAdapter(
            name="heater",
            port="fake://test",
            rate_hz=100.0,
            snapshot_period_s=0.001,  # fire on nearly every tick
            controller_factory=factory,
        )
        adapter.configure_channels(_channels_for_heater())
        await adapter.open()
        await adapter.start(make_start_ctx())
        emissions = await _drain(adapter, max_records=8)
        await adapter.close()
        _records, _samples, snapshots = _split(emissions)
        # Initial + several periodic
        assert len(snapshots) >= 2

    async def test_auto_reconnect_absorbs_disconnect(self) -> None:
        # auto_reconnect=True: a transient WatlowConnectionError on the first
        # poll is absorbed; subsequent polls succeed.
        stub = StubWatlowController(
            signals={("process_value", 1): 25.0, ("setpoint", 1): 30.0},
        )
        stub.raise_on_poll = WatlowConnectionError(
            "transient drop", context=ErrorContext(port="fake://test")
        )

        async def factory() -> Any:
            return stub

        adapter = WatlowAdapter(
            name="heater",
            port="fake://test",
            rate_hz=100.0,
            snapshot_period_s=1e6,
            auto_reconnect=True,
            controller_factory=factory,
        )
        adapter.configure_channels(_channels_for_heater())
        await adapter.open()
        await adapter.start(make_start_ctx())
        # Even with the first poll raising, we should see records once the
        # transient is absorbed and the next tick succeeds.
        emissions = await _drain(adapter, max_records=2)
        await adapter.close()
        records, _, _ = _split(emissions)
        assert len(records) >= 2

    async def test_timeout_propagates_when_auto_reconnect_off(self) -> None:
        stub = StubWatlowController(
            signals={("process_value", 1): 25.0, ("setpoint", 1): 30.0},
        )
        stub.raise_on_poll = WatlowTimeoutError(
            "timed out", context=ErrorContext(port="fake://test")
        )

        async def factory() -> Any:
            return stub

        adapter = WatlowAdapter(
            name="heater",
            port="fake://test",
            rate_hz=100.0,
            snapshot_period_s=1e6,
            auto_reconnect=False,
            controller_factory=factory,
        )
        adapter.configure_channels(_channels_for_heater())
        await adapter.open()
        await adapter.start(make_start_ctx())
        with pytest.raises(AdapterError):
            async for _e in adapter.stream():
                pass
        await adapter.close()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


class TestCommands:
    async def test_unauthorized_command_rejected(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        result = await adapter.command(
            DeviceCommand(
                kind="set_setpoint",
                target="setpoint:1",
                payload={"value": 200.0},
                issued_by="abr",
            )
        )
        assert result.accepted is False
        assert "unauthorized" in result.detail
        # No call ever reached the controller
        assert stub.set_setpoint_calls == []
        await adapter.close()

    async def test_authorized_set_setpoint_dispatches(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        result = await adapter.set_setpoint(
            500.0,
            instance=1,
            issued_by="abr",
            authorization_id="run-42",
        )
        assert result.accepted is True
        assert len(stub.set_setpoint_calls) == 1
        assert stub.set_setpoint_calls[0]["value"] == 500.0
        # Authorization implies confirm=True at the watlowlib boundary.
        assert stub.set_setpoint_calls[0]["confirm"] is True
        await adapter.close()

    async def test_manual_confirmation_dispatches(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        result = await adapter.set_setpoint(
            420.0,
            issued_by="abr",
            confirmed_by="abr",
        )
        assert result.accepted is True
        assert stub.set_setpoint_calls[0]["value"] == 420.0
        await adapter.close()

    async def test_write_parameter_dispatches(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        result = await adapter.write_parameter(
            "setpoint",
            333.0,
            issued_by="abr",
            authorization_id="run-7",
        )
        assert result.accepted is True
        assert stub.write_parameter_calls[0]["name"] == "setpoint"
        assert stub.write_parameter_calls[0]["value"] == 333.0
        assert stub.write_parameter_calls[0]["confirm"] is True
        await adapter.close()

    async def test_command_against_unopened_adapter_rejected(self) -> None:
        adapter, _ = _make_adapter()
        result = await adapter.set_setpoint(100.0, issued_by="abr", authorization_id="run-1")
        assert result.accepted is False
        assert "not open" in result.detail

    async def test_unknown_command_kind(self) -> None:
        adapter, _ = _make_adapter()
        await adapter.open()
        with pytest.raises(AdapterError):
            await adapter.command(
                DeviceCommand(
                    kind="bogus",
                    issued_by="abr",
                    authorization_id="run-1",
                )
            )
        await adapter.close()

    async def test_set_setpoint_error_wrapped(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        stub.raise_on_set_setpoint = WatlowTimeoutError(
            "timed out", context=ErrorContext(port="fake://test")
        )
        with pytest.raises(AdapterError):
            await adapter.set_setpoint(100.0, issued_by="abr", authorization_id="run-1")
        await adapter.close()

    async def test_set_setpoint_inverts_calibration(self) -> None:
        """User-facing °C value is inverted to °F before reaching the wire.

        Pins the contract behind ``wire_temperature_unit = "F"`` rigs:
        the operator types Celsius, the adapter walks the channel
        calibration backwards to compute the Fahrenheit value the
        device expects. Without this, setpoint writes silently land in
        the wire unit and the rig overshoots by ~18°C (32°F).
        """
        stub = StubWatlowController(signals={("setpoint", 1): 0.0})

        async def factory() -> Any:
            return stub

        adapter = WatlowAdapter(
            name="heater",
            port="fake://stub",
            rate_hz=50.0,
            controller_factory=factory,
        )
        # Setpoint channel: degF wire -> degC user-facing via F/C definition points.
        f_to_c = LinearTwoPoint(
            input_unit="degF",
            output_unit="degC",
            ref_low_raw=32.0,
            ref_low_value=0.0,
            ref_high_raw=212.0,
            ref_high_value=100.0,
        )
        adapter.configure_channels(
            [
                ChannelSpec(
                    name="heater.setpoint",
                    kind=ChannelKind.SETPOINT,
                    source=WatlowParameter(device="heater", parameter="setpoint", instance=1),
                    unit="degF",
                    derived_unit="degC",
                    calibration=f_to_c,
                ),
            ]
        )
        await adapter.open()
        result = await adapter.set_setpoint(100.0, issued_by="abr", authorization_id="run-1")
        assert result.accepted is True
        # 100 °C should land on the wire as 212 °F.
        assert stub.set_setpoint_calls[0]["value"] == pytest.approx(212.0)
        # Detail string carries both the user value and the wire value so
        # operator-facing logs are unambiguous.
        assert "user=100.0" in result.detail
        assert "wire=212.0" in result.detail
        await adapter.close()

    async def test_set_setpoint_identity_passthrough(self) -> None:
        """Identity calibration on the setpoint channel: value passes
        through unchanged. Pins the no-op branch of the inversion logic
        so rigs without a unit-conversion calibration don't accidentally
        get a transformation applied."""
        adapter, stub = _make_adapter()  # default channels use Identity
        await adapter.open()
        await adapter.set_setpoint(420.0, issued_by="abr", authorization_id="run-1")
        assert stub.set_setpoint_calls[0]["value"] == pytest.approx(420.0)
        await adapter.close()

    async def test_set_setpoint_no_channel_passthrough(self) -> None:
        """Adapter driven without any configured channels (one-shot
        diagnostic or test harness): no inversion possible, value passes
        through unchanged. The adapter must not refuse to dispatch."""
        stub = StubWatlowController(signals={("setpoint", 1): 0.0})

        async def factory() -> Any:
            return stub

        adapter = WatlowAdapter(
            name="heater",
            port="fake://stub",
            rate_hz=50.0,
            controller_factory=factory,
        )
        # Intentionally do NOT call configure_channels.
        await adapter.open()
        await adapter.set_setpoint(123.0, issued_by="abr", authorization_id="run-1")
        assert stub.set_setpoint_calls[0]["value"] == pytest.approx(123.0)
        await adapter.close()


# ---------------------------------------------------------------------------
# read_pv (read-only, no authorization gate)
# ---------------------------------------------------------------------------


class TestReadPV:
    async def test_read_pv_no_auth_required(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        reading = await adapter.read_pv()
        assert reading.value == 400.0
        assert stub.read_pv_calls == 1
        await adapter.close()

    async def test_read_pv_before_open_raises(self) -> None:
        adapter, _ = _make_adapter()
        with pytest.raises(AdapterError):
            await adapter.read_pv()


# ---------------------------------------------------------------------------
# read_state_snapshot — operator-facing readback for the manual control card
# ---------------------------------------------------------------------------


class TestReadStateSnapshot:
    async def test_read_state_snapshot_returns_setpoint_and_pv(self) -> None:
        adapter, _stub = _make_adapter()
        await adapter.open()
        try:
            snapshot = await adapter.read_state_snapshot()
        finally:
            await adapter.close()
        assert snapshot is not None
        assert snapshot.setpoint == pytest.approx(410.0)
        assert snapshot.process_value == pytest.approx(400.0)
        # Stub display_unit is Unit.CELSIUS by default -> string "C".
        assert snapshot.setpoint_unit == "C"
        assert snapshot.process_value_unit == "C"

    async def test_read_state_snapshot_before_open_returns_none(self) -> None:
        adapter, _ = _make_adapter()
        result = await adapter.read_state_snapshot()
        assert result is None


# ---------------------------------------------------------------------------
# Display unit (parameter 17050) — watlowlib 0.3.0 typed-units integration
# ---------------------------------------------------------------------------


class TestDisplayUnits:
    async def test_open_caches_display_unit(self) -> None:
        adapter, _stub = _make_adapter()
        await adapter.open()
        try:
            # Default stub display unit is Unit.CELSIUS; open() primes the
            # adapter's local cache so snapshot can render it without I/O.
            assert adapter.display_unit is Unit.CELSIUS
        finally:
            await adapter.close()

    async def test_snapshot_includes_display_unit(self) -> None:
        adapter, _ = _make_adapter(rate_hz=100.0)
        await adapter.open()
        await adapter.start(make_start_ctx())
        emissions = await _drain(adapter, max_records=2)
        await adapter.close()
        _r, _s, snaps = _split(emissions)
        assert snaps
        assert snaps[0].fields.get("display_unit") == "C"

    async def test_snapshot_display_unit_none_when_device_rejects(self) -> None:
        # display_unit=None on the stub means read_display_units returns
        # None, mirroring a device that doesn't expose 17050.
        stub = StubWatlowController(
            signals={("process_value", 1): 100.0, ("setpoint", 1): 110.0},
            display_unit=None,
        )

        async def factory() -> Any:
            return stub

        adapter = WatlowAdapter(
            name="heater",
            port="fake://test",
            rate_hz=100.0,
            snapshot_period_s=1e6,
            controller_factory=factory,
        )
        adapter.configure_channels(_channels_for_heater())
        await adapter.open()
        await adapter.start(make_start_ctx())
        emissions = await _drain(adapter, max_records=2)
        await adapter.close()
        _r, _s, snaps = _split(emissions)
        assert snaps
        assert snaps[0].fields.get("display_unit") is None

    async def test_read_display_units_helper(self) -> None:
        adapter, _ = _make_adapter()
        await adapter.open()
        try:
            unit = await adapter.read_display_units()
            assert unit is Unit.CELSIUS
        finally:
            await adapter.close()

    async def test_set_display_units_dispatches(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        result = await adapter.set_display_units(
            Unit.FAHRENHEIT,
            issued_by="abr",
            authorization_id="run-7",
        )
        assert result.accepted is True
        assert len(stub.set_display_unit_calls) == 1
        call = stub.set_display_unit_calls[0]
        assert call["unit"] == "F"  # serialized through DeviceCommand payload
        assert call["confirm"] is True
        # Adapter cache reflects the post-write echo.
        assert adapter.display_unit is Unit.FAHRENHEIT
        await adapter.close()

    async def test_set_display_units_string_alias(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        result = await adapter.set_display_units(
            "celsius",
            issued_by="abr",
            authorization_id="run-7",
        )
        assert result.accepted is True
        assert stub.set_display_unit_calls[0]["unit"] == "celsius"
        assert adapter.display_unit is Unit.CELSIUS
        await adapter.close()

    async def test_set_display_units_unauthorized_rejected(self) -> None:
        adapter, stub = _make_adapter()
        await adapter.open()
        result = await adapter.command(
            DeviceCommand(
                kind="set_display_units",
                payload={"unit": "F"},
                issued_by="abr",
            )
        )
        assert result.accepted is False
        assert stub.set_display_unit_calls == []
        await adapter.close()


# ---------------------------------------------------------------------------
# Wire-side / declared-unit drift check
# ---------------------------------------------------------------------------


def _channels_for_heater_with_fahrenheit() -> list[ChannelSpec]:
    """Channels declare degF; coupled with the default Unit.CELSIUS stub,
    every sample lands as a drift mismatch."""
    return [
        ChannelSpec(
            name="heater.pv",
            kind=ChannelKind.PROCESS_VAR,
            source=WatlowParameter(device="heater", parameter="process_value", instance=1),
            unit="degF",
            derived_unit="degF",
            calibration=Identity(input_unit="degF", output_unit="degF"),
        ),
    ]


def _channels_for_heater_with_mass_unit() -> list[ChannelSpec]:
    """Channels declare grams; coupled with Unit.CELSIUS, every sample
    is *dimensionally* incompatible — sharper assert than degF vs degC."""
    return [
        ChannelSpec(
            name="heater.pv",
            kind=ChannelKind.PROCESS_VAR,
            source=WatlowParameter(device="heater", parameter="process_value", instance=1),
            unit="g",
            derived_unit="g",
            calibration=Identity(input_unit="g", output_unit="g"),
        ),
    ]


class TestUnitDrift:
    async def test_compatible_units_emit_channel_samples(self) -> None:
        # degC channel + Unit.CELSIUS wire → no drift; happy path.
        adapter, _ = _make_adapter(rate_hz=100.0)
        await adapter.open()
        await adapter.start(make_start_ctx())
        emissions = await _drain(adapter, max_records=4)
        await adapter.close()
        _r, samples, _ = _split(emissions)
        # Two channels (pv + setpoint), Unit.CELSIUS matches degC, so we
        # expect ChannelSamples on every tick.
        assert samples
        events = [e for e in emissions if isinstance(e, DeviceEvent)]
        assert events == []

    async def test_mismatch_skips_channel_samples_and_emits_event(self) -> None:
        # Channel declares grams; wire reports °C — dimensionally incompatible.
        stub = StubWatlowController(
            signals={("process_value", 1): 400.0},
            display_unit=Unit.CELSIUS,
        )

        async def factory() -> Any:
            return stub

        adapter = WatlowAdapter(
            name="heater",
            port="fake://test",
            parameters=("process_value",),
            rate_hz=100.0,
            snapshot_period_s=1e6,
            controller_factory=factory,
        )
        adapter.configure_channels(_channels_for_heater_with_mass_unit())
        await adapter.open()
        await adapter.start(make_start_ctx())
        emissions = await _drain(adapter, max_records=4)
        await adapter.close()

        records, samples, _ = _split(emissions)
        events = [e for e in emissions if isinstance(e, DeviceEvent)]
        # Native row preserved for every tick…
        assert len(records) >= 4
        # …but no ChannelSamples derived (channel quarantined).
        assert samples == []
        # …and exactly one event (one-shot per channel; not per tick).
        unit_events = [e for e in events if e.kind == "unit_mismatch"]
        assert len(unit_events) == 1
        ev = unit_events[0]
        assert ev.severity == "error"
        assert ev.metadata.get("channel") == "heater.pv"
        assert ev.metadata.get("declared_unit") == "g"
        assert ev.metadata.get("wire_unit") == "C"

    async def test_degf_vs_degc_is_a_drift(self) -> None:
        # The realistic misconfig: same dimension, different scale. The
        # adapter compares canonical pint names (not just dimensionality)
        # so this fires.
        stub = StubWatlowController(
            signals={("process_value", 1): 400.0},
            display_unit=Unit.CELSIUS,
        )

        async def factory() -> Any:
            return stub

        adapter = WatlowAdapter(
            name="heater",
            port="fake://test",
            parameters=("process_value",),
            rate_hz=100.0,
            snapshot_period_s=1e6,
            controller_factory=factory,
        )
        adapter.configure_channels(_channels_for_heater_with_fahrenheit())
        await adapter.open()
        await adapter.start(make_start_ctx())
        emissions = await _drain(adapter, max_records=2)
        await adapter.close()
        events = [e for e in emissions if isinstance(e, DeviceEvent)]
        unit_events = [e for e in events if e.kind == "unit_mismatch"]
        assert len(unit_events) == 1
        assert unit_events[0].metadata.get("declared_unit") == "degF"
        assert unit_events[0].metadata.get("wire_unit") == "C"

    async def test_set_display_units_clears_drift_quarantine(self) -> None:
        # Start with a mismatch; quarantine the channel. Then call
        # set_display_units; the adapter clears its quarantine set so the
        # next tick re-evaluates. We don't drive a new mismatch here; the
        # cache-clear is the contract.
        stub = StubWatlowController(
            signals={("process_value", 1): 400.0},
            display_unit=Unit.CELSIUS,
        )

        async def factory() -> Any:
            return stub

        adapter = WatlowAdapter(
            name="heater",
            port="fake://test",
            parameters=("process_value",),
            rate_hz=100.0,
            snapshot_period_s=1e6,
            controller_factory=factory,
        )
        adapter.configure_channels(_channels_for_heater_with_mass_unit())
        await adapter.open()
        await adapter.start(make_start_ctx())
        await _drain(adapter, max_records=2)
        # Mismatch is now recorded in the adapter's quarantine set.
        assert "heater.pv" in adapter._drift_skipped_channels  # type: ignore[attr-defined]
        await adapter.close()

        # Re-open to call set_display_units (the stream loop has finished).
        await adapter.open()
        await adapter.set_display_units(
            Unit.FAHRENHEIT,
            issued_by="abr",
            authorization_id="run-1",
        )
        assert adapter._drift_skipped_channels == set()  # type: ignore[attr-defined]
        await adapter.close()
