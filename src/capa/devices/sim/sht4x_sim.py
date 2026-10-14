"""Simulated SHT4x adapter — one temperature / humidity pair per tick.

Mirrors :class:`capa.devices.sht4x.Sht4xAdapter`: the same wide row via
:func:`capa.devices.sht4x.sht4x_row`, the same ``sht4x_reading`` binding,
the same commands. ``temperature_signal`` and ``humidity_signal`` provide
the values; both default to a steady lab (22 °C, 50 %RH) so a bare
``[[devices]]`` entry works.

Keeps the precision the real adapter keeps, so ``device_settings`` and
the manual card read back what they set. A heater pulse adds a warm
offset that decays over ten seconds, so the perturbation is visible in
a sim run too.
"""

from __future__ import annotations

import math
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import anyio

from capa.channels.spec import ChannelSpec, Sht4xReading
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
    DeviceEvent,
    DeviceSnapshot,
    SourceRecord,
)
from capa.devices.sht4x import (
    HEATER_DURATIONS_S,
    HEATER_MAX_DUTY,
    HEATER_POWERS_MW,
    PRECISIONS,
    SHT4X_SETTINGS,
    Measurement,
    Sht4xStateSnapshot,
    convert_humidity,
    convert_temperature,
    raw_from_humidity,
    raw_from_temperature,
    sht4x_row,
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
from capa.devices.sim._signals import Constant, SignalFn, signal_from_dict

if TYPE_CHECKING:
    from capa.devices.registry import AdapterDescriptor

ADAPTER_ID: Final[str] = "sht4x"

_HEATER_BUMP_C: Final[float] = 5.0
"""Warm offset right after a pulse."""

_HEATER_DECAY_S: Final[float] = 10.0
"""Time constant of the offset's decay."""

_SIM_LATENCY_S: Final[float] = 0.012


@dataclass(slots=True)
class Sht4xSim:
    """Simulated SHT4x ambient sensor adapter.

    Emits one record per tick. ``temperature_signal`` is in °C and
    ``humidity_signal`` in %RH; the raw words are derived from them so
    the row looks exactly like the real adapter's.
    """

    name: str
    tick_period_s: float = 1.0
    temperature_signal: SignalFn = field(default_factory=lambda: Constant(22.0))
    humidity_signal: SignalFn = field(default_factory=lambda: Constant(50.0))
    precision: str = "high"
    sensor_serial: str = "0x53494d00"
    capabilities: frozenset[Capability] = frozenset(
        {Capability.HAS_PARAMETER_CONFIG, Capability.HAS_SENSOR_HEATER}
    )
    _lifecycle: AdapterLifecycle = field(default_factory=AdapterLifecycle)
    _channels: list[ChannelSpec] = field(default_factory=list)
    _clock: RunClock | None = None
    _seq: int = 0
    _last: Measurement | None = None
    _events: list[DeviceEvent] = field(default_factory=list)
    _heater_pulses: int = 0
    _last_heater_end: float | None = None
    _heater_free_at: float = 0.0

    @classmethod
    def from_params(
        cls,
        *,
        name: str,
        temperature_signal: dict[str, object] | None = None,
        humidity_signal: dict[str, object] | None = None,
        tick_period_s: float = 1.0,
        precision: str = "high",
    ) -> Sht4xSim:
        """TOML-friendly constructor used by the engine adapter resolver.

        ``temperature_signal`` / ``humidity_signal`` are serialisable signal
        specs (``{"kind": "constant", "value": 22.0}`` etc.)."""
        if precision not in PRECISIONS:
            raise AdapterError(
                f"sht4x_sim {name!r}: unknown precision {precision!r}; "
                f"expected one of {', '.join(PRECISIONS)}"
            )
        kwargs: dict[str, Any] = {
            "name": name,
            "tick_period_s": tick_period_s,
            "precision": precision,
        }
        if temperature_signal is not None:
            kwargs["temperature_signal"] = signal_from_dict(temperature_signal)
        if humidity_signal is not None:
            kwargs["humidity_signal"] = signal_from_dict(humidity_signal)
        return cls(**kwargs)

    def configure_channels(self, specs: list[ChannelSpec]) -> None:
        """Bind this adapter to the channel specs that target it."""
        self._channels = channels_for_device(
            specs, device=self.name, binding_source="sht4x_reading"
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
                "precision": self.precision,
                "rate_hz": 1.0 / self.tick_period_s if self.tick_period_s > 0 else 0.0,
                "channel_count": len(self._channels),
                "state": self._lifecycle.state,
                "sensor_serial": self.sensor_serial,
                "last_temperature_c": last.temperature_c if last else None,
                "last_humidity_pct": last.humidity_pct if last else None,
                "heater_pulses": self._heater_pulses,
            },
        )

    async def stream(self) -> AsyncIterator[DeviceEmission]:
        """Yield emissions while sampling. See :class:`~capa.devices.adapter.DeviceAdapter`."""
        if self._clock is None:
            raise AdapterError("sht4x_sim.stream() requires start() first")
        while self._lifecycle.state == "running":
            for emission in self.tick_once():
                yield emission
            await anyio.sleep(self.tick_period_s)

    def tick_once(self) -> list[DeviceEmission]:
        """Drive the simulator one step (test-only helper; production paths use :meth:`stream`)."""
        if self._clock is None:
            raise AdapterError("sht4x_sim.tick_once() requires start() first")
        clock = self._clock
        t_now_s = clock.t_mono()
        t_mono_ns, _requested, _received, midpoint_at, _t_utc, latency_s = synth_timing(
            clock, poll_latency_s=_SIM_LATENCY_S
        )
        measurement = self._measure(t_now_s, latency_s=latency_s, precision=self.precision)
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
            row=sht4x_row(measurement, precision=self.precision, latency_s=latency_s),
            metadata={"sensor_serial": self.sensor_serial, "i2c_address": 0x44},
        )
        emissions: list[DeviceEmission] = [record]
        for spec in self._channels:
            binding = spec.source
            assert isinstance(binding, Sht4xReading)
            raw_value = (
                measurement.temperature_c
                if binding.field == "temperature"
                else measurement.humidity_pct
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
        while self._events:
            emissions.append(self._events.pop(0))
        return emissions

    def _measure(self, t_run_s: float, *, latency_s: float, precision: str) -> Measurement:
        temperature = float(self.temperature_signal(t_run_s)) + self._heater_offset()
        humidity = float(self.humidity_signal(t_run_s))
        temperature_raw = raw_from_temperature(temperature)
        humidity_raw = raw_from_humidity(humidity)
        return Measurement(
            temperature_c=convert_temperature(temperature_raw),
            humidity_pct=convert_humidity(humidity_raw),
            temperature_raw=temperature_raw,
            humidity_raw=humidity_raw,
            precision=precision,
            latency_s=latency_s,
        )

    def _heater_offset(self) -> float:
        if self._last_heater_end is None:
            return 0.0
        age = time.monotonic() - self._last_heater_end
        return _HEATER_BUMP_C * math.exp(-age / _HEATER_DECAY_S)

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        """Dispatch a generic :class:`DeviceCommand`. See :class:`~capa.devices.adapter.DeviceAdapter`."""
        clock = self._clock or RunClock.now()
        rejection = reject_unless_authorized(
            cmd, adapter_id=ADAPTER_ID, device_name=self.name, clock=clock
        )
        if rejection is not None:
            return rejection
        if cmd.kind == "set_precision":
            precision = str(cmd.payload["precision"]).strip().lower()
            if precision not in PRECISIONS:
                raise AdapterError(
                    f"sht4x_sim {self.name!r} set_precision: unknown precision {precision!r}; "
                    f"expected one of {', '.join(PRECISIONS)}"
                )
            self.precision = precision
            return make_accepted_result(
                detail=f"set_precision precision={precision!r}", clock=clock
            )
        if cmd.kind == "heater_pulse":
            return self._heater_pulse(cmd.payload, clock)
        if cmd.kind == "soft_reset":
            return make_accepted_result(
                detail=f"soft_reset serial={self.sensor_serial}", clock=clock
            )
        raise AdapterError(f"sht4x_sim {self.name!r}: unknown command kind {cmd.kind!r}")

    def _heater_pulse(self, payload: dict[str, Any], clock: RunClock) -> CommandResult:
        power_mw = int(payload["power_mw"])
        duration_s = float(payload["duration_s"])
        if power_mw not in HEATER_POWERS_MW or duration_s not in HEATER_DURATIONS_S:
            raise AdapterError(
                f"sht4x_sim {self.name!r} heater_pulse: no {power_mw} mW / {duration_s} s pulse"
            )
        remaining = self._heater_free_at - time.monotonic()
        if remaining > 0:
            return CommandResult(
                accepted=False,
                detail=f"heater refused: duty-cycle hold-off, {remaining:.0f} s until the next pulse",
                t_mono_ns=clock.t_mono_ns(),
                t_utc=datetime.now(UTC),
            )
        now = time.monotonic()
        self._heater_pulses += 1
        self._last_heater_end = now
        self._heater_free_at = now + duration_s / HEATER_MAX_DUTY - duration_s
        t_run_s = self._clock.t_mono() if self._clock is not None else 0.0
        measurement = self._measure(t_run_s, latency_s=duration_s, precision="high")
        self._events.append(
            DeviceEvent(
                adapter=ADAPTER_ID,
                device=self.name,
                t_mono_ns=clock.t_mono_ns(),
                t_utc=datetime.now(UTC),
                kind="heater_pulse",
                message=(
                    f"heater pulse {power_mw} mW for {duration_s:g} s; reading at end of pulse "
                    f"{measurement.temperature_c:.2f} °C, {measurement.humidity_pct:.1f} %RH"
                ),
                severity="info",
                metadata={"power_mw": power_mw, "duration_s": duration_s},
            )
        )
        return make_accepted_result(
            detail=(
                f"heater_pulse power_mw={power_mw} duration_s={duration_s:g} "
                f"end_of_pulse={measurement.temperature_c:.2f}C/{measurement.humidity_pct:.1f}%RH"
            ),
            clock=clock,
        )

    async def read_once(self) -> Measurement:
        """One reading outside the stream, like the real adapter's."""
        if self._lifecycle.state == "closed":
            raise AdapterError(f"sht4x_sim {self.name!r} is not open")
        t_run_s = self._clock.t_mono() if self._clock is not None else 0.0
        self._last = self._measure(t_run_s, latency_s=_SIM_LATENCY_S, precision=self.precision)
        return self._last

    async def read_state_snapshot(self) -> Sht4xStateSnapshot | None:
        """The precision, identity and last reading. ``None`` before
        :meth:`open`, like the real adapter."""
        if self._lifecycle.state == "closed":
            return None
        last = self._last
        return Sht4xStateSnapshot(
            precision=self.precision,
            sensor_serial=self.sensor_serial,
            bridge_serial=None,
            bridge_firmware="sim",
            last_temperature_c=last.temperature_c if last else None,
            last_humidity_pct=last.humidity_pct if last else None,
            heater_pulses=self._heater_pulses,
            seconds_since_heater_pulse=(
                time.monotonic() - self._last_heater_end
                if self._last_heater_end is not None
                else None
            ),
        )


__all__ = ["ADAPTER_ID", "DESCRIPTOR", "Sht4xSim"]


def _build_descriptor() -> AdapterDescriptor:
    from capa.devices._templates import (  # noqa: PLC0415
        SHT4X_AMBIENT_HUMIDITY,
        SHT4X_AMBIENT_TEMPERATURE,
    )
    from capa.devices.registry import AdapterDescriptor  # noqa: PLC0415

    return AdapterDescriptor(
        id="capa.devices.sim.sht4x_sim",
        label="Sensirion SHT4x (simulated)",
        family="sim",
        adapter_factory=Sht4xSim,
        params_model=None,
        supported_binding_sources=("sht4x_reading",),
        default_params={},
        channel_templates=(SHT4X_AMBIENT_TEMPERATURE, SHT4X_AMBIENT_HUMIDITY),
        capabilities=frozenset({Capability.HAS_PARAMETER_CONFIG, Capability.HAS_SENSOR_HEATER}),
        settings=SHT4X_SETTINGS,
    )


DESCRIPTOR = _build_descriptor()

from capa.devices.registry import register as _register  # noqa: E402

_register(DESCRIPTOR)
