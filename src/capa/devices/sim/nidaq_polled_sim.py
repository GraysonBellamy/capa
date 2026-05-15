"""Simulated NI-DAQ polled adapter — wide-row :class:`DaqReading` per tick.

Mirrors :func:`nidaqlib.sinks.base.reading_to_row`; one row per poll with one
column per channel and a parallel ``<channel>_unit`` column.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import anyio
from nidaqlib.sinks.base import reading_to_row
from nidaqlib.tasks.models import DaqReading

from capa.channels.spec import ChannelSpec, NIDAQReadingField
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
from capa.devices.sim._signals import SignalFn, signals_from_mapping

if TYPE_CHECKING:
    from capa.devices.registry import AdapterDescriptor

ADAPTER_ID: Final[str] = "nidaq_polled"


@dataclass(slots=True)
class NIDAQPolledSim:
    """Simulated NI-DAQ polled adapter.

    ``signals`` is keyed by channel display name (matching
    :class:`NIDAQReadingField.field`). One :class:`DaqReading` is emitted per
    tick, with all signals evaluated at the same monotonic timestamp.
    """

    name: str
    """``device`` in the SourceRecord; matches the ``device`` field of the
    underlying ``DaqReading``."""
    task: str = "default_task"
    """Task name (``TaskSpec.name``); matches ``DaqReading.task`` and the
    ``task`` field of :class:`NIDAQReadingField`."""
    tick_period_s: float = 0.1
    signals: dict[str, SignalFn] = field(default_factory=dict)
    units: dict[str, str | None] = field(default_factory=dict)
    """Per-channel display unit, e.g. ``{"TC_top_1": "K", "AI0": "V"}``."""
    capabilities: frozenset[Capability] = frozenset(
        {Capability.HARDWARE_CLOCKED, Capability.SUPPORTS_DISCOVERY}
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
        task: str = "default_task",
        tick_period_s: float = 0.1,
        signals: dict[str, dict[str, object]] | None = None,
        units: dict[str, str | None] | None = None,
    ) -> NIDAQPolledSim:
        """TOML-friendly constructor.

        ``signals`` maps a channel display name (matching
        :class:`capa.channels.spec.NIDAQReadingField.field`) to a
        serialisable signal spec."""
        return cls(
            name=name,
            task=task,
            tick_period_s=tick_period_s,
            signals=signals_from_mapping(signals or {}),
            units=units or {},
        )

    def configure_channels(self, specs: list[ChannelSpec]) -> None:
        self._channels = channels_for_device(
            specs, device=self.name, binding_source="nidaq_reading_field"
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
                "task": self.task,
                "channel_count": len(self.signals),
                "state": self._lifecycle.state,
            },
        )

    async def stream(self) -> AsyncIterator[DeviceEmission]:
        if self._clock is None:
            raise AdapterError("nidaq_polled_sim.stream() requires start() first")
        while self._lifecycle.state == "running":
            for emission in self.tick_once():
                yield emission
            await anyio.sleep(self.tick_period_s)

    def tick_once(self) -> list[DeviceEmission]:
        if self._clock is None:
            raise AdapterError("nidaq_polled_sim.tick_once() requires start() first")
        clock = self._clock
        t_now_s = clock.t_mono()
        t_mono_ns, requested_at, received_at, midpoint_at, _t_utc, latency_s = synth_timing(clock)
        values: dict[str, float | int | bool] = {
            ch: float(sig(t_now_s)) for ch, sig in self.signals.items()
        }
        units: dict[str, str | None] = {ch: self.units.get(ch) for ch in self.signals}
        reading = DaqReading(
            device=self.name,
            task=self.task,
            values=MappingProxyType(values),
            units=MappingProxyType(units),
            requested_at=requested_at,
            received_at=received_at,
            midpoint_at=midpoint_at,
            monotonic_ns=t_mono_ns,
            latency_s=latency_s,
        )
        row = reading_to_row(reading)
        self._seq += 1
        record_id = make_record_id(ADAPTER_ID, self.name, self._seq)
        record = SourceRecord(
            record_id=record_id,
            adapter=ADAPTER_ID,
            device=self.name,
            shape="wide_row",
            t_mono_ns=t_mono_ns,
            t_utc=midpoint_at,
            row=row,
            metadata={"task": self.task},
        )
        emissions: list[DeviceEmission] = [record]

        for spec in self._channels:
            binding = spec.source
            assert isinstance(binding, NIDAQReadingField)
            if binding.task != self.task:
                continue
            raw_value = values.get(binding.field)
            if raw_value is None:
                continue
            emissions.append(
                build_channel_sample(
                    spec=spec,
                    raw_value=float(raw_value),
                    t_mono_ns=t_mono_ns,
                    source_record_id=record_id,
                    source_field=binding.field,
                )
            )
        return emissions

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        if cmd.authorization_id is None and cmd.confirmed_by is None:
            return CommandResult(
                accepted=False,
                detail="nidaq_polled_sim refuses unauthorized commands",
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


__all__ = ["ADAPTER_ID", "DESCRIPTOR", "NIDAQPolledSim"]


def _build_descriptor() -> AdapterDescriptor:
    from capa.devices._templates import NIDAQ_THERMOCOUPLE  # noqa: PLC0415
    from capa.devices.registry import AdapterDescriptor  # noqa: PLC0415

    return AdapterDescriptor(
        id="capa.devices.sim.nidaq_polled_sim",
        label="NI-DAQ polled task (simulated)",
        family="sim",
        adapter_factory=NIDAQPolledSim,
        params_model=None,
        supported_binding_sources=("nidaq_reading_field",),
        default_params={},
        channel_templates=(NIDAQ_THERMOCOUPLE,),
    )


DESCRIPTOR = _build_descriptor()

from capa.devices.registry import register as _register  # noqa: E402

_register(DESCRIPTOR)
