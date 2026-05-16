"""Simulated Alicat adapter — wide-row ``Sample``\\ s.

Mirrors :class:`alicatlib.streaming.Sample` shape via
:func:`alicatlib.sample_to_row`. One ``Reading`` per poll, with
firmware-dependent measurement fields (``Mass_Flow``, ``Abs_Press``,
``Mass_Flow_Setpt``, ``Mix_Gas``, …).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import anyio
from alicatlib import (
    Reading,
    sample_to_row,
)
from alicatlib import (
    Sample as AlicatSample,
)
from alicatlib.devices.reading import (
    DataFrameFormat,
    DataFrameFormatFlavor,
)

from capa.channels.spec import AlicatFrameField, ChannelSpec
from capa.core.clock import RunClock
from capa.core.errors import AdapterError
from capa.devices.adapter import (
    AdapterLifecycle,
    AdapterStartContext,
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
    make_accepted_result,
    make_record_id,
    now_utc,
    reject_unless_authorized,
    synth_timing,
)
from capa.devices.sim._signals import SignalFn, signals_from_mapping

if TYPE_CHECKING:
    from capa.devices.registry import AdapterDescriptor

ADAPTER_ID: Final[str] = "alicat"


_EMPTY_FORMAT = DataFrameFormat(fields=(), flavor=DataFrameFormatFlavor.DEFAULT)


@dataclass(slots=True)
class AlicatSim:
    """Simulated Alicat MFC/MFM adapter.

    ``signals`` is keyed by underscored field names (the keys
    :meth:`alicatlib.Reading.as_dict` exposes —
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

    async def start(self, ctx: AdapterStartContext) -> None:
        self._lifecycle.start()
        self._clock = ctx.clock

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

        reading = Reading(
            unit_id=self.unit_id,
            reading_format=_EMPTY_FORMAT,
            values=MappingProxyType(values),
            values_by_statistic=MappingProxyType({}),
            status=frozenset(),
            received_at=received_at,
            t_mono_ns=t_mono_ns,
        )
        sample = AlicatSample(
            device=self.name,
            unit_id=self.unit_id,
            t_mono_ns=t_mono_ns,
            t_utc=midpoint_at,
            requested_at=requested_at,
            received_at=received_at,
            latency_s=latency_s,
            reading=reading,
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
        clock = self._clock or RunClock.now()
        rejection = reject_unless_authorized(
            cmd, adapter_id=ADAPTER_ID, device_name=self.name, clock=clock
        )
        if rejection is not None:
            return rejection
        return make_accepted_result(detail=f"sim ack {cmd.kind} target={cmd.target}", clock=clock)

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


__all__ = ["ADAPTER_ID", "DESCRIPTOR", "AlicatSim"]


def _build_descriptor() -> AdapterDescriptor:
    from capa.devices._templates import ALICAT_PURGE_FLOW  # noqa: PLC0415
    from capa.devices.registry import AdapterDescriptor  # noqa: PLC0415

    return AdapterDescriptor(
        id="capa.devices.sim.alicat_sim",
        label="Alicat MFC / MFM (simulated)",
        family="sim",
        adapter_factory=AlicatSim,
        params_model=None,
        supported_binding_sources=("alicat_frame_field",),
        default_params={},
        channel_templates=(ALICAT_PURGE_FLOW,),
    )


DESCRIPTOR = _build_descriptor()

from capa.devices.registry import register as _register  # noqa: E402

_register(DESCRIPTOR)
