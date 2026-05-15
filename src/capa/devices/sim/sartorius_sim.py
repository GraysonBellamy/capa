"""Simulated Sartorius adapter — single balance reading per tick.

Mirrors :class:`sartoriuslib.streaming.Sample` shape via
:func:`sartoriuslib.sinks.base.sample_to_row`. Carries stability / overload /
underload flags so a procedure can verify "balance was stable for >= 5 s
prior to ignition" using the preserved native row.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import anyio
from sartoriuslib.devices.models import Reading
from sartoriuslib.protocol.base import ProtocolKind
from sartoriuslib.registry.units import Sign, Unit
from sartoriuslib.sinks.base import sample_to_row
from sartoriuslib.streaming import Sample as SartoriusSample

from capa.channels.spec import ChannelSpec, SartoriusReading
from capa.core.clock import RunClock
from capa.core.errors import AdapterError
from capa.devices.adapter import (
    AdapterLifecycle,
    Capability,
    CommandResult,
    DeviceCommand,
)
from capa.devices.records import (
    DeviceEmission,
    DeviceSnapshot,
    SourceRecord,
)
from capa.devices.sim._base import (
    build_channel_sample,
    channels_for_device,
    make_record_id,
    now_utc,
    synth_timing,
)
from capa.devices.sim._signals import SignalFn, signal_from_dict

if TYPE_CHECKING:
    from capa.devices.registry import AdapterDescriptor

ADAPTER_ID: Final[str] = "sartorius"


@dataclass(slots=True)
class SartoriusSim:
    """Simulated Sartorius balance adapter.

    Emits one ``Sample`` per tick. ``mass_signal`` provides the value (in
    grams by default — pick a unit at construction); ``stable_after_s``
    declares when the balance flips ``stable=True`` (simulating settling).
    """

    name: str
    tick_period_s: float = 0.5
    mass_signal: SignalFn | None = None
    """Signal generator producing the balance reading. Required."""
    unit: Unit = Unit.G
    protocol: ProtocolKind = ProtocolKind.XBPI
    stable_after_s: float = 0.0
    """The balance reports ``stable=False`` until ``t_mono >= stable_after_s``,
    then ``stable=True``. Use to simulate a settling window."""
    capabilities: frozenset[Capability] = frozenset({Capability.HAS_TARE, Capability.HAS_ZERO})
    _lifecycle: AdapterLifecycle = field(default_factory=AdapterLifecycle)
    _channels: list[ChannelSpec] = field(default_factory=list)
    _clock: RunClock | None = None
    _seq: int = 0
    _sequence: int = 0

    @classmethod
    def from_params(
        cls,
        *,
        name: str,
        mass_signal: dict[str, object] | None = None,
        unit: str | None = None,
        protocol: str | None = None,
        tick_period_s: float = 0.5,
        stable_after_s: float = 0.0,
    ) -> SartoriusSim:
        """TOML-friendly constructor used by the engine adapter resolver.

        ``mass_signal`` is a serialisable signal spec
        (``{"kind": "constant", "value": 5.0}`` etc.); ``unit`` / ``protocol``
        are stringified versions of the matching enum members."""
        signal: SignalFn | None = signal_from_dict(mass_signal) if mass_signal is not None else None
        out_unit = Unit(unit) if unit is not None else Unit.G
        out_protocol = ProtocolKind(protocol) if protocol is not None else ProtocolKind.XBPI
        return cls(
            name=name,
            tick_period_s=tick_period_s,
            mass_signal=signal,
            unit=out_unit,
            protocol=out_protocol,
            stable_after_s=stable_after_s,
        )

    def configure_channels(self, specs: list[ChannelSpec]) -> None:
        self._channels = channels_for_device(
            specs, device=self.name, binding_source="sartorius_reading"
        )

    @property
    def expected_emission_rate_hz(self) -> float:
        rate = 1.0 / self.tick_period_s if self.tick_period_s > 0 else 0.0
        return rate * (1 + len(self._channels))

    @property
    def resource_id(self) -> str:
        return f"sim:{self.name}"

    async def open(self) -> None:
        self._lifecycle.open()

    async def close(self) -> None:
        self._lifecycle.close()

    async def start(self, clock: RunClock | None = None) -> None:
        self._lifecycle.start()
        self._clock = clock or RunClock.now()
        self._sequence = 0

    async def stop(self) -> None:
        self._lifecycle.stop()

    async def snapshot(self) -> DeviceSnapshot:
        clock = self._clock or RunClock.now()
        return DeviceSnapshot(
            adapter=ADAPTER_ID,
            device=self.name,
            t_mono_ns=clock.t_mono_ns(),
            t_utc=now_utc(),
            health="ok" if self._lifecycle.state == "running" else "down",
            fields={
                "protocol": self.protocol.value,
                "unit": self.unit.value,
                "channel_count": len(self._channels),
                "state": self._lifecycle.state,
            },
        )

    async def stream(self) -> AsyncIterator[DeviceEmission]:
        if self._clock is None:
            raise AdapterError("sartorius_sim.stream() requires start() first")
        while self._lifecycle.state == "running":
            for emission in self.tick_once():
                yield emission
            await anyio.sleep(self.tick_period_s)

    def tick_once(self) -> list[DeviceEmission]:
        if self._clock is None:
            raise AdapterError("sartorius_sim.tick_once() requires start() first")
        if self.mass_signal is None:
            raise AdapterError(f"sartorius_sim {self.name!r}: mass_signal is required")
        clock = self._clock
        t_now_s = clock.t_mono()
        t_mono_ns, requested_at, received_at, midpoint_at, _t_utc, latency_s = synth_timing(clock)
        value = float(self.mass_signal(t_now_s))
        stable = t_now_s >= self.stable_after_s
        sign = Sign.ZERO if value == 0 else Sign.POSITIVE if value > 0 else Sign.NEGATIVE
        self._sequence += 1
        reading = Reading(
            value=value,
            unit=self.unit,
            sign=sign,
            stable=stable,
            overload=False,
            underload=False,
            decimals=3,
            sequence=self._sequence,
            status_flags=MappingProxyType({"stable": stable}),
            protocol=self.protocol,
            received_at=received_at,
            monotonic_ns=t_mono_ns,
            raw=b"",
        )
        sample = SartoriusSample(
            device=self.name,
            reading=reading,
            requested_at=requested_at,
            received_at=received_at,
            midpoint_at=midpoint_at,
            monotonic_ns=t_mono_ns,
            latency_s=latency_s,
            protocol=self.protocol,
        )
        row = sample_to_row(sample)
        self._seq += 1
        record_id = make_record_id(ADAPTER_ID, self.name, self._seq)
        record = SourceRecord(
            record_id=record_id,
            adapter=ADAPTER_ID,
            device=self.name,
            shape="single_value_row",
            t_mono_ns=t_mono_ns,
            t_utc=midpoint_at,
            row=row,
            metadata={"protocol": self.protocol.value},
        )
        emissions: list[DeviceEmission] = [record]

        for spec in self._channels:
            binding = spec.source
            assert isinstance(binding, SartoriusReading)
            field_name = binding.field
            if field_name == "value":
                raw_value: float | None = value
            elif field_name == "stable":
                raw_value = 1.0 if stable else 0.0
            else:
                # Unknown field; surface no sample
                continue
            if raw_value is None:
                continue
            emissions.append(
                build_channel_sample(
                    spec=spec,
                    raw_value=float(raw_value),
                    t_mono_ns=t_mono_ns,
                    source_record_id=record_id,
                    source_field=field_name,
                    status="ok" if stable else "settling",
                )
            )
        return emissions

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        if cmd.authorization_id is None and cmd.confirmed_by is None:
            return CommandResult(
                accepted=False,
                detail="sartorius_sim refuses unauthorized commands",
                t_mono_ns=(self._clock or RunClock.now()).t_mono_ns(),
                t_utc=now_utc(),
            )
        clock = self._clock or RunClock.now()
        return CommandResult(
            accepted=True,
            detail=f"sim ack {cmd.kind}",
            t_mono_ns=clock.t_mono_ns(),
            t_utc=now_utc(),
        )

    async def tare(
        self,
        *,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        return await self.command(
            DeviceCommand(
                kind="tare",
                payload={},
                issued_by=confirmed_by or "sim",
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )


__all__ = ["ADAPTER_ID", "DESCRIPTOR", "SartoriusSim"]


def _build_descriptor() -> AdapterDescriptor:
    from capa.devices._templates import SARTORIUS_MASS  # noqa: PLC0415
    from capa.devices.registry import AdapterDescriptor  # noqa: PLC0415

    return AdapterDescriptor(
        id="capa.devices.sim.sartorius_sim",
        label="Sartorius balance (simulated)",
        family="sim",
        adapter_factory=SartoriusSim,
        params_model=None,
        supported_binding_sources=("sartorius_reading",),
        default_params={},
        channel_templates=(SARTORIUS_MASS,),
    )


DESCRIPTOR = _build_descriptor()

from capa.devices.registry import register as _register  # noqa: E402

_register(DESCRIPTOR)
