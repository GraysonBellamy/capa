"""Simulated Alicat adapter — wide-row ``Sample``\\ s.

Mirrors :class:`alicatlib.streaming.Sample` shape via
:func:`alicatlib.sinks.base.sample_to_row`. One ``DataFrame`` per poll, with
firmware-dependent measurement fields (``Mass_Flow``, ``Abs_Press``,
``Mass_Flow_Setpt``, ``Mix_Gas``, …).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

import anyio
from alicatlib.devices.data_frame import (
    DataFrame,
    DataFrameFormat,
    DataFrameFormatFlavor,
)
from alicatlib.sinks.base import sample_to_row
from alicatlib.streaming import Sample as AlicatSample

from capa.channels.spec import AlicatFrameField, ChannelSpec
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

ADAPTER_ID: Final[str] = "alicat"


_EMPTY_FORMAT = DataFrameFormat(fields=(), flavor=DataFrameFormatFlavor.DEFAULT)


@dataclass(slots=True)
class AlicatSim:
    """Simulated Alicat MFC/MFM adapter.

    ``signals`` is keyed by underscored field names (the keys
    :class:`alicatlib.devices.data_frame.DataFrame.as_dict` exposes —
    ``"Mass_Flow"``, ``"Abs_Press"``, etc.).
    """

    name: str
    unit_id: str = "A"
    tick_period_s: float = 0.5
    signals: dict[str, SignalFn] = field(default_factory=dict)
    """``{frame_field: signal}``."""
    static_fields: dict[str, float | str | None] = field(default_factory=dict)
    """Frame fields that are not generated per-tick (e.g. ``"Mix_Gas"``).
    Merged into the emitted DataFrame's ``values`` verbatim."""
    capabilities: frozenset[Capability] = frozenset(
        {
            Capability.HAS_SETPOINT,
            Capability.HAS_TARE,
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
        unit_id: str = "A",
        tick_period_s: float = 0.5,
        signals: dict[str, dict[str, object]] | None = None,
        static_fields: dict[str, float | str | None] | None = None,
    ) -> AlicatSim:
        """TOML-friendly constructor.

        ``signals`` maps a frame-field name (``"Mass_Flow"`` etc.) to a
        serialisable signal spec — see
        :func:`capa.devices.sim._signals.signal_from_dict`."""
        return cls(
            name=name,
            unit_id=unit_id,
            tick_period_s=tick_period_s,
            signals=signals_from_mapping(signals or {}),
            static_fields=static_fields or {},
        )

    def configure_channels(self, specs: list[ChannelSpec]) -> None:
        self._channels = channels_for_device(
            specs, device=self.name, binding_source="alicat_frame_field"
        )

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
                "unit_id": self.unit_id,
                "channel_count": len(self._channels),
                "state": self._lifecycle.state,
            },
        )

    async def stream(self) -> AsyncIterator[DeviceEmission]:
        if self._clock is None:
            raise AdapterError("alicat_sim.stream() requires start() first")
        while self._lifecycle.state == "running":
            for emission in self.tick_once():
                yield emission
            await anyio.sleep(self.tick_period_s)

    def tick_once(self) -> list[DeviceEmission]:
        """Build one wide-row ``DataFrame`` and yield the SourceRecord plus
        one ChannelSample per declared channel."""
        if self._clock is None:
            raise AdapterError("alicat_sim.tick_once() requires start() first")
        clock = self._clock
        t_now_s = clock.t_mono()

        t_mono_ns, requested_at, received_at, midpoint_at, _t_utc, latency_s = synth_timing(clock)

        values: dict[str, float | str | None] = dict(self.static_fields)
        for field_name, signal in self.signals.items():
            values[field_name] = float(signal(t_now_s))

        frame = DataFrame(
            unit_id=self.unit_id,
            format=_EMPTY_FORMAT,
            values=MappingProxyType(values),
            values_by_statistic=MappingProxyType({}),
            status=frozenset(),
            received_at=received_at,
            monotonic_ns=t_mono_ns,
        )
        sample = AlicatSample(
            device=self.name,
            unit_id=self.unit_id,
            monotonic_ns=t_mono_ns,
            requested_at=requested_at,
            received_at=received_at,
            midpoint_at=midpoint_at,
            latency_s=latency_s,
            frame=frame,
        )
        row = sample_to_row(sample)
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
            metadata={"unit_id": self.unit_id},
        )
        emissions: list[DeviceEmission] = [record]

        for spec in self._channels:
            binding = spec.source
            assert isinstance(binding, AlicatFrameField)
            raw_value = values.get(binding.field)
            if raw_value is None or not isinstance(raw_value, (int, float)):
                # Field absent or non-numeric in this frame; surface a status row.
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
                detail="alicat_sim refuses unauthorized commands",
                t_mono_ns=(self._clock or RunClock.now()).t_mono_ns(),
                t_utc=now_utc(),
            )
        clock = self._clock or RunClock.now()
        return CommandResult(
            accepted=True,
            detail=f"sim ack {cmd.kind} target={cmd.target}",
            t_mono_ns=clock.t_mono_ns(),
            t_utc=now_utc(),
        )

    async def set_flow_setpoint(
        self,
        value: float,
        *,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        return await self.command(
            DeviceCommand(
                kind="set_flow_setpoint",
                target="Mass_Flow_Setpt",
                payload={"value": value},
                issued_by=confirmed_by or "sim",
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )


__all__ = ["ADAPTER_ID", "AlicatSim"]
