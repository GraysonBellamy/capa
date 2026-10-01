"""Simulated Fuji ZP-series gas analyzer — one wide row per tick.

Builds a real :class:`fujilib.Frame` per tick with the builders of
``fujilib.testing.frames``, wraps it with :meth:`fujilib.Sample.from_frame`
and flattens it with :func:`fujilib.sample_to_row`, so a simulated bundle's
``device_records/fuji.parquet`` has exactly the columns of a real one.

The command surface mirrors the real adapter's closely enough to exercise the
manual-control card without hardware: the settings verbs change the
simulator's own settings, and a manual zero or span reaches the wait step,
becomes steady after ``settle_s``, and on commit pulls the channel's reading
onto the named gas.

What it does not model: every channel has one range, in vol%; the range
method and the hold value are acknowledged and not kept, as is any verb it
does not know; a calibration's record is a short one marked ``simulated``;
and there are no outages, so no row is an error row or ``settling``.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

import anyio
from fujilib import ChannelId, Frame, Gas, ReadingState, Sample, Unit, sample_to_row
from fujilib.devices.panel import calibration_record_header
from fujilib.registry.channels import coerce_channel_map
from fujilib.testing import frames as build

from capa.channels.spec import ChannelSpec, FujiChannel
from capa.core.clock import RunClock
from capa.core.errors import AdapterError
from capa.devices.adapter import (
    AdapterLifecycle,
    AdapterStartContext,
    Capability,
    CommandResult,
    DeviceCommand,
)
from capa.devices.fuji import FujiReadback, FujiStateSnapshot, Scalar
from capa.devices.fuji_calibration import IDLE, CalibrationStatus
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
)
from capa.devices.sim._signals import Constant, SignalFn, signals_from_mapping

if TYPE_CHECKING:
    from capa.devices.registry import AdapterDescriptor

# The simulator's records go to the same file, under the same schema, as the
# real adapter's.
ADAPTER_ID: Final[str] = "fuji"

_DEFAULT_CHANNEL_MAP: Final[dict[str, str]] = {"CH1": "co2", "CH2": "co", "CH3": "o2"}
# Ambient air, as a three-component analyzer reads it.
_AMBIENT: Final[dict[Gas, float]] = {Gas.CO2: 0.04, Gas.CO: 0.0, Gas.O2: 20.95}
# Full scale and decimals per gas, as on the bench analyzer's first range.
_FULL_SCALE: Final[dict[Gas, float]] = {Gas.CO2: 10.0, Gas.CO: 1.0, Gas.O2: 25.0}
_DECIMALS: Final[dict[Gas, int]] = {Gas.CO: 3}
# Verbs that need a person's confirmation on the real analyzer too.
_NEEDS_PERSON: Final = frozenset({"set_calibration_gas", "calibration_begin", "calibration_commit"})
_LATENCY_MS: Final = 2.0


@dataclass(slots=True)
class FujiSim:
    """Simulated Fuji gas-analyzer adapter.

    ``signals`` is keyed by analyzer channel (``"CH3"``) and gives that
    channel's concentration in vol%; a mapped channel without a signal reads
    ambient air. ``hold_from_s`` switches output hold on at that run time, to
    exercise the ``hold`` status and the validity field.
    """

    name: str
    channel_map: dict[str, str] = field(default_factory=lambda: dict(_DEFAULT_CHANNEL_MAP))
    signals: dict[str, SignalFn] = field(default_factory=dict)
    """``{analyzer_channel: signal}`` in vol%."""
    tick_period_s: float = 1.0
    hold_from_s: float | None = None
    settle_s: float = 2.0
    """How long after a calibration begins the reading counts as steady."""
    capabilities: frozenset[Capability] = frozenset(
        {
            Capability.READS_PROCESS_VAR,
            Capability.HAS_PARAMETER_CONFIG,
            Capability.HAS_GAS_CALIBRATION,
        }
    )
    _lifecycle: AdapterLifecycle = field(default_factory=AdapterLifecycle)
    _channels: list[ChannelSpec] = field(default_factory=list)
    _clock: RunClock | None = None
    _seq: int = 0
    _gases: dict[ChannelId, Gas] = field(default_factory=dict)
    _settings: dict[str, Scalar] = field(default_factory=dict)
    _offsets: dict[ChannelId, float] = field(default_factory=dict)
    _calibration: CalibrationStatus = IDLE
    _calibration_began: float = 0.0

    def __post_init__(self) -> None:
        self._gases = dict(coerce_channel_map({c: g for c, g in self.channel_map.items()}))
        self._settings = {
            "response_time_o2_s": 15,
            "hold_mode": "last_value",
            "output_hold": False,
        }
        for slot in range(1, 5):
            self._settings[f"response_time_ndir{slot}_s"] = 15
        for channel, gas in self._gases.items():
            prefix = f"ch{channel.number}"
            full_scale = _FULL_SCALE.get(gas, 100.0)
            self._settings[f"{prefix}_range"] = 1
            self._settings[f"{prefix}_unit"] = Unit.VOL_PERCENT.value
            self._settings[f"{prefix}_full_scale"] = full_scale
            self._settings[f"{prefix}_range1_zero_gas"] = 0.0
            self._settings[f"{prefix}_range1_span_gas"] = (
                _AMBIENT[Gas.O2] if gas is Gas.O2 else full_scale
            )

    @classmethod
    def from_params(
        cls,
        *,
        name: str,
        channel_map: dict[str, str] | None = None,
        signals: dict[str, dict[str, object]] | None = None,
        tick_period_s: float = 1.0,
        hold_from_s: float | None = None,
        settle_s: float = 2.0,
    ) -> FujiSim:
        """TOML-friendly constructor used by the engine adapter resolver.

        ``signals`` maps an analyzer channel (``"CH3"``) to a serialisable
        signal spec — see :func:`capa.devices.sim._signals.signal_from_dict`."""
        return cls(
            name=name,
            channel_map=dict(channel_map)
            if channel_map is not None
            else dict(_DEFAULT_CHANNEL_MAP),
            signals=signals_from_mapping(signals or {}),
            tick_period_s=tick_period_s,
            hold_from_s=hold_from_s,
            settle_s=settle_s,
        )

    def configure_channels(self, specs: list[ChannelSpec]) -> None:
        """Bind this adapter to the channel specs that target it."""
        self._channels = channels_for_device(specs, device=self.name, binding_source="fuji_channel")

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
    def calibration(self) -> CalibrationStatus:
        """The simulated calibration under way, or the last one."""
        return self._with_steadiness(self._calibration)

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
        if self._calibration.state == "waiting":
            # As on the real adapter: the manual panel cannot reach it during a run.
            _ = self._cancel()

    async def stop(self) -> None:
        """Stop sampling without closing the connection."""
        self._lifecycle.stop()

    async def snapshot(self) -> DeviceSnapshot:
        """Return a health/status snapshot. See :class:`~capa.devices.adapter.DeviceAdapter`."""
        clock = self._clock or RunClock.now()
        fields: dict[str, Scalar] = {
            "channel_count": len(self._channels),
            "state": self._lifecycle.state,
            "channel_map": ", ".join(f"{c.value}={g.value}" for c, g in self._gases.items()),
            "model": "ZPA (simulated)",
        }
        fields.update(self._settings)
        return DeviceSnapshot(
            adapter=ADAPTER_ID,
            device=self.name,
            t_mono_ns=clock.t_mono_ns(),
            t_utc=now_utc(),
            health="ok" if self._lifecycle.state == "running" else "down",
            fields=fields,
        )

    async def stream(self) -> AsyncIterator[DeviceEmission]:
        """Yield emissions while sampling. See :class:`~capa.devices.adapter.DeviceAdapter`."""
        if self._clock is None:
            raise AdapterError("fuji_sim.stream() requires start() first")
        while self._lifecycle.state == "running":
            for emission in self.tick_once():
                yield emission
            await anyio.sleep(self.tick_period_s)

    def tick_once(self) -> list[DeviceEmission]:
        """Build one poll's frame and yield its SourceRecord plus one
        ChannelSample per declared channel (test-only helper; production
        paths use :meth:`stream`)."""
        if self._clock is None:
            raise AdapterError("fuji_sim.tick_once() requires start() first")
        clock = self._clock
        frame = self._frame(clock.t_mono())
        sample = Sample.from_frame(frame, device=self.name, address=1)
        t_mono_ns = sample.t_mono_ns - clock.started_mono_ns
        self._seq += 1
        record_id = make_record_id(ADAPTER_ID, self.name, self._seq)
        record = SourceRecord(
            record_id=record_id,
            adapter=ADAPTER_ID,
            device=self.name,
            shape="wide_row",
            t_mono_ns=t_mono_ns,
            t_utc=sample.t_utc,
            row=sample_to_row(sample),
            metadata={"address": sample.address},
        )
        emissions: list[DeviceEmission] = [record]
        readings = {reading.channel.value: reading for reading in frame.readings}
        for spec in self._channels:
            binding = spec.source
            assert isinstance(binding, FujiChannel)
            reading = readings.get(binding.channel)
            if reading is None or reading.value is None:
                continue
            if binding.field == "valid":
                raw_value = 1.0 if reading.valid else 0.0
            else:
                raw_value = reading.value
            emissions.append(
                build_channel_sample(
                    spec=spec,
                    raw_value=raw_value,
                    t_mono_ns=t_mono_ns,
                    source_record_id=record_id,
                    source_field=f"ch{reading.channel.number}_{binding.field}",
                    status=reading.state.value,
                )
            )
        return emissions

    def _frame(self, t_s: float) -> Frame:
        """The analyzer's frame at run time ``t_s``."""
        held = self.hold_from_s is not None and t_s >= self.hold_from_s
        calibrating = (
            ChannelId(self._calibration.channel)
            if self._calibration.state == "waiting" and self._calibration.channel is not None
            else None
        )
        readings = []
        for channel, gas in self._gases.items():
            decimals = _DECIMALS.get(gas, 2)
            signal = self.signals.get(channel.value, Constant(_AMBIENT.get(gas, 0.0)))
            value = float(signal(t_s)) + self._offsets.get(channel, 0.0)
            state = ReadingState.OK
            if channel is calibrating:
                state = ReadingState.CALIBRATING
            elif held:
                state = ReadingState.HOLD
            readings.append(
                build.reading(
                    channel,
                    gas,
                    round(value * 10**decimals),
                    decimals,
                    state=state,
                    channel_status=build.status(
                        hold=held, zero=channel is calibrating and self._calibration.kind == "zero"
                    ),
                )
            )
        origin, mono = now_utc(), time.monotonic_ns()
        return build.frame(
            tuple(readings),
            readings_timing=build.timing(0.0, _LATENCY_MS, origin=origin, mono_origin_ns=mono),
            status_timing=build.timing(
                _LATENCY_MS, _LATENCY_MS, origin=origin, mono_origin_ns=mono
            ),
        )

    # ------------------------------------------------------------------ commands

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        """Dispatch a generic :class:`DeviceCommand`. See :class:`~capa.devices.adapter.DeviceAdapter`.

        The authorization gate first; then, as on the real analyzer, the verbs
        that change what it calibrates against need a person's confirmation.
        """
        clock = self._clock or RunClock.now()
        rejection = reject_unless_authorized(
            cmd, adapter_id=ADAPTER_ID, device_name=self.name, clock=clock
        )
        if rejection is not None:
            return rejection
        if cmd.kind in _NEEDS_PERSON and cmd.confirmed_by is None:
            return self._refused(
                f"{cmd.kind} needs a person's confirmation at the interface", clock
            )
        try:
            detail = self._apply(cmd)
        except (KeyError, ValueError) as exc:
            return self._refused(f"{cmd.kind} refused: {exc}", clock)
        return make_accepted_result(detail=detail, clock=clock)

    @staticmethod
    def _refused(detail: str, clock: RunClock) -> CommandResult:
        return CommandResult(
            accepted=False, detail=detail, t_mono_ns=clock.t_mono_ns(), t_utc=now_utc()
        )

    def _apply(self, cmd: DeviceCommand) -> str:
        kind, payload = cmd.kind, cmd.payload
        if kind == "set_response_time":
            target = str(payload["target"]).lower()
            slot = target if target in {"o2", "ndir1", "ndir2", "ndir3", "ndir4"} else None
            if slot is None:
                gas = self._gases[ChannelId(target.upper())]
                ndir = [c for c, g in self._gases.items() if g is not Gas.O2]
                slot = "o2" if gas is Gas.O2 else f"ndir{ndir.index(ChannelId(target.upper())) + 1}"
            self._settings[f"response_time_{slot}_s"] = int(payload["seconds"])
        elif kind == "set_output_hold":
            self._settings["output_hold"] = bool(payload["enabled"])
        elif kind == "set_hold_mode":
            self._settings["hold_mode"] = str(payload["mode"])
        elif kind == "set_range":
            self._settings[f"ch{self._number(payload)}_range"] = int(payload["range"])
        elif kind == "set_calibration_gas":
            key = f"ch{self._number(payload)}_range{int(payload['range'])}_{payload['kind']}_gas"
            self._settings[key] = float(payload["value"])
        elif kind == "calibration_plan":
            number, cal_kind = self._number(payload), self._kind(payload)
            setting = self._settings[f"ch{number}_range1_{cal_kind}_gas"]
            return f"calibration_plan: {_plan_text(number, cal_kind, float(setting or 0.0))}"
        elif kind == "calibration_begin":
            return self._begin(cmd)
        elif kind == "calibration_commit":
            return self._commit()
        elif kind == "calibration_cancel":
            return self._cancel()
        return f"sim ack {kind}"

    def _number(self, payload: dict[str, Any]) -> int:
        channel = ChannelId(str(payload["channel"]).upper())
        if channel not in self._gases:
            raise ValueError(f"{channel.value} is not in the channel map")
        return channel.number

    @staticmethod
    def _kind(payload: dict[str, Any]) -> str:
        kind = str(payload["kind"])
        if kind not in {"zero", "span"}:
            raise ValueError(f"kind must be 'zero' or 'span', got {kind!r}")
        return kind

    # ------------------------------------------------------------------ calibration

    def _begin(self, cmd: DeviceCommand) -> str:
        if self._calibration.state == "waiting":
            raise ValueError("a calibration is already under way")
        payload = cmd.payload
        number = self._number(payload)
        kind = self._kind(payload)
        value = float(payload["gas_value"])
        setting = self._settings[f"ch{number}_range1_{kind}_gas"]
        if value != setting:
            raise ValueError(
                f"the gas named ({value:g}) is not CH{number}'s {kind}-gas setting ({setting}); "
                "change the setting first"
            )
        plan = _plan_text(number, kind, value)
        self._calibration = CalibrationStatus(
            state="waiting",
            channel=f"CH{number}",
            kind=kind,
            gas_value=value,
            gas_unit=payload.get("gas_unit"),
            gas_label=payload.get("gas_label"),
            plan=plan,
        )
        self._calibration_began = time.monotonic()
        return f"calibration_begin: {plan}; on the wait step"

    def _with_steadiness(self, status: CalibrationStatus) -> CalibrationStatus:
        if status.state != "waiting":
            return status
        waited = time.monotonic() - self._calibration_began
        steady = waited >= self.settle_s
        reason = "steady" if steady else f"settling: {waited:.1f} s of {self.settle_s:g} s"
        return replace(
            status, steady=steady, waited_s=waited, reasons=(f"{status.channel}: {reason}",)
        )

    def _commit(self) -> str:
        status = self.calibration
        if status.state != "waiting" or status.channel is None or status.gas_value is None:
            raise ValueError("no calibration is under way")
        if not status.steady:
            raise ValueError(f"the reading is not steady on the gas: {'; '.join(status.reasons)}")
        channel = ChannelId(status.channel)
        gas = self._gases[channel]
        signal = self.signals.get(channel.value, Constant(_AMBIENT.get(gas, 0.0)))
        clock = self._clock or RunClock.now()
        # The calibration moves the reading onto the named gas from here on.
        self._offsets[channel] = status.gas_value - float(signal(clock.t_mono()))
        self._calibration = replace(
            status,
            state="ended",
            outcome="completed",
            clean=True,
            record=self._record(status, "completed"),
        )
        return f"{status.kind} of {status.channel}: completed"

    def _cancel(self) -> str:
        status = self.calibration
        if status.state != "waiting":
            raise ValueError("no calibration is under way")
        self._calibration = replace(
            status,
            state="ended",
            outcome="cancelled",
            clean=True,
            record=self._record(status, "cancelled"),
        )
        return f"{status.kind} of {status.channel}: cancelled"

    def _record(self, status: CalibrationStatus, outcome: str) -> MappingProxyType[str, object]:
        """A record in the shape of the real adapter's, marked as simulated."""
        record = calibration_record_header(address=1, source="simulated")
        record |= {
            "kind": status.kind,
            "outcome": outcome,
            "channels": [status.channel],
            "named_gas": {
                status.channel: {
                    "value": status.gas_value,
                    "unit": status.gas_unit,
                    "label": status.gas_label,
                }
            },
            "calibrating_key_sent": outcome == "completed",
            "run_ended_at": now_utc().isoformat(),
        }
        return MappingProxyType(record)

    async def read_state_snapshot(self) -> FujiStateSnapshot:
        """The manual-control card's read-back: calibration, readings, settings."""
        clock = self._clock or RunClock.now()
        frame = self._frame(clock.t_mono())
        return FujiStateSnapshot(
            calibration=self.calibration,
            readings=tuple(
                FujiReadback(
                    channel=reading.channel.value,
                    gas=reading.gas.value,
                    value=reading.value,
                    unit=reading.unit.value,
                    state=reading.state.value,
                )
                for reading in frame.readings
            ),
            settings=MappingProxyType(dict(self._settings)),
        )


def _plan_text(number: int, kind: str, gas: float) -> str:
    """What a calibration reaches, worded as the real adapter words it."""
    return f"{kind} of CH{number}: CH{number} range 1 against {gas:g} vol%"


__all__ = ["ADAPTER_ID", "DESCRIPTOR", "FujiSim"]


def _build_descriptor() -> AdapterDescriptor:
    from capa.devices._templates import FUJI_CO, FUJI_CO2, FUJI_O2  # noqa: PLC0415
    from capa.devices.registry import AdapterDescriptor  # noqa: PLC0415

    return AdapterDescriptor(
        id="capa.devices.sim.fuji_sim",
        label="Fuji ZP gas analyzer (simulated)",
        family="sim",
        adapter_factory=FujiSim,
        params_model=None,
        supported_binding_sources=("fuji_channel",),
        default_params={},
        channel_templates=(FUJI_CO2, FUJI_CO, FUJI_O2),
    )


DESCRIPTOR = _build_descriptor()

from capa.devices.registry import register as _register  # noqa: E402

_register(DESCRIPTOR)
