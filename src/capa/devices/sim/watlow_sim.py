"""Simulated Watlow adapter — long-format ``Sample``\\ s.

Mirrors :class:`watlowlib.streaming.Sample`'s shape exactly via
:func:`watlowlib.sinks.base.sample_to_row` so the resulting
``device_records/watlow.parquet`` is indistinguishable from what a real
Watlow recorder would emit.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, cast

import anyio
from watlowlib import (
    ProtocolKind,
    Unit,
    WatlowValidationError,
    sample_to_row,
)
from watlowlib import (
    Sample as WatlowSample,
)
from watlowlib.registry.units import coerce_unit

from capa.channels.spec import ChannelSpec, WatlowParameter
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
from capa.devices.sim._signals import SignalFn, watlow_signals_from_mapping

if TYPE_CHECKING:
    from capa.devices.registry import AdapterDescriptor

ADAPTER_ID: Final[str] = "watlow"


@dataclass(slots=True)
class WatlowSim:
    """Simulated Watlow controller adapter.

    ``signals`` is keyed by ``(parameter, instance)`` tuples — mirroring how
    :class:`watlowlib.streaming.Sample`\\ s are addressed (one row per
    ``(parameter, instance)`` per tick).

    lists Watlow as long-format: this sim emits one ``SourceRecord``
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
    parameter_units: dict[str, Unit | str | None] = field(default_factory=dict)
    """Map parameter name → display unit. The real adapter populates
    :attr:`watlowlib.streaming.Sample.unit` with a :class:`Unit` enum for
    every temperature / output read (parameter 17050 — see
    :mod:`watlowlib.registry.units`). The sim mirrors that: callers pass
    a :class:`Unit`, a known alias (``"C"`` / ``"degC"`` / ``"celsius"`` /
    ``"%"``) which is coerced via :func:`watlowlib.coerce_unit`, or any
    other string (preserved verbatim — escape hatch for cross-vendor or
    custom-unit rows). ``None`` means the parameter has no unit."""
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
        ``"process_value"`` defaults to instance 1.

        ``parameter_units`` entries that match a known watlowlib alias are
        coerced to :class:`Unit`; unknown strings pass through unchanged so
        the sim retains its cross-vendor escape hatch."""
        out_signals = watlow_signals_from_mapping(cast(dict[object, object], signals or {}))
        coerced_units: dict[str, Unit | str | None] = {}
        for param, raw in (parameter_units or {}).items():
            coerced_units[param] = _coerce_sim_unit(raw)
        return cls(
            name=name,
            address=address,
            protocol=ProtocolKind(protocol) if protocol is not None else ProtocolKind.STDBUS,
            tick_period_s=tick_period_s,
            signals=out_signals,
            parameter_units=coerced_units,
            parameter_ids=parameter_ids or {},
        )

    def configure_channels(self, specs: list[ChannelSpec]) -> None:
        """Bind the adapter to the channels declared in the experiment config."""
        self._channels = channels_for_device(
            specs, device=self.name, binding_source="watlow_parameter"
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

    async def open(self) -> None:
        # CAPA_SIM_OPEN_DELAY_MS / CAPA_SIM_OPEN_FAIL exist so doc-tooling
        # can hold the connection strip in its brief CONNECTING state or
        # force it into FAILED state, neither of which happens naturally
        # under a clean sim apply.
        """Open the underlying connection. See :class:`~capa.devices.adapter.DeviceAdapter`."""
        import os  # noqa: PLC0415

        delay_ms = int(os.environ.get("CAPA_SIM_OPEN_DELAY_MS", "0") or "0")
        if delay_ms > 0:
            await anyio.sleep(delay_ms / 1000.0)
        if os.environ.get("CAPA_SIM_OPEN_FAIL"):
            raise AdapterError(f"watlow_sim:{self.name}: open refused (CAPA_SIM_OPEN_FAIL set)")
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
        return DeviceSnapshot(
            adapter=ADAPTER_ID,
            device=self.name,
            t_mono_ns=clock.t_mono_ns(),
            t_utc=now_utc(),
            health="ok" if self._lifecycle.state == "running" else "down",
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

        # Mirror the real Watlow's per-tick fanout convention: the first
        # SourceRecord in this batch carries ``tick_first=True`` in metadata
        # so the worker counts one poll per tick rather than one per
        # parameter. See watlow.py for the matching real-adapter logic.
        for tick_idx, ((parameter, instance), signal) in enumerate(self.signals.items()):
            t_mono_ns, requested_at, received_at, midpoint_at, _t_utc, latency_s = synth_timing(
                clock
            )
            value = float(signal(t_now_s))
            # Coerce here so direct WatlowSim(parameter_units={"process_value":
            # "degC"}, ...) construction (bypassing from_params) still
            # produces Unit.CELSIUS on the wire — sim/real parity for
            # device_records/watlow.parquet.
            unit = _coerce_sim_unit_value(self.parameter_units.get(parameter))
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
                t_mono_ns=t_mono_ns,
                t_utc=midpoint_at,
                t_midpoint_mono_ns=None,
                requested_at=requested_at,
                received_at=received_at,
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
                metadata={"parameter_id": parameter_id, "tick_first": tick_idx == 0},
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
        # Sim always accepts authorized commands. Real adapter writes to the device.
        """Dispatch a generic :class:`DeviceCommand`. See :class:`~capa.devices.adapter.DeviceAdapter`."""
        clock = self._clock or RunClock.now()
        rejection = reject_unless_authorized(
            cmd, adapter_id=ADAPTER_ID, device_name=self.name, clock=clock
        )
        if rejection is not None:
            return rejection
        return make_accepted_result(detail=f"sim ack {cmd.kind} target={cmd.target}", clock=clock)

    # Convenience typed methods (parallel to the real adapter's two-tier API,
    # ). Tests may use these instead of going through .command().
    async def set_setpoint(
        self,
        value: float,
        *,
        instance: int = 1,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Set the controller setpoint. Authorization rules match :meth:`command`."""
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


def _coerce_sim_unit(value: str | None) -> Unit | str | None:
    """Try to coerce a string unit to a watlowlib :class:`Unit`; on miss,
    return the original string unchanged so cross-vendor / custom-unit
    sims still preserve their tag verbatim. ``None`` passes through.

    The ``str | None`` signature matches the ``from_params`` TOML-loader
    shape; for the broader sample-build path use
    :func:`_coerce_sim_unit_value` (which also accepts an already-coerced
    :class:`Unit`).
    """
    if value is None:
        return None
    try:
        return coerce_unit(value)
    except WatlowValidationError:
        return value


def _coerce_sim_unit_value(value: Unit | str | None) -> Unit | str | None:
    """Same coercion as :func:`_coerce_sim_unit` but tolerant of an
    already-coerced :class:`Unit` (which it returns unchanged)."""
    if value is None or isinstance(value, Unit):
        return value
    try:
        return coerce_unit(value)
    except WatlowValidationError:
        return value


__all__ = ["ADAPTER_ID", "DESCRIPTOR", "WatlowSim"]


def _build_descriptor() -> AdapterDescriptor:
    from capa.devices._templates import WATLOW_HEATER_PV, WATLOW_HEATER_SETPOINT  # noqa: PLC0415
    from capa.devices.registry import AdapterDescriptor  # noqa: PLC0415

    return AdapterDescriptor(
        id="capa.devices.sim.watlow_sim",
        label="Watlow PM-series (simulated)",
        family="sim",
        adapter_factory=WatlowSim,
        params_model=None,
        supported_binding_sources=("watlow_parameter",),
        default_params={},
        channel_templates=(WATLOW_HEATER_PV, WATLOW_HEATER_SETPOINT),
    )


DESCRIPTOR = _build_descriptor()

from capa.devices.registry import register as _register  # noqa: E402

_register(DESCRIPTOR)
