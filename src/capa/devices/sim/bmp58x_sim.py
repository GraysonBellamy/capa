"""Simulated BMP58x adapter — one pressure / temperature pair per tick.

Mirrors :class:`capa.devices.bmp58x.Bmp58xAdapter`: the same wide row via
:func:`capa.devices.bmp58x.bmp58x_row`, the same ``bmp58x_reading``
binding, the same commands. ``pressure_signal`` (Pa) and
``temperature_signal`` (°C) provide the values; both default to a steady
lab (101 325 Pa, 22 °C) so a bare ``[[devices]]`` entry works.

Keeps the oversampling the real adapter keeps, so ``device_settings``
reads back what it set.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import anyio

from capa.channels.spec import Bmp58xReading, ChannelSpec
from capa.core.clock import RunClock
from capa.core.errors import AdapterError
from capa.devices.adapter import (
    AdapterLifecycle,
    AdapterStartContext,
    Capability,
    CommandResult,
    DeviceCommand,
)
from capa.devices.bmp58x import (
    BMP58X_SETTINGS,
    Bmp58xStateSnapshot,
    Measurement,
    bmp58x_row,
    convert_pressure,
    convert_temperature,
    osr_code,
    raw_from_pressure,
    raw_from_temperature,
)
from capa.devices.records import DeviceEmission, DeviceSnapshot, SourceRecord
from capa.devices.sim._base import (
    build_channel_sample,
    channels_for_device,
    make_accepted_result,
    make_record_id,
    now_utc,
    reject_unless_authorized,
    synth_timing,
)
from capa.devices.sim._signals import Constant, SignalFn, signal_from_dict

if TYPE_CHECKING:
    from capa.devices.registry import AdapterDescriptor

ADAPTER_ID: Final[str] = "bmp58x"

_SIM_LATENCY_S: Final[float] = 0.06
_SIM_CHIP: Final[str] = "BMP580/BMP581"
_SIM_REVISION: Final[str] = "0x32"


@dataclass(slots=True)
class Bmp58xSim:
    """Simulated BMP58x barometer adapter.

    Emits one record per tick. ``pressure_signal`` is in Pa and
    ``temperature_signal`` in °C; the raw words are derived from them so
    the row looks exactly like the real adapter's.
    """

    name: str
    tick_period_s: float = 1.0
    pressure_signal: SignalFn = field(default_factory=lambda: Constant(101_325.0))
    temperature_signal: SignalFn = field(default_factory=lambda: Constant(22.0))
    pressure_oversampling: int = 64
    temperature_oversampling: int = 4
    capabilities: frozenset[Capability] = frozenset({Capability.HAS_PARAMETER_CONFIG})
    _lifecycle: AdapterLifecycle = field(default_factory=AdapterLifecycle)
    _channels: list[ChannelSpec] = field(default_factory=list)
    _clock: RunClock | None = None
    _seq: int = 0
    _last: Measurement | None = None

    @classmethod
    def from_params(
        cls,
        *,
        name: str,
        pressure_signal: dict[str, object] | None = None,
        temperature_signal: dict[str, object] | None = None,
        tick_period_s: float = 1.0,
        pressure_oversampling: int = 64,
        temperature_oversampling: int = 4,
    ) -> Bmp58xSim:
        """TOML-friendly constructor used by the engine adapter resolver.

        ``pressure_signal`` / ``temperature_signal`` are serialisable signal
        specs (``{"kind": "constant", "value": 101325.0}`` etc.)."""
        for value in (pressure_oversampling, temperature_oversampling):
            try:
                osr_code(value)
            except ValueError as exc:
                raise AdapterError(f"bmp58x_sim {name!r}: {exc}") from exc
        kwargs: dict[str, Any] = {
            "name": name,
            "tick_period_s": tick_period_s,
            "pressure_oversampling": pressure_oversampling,
            "temperature_oversampling": temperature_oversampling,
        }
        if pressure_signal is not None:
            kwargs["pressure_signal"] = signal_from_dict(pressure_signal)
        if temperature_signal is not None:
            kwargs["temperature_signal"] = signal_from_dict(temperature_signal)
        return cls(**kwargs)

    def configure_channels(self, specs: list[ChannelSpec]) -> None:
        """Bind this adapter to the channel specs that target it."""
        self._channels = channels_for_device(
            specs, device=self.name, binding_source="bmp58x_reading"
        )

    @property
    def expected_emission_rate_hz(self) -> float:
        """Emission rate hint for queue sizing. See :class:`~capa.devices.adapter.DeviceAdapter`."""
        rate = 1.0 / self.tick_period_s if self.tick_period_s > 0 else 0.0
        return rate * (1 + len(self._channels))

    @property
    def resource_id(self) -> str:
        """Stable contention-domain identifier. See :class:`~capa.devices.adapter.DeviceAdapter`."""
        return f"sim:{self.name}"

    @property
    def last_measurement(self) -> Measurement | None:
        """The most recent reading, from the stream or a one-shot read."""
        return self._last

    async def open(self) -> None:
        """Open the underlying connection. See :class:`~capa.devices.adapter.DeviceAdapter`."""
        self._lifecycle.open()

    async def close(self) -> None:
        """Close the underlying connection. Idempotent."""
        self._lifecycle.close()

    async def start(self, ctx: AdapterStartContext) -> None:
        """Begin sampling. See :class:`~capa.devices.adapter.DeviceAdapter`."""
        self._lifecycle.start()
        self._clock = ctx.clock

    async def stop(self) -> None:
        """Stop sampling without closing the connection."""
        self._lifecycle.stop()

    async def snapshot(self) -> DeviceSnapshot:
        """Return a health/status snapshot. See :class:`~capa.devices.adapter.DeviceAdapter`."""
        clock = self._clock or RunClock.now()
        last = self._last
        return DeviceSnapshot(
            adapter=ADAPTER_ID,
            device=self.name,
            t_mono_ns=clock.t_mono_ns(),
            t_utc=now_utc(),
            health="ok" if self._lifecycle.state == "running" else "down",
            fields={
                "pressure_oversampling": self.pressure_oversampling,
                "temperature_oversampling": self.temperature_oversampling,
                "rate_hz": 1.0 / self.tick_period_s if self.tick_period_s > 0 else 0.0,
                "channel_count": len(self._channels),
                "state": self._lifecycle.state,
                "chip": _SIM_CHIP,
                "last_pressure_pa": last.pressure_pa if last else None,
                "last_temperature_c": last.temperature_c if last else None,
            },
        )

    async def stream(self) -> AsyncIterator[DeviceEmission]:
        """Yield emissions while sampling. See :class:`~capa.devices.adapter.DeviceAdapter`."""
        if self._clock is None:
            raise AdapterError("bmp58x_sim.stream() requires start() first")
        while self._lifecycle.state == "running":
            for emission in self.tick_once():
                yield emission
            await anyio.sleep(self.tick_period_s)

    def tick_once(self) -> list[DeviceEmission]:
        """Drive the simulator one step (test-only helper; production paths use :meth:`stream`)."""
        if self._clock is None:
            raise AdapterError("bmp58x_sim.tick_once() requires start() first")
        clock = self._clock
        t_now_s = clock.t_mono()
        t_mono_ns, _requested, _received, midpoint_at, _t_utc, latency_s = synth_timing(
            clock, poll_latency_s=_SIM_LATENCY_S
        )
        measurement = self._measure(t_now_s, latency_s=latency_s)
        self._last = measurement
        self._seq += 1
        record_id = make_record_id(ADAPTER_ID, self.name, self._seq)
        record = SourceRecord(
            record_id=record_id,
            adapter=ADAPTER_ID,
            device=self.name,
            shape="wide_row",
            t_mono_ns=t_mono_ns,
            t_utc=midpoint_at,
            row=bmp58x_row(
                measurement,
                pressure_oversampling=self.pressure_oversampling,
                temperature_oversampling=self.temperature_oversampling,
                latency_s=latency_s,
            ),
            metadata={"chip": _SIM_CHIP, "revision": _SIM_REVISION, "i2c_address": 0x47},
        )
        emissions: list[DeviceEmission] = [record]
        for spec in self._channels:
            binding = spec.source
            assert isinstance(binding, Bmp58xReading)
            raw_value = (
                measurement.pressure_pa
                if binding.field == "pressure"
                else measurement.temperature_c
            )
            emissions.append(
                build_channel_sample(
                    spec=spec,
                    raw_value=raw_value,
                    t_mono_ns=t_mono_ns,
                    source_record_id=record_id,
                    source_field=binding.field,
                )
            )
        return emissions

    def _measure(self, t_run_s: float, *, latency_s: float) -> Measurement:
        pressure_raw = raw_from_pressure(float(self.pressure_signal(t_run_s)))
        temperature_raw = raw_from_temperature(float(self.temperature_signal(t_run_s)))
        return Measurement(
            pressure_pa=convert_pressure(pressure_raw),
            temperature_c=convert_temperature(temperature_raw),
            pressure_raw=pressure_raw,
            temperature_raw=temperature_raw,
            pressure_oversampling=self.pressure_oversampling,
            temperature_oversampling=self.temperature_oversampling,
            latency_s=latency_s,
        )

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        """Dispatch a generic :class:`DeviceCommand`. See :class:`~capa.devices.adapter.DeviceAdapter`."""
        clock = self._clock or RunClock.now()
        rejection = reject_unless_authorized(
            cmd, adapter_id=ADAPTER_ID, device_name=self.name, clock=clock
        )
        if rejection is not None:
            return rejection
        if cmd.kind == "set_oversampling":
            pressure = self._oversampling_from(cmd.payload, "pressure")
            temperature = self._oversampling_from(cmd.payload, "temperature")
            if pressure is None and temperature is None:
                raise AdapterError(
                    f"bmp58x_sim {self.name!r} set_oversampling needs 'pressure' and/or "
                    f"'temperature'"
                )
            if pressure is not None:
                self.pressure_oversampling = pressure
            if temperature is not None:
                self.temperature_oversampling = temperature
            return make_accepted_result(
                detail=(
                    f"set_oversampling pressure={self.pressure_oversampling}x "
                    f"temperature={self.temperature_oversampling}x"
                ),
                clock=clock,
            )
        if cmd.kind == "soft_reset":
            return make_accepted_result(
                detail=f"soft_reset chip={_SIM_CHIP} rev={_SIM_REVISION}", clock=clock
            )
        raise AdapterError(f"bmp58x_sim {self.name!r}: unknown command kind {cmd.kind!r}")

    def _oversampling_from(self, payload: dict[str, Any], key: str) -> int | None:
        if payload.get(key) is None:
            return None
        try:
            value = int(payload[key])
            osr_code(value)
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"bmp58x_sim {self.name!r} set_oversampling {key}: {exc}") from exc
        return value

    async def read_once(self) -> Measurement:
        """One reading outside the stream, like the real adapter's."""
        if self._lifecycle.state == "closed":
            raise AdapterError(f"bmp58x_sim {self.name!r} is not open")
        t_run_s = self._clock.t_mono() if self._clock is not None else 0.0
        self._last = self._measure(t_run_s, latency_s=_SIM_LATENCY_S)
        return self._last

    async def read_state_snapshot(self) -> Bmp58xStateSnapshot | None:
        """The oversampling, identity and last reading. ``None`` before
        :meth:`open`, like the real adapter."""
        if self._lifecycle.state == "closed":
            return None
        last = self._last
        return Bmp58xStateSnapshot(
            pressure_oversampling=self.pressure_oversampling,
            temperature_oversampling=self.temperature_oversampling,
            chip=_SIM_CHIP,
            revision=_SIM_REVISION,
            bridge_serial=None,
            bridge_firmware="sim",
            last_pressure_pa=last.pressure_pa if last else None,
            last_temperature_c=last.temperature_c if last else None,
        )


__all__ = ["ADAPTER_ID", "DESCRIPTOR", "Bmp58xSim"]


def _build_descriptor() -> AdapterDescriptor:
    from capa.devices._templates import (  # noqa: PLC0415
        BMP58X_AMBIENT_PRESSURE,
        BMP58X_AMBIENT_TEMPERATURE,
    )
    from capa.devices.registry import AdapterDescriptor  # noqa: PLC0415

    return AdapterDescriptor(
        id="capa.devices.sim.bmp58x_sim",
        label="Bosch BMP58x barometer (simulated)",
        family="sim",
        adapter_factory=Bmp58xSim,
        params_model=None,
        supported_binding_sources=("bmp58x_reading",),
        default_params={},
        channel_templates=(BMP58X_AMBIENT_PRESSURE, BMP58X_AMBIENT_TEMPERATURE),
        capabilities=frozenset({Capability.HAS_PARAMETER_CONFIG}),
        settings=BMP58X_SETTINGS,
    )


DESCRIPTOR = _build_descriptor()

from capa.devices.registry import register as _register  # noqa: E402

_register(DESCRIPTOR)
