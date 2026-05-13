"""In-process stub :class:`watlowlib.Controller` for capa tests.

Duck-types the surface :class:`capa.devices.watlow.WatlowAdapter` actually
calls (``__aenter__`` / ``__aexit__`` / ``identify`` / ``poll_many`` /
``set_setpoint`` / ``write_parameter`` / ``read_pv``) so unit and integration
tests can drive the real adapter without scripting bytes on a
:class:`watlowlib.transport.fake.FakeTransport`.

The stub records every command call so tests can assert on dispatch.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from watlowlib import PARAMETERS, Unit
from watlowlib.devices.capability import Capability as WatlowCapability
from watlowlib.devices.models import (
    DeviceHealth,
    DeviceInfo,
    ParameterEntry,
    PartNumber,
    Reading,
)
from watlowlib.protocol.base import ProtocolKind
from watlowlib.registry.families import ControllerFamily
from watlowlib.registry.units import resolve_unit
from watlowlib.streaming.sample import Sample
from watlowlib.transport.base import SerialSettings


def make_pm_device_info(*, address: int = 1) -> DeviceInfo:
    """Synthesize a plausible PM3 :class:`DeviceInfo` for tests."""
    part = PartNumber(raw="PM3C1AJ-AAAAAAA", family=ControllerFamily.PM)
    return DeviceInfo(
        part_number=part,
        hardware_id=1234,
        firmware_id=5678,
        serial_number="SN-TEST-001",
        family=ControllerFamily.PM,
        protocol=ProtocolKind.STDBUS,
        address=address,
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
    Failed-poll behavior is simulated via :attr:`raise_on_poll`; failed
    setpoint writes via :attr:`raise_on_set_setpoint`. Both are one-shot so
    retries can succeed.
    """

    def __init__(
        self,
        *,
        signals: dict[tuple[str, int], float | int | None],
        info: DeviceInfo | None = None,
        display_unit: Unit | None = Unit.CELSIUS,
    ) -> None:
        self.signals = signals
        self.info = info or make_pm_device_info()
        # Sample.unit / Reading.unit are derived from the registry's
        # per-parameter ``unit_kind`` resolved against this stub's display
        # unit. Set to ``None`` for tests that exercise non-physical signals
        # (e.g. mock-voltage calibration paths) where the drift check should
        # not fire.
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

    async def identify(
        self, *, query_configured_protocol: bool = False, **_kwargs: Any
    ) -> DeviceInfo:
        self.identify_calls += 1
        return self.info

    async def poll_many(
        self,
        parameters: Sequence[str | int],
        *,
        names: Sequence[str] | None = None,
        instances: Sequence[int] = (1,),
    ) -> list[Sample]:
        del names
        if self.raise_on_poll is not None:
            exc = self.raise_on_poll
            self.raise_on_poll = None  # one-shot
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
                        address=self.info.address,
                        protocol=ProtocolKind.STDBUS,
                        parameter=param,
                        parameter_id=spec.parameter_id,
                        instance=inst,
                        value=value,
                        unit=resolve_unit(spec.unit_kind, self.display_unit),
                        monotonic_ns=mono,
                        requested_at=now,
                        received_at=now,
                        midpoint_at=now,
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
        # Mirror the real controller: accept Unit-or-alias, normalize to Unit.
        if isinstance(unit, Unit):
            self.display_unit = unit
        else:
            from watlowlib.registry.units import coerce_unit

            self.display_unit = coerce_unit(unit)
        return self.display_unit


__all__ = ["StubWatlowController", "make_pm_device_info"]
