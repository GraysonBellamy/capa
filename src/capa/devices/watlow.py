"""Real :class:`WatlowAdapter` — wraps a :class:`watlowlib.Controller` (P0d).

Plan §16 P0d entry: "real :class:`WatlowAdapter` (smallest viable real device);
Watlow ``SourceRecord`` preservation; Watlow parameter-to-channel mapping;
hardware smoke-test gate."

Architecture (plan §5.2 / §7.2):

* ``open`` opens the serial transport via :func:`watlowlib.open_device` and runs
  :meth:`watlowlib.Controller.identify` to capture firmware / part-number
  metadata for the manifest's equipment block.
* ``start`` captures the run :class:`RunClock` and arms the streaming pipeline.
* ``stream`` drives :func:`watlowlib.record` (the library's own absolute-target
  recorder) and reshapes every emitted :class:`watlowlib.streaming.Sample` into
  one :class:`SourceRecord` (long-format row preserved verbatim via
  :func:`watlowlib.sinks.sample_to_row`) plus zero or more :class:`ChannelSample`\\
  s mapped from the configured :class:`WatlowParameter` bindings.
* ``snapshot`` returns a :class:`DeviceSnapshot` from the cached ``DeviceInfo``.
* ``command`` enforces the authorization gate (plan §9: every device write
  carries ``issued_by`` plus either ``authorization_id`` or ``confirmed_by``)
  and dispatches to :meth:`watlowlib.Controller.set_setpoint` /
  :meth:`watlowlib.Controller.write_parameter`.

For tests, an opt-in ``controller_factory`` kwarg lets the adapter run against
an in-process :class:`watlowlib.transport.fake.FakeTransport` without touching
a serial port.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Final, Literal

import watlowlib
from pydantic import BaseModel, ConfigDict, Field
from watlowlib.devices.controller import Controller
from watlowlib.devices.models import DeviceInfo, Reading
from watlowlib.errors import WatlowError
from watlowlib.protocol.base import ProtocolKind
from watlowlib.sinks.base import sample_to_row
from watlowlib.streaming import OverflowPolicy, Sample
from watlowlib.streaming.recorder import record as watlow_record
from watlowlib.transport.base import ByteSize, Parity, SerialSettings, StopBits

from capa.channels.spec import ChannelSpec, WatlowParameter
from capa.core.clock import RunClock
from capa.core.errors import AdapterError
from capa.devices._helpers import (
    LastSampleTracker,
    WatchdogState,
    build_channel_sample,
    channels_for_device,
    make_record_id,
)
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

ADAPTER_ID: Final[str] = "watlow"

ProtocolName = Literal["stdbus", "modbus_rtu", "auto"]
"""Lowercase string form accepted in TOML/YAML configs; mapped to
:class:`ProtocolKind` at adapter construction."""

_PROTOCOL_BY_NAME: Final[dict[ProtocolName, ProtocolKind]] = {
    "stdbus": ProtocolKind.STDBUS,
    "modbus_rtu": ProtocolKind.MODBUS_RTU,
    "auto": ProtocolKind.AUTO,
}


# ---------------------------------------------------------------------------
# Adapter params (Pydantic) — what shows up under ``[devices.params]`` in TOML.
# ---------------------------------------------------------------------------


class WatlowAdapterParams(BaseModel):
    """Per-device adapter configuration for a real Watlow controller.

    Plan §5.4: adapter-specific knobs live under ``DeviceConfig.params`` and are
    parsed by the adapter at construction time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    port: str
    """Serial-port path: ``/dev/ttyUSB0`` (Linux), ``COM3`` (Windows), or
    ``fake://...`` for tests using a pre-built controller."""

    address: int = Field(ge=1, le=247, default=1)
    """Bus address. Std Bus accepts ``1..16``; Modbus RTU accepts ``1..247``."""

    protocol: ProtocolName = "stdbus"
    """Wire protocol. ``auto`` runs the conservative detector
    (plan-and-watlowlib §7); pin one in production."""

    baudrate: int = 38400
    parity: Literal["none", "even", "odd"] = "none"
    stopbits: Literal[1, 2] = 1
    bytesize: Literal[7, 8] = 8

    parameters: tuple[str, ...] = ("process_value", "setpoint")
    """Parameter names polled per tick. Names must resolve in
    :data:`watlowlib.PARAMETERS`."""

    instances: tuple[int, ...] = (1,)
    """1-indexed loop / channel selectors; single-loop devices use ``(1,)``."""

    rate_hz: float = Field(gt=0, le=100.0, default=1.0)
    """Polling cadence. Production runs should sit at 1–5 Hz: Std Bus
    round-trips on the EZ-ZONE PM family are ~50 ms per parameter, so even
    Modbus tops out near 10 Hz for two-parameter polls. The 100 Hz upper
    bound exists only to catch typos (``rate_hz=1000``) and to give unit
    tests a fast-iteration path against an in-process stub controller."""

    auto_reconnect: bool = True
    """When ``True``, transient :class:`WatlowConnectionError`\\ s do not
    terminate the stream — they count as ``samples_late`` and the recorder
    retries on the next tick."""

    snapshot_period_s: float = Field(gt=0, default=30.0)
    """Cadence of :class:`DeviceSnapshot` emissions during a run."""

    identify_on_open: bool = True
    """Run :meth:`Controller.identify` immediately after opening, populating
    the cached :class:`DeviceInfo` used by :meth:`WatlowAdapter.snapshot`."""

    def to_serial_settings(self) -> SerialSettings:
        """Build the :class:`SerialSettings` watlowlib expects.

        Convert the human-friendly literals (``"none"`` / ``1`` / ``8``) into
        the :mod:`anyserial` enum values that :class:`SerialSettings` expects
        statically. ``SerialSettings.__post_init__`` accepts the wider runtime
        forms too, but converting here keeps the call type-clean for
        ``mypy --strict`` consumers.
        """
        return SerialSettings(
            port=self.port,
            baudrate=self.baudrate,
            parity=Parity(self.parity),
            stopbits=StopBits(str(self.stopbits)),
            bytesize=ByteSize(str(self.bytesize)),
        )

    def protocol_kind(self) -> ProtocolKind:
        return _PROTOCOL_BY_NAME[self.protocol]


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


ControllerFactory = Callable[[], Awaitable[Controller]]
"""Test seam: a factory returning an *unopened* :class:`Controller`. The
adapter calls it at :meth:`WatlowAdapter.open` time and then awaits
``__aenter__``. Production code leaves this ``None`` and the adapter calls
:func:`watlowlib.open_device` itself."""


class WatlowAdapter:
    """Real Watlow adapter.

    Two construction shapes:

    * ``WatlowAdapter(name=..., **params_kwargs)`` — the engine's
      :func:`_construct_adapters` path uses this. Per-device params from
      ``[devices.params]`` are forwarded as kwargs and parsed into a
      :class:`WatlowAdapterParams`.
    * ``WatlowAdapter(name=..., params=WatlowAdapterParams(...))`` — for
      programmatic construction in tests.

    Both shapes accept an optional ``controller_factory`` kwarg as a test
    seam: when supplied, the adapter calls it instead of
    :func:`watlowlib.open_device` so unit tests can wire up a
    :class:`watlowlib.transport.fake.FakeTransport`-backed controller.
    """

    __slots__ = (
        "_channels",
        "_clock",
        "_controller",
        "_controller_factory",
        "_device_info",
        "_last_sample",
        "_last_snapshot_t_mono_ns",
        "_lifecycle",
        "_seq",
        "_stop_requested",
        "capabilities",
        "name",
        "params",
    )

    name: str
    params: WatlowAdapterParams
    capabilities: frozenset[Capability]

    def __init__(
        self,
        *,
        name: str,
        params: WatlowAdapterParams | None = None,
        controller_factory: ControllerFactory | None = None,
        **params_kwargs: Any,
    ) -> None:
        if params is not None and params_kwargs:
            raise TypeError("WatlowAdapter accepts either `params=` or per-field kwargs, not both")
        if params is None:
            params = WatlowAdapterParams.model_validate(params_kwargs)
        self.name = name
        self.params = params
        self.capabilities = frozenset(
            {
                Capability.HAS_SETPOINT,
                Capability.HAS_RAMP,
                Capability.READS_PROCESS_VAR,
            }
        )
        self._controller_factory: ControllerFactory | None = controller_factory
        self._controller: Controller | None = None
        self._device_info: DeviceInfo | None = None
        self._channels: list[ChannelSpec] = []
        self._clock: RunClock | None = None
        self._lifecycle = AdapterLifecycle()
        self._seq = 0
        # Sentinel so the first poll tick always emits a snapshot.
        self._last_snapshot_t_mono_ns = -(2**62)
        self._last_sample = LastSampleTracker()
        self._stop_requested = False

    # ------------------------------------------------------------------ wiring

    def configure_channels(self, specs: list[ChannelSpec]) -> None:
        """Bind the adapter to the channels declared in the experiment config.

        Filters to :class:`WatlowParameter`-bound specs whose ``device`` name
        matches ours; same shape the sim uses so the routing logic is
        identical.
        """
        self._channels = channels_for_device(
            specs, device=self.name, binding_source="watlow_parameter"
        )

    @property
    def device_info(self) -> DeviceInfo | None:
        """The cached :class:`DeviceInfo` from the last :meth:`open` /
        :meth:`identify`. ``None`` until the adapter has been opened with
        ``identify_on_open=True``."""
        return self._device_info

    # ------------------------------------------------------------------ lifecycle

    async def open(self) -> None:
        """Open the underlying transport and (optionally) identify the device.

        Idempotent: a second call on an already-open adapter is a no-op.
        """
        if self._lifecycle.state in ("open", "running"):
            return
        try:
            self._controller = await self._build_controller()
            await self._enter_controller(self._controller)
            if self.params.identify_on_open:
                self._device_info = await self._controller.identify(
                    query_configured_protocol=True,
                )
        except WatlowError as exc:
            await self._safe_close_controller()
            raise AdapterError(
                f"watlow {self.name!r} open failed: {exc}", device=self.name
            ) from exc
        self._lifecycle.open()

    async def close(self) -> None:
        """Release the bus / handle. Idempotent."""
        if self._lifecycle.state == "closed":
            return
        await self._safe_close_controller()
        self._controller = None
        self._lifecycle.close()

    async def start(self, clock: RunClock | None = None) -> None:
        """Capture the :class:`RunClock` anchor and arm the streaming loop."""
        self._lifecycle.start()
        self._clock = clock or RunClock.now()
        self._stop_requested = False
        self._last_sample.reset()
        # Force a snapshot on the first stream tick so the manifest sees a
        # device-health row from the start.
        self._last_snapshot_t_mono_ns = -(2**62)

    async def stop(self) -> None:
        """Request the streaming loop to exit cleanly.

        The next batch arrival from :func:`watlowlib.record` lets ``stream``
        observe the request and break out of its async-cm. Idempotent.
        """
        if self._lifecycle.state != "running":
            return
        self._stop_requested = True
        self._lifecycle.stop()

    async def snapshot(self) -> DeviceSnapshot:
        """Build a :class:`DeviceSnapshot` from cached :class:`DeviceInfo`.

        Plan §13.1: snapshots feed ``status.sqlite`` for diagnostics. The
        cached ``DeviceInfo`` is captured at :meth:`open`; this method does no
        I/O so it is safe to call from the engine while the stream is in
        flight.
        """
        clock = self._clock or RunClock.now()
        return DeviceSnapshot(
            adapter=ADAPTER_ID,
            device=self.name,
            t_mono_ns=clock.t_mono_ns(),
            t_utc=datetime.now(UTC),
            healthy=self._lifecycle.state in ("open", "running"),
            fields=self._snapshot_fields(),
        )

    # ------------------------------------------------------------------ stream

    async def stream(self) -> AsyncIterator[DeviceEmission]:
        """Yield :class:`DeviceEmission`\\ s while sampling is active.

        Drives :func:`watlowlib.record` with the configured cadence. Every
        emitted :class:`watlowlib.streaming.Sample` becomes one
        :class:`SourceRecord` (preserving the library-native long-format row)
        plus one :class:`ChannelSample` per matching :class:`WatlowParameter`
        binding.
        """
        if self._controller is None:
            raise AdapterError(
                f"watlow {self.name!r} stream() requires open() first",
                device=self.name,
            )
        if self._clock is None:
            raise AdapterError(
                f"watlow {self.name!r} stream() requires start() first",
                device=self.name,
            )

        # Yield an initial snapshot so the manifest's equipment block has
        # something to show before the first poll lands.
        snap = await self.snapshot()
        self._last_snapshot_t_mono_ns = snap.t_mono_ns
        yield snap

        # The recorder runs inside an anyio task group; any
        # ``WatlowError`` raised by the producer is re-raised wrapped in a
        # ``BaseExceptionGroup``. ``except*`` unwraps the first underlying
        # error so we can re-raise it as an ``AdapterError`` with full
        # device context — keeping the engine's error story uniform across
        # sim and real adapters.
        try:
            async with watlow_record(
                self._controller,
                parameters=self.params.parameters,
                rate_hz=self.params.rate_hz,
                instances=self.params.instances,
                overflow=OverflowPolicy.BLOCK,
                buffer_size=64,
                auto_reconnect=self.params.auto_reconnect,
            ) as batches:
                async for batch in batches:
                    if self._stop_requested:
                        break
                    for sample in batch:
                        record = self._record_for(sample)
                        yield record
                        self._last_sample.mark(record.t_mono_ns)
                        for cs in self._channel_samples_for(sample, record.record_id):
                            yield cs
                    # Periodic snapshot — cadence-bounded, never per-tick.
                    if self._snapshot_due():
                        snap = await self.snapshot()
                        self._last_snapshot_t_mono_ns = snap.t_mono_ns
                        yield snap
        except* WatlowError as eg:
            first = next(iter(eg.exceptions))
            raise AdapterError(
                f"watlow {self.name!r} stream failed: {first}", device=self.name
            ) from first

    # ------------------------------------------------------------------ commands

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        """Issue a generic command. Authorization gate first, then dispatch.

        Plan §9: commands without either ``authorization_id`` (run-arm cover)
        or ``confirmed_by`` (manual UI confirmation) are refused at the
        adapter boundary, regardless of the underlying device's own gates.
        """
        clock = self._clock or RunClock.now()
        if cmd.authorization_id is None and cmd.confirmed_by is None:
            return CommandResult(
                accepted=False,
                detail=f"watlow {self.name!r} refuses unauthorized commands",
                t_mono_ns=clock.t_mono_ns(),
                t_utc=datetime.now(UTC),
            )
        if self._controller is None:
            return CommandResult(
                accepted=False,
                detail=f"watlow {self.name!r} not open",
                t_mono_ns=clock.t_mono_ns(),
                t_utc=datetime.now(UTC),
            )

        try:
            detail = await self._dispatch_command(cmd)
        except WatlowError as exc:
            raise AdapterError(
                f"watlow {self.name!r} command {cmd.kind!r} failed: {exc}",
                device=self.name,
            ) from exc

        return CommandResult(
            accepted=True,
            detail=detail,
            t_mono_ns=clock.t_mono_ns(),
            t_utc=datetime.now(UTC),
        )

    async def _dispatch_command(self, cmd: DeviceCommand) -> str:
        """Dispatch a generic :class:`DeviceCommand` to the right typed call.

        Recognized ``cmd.kind`` values:

        * ``"set_setpoint"`` — payload ``{"value": float, "instance": int=1}``.
        * ``"write_parameter"`` / ``"set_parameter"`` — payload
          ``{"name": str, "value": float|int, "instance": int=1}``.
        """
        assert self._controller is not None
        kind = cmd.kind
        if kind == "set_setpoint":
            value = float(cmd.payload["value"])
            instance = int(cmd.payload.get("instance", 1))
            reading = await self._controller.set_setpoint(value, instance=instance, confirm=True)
            return f"set_setpoint instance={instance} echoed={reading.value!r}"
        if kind in ("write_parameter", "set_parameter"):
            name = str(cmd.payload["name"])
            value = cmd.payload["value"]
            instance = int(cmd.payload.get("instance", 1))
            entry = await self._controller.write_parameter(
                name, value, instance=instance, confirm=True
            )
            return f"write_parameter {name} instance={instance} echoed={entry.value!r}"
        raise AdapterError(
            f"watlow {self.name!r}: unknown command kind {kind!r}",
            device=self.name,
        )

    # Typed helpers — the IDE-friendly parallel to ``.command()``.
    async def set_setpoint(
        self,
        value: float,
        *,
        instance: int = 1,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        return await self.command(
            DeviceCommand(
                kind="set_setpoint",
                target=f"setpoint:{instance}",
                payload={"value": value, "instance": instance},
                issued_by=issued_by,
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )

    async def write_parameter(
        self,
        parameter: str,
        value: float | int | str | bool,
        *,
        instance: int = 1,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        return await self.command(
            DeviceCommand(
                kind="write_parameter",
                target=f"{parameter}:{instance}",
                payload={"name": parameter, "value": value, "instance": instance},
                issued_by=issued_by,
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )

    async def read_pv(self, *, instance: int = 1) -> Reading:
        """Read the current process value (no authorization gate — read-only)."""
        if self._controller is None:
            raise AdapterError(
                f"watlow {self.name!r} read_pv() requires open() first",
                device=self.name,
            )
        try:
            return await self._controller.read_pv(instance=instance)
        except WatlowError as exc:
            raise AdapterError(
                f"watlow {self.name!r} read_pv failed: {exc}", device=self.name
            ) from exc

    # ------------------------------------------------------------------ helpers

    async def _build_controller(self) -> Controller:
        """Construct the underlying :class:`Controller`.

        Default path: :func:`watlowlib.open_device` with the configured port +
        protocol. Test path: the injected ``controller_factory`` returns a
        controller built over a :class:`watlowlib.transport.fake.FakeTransport`.
        """
        if self._controller_factory is not None:
            return await self._controller_factory()
        return await watlowlib.open_device(
            self.params.port,
            protocol=self.params.protocol_kind(),
            address=self.params.address,
            serial_settings=self.params.to_serial_settings(),
        )

    async def _enter_controller(self, controller: Controller) -> None:
        """Enter the controller's async-context-manager.

        :func:`watlowlib.open_device` returns an *unopened* controller (except
        under ``protocol=AUTO``, which opens the transport during detection
        and skips the second open in :meth:`Controller.__aenter__`). We always
        call ``__aenter__`` so the lifecycle is uniform.
        """
        await controller.__aenter__()

    async def _safe_close_controller(self) -> None:
        if self._controller is None:
            return
        try:
            await self._controller.__aexit__(None, None, None)
        except WatlowError:
            # Cleanup path: don't mask whatever the original failure was.
            return

    def _record_for(self, sample: Sample) -> SourceRecord:
        """Convert a watlowlib :class:`Sample` into a long-format
        :class:`SourceRecord`.

        Uses the library's own :func:`watlowlib.sinks.sample_to_row` helper so
        the row schema matches what an offline ``watlowlib`` recorder would
        produce — important for ``device_records/watlow.parquet`` parity
        across sim and real bundles.
        """
        assert self._clock is not None
        row = sample_to_row(sample)
        # Translate the library's monotonic_ns (host clock) into a run-relative
        # offset so it joins cleanly with ChannelSample.t_mono_ns.
        t_mono_ns = sample.monotonic_ns - self._clock.started_mono_ns
        self._seq += 1
        return SourceRecord(
            record_id=make_record_id(ADAPTER_ID, self.name, self._seq),
            adapter=ADAPTER_ID,
            device=self.name,
            shape="long_row",
            t_mono_ns=t_mono_ns,
            t_utc=sample.midpoint_at,
            row=row,
            metadata={"parameter_id": sample.parameter_id},
        )

    def _channel_samples_for(self, sample: Sample, record_id: str) -> list[DeviceEmission]:
        """Map ``sample`` against the configured :class:`WatlowParameter`
        bindings. Yields one :class:`ChannelSample` per matching channel."""
        assert self._clock is not None
        if sample.value is None or isinstance(sample.value, str):
            # Sensor-fail / overload / textual values cannot be calibrated;
            # the row is still preserved in device_records, but no
            # ChannelSample is emitted.
            return []
        t_mono_ns = sample.monotonic_ns - self._clock.started_mono_ns
        emissions: list[DeviceEmission] = []
        for spec in self._channels:
            binding = spec.source
            assert isinstance(binding, WatlowParameter)
            if binding.parameter == sample.parameter and binding.instance == sample.instance:
                emissions.append(
                    build_channel_sample(
                        spec=spec,
                        raw_value=float(sample.value),
                        t_mono_ns=t_mono_ns,
                        source_record_id=record_id,
                        source_field=sample.parameter,
                    )
                )
        return emissions

    def watchdog_state(self) -> WatchdogState:
        """Watchdog view consumed by the engine's silent-device task (plan §13.2)."""
        return WatchdogState(
            device=self.name,
            last_t_mono_ns=self._last_sample.last_t_mono_ns,
            expected_period_ns=int(1e9 / self.params.rate_hz),
        )

    def _snapshot_due(self) -> bool:
        if self._clock is None:
            return False
        elapsed_ns = self._clock.t_mono_ns() - self._last_snapshot_t_mono_ns
        return elapsed_ns >= int(self.params.snapshot_period_s * 1e9)

    def _snapshot_fields(self) -> dict[str, float | int | str | bool | None]:
        info = self._device_info
        out: dict[str, float | int | str | bool | None] = {
            "address": self.params.address,
            "protocol": self.params.protocol,
            "channel_count": len(self._channels),
            "state": self._lifecycle.state,
        }
        if info is not None:
            out["part_number"] = info.part_number.raw or None
            out["family"] = info.family.value
            out["hardware_id"] = info.hardware_id
            out["firmware_id"] = info.firmware_id
            out["serial_number"] = info.serial_number
            out["health"] = info.health.value
            out["loops"] = info.loops
            if info.configured_protocol is not None:
                out["configured_protocol"] = info.configured_protocol.value
        return out


# ---------------------------------------------------------------------------
# CLI handshake hook (``capa validate --strict``)
# ---------------------------------------------------------------------------


async def handshake(params: dict[str, Any]) -> str:
    """Read-only open + identify + close. Used by ``capa validate --strict``.

    Returns a one-line summary of the device identity. Raises
    :class:`AdapterError` on any failure so the CLI can surface the wiring
    problem before the operator arms a run.
    """
    parsed = WatlowAdapterParams.model_validate(params)
    try:
        controller = await watlowlib.open_device(
            parsed.port,
            protocol=parsed.protocol_kind(),
            address=parsed.address,
            serial_settings=parsed.to_serial_settings(),
        )
        async with controller as ctrl:
            info = await ctrl.identify(query_configured_protocol=True)
    except WatlowError as exc:
        raise AdapterError(f"watlow handshake failed at {parsed.port}: {exc}") from exc
    return (
        f"watlow part={info.part_number.raw or '?'} "
        f"fw={info.firmware_id} hw={info.hardware_id} "
        f"family={info.family.value} health={info.health.value}"
    )


__all__ = [
    "ADAPTER_ID",
    "WatlowAdapter",
    "WatlowAdapterParams",
    "handshake",
]
