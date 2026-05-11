"""Simulated Watlow adapter — long-format ``Sample``\\ s.

Mirrors :class:`watlowlib.streaming.Sample`'s shape exactly via
:func:`watlowlib.sinks.base.sample_to_row` so the resulting
``device_records/watlow.parquet`` (P0b) is indistinguishable from what a real
Watlow recorder would emit.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Final, cast

import anyio
from watlowlib.protocol.base import ProtocolKind
from watlowlib.sinks.base import sample_to_row
from watlowlib.streaming import Sample as WatlowSample

from capa.channels.spec import ChannelSpec, WatlowParameter
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
from capa.devices.sim._signals import SignalFn, watlow_signals_from_mapping

ADAPTER_ID: Final[str] = "watlow"


@dataclass(slots=True)
class WatlowSim:
    """Simulated Watlow controller adapter.

    ``signals`` is keyed by ``(parameter, instance)`` tuples — mirroring how
    :class:`watlowlib.streaming.Sample`\\ s are addressed (one row per
    ``(parameter, instance)`` per tick).

    Plan §5.6 lists Watlow as long-format: this sim emits one ``SourceRecord``
    per ``(parameter, instance)`` per tick, plus one ``ChannelSample`` per
    declared channel.
    """

    name: str
    address: int = 1
    protocol: ProtocolKind = ProtocolKind.STDBUS
    tick_period_s: float = 1.0
    signals: dict[tuple[str, int], SignalFn] = field(default_factory=dict)
    """``{(parameter_name, instance): signal}`` — e.g.
    ``{("process_value", 1): Sine(amplitude=5, frequency_hz=0.05, offset=400)}``.
    """
    parameter_units: dict[str, str | None] = field(default_factory=dict)
    """Map parameter name → display unit. Watlow's ``Sample.unit`` is often
    ``None`` (the registry doesn't carry per-parameter units yet); set
    explicitly here when the test wants a unit string preserved into the
    SourceRecord row."""
    parameter_ids: dict[str, int] = field(default_factory=dict)
    """Optional override of registry ``parameter_id`` per parameter name. The
    real registry assigns these (e.g. ``"process_value" -> 4001``); sims
    default to a stable hash if unset."""
    capabilities: frozenset[Capability] = frozenset(
        {
            Capability.HAS_SETPOINT,
            Capability.HAS_RAMP,
            Capability.READS_PROCESS_VAR,
        }
    )
    _lifecycle: AdapterLifecycle = field(default_factory=AdapterLifecycle)
    _channels: list[ChannelSpec] = field(default_factory=list)
    _clock: RunClock | None = None
    _seq: int = 0

    @classmethod
    def from_params(
        cls,
        *,
        name: str,
        address: int = 1,
        protocol: str | None = None,
        tick_period_s: float = 1.0,
        signals: dict[str, dict[str, object]] | None = None,
        parameter_units: dict[str, str | None] | None = None,
        parameter_ids: dict[str, int] | None = None,
    ) -> WatlowSim:
        """TOML-friendly constructor.

        ``signals`` maps ``"<parameter>/<instance>"`` (e.g. ``"setpoint/1"``)
        to a serialisable signal spec — see
        :func:`capa.devices.sim._signals.signal_from_dict`. Bare
        ``"process_value"`` defaults to instance 1."""
        out_signals = watlow_signals_from_mapping(cast(dict[object, object], signals or {}))
        return cls(
            name=name,
            address=address,
            protocol=ProtocolKind(protocol) if protocol is not None else ProtocolKind.STDBUS,
            tick_period_s=tick_period_s,
            signals=out_signals,
            parameter_units=parameter_units or {},
            parameter_ids=parameter_ids or {},
        )

    def configure_channels(self, specs: list[ChannelSpec]) -> None:
        """Bind the adapter to the channels declared in the experiment config."""
        self._channels = channels_for_device(
            specs, device=self.name, binding_source="watlow_parameter"
        )

    @property
    def expected_emission_rate_hz(self) -> float:
        rate = 1.0 / self.tick_period_s if self.tick_period_s > 0 else 0.0
        return rate * (1 + len(self._channels))

    async def open(self) -> None:
        self._lifecycle.open()

    async def close(self) -> None:
        self._lifecycle.close()

    async def start(self, clock: RunClock | None = None) -> None:
        self._lifecycle.start()
        self._clock = clock or RunClock.now()

    async def stop(self) -> None:
        self._lifecycle.stop()

    async def snapshot(self) -> DeviceSnapshot:
        clock = self._clock or RunClock.now()
        return DeviceSnapshot(
            adapter=ADAPTER_ID,
            device=self.name,
            t_mono_ns=clock.t_mono_ns(),
            t_utc=now_utc(),
            healthy=self._lifecycle.state == "running",
            fields={
                "address": self.address,
                "protocol": self.protocol.value,
                "channel_count": len(self._channels),
                "state": self._lifecycle.state,
            },
        )

    async def stream(self) -> AsyncIterator[DeviceEmission]:
        """Yield emissions for one tick at a time.

        The stream stops when :meth:`stop` is called from outside; tests that
        want a bounded stream typically iterate ``ticks=N`` ticks via
        :meth:`tick_once` instead.
        """
        if self._clock is None:
            raise AdapterError("watlow_sim.stream() requires start() first")
        while self._lifecycle.state == "running":
            for emission in self.tick_once():
                yield emission
            await anyio.sleep(self.tick_period_s)

    def tick_once(self) -> list[DeviceEmission]:
        """Produce one tick worth of emissions synchronously.

        Returns a flat list of (SourceRecord, ChannelSample, ...) per
        ``(parameter, instance)`` in :attr:`signals`. Watlow polls a small
        group of parameters per tick, so each tick produces N pairs.
        """
        if self._clock is None:
            raise AdapterError("watlow_sim.tick_once() requires start() first")
        emissions: list[DeviceEmission] = []
        clock = self._clock
        t_now_s = clock.t_mono()

        for (parameter, instance), signal in self.signals.items():
            t_mono_ns, requested_at, received_at, midpoint_at, _t_utc, latency_s = synth_timing(
                clock
            )
            value = float(signal(t_now_s))
            unit = self.parameter_units.get(parameter)
            parameter_id = self.parameter_ids.get(parameter, _stable_param_id(parameter))

            sample = WatlowSample(
                device=self.name,
                address=self.address,
                protocol=self.protocol,
                parameter=parameter,
                parameter_id=parameter_id,
                instance=instance,
                value=value,
                unit=unit,
                monotonic_ns=t_mono_ns,
                requested_at=requested_at,
                received_at=received_at,
                midpoint_at=midpoint_at,
                latency_s=latency_s,
                raw=b"",
            )
            row = sample_to_row(sample)
            self._seq += 1
            record_id = make_record_id(ADAPTER_ID, self.name, self._seq)
            record = SourceRecord(
                record_id=record_id,
                adapter=ADAPTER_ID,
                device=self.name,
                shape="long_row",
                t_mono_ns=t_mono_ns,
                t_utc=midpoint_at,
                row=row,
                metadata={"parameter_id": parameter_id},
            )
            emissions.append(record)

            for spec in self._channels:
                binding = spec.source
                assert isinstance(binding, WatlowParameter)
                if binding.parameter == parameter and binding.instance == instance:
                    emissions.append(
                        build_channel_sample(
                            spec=spec,
                            raw_value=value,
                            t_mono_ns=t_mono_ns,
                            source_record_id=record_id,
                            source_field=parameter,
                        )
                    )
        return emissions

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        if cmd.authorization_id is None and cmd.confirmed_by is None:
            return CommandResult(
                accepted=False,
                detail="watlow_sim refuses unauthorized commands",
                t_mono_ns=(self._clock or RunClock.now()).t_mono_ns(),
                t_utc=now_utc(),
            )
        # Sim always accepts. Real adapter (P0d) writes to the device.
        clock = self._clock or RunClock.now()
        return CommandResult(
            accepted=True,
            detail=f"sim ack {cmd.kind} target={cmd.target}",
            t_mono_ns=clock.t_mono_ns(),
            t_utc=now_utc(),
        )

    # Convenience typed methods (parallel to the real adapter's two-tier API,
    # plan §5.2). Tests may use these instead of going through .command().
    async def set_setpoint(
        self,
        value: float,
        *,
        instance: int = 1,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        return await self.command(
            DeviceCommand(
                kind="set_setpoint",
                target=f"setpoint:{instance}",
                payload={"value": value, "instance": instance},
                issued_by=confirmed_by or "sim",
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )


def _stable_param_id(parameter: str) -> int:
    """Deterministic small-int parameter id for sims, derived from the name."""
    return abs(hash(parameter)) % 99999


__all__ = ["ADAPTER_ID", "WatlowSim"]
