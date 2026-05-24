"""Real :class:`WatlowAdapter` — wraps a :class:`watlowlib.Controller`.

"real :class:`WatlowAdapter` (smallest viable real device);
Watlow ``SourceRecord`` preservation; Watlow parameter-to-channel mapping;
hardware smoke-test gate."

Architecture ():

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
* ``command`` enforces the authorization gate (every device write
  carries ``issued_by`` plus either ``authorization_id`` or ``confirmed_by``)
  and dispatches to :meth:`watlowlib.Controller.set_setpoint` /
  :meth:`watlowlib.Controller.write_parameter`.

For tests, an opt-in ``controller_factory`` kwarg lets the adapter run against
an in-process :class:`watlowlib.transport.fake.FakeTransport` without touching
a serial port.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal

import structlog
import watlowlib
from pydantic import BaseModel, ConfigDict, Field
from watlowlib import (
    Controller,
    DeviceInfo,
    OverflowPolicy,
    ProtocolKind,
    Reading,
    Sample,
    Unit,
    WatlowError,
    sample_to_row,
)
from watlowlib.streaming.recorder import record as watlow_record
from watlowlib.transport.base import ByteSize, Parity, SerialSettings, StopBits
from watlowlib.units import to_pint

from capa.channels.spec import ChannelSpec, WatlowParameter
from capa.core.clock import RunClock
from capa.core.errors import AdapterError
from capa.core.units import canonicalize_unit, units_compatible
from capa.devices._helpers import (
    WatchdogState,
    build_channel_sample,
    channels_for_device,
    make_accepted_result,
    make_not_open_result,
    make_record_id,
    reject_unless_authorized,
    serial_resource_id,
)
from capa.devices.adapter import (
    AdapterStartContext,
    Capability,
    CommandResult,
    DeviceCommand,
)
from capa.devices.records import (
    DeviceEmission,
    DeviceEvent,
    DeviceHealth,
    DeviceSnapshot,
    SourceRecord,
)
from capa.devices.runtime_state import AdapterRuntimeState

if TYPE_CHECKING:
    from capa.devices.registry import AdapterDescriptor

ADAPTER_ID: Final[str] = "watlow"

_logger = structlog.get_logger("capa.devices.watlow")

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

    adapter-specific knobs live under ``DeviceConfig.params`` and are
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
    from watlowlib; pin one in production."""

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
    terminate the stream — they are logged and the recorder retries on the
    next tick."""

    snapshot_period_s: float = Field(gt=0, default=30.0)
    """Cadence of :class:`DeviceSnapshot` emissions during a run."""

    identify_on_open: bool = True
    """Run :meth:`Controller.identify` immediately after opening, populating
    the cached :class:`DeviceInfo` used by :meth:`WatlowAdapter.snapshot`."""

    io_timeout_s: float | None = Field(default=None, gt=0.0)
    """Per-call wall-clock bound for the request→reply round-trip on
    ``set_setpoint`` / ``write_parameter`` / ``read_pv`` / ``identify``.
    ``None`` (the default) inherits :data:`watlowlib.config.DEFAULTS.io_timeout_s`
    (1.0s). Bump to 2.0–3.0 when the bus is slow — e.g. a USB-RS485
    bridge with a high latency timer, or a controller that occasionally
    needs more than one tick to respond. Streaming reads ignore this
    knob; ``watlowlib.streaming.record`` always uses the library default."""

    wire_temperature_unit: Literal["C", "F", "celsius", "fahrenheit", "degC", "degF"] | None = "F"
    """Externally-verified scale of temperature values on the wire.

    Default is ``"F"`` because the rig's PM3 (PM3R1CA fw=1) was
    empirically verified to emit Fahrenheit values on the wire — with
    the panel set to °C, the comms readback differed from the panel
    by exactly the F↔C conversion (panel PV=18.7°C vs comms 65.65;
    65.65°F = 18.69°C). Parameter 3005 controls only the panel
    display, not the wire scale; parameter 17050 is label-only on at
    least one firmware revision. The wire scale appears to be hardware-
    pinned on this SKU.

    If you bring up a different PM3 (or a different SKU/firmware)
    whose wire is in °C, override this in the TOML
    (``wire_temperature_unit = "C"``). Verify empirically with
    ``watlow-diag probe-unit`` (compare a known panel reading against
    the comms readback) before pinning it. Set to ``None`` to suppress
    the assertion entirely — :attr:`Sample.unit` will be ``None`` for
    temperature parameters and the per-channel drift check becomes a
    no-op."""

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
# Operator-facing readback snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WatlowStateSnapshot:
    """One-shot readback of the operator-facing values on a Watlow loop.

    Built by :meth:`WatlowAdapter.read_state_snapshot` on the worker loop and
    consumed by the manual-control card's setpoint widget on the qasync loop.
    Values are wire-side: the card applies the bound channel's forward
    calibration to render them in the user-facing unit. ``None`` means the
    read was attempted but rejected by the device (or no controller is open).
    """

    setpoint: float | None = None
    """Wire-side setpoint value for loop 1; ``None`` when the read failed."""

    setpoint_unit: str | None = None
    """The :class:`watlowlib.Unit` value (``"C"`` / ``"F"`` / ``"%"``) the
    library tagged on the reading, or ``None`` when the device doesn't tag
    it (typical for ``assert_wire_temperature_unit=None``)."""

    process_value: float | None = None
    """Wire-side PV value for loop 1."""

    process_value_unit: str | None = None
    """Wire-side unit tag for the PV read."""


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

    * ``WatlowAdapter(name=..., **params_kwargs)`` — the materialization
      path uses this. Per-device params from ``[devices.params]`` are
      forwarded as kwargs and parsed into a
      :class:`WatlowAdapterParams`.
    * ``WatlowAdapter(name=..., params=WatlowAdapterParams(...))`` — for
      programmatic construction in tests.

    Both shapes accept an optional ``controller_factory`` kwarg as a test
    seam: when supplied, the adapter calls it instead of
    :func:`watlowlib.open_device` so unit tests can wire up a
    :class:`watlowlib.transport.fake.FakeTransport`-backed controller.

    File layout:

    * **Runtime state** — ``__init__``, ``configure_channels``, lifecycle
      (``open``/``close``/``start``/``stop``), ``snapshot``, ``stream``,
      ``watchdog_state``, snapshot-bookkeeping helpers.
    * **Command surface** — ``command`` (authorization gate), ``_dispatch_command``,
      typed wrappers (``set_setpoint``, ``write_parameter``, …), and the
      manual-control card's ``read_state_snapshot``.
    * **Vendor protocol** — watlowlib-specific code: controller construction,
      sample → ``SourceRecord`` conversion, channel routing, drift detection.
      Stays here because it is genuinely watlowlib-shaped.
    """

    __slots__ = (
        "_channels",
        "_controller",
        "_controller_factory",
        "_device_info",
        "_display_unit",
        "_drift_event_buffer",
        "_drift_skipped_channels",
        "_state",
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
                Capability.HAS_PARAMETER_CONFIG,
            }
        )
        self._controller_factory: ControllerFactory | None = controller_factory
        self._controller: Controller | None = None
        self._device_info: DeviceInfo | None = None
        self._display_unit: Unit | None = None
        self._channels: list[ChannelSpec] = []
        self._state = AdapterRuntimeState()
        # Channels whose wire-side unit was checked against the declared
        # channel unit and didn't match — we skip ChannelSample derivation
        # for these so a misconfigured rig surfaces in events.sqlite rather
        # than producing silently-wrong calibrated values.
        self._drift_skipped_channels: set[str] = set()
        # DeviceEvents queued by ``_channel_samples_for`` for emission in
        # the next ``stream()`` iteration. Adapters cannot synchronously
        # yield from a helper method, so the event buffer is the bridge.
        self._drift_event_buffer: list[DeviceEvent] = []

    # =====================================================================
    # SECTION 1 — Runtime state: lifecycle, wiring, snapshot/stream/health
    # =====================================================================

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
    def expected_emission_rate_hz(self) -> float:
        # One SourceRecord + one ChannelSample per bound channel per poll.
        return self.params.rate_hz * (1 + len(self._channels))

    @property
    def resource_id(self) -> str:
        return serial_resource_id(self.params.port)

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
        if self._state.lifecycle.state in ("open", "running"):
            return
        try:
            self._controller = await self._build_controller()
            if self.params.identify_on_open:
                self._device_info = await self._controller.identify(
                    query_configured_protocol=True,
                    timeout=self.params.io_timeout_s,
                )
                # Prime the session cache for parameter 17050's *label* so the
                # snapshot shows what the device thinks its comms-display unit
                # is. Best-effort: ``read_comms_unit_label`` returns ``None``
                # on a device that rejects the read, and that ``None`` is
                # itself useful state (it tells the snapshot we tried). Note
                # this is diagnostic only — under watlowlib 0.4 the wire-side
                # tag on Sample.unit comes from ``wire_temperature_unit``, not
                # this label.
                self._display_unit = await self._controller.read_comms_unit_label()
        except WatlowError as exc:
            await self._safe_close_controller()
            raise AdapterError(
                f"watlow {self.name!r} open failed: {exc}", device=self.name
            ) from exc
        self._state.lifecycle.open()

    async def close(self) -> None:
        """Release the bus / handle. Idempotent."""
        if self._state.lifecycle.state == "closed":
            return
        await self._safe_close_controller()
        self._controller = None
        self._state.lifecycle.close()

    async def start(self, ctx: AdapterStartContext) -> None:
        """Capture the :class:`RunClock` anchor and arm the streaming loop."""
        self._state.on_start(ctx.clock)
        # New run, fresh drift-check state — a misconfig that was fixed
        # between runs should not stay quarantined.
        self._drift_skipped_channels.clear()
        self._drift_event_buffer.clear()

    async def stop(self) -> None:
        """Request the streaming loop to exit cleanly.

        The next batch arrival from :func:`watlowlib.record` lets ``stream``
        observe the request and break out of its async-cm. Idempotent.
        """
        self._state.request_stop()

    async def snapshot(self) -> DeviceSnapshot:
        """Build a :class:`DeviceSnapshot` from the library snapshot + live health.

        Reads ``controller.snapshot()`` (I/O-free) for cached identity,
        family, capabilities, and the session counters, and projects
        into capa's emission shape. Health derives from the shared
        :meth:`AdapterRuntimeState.compute_health` for parity with the
        other real adapters.
        """
        clock = self._state.clock or RunClock.now()
        return DeviceSnapshot(
            adapter=ADAPTER_ID,
            device=self.name,
            t_mono_ns=clock.t_mono_ns(),
            t_utc=datetime.now(UTC),
            health=self._compute_health(clock=clock),
            fields=await self._snapshot_fields(),
        )

    def _compute_health(self, *, clock: RunClock) -> DeviceHealth:
        """Derive the operator-facing health pill.

        Mirrors the session's ``recoverable_error_count`` (kept dormant
        in watlowlib today — no transient class is wired) into the
        runtime state so the shared
        :meth:`AdapterRuntimeState.compute_health` logic still applies.
        """
        if self._controller is not None:
            self._state.recoverable_error_count = self._controller.session.recoverable_error_count
        return self._state.compute_health(clock=clock, rate_hz=self.params.rate_hz)

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
        if self._state.clock is None:
            raise AdapterError(
                f"watlow {self.name!r} stream() requires start() first",
                device=self.name,
            )

        # Yield an initial snapshot so the manifest's equipment block has
        # something to show before the first poll lands.
        snap = await self.snapshot()
        self._state.last_snapshot_t_mono_ns = snap.t_mono_ns
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
            ) as recording:
                async for batch in recording.stream:
                    if self._state.stop_requested:
                        break
                    # Watlow is the one adapter whose ``poll_many`` returns
                    # multiple Samples per acquisition tick (one per polled
                    # parameter). Mark the first SourceRecord of each batch
                    # so the worker can count one poll per tick rather than
                    # one per parameter — without that flag, a 1 Hz heater
                    # with 2 parameters bins as ~2 Hz at best, or thousands
                    # of Hz when the within-batch yields are sub-millisecond.
                    for i, sample in enumerate(batch):
                        record = self._record_for(sample, tick_first=(i == 0))
                        yield record
                        self._state.last_sample.mark(record.t_mono_ns)
                        for cs in self._channel_samples_for(sample, record.record_id):
                            yield cs
                    # Drain any drift-mismatch events queued by
                    # ``_channel_samples_for``. Emitting after the batch (vs.
                    # inline) keeps record/sample ordering deterministic and
                    # lets the test assertion on event count match
                    # one-event-per-channel exactly.
                    while self._drift_event_buffer:
                        yield self._drift_event_buffer.pop(0)
                    # Periodic snapshot — cadence-bounded, never per-tick.
                    if self._state.snapshot_due(period_s=self.params.snapshot_period_s):
                        snap = await self.snapshot()
                        self._state.last_snapshot_t_mono_ns = snap.t_mono_ns
                        yield snap
        except* WatlowError as eg:
            first = next(iter(eg.exceptions))
            raise AdapterError(
                f"watlow {self.name!r} stream failed: {first}", device=self.name
            ) from first

    # --------------------------------------------------- silence state / snapshot

    def watchdog_state(self) -> WatchdogState:
        """Return a compact silence-state view for tests and future policy work."""
        return self._state.watchdog(device=self.name, rate_hz=self.params.rate_hz)

    async def _snapshot_fields(self) -> dict[str, float | int | str | bool | None]:
        lib_snap = await self._controller.snapshot() if self._controller is not None else None
        info = self._device_info
        recoverable = lib_snap.recoverable_error_count if lib_snap is not None else 0
        out: dict[str, float | int | str | bool | None] = {
            "address": self.params.address,
            "protocol": self.params.protocol,
            "channel_count": len(self._channels),
            "state": self._state.lifecycle.state,
            "display_unit": self._display_unit.value if self._display_unit is not None else None,
            "recoverable_errors": recoverable,
        }
        if lib_snap is not None and lib_snap.family is not None:
            out["family"] = lib_snap.family.value
        if info is not None:
            out["part_number"] = info.part_number.raw or None
            out["hardware_id"] = info.hardware_id
            out["firmware_id"] = info.firmware_id
            out["serial_number"] = info.serial_number
            out["health"] = info.health.value
            out["loops"] = info.loops
            if info.configured_protocol is not None:
                out["configured_protocol"] = info.configured_protocol.value
        return out

    # =====================================================================
    # SECTION 2 — Command surface: authorization gate + typed wrappers
    # =====================================================================

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        """Issue a generic command. Authorization gate first, then dispatch.

        commands without either ``authorization_id`` (run-arm cover)
        or ``confirmed_by`` (manual UI confirmation) are refused at the
        adapter boundary, regardless of the underlying device's own gates.
        """
        clock = self._state.clock or RunClock.now()
        rejection = reject_unless_authorized(
            cmd, adapter_id=ADAPTER_ID, device_name=self.name, clock=clock
        )
        if rejection is not None:
            return rejection
        if self._controller is None:
            return make_not_open_result(adapter_id=ADAPTER_ID, device_name=self.name, clock=clock)

        try:
            detail = await self._dispatch_command(cmd)
        except WatlowError as exc:
            raise AdapterError(
                f"watlow {self.name!r} command {cmd.kind!r} failed: {exc}",
                device=self.name,
            ) from exc

        return make_accepted_result(detail=detail, clock=clock)

    async def _dispatch_command(self, cmd: DeviceCommand) -> str:
        """Dispatch a generic :class:`DeviceCommand` to the right typed call.

        Recognized ``cmd.kind`` values:

        * ``"set_setpoint"`` — payload ``{"value": float, "instance": int=1}``.
        * ``"write_parameter"`` / ``"set_parameter"`` — payload
          ``{"name": str, "value": float|int, "instance": int=1}``.
        """
        assert self._controller is not None
        kind = cmd.kind
        timeout = self.params.io_timeout_s
        if kind == "set_setpoint":
            value = float(cmd.payload["value"])
            instance = int(cmd.payload.get("instance", 1))
            # Setpoint values arrive in the bound channel's user-facing unit
            # (``derived_unit``). The wire expects ``unit`` — invert the
            # channel calibration so the operator sees a Celsius API even
            # when the device speaks Fahrenheit. No-op if no matching
            # channel is configured (raw command from a test or diagnostic)
            # or the calibration is identity.
            wire_value = self._invert_setpoint(value, instance=instance)
            reading = await self._controller.set_setpoint(
                wire_value, instance=instance, confirm=True, timeout=timeout
            )
            # Round the human-facing values in the detail string. Float
            # noise from the inverse-calibration math (e.g. ``(100 -
            # -17.778) / 0.5556`` lands at 211.9999…, not 212.0) is
            # surprising in operator-facing logs even though the value
            # going to the controller is mathematically correct.
            echoed_value = reading.value
            echoed_str = (
                f"{round(float(echoed_value), 4)!r}"
                if isinstance(echoed_value, int | float)
                else f"{echoed_value!r}"
            )
            return (
                f"set_setpoint instance={instance} "
                f"user={round(value, 4)!r} wire={round(wire_value, 4)!r} "
                f"echoed={echoed_str}"
            )
        if kind in ("write_parameter", "set_parameter"):
            name = str(cmd.payload["name"])
            value = cmd.payload["value"]
            instance = int(cmd.payload.get("instance", 1))
            entry = await self._controller.write_parameter(
                name, value, instance=instance, confirm=True, timeout=timeout
            )
            return f"write_parameter {name} instance={instance} echoed={entry.value!r}"
        if kind == "set_display_units":
            unit_arg = cmd.payload["unit"]
            # Writes parameter 17050 (the comms-display *label*). RWE /
            # persistent; the authorization gate above already accepted, so
            # we pass confirm=True through to watlowlib's own gate. Under
            # watlowlib 0.4 this is label-only — it does NOT change the
            # scale of values on the wire, only what the device reports for
            # 17050. We still cache the echoed value for the snapshot and
            # clear the drift-skip set in case the operator's
            # ``wire_temperature_unit`` assertion was wrong and is being
            # corrected via a re-open elsewhere.
            echoed = await self._controller.set_comms_unit_label(
                unit_arg, confirm=True, timeout=timeout
            )
            self._display_unit = echoed
            self._drift_skipped_channels.clear()
            echoed_display = echoed.value if echoed is not None else None
            return f"set_display_units echoed={echoed_display!r}"
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
            return await self._controller.read_pv(
                instance=instance, timeout=self.params.io_timeout_s
            )
        except WatlowError as exc:
            raise AdapterError(
                f"watlow {self.name!r} read_pv failed: {exc}", device=self.name
            ) from exc

    async def read_state_snapshot(self) -> WatlowStateSnapshot | None:
        """One-shot read of setpoint + PV for the manual-control card.

        Returns ``None`` when no controller is open (card built before the
        pool finished its initial open()). Individual reads that fail are
        captured as ``None`` fields rather than raising — a temporarily
        unreachable parameter shouldn't kill the whole snapshot.

        No authorization gate (read-only), no shield (no half-transaction
        failure mode). The watlowlib session serializes commands so this
        won't race the streaming recorder on the same bus.
        """
        if self._controller is None:
            return None
        timeout = self.params.io_timeout_s
        sp_value: float | None = None
        sp_unit: str | None = None
        try:
            sp = await self._controller.read_setpoint(instance=1, timeout=timeout)
            sp_value = float(sp.value) if isinstance(sp.value, int | float) else None
            sp_unit = sp.unit.value if isinstance(sp.unit, Unit) else None
        except WatlowError as exc:
            _logger.debug(
                "watlow.read_setpoint_failed",
                device=self.name,
                error=str(exc),
            )
        pv_value: float | None = None
        pv_unit: str | None = None
        try:
            pv = await self._controller.read_pv(instance=1, timeout=timeout)
            pv_value = float(pv.value) if isinstance(pv.value, int | float) else None
            pv_unit = pv.unit.value if isinstance(pv.unit, Unit) else None
        except WatlowError as exc:
            _logger.debug(
                "watlow.read_pv_failed",
                device=self.name,
                error=str(exc),
            )
        return WatlowStateSnapshot(
            setpoint=sp_value,
            setpoint_unit=sp_unit,
            process_value=pv_value,
            process_value_unit=pv_unit,
        )

    async def set_display_units(
        self,
        unit: Unit | str,
        *,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Write the comms display unit (parameter 17050). RWE / persistent.

        Accepts a :class:`Unit` or a case-insensitive string alias
        (``"C"`` / ``"F"`` / ``"celsius"`` / ``"degF"`` / ``"°C"``).
        Mirrors :meth:`sartoriuslib.Balance.set_display_unit` in shape so
        the manual-control card's dispatch site is uniform across adapters.
        """
        return await self.command(
            DeviceCommand(
                kind="set_display_units",
                target="display_units",
                payload={"unit": unit if isinstance(unit, str) else unit.value},
                issued_by=issued_by,
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )

    async def read_display_units(self) -> Unit | None:
        """Read the cached comms-display *label* (parameter 17050).

        Under watlowlib 0.4 this is the label-only register, not the
        wire-side unit — see :attr:`WatlowAdapterParams.wire_temperature_unit`
        for the latter. The cached value is primed at :meth:`open` and
        refreshed on every call here / on :meth:`set_display_units`.
        """
        if self._controller is None:
            raise AdapterError(
                f"watlow {self.name!r} read_display_units() requires open() first",
                device=self.name,
            )
        try:
            self._display_unit = await self._controller.read_comms_unit_label()
        except WatlowError as exc:
            raise AdapterError(
                f"watlow {self.name!r} read_display_units failed: {exc}", device=self.name
            ) from exc
        return self._display_unit

    @property
    def display_unit(self) -> Unit | None:
        """The cached comms-display *label* (parameter 17050) from the last
        :meth:`open` / :meth:`read_display_units` / :meth:`set_display_units`.
        ``None`` until the adapter has been opened, or when the device
        rejects the read. Diagnostic only — the wire-side scale comes from
        :attr:`WatlowAdapterParams.wire_temperature_unit`."""
        return self._display_unit

    # =====================================================================
    # SECTION 3 — Vendor protocol: watlowlib-specific controller / sample / drift
    # =====================================================================

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
            assert_wire_temperature_unit=self.params.wire_temperature_unit,
        )

    async def _safe_close_controller(self) -> None:
        if self._controller is None:
            return
        try:
            await self._controller.close()
        except WatlowError:
            # Cleanup path: don't mask whatever the original failure was.
            return

    def _record_for(self, sample: Sample, *, tick_first: bool = True) -> SourceRecord:
        """Convert a watlowlib :class:`Sample` into a long-format
        :class:`SourceRecord`.

        Uses the library's own :func:`watlowlib.sinks.sample_to_row` helper so
        the row schema matches what an offline ``watlowlib`` recorder would
        produce — important for ``device_records/watlow.parquet`` parity
        across sim and real bundles.

        ``tick_first`` lands in :attr:`SourceRecord.metadata` so the worker
        can collapse the per-parameter fanout back into one observation per
        acquisition tick when computing the operator-facing poll rate.
        """
        clock = self._state.clock
        assert clock is not None
        row = sample_to_row(sample)
        # Translate the library's monotonic timestamp (host clock) into a
        # run-relative offset so it joins cleanly with ChannelSample.t_mono_ns.
        t_mono_ns = sample.t_mono_ns - clock.started_mono_ns
        self._state.seq += 1
        return SourceRecord(
            record_id=make_record_id(ADAPTER_ID, self.name, self._state.seq),
            adapter=ADAPTER_ID,
            device=self.name,
            shape="long_row",
            t_mono_ns=t_mono_ns,
            t_utc=sample.t_utc,
            row=row,
            metadata={"parameter_id": sample.parameter_id, "tick_first": tick_first},
        )

    def _invert_setpoint(self, value: float, *, instance: int) -> float:
        """Return the wire-unit value corresponding to a user-facing
        ``value`` for the setpoint channel bound at ``instance``.

        Looks up the configured channel whose binding is
        ``(parameter="setpoint", instance=instance)`` and inverts its
        calibration. Returns ``value`` unchanged when no such channel
        is configured (the adapter is being driven directly without a
        channel mapping — e.g. a one-shot diagnostic) or when the
        calibration doesn't expose an ``invert`` (Polynomial, Lookup,
        CustomCallable, Piecewise — those calibrations don't have a
        unique inverse and should be re-configured as LinearTwoPoint
        when used on a setpoint channel).
        """
        for spec in self._channels:
            binding = spec.source
            assert isinstance(binding, WatlowParameter)
            if binding.parameter != "setpoint" or binding.instance != instance:
                continue
            invert = getattr(spec.calibration, "invert", None)
            if invert is None:
                return value
            return float(invert(value))
        return value

    def _channel_samples_for(self, sample: Sample, record_id: str) -> list[DeviceEmission]:
        """Map ``sample`` against the configured :class:`WatlowParameter`
        bindings. Yields one :class:`ChannelSample` per matching channel.

        Performs a one-shot drift check per channel: when ``sample.unit`` is
        a known :class:`Unit`, verify dimensional compatibility with the
        channel's declared ``unit``. On mismatch we quarantine the channel
        (skip :class:`ChannelSample` derivation until the next ``start()``
        or successful ``set_display_units``) and queue a
        :class:`DeviceEvent` for the next ``stream()`` iteration to yield.
        Native row stays in ``device_records/watlow.parquet`` so an analyst
        can still see what the device was reporting.
        """
        clock = self._state.clock
        assert clock is not None
        if sample.value is None or isinstance(sample.value, str):
            # Sensor-fail / overload / textual values cannot be calibrated;
            # the row is still preserved in device_records, but no
            # ChannelSample is emitted.
            return []
        t_mono_ns = sample.t_mono_ns - clock.started_mono_ns
        emissions: list[DeviceEmission] = []
        for spec in self._channels:
            binding = spec.source
            assert isinstance(binding, WatlowParameter)
            if binding.parameter != sample.parameter or binding.instance != sample.instance:
                continue
            if spec.name in self._drift_skipped_channels:
                continue
            if self._unit_mismatch(spec, sample):
                # Quarantine and queue an event; surfaces in the events
                # dock and events.sqlite at the next stream iteration.
                self._drift_skipped_channels.add(spec.name)
                self._drift_event_buffer.append(self._drift_event(spec, sample, t_mono_ns))
                continue
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

    def _unit_mismatch(self, spec: ChannelSpec, sample: Sample) -> bool:
        """``True`` when ``sample.unit`` is a known :class:`Unit` whose pint
        equivalent disagrees with ``spec.unit``.

        Compares canonical pint names (``"degC"`` vs ``"celsius"`` collapse;
        ``degC`` vs ``degF`` do not). Dimensional-only compatibility would
        accept °C vs °F — pint treats both as temperature — but a wire
        reporting °F into a channel declared °C is exactly the misconfig the
        check exists to catch, so we go stricter.

        ``sample.unit is None`` (registry has no unit kind for this parameter,
        or device rejected the 17050 read) → no check, returns ``False``.
        Free-form string units (sims, cross-vendor rows) are also skipped.
        """
        if not isinstance(sample.unit, Unit):
            return False
        pint_unit = to_pint(sample.unit)
        if pint_unit is None:
            return False
        # A dimensional mismatch (g vs degC) and a scale mismatch (degC vs
        # degF) are both genuine misconfigs. The dimensional check fires
        # first because canonicalization of incompatible-dimension units
        # would still produce two valid pint names; we want the dimensional
        # branch to phrase the error so we keep both checks.
        if not units_compatible(pint_unit, spec.unit):
            return True
        return canonicalize_unit(pint_unit) != canonicalize_unit(spec.unit)

    def _drift_event(self, spec: ChannelSpec, sample: Sample, t_mono_ns: int) -> DeviceEvent:
        """Build the :class:`DeviceEvent` for a wire/declared unit mismatch."""
        wire_unit = sample.unit
        assert isinstance(wire_unit, Unit)  # _unit_mismatch guards this
        wire_str = to_pint(wire_unit) or wire_unit.value
        return DeviceEvent(
            adapter=ADAPTER_ID,
            device=self.name,
            t_mono_ns=t_mono_ns,
            t_utc=datetime.now(UTC),
            kind="unit_mismatch",
            severity="error",
            message=(
                f"channel {spec.name!r} declares unit {spec.unit!r} but wire-side "
                f"unit is {wire_unit.value!r} ({wire_str!r}); ChannelSample "
                f"derivation skipped. Fix the channel config or the device's "
                f"parameter 17050 (set_display_units) to recover."
            ),
            metadata={
                "channel": spec.name,
                "parameter": sample.parameter,
                "instance": sample.instance,
                "declared_unit": spec.unit,
                "wire_unit": wire_unit.value,
            },
        )


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
            assert_wire_temperature_unit=parsed.wire_temperature_unit,
        )
        try:
            info = await controller.identify(query_configured_protocol=True)
            display_unit = await controller.read_comms_unit_label()
        finally:
            await controller.close()
    except WatlowError as exc:
        raise AdapterError(f"watlow handshake failed at {parsed.port}: {exc}") from exc
    unit_str = display_unit.value if display_unit is not None else "?"
    return (
        f"watlow part={info.part_number.raw or '?'} "
        f"fw={info.firmware_id} hw={info.hardware_id} "
        f"family={info.family.value} health={info.health.value} "
        f"display_unit={unit_str}"
    )


__all__ = [
    "ADAPTER_ID",
    "DESCRIPTOR",
    "WatlowAdapter",
    "WatlowAdapterParams",
    "WatlowStateSnapshot",
    "discover",
    "handshake",
]


# ---------------------------------------------------------------------------
# Discovery hook (``capa devices discover`` / Setup editor scan)
# ---------------------------------------------------------------------------


async def discover(
    *,
    ports: list[str] | None = None,
    addresses: tuple[int, ...] | None = None,
    baudrates: tuple[int, ...] | None = None,
    timeout_s: float = 0.5,
) -> list[dict[str, Any]]:
    """Probe local serial buses for Watlow PM-series controllers.

    Thin wrapper over :func:`watlowlib.find_devices` (shipped in
    watlowlib 0.5.0). The library iterates the cartesian product of
    ``ports × baudrates × protocols × addresses``, runs
    :meth:`Controller.identify` per probe, and short-circuits a port
    if it can't be opened.

    ``addresses`` and ``baudrates`` default to
    :data:`watlowlib.DEFAULT_DISCOVERY_ADDRESSES` (``(1,)``) and
    :data:`watlowlib.DEFAULT_DISCOVERY_BAUDRATES` (``(38400, 19200,
    9600)``). CAPA rigs near-universally leave the Watlow at address
    1, so the default address sweep is single-shot; operators on a
    non-default address can pass ``addresses=range(1, 248)`` and
    accept the longer scan.

    Only ``ok=True`` rows (the controller identified successfully)
    surface in the result. Silent or errored probes are dropped — the
    Setup Discover dialog only needs hits.
    """
    if ports is not None and not ports:
        return []

    try:
        results = await watlowlib.find_devices(
            ports=ports,
            addresses=addresses,
            baudrates=baudrates,
            per_probe_timeout_s=timeout_s,
        )
    except WatlowError:
        return []

    # One physical controller lives at exactly one (port, address). Dedup
    # on that pair, first-hit-wins: find_devices iterates outermost-port,
    # then baudrate (38400 / 19200 / 9600), then protocol (STDBUS /
    # MODBUS_RTU), then address. The first hit is therefore the most
    # likely production config. Collapsing here keeps the Discover dialog
    # readable when a bus answers at multiple bauds or protocols.
    rows: list[dict[str, Any]] = []
    seen_devices: set[tuple[str, str | int | None]] = set()
    for result in results:
        if not result.ok or result.device_info is None:
            continue
        device_key = (result.port, result.address)
        if device_key in seen_devices:
            continue
        seen_devices.add(device_key)
        info = result.device_info
        rows.append(
            {
                "adapter": ADAPTER_ID,
                "port": result.port,
                "address": result.address,
                "baudrate": result.baudrate,
                "protocol": result.protocol.value if result.protocol is not None else None,
                "model": info.part_number.raw or None,
                "firmware": str(info.firmware_id),
                "hardware": str(info.hardware_id),
                "family": info.family.value,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Setup-editor descriptor (). Lives next to the adapter so the
# descriptor and adapter cannot drift; the bottom-of-file register() call
# adds this to capa.devices.registry.ADAPTERS at import time.
# ---------------------------------------------------------------------------


def _build_descriptor() -> AdapterDescriptor:
    # Local imports keep registry.py / _templates.py off the hot import
    # path during runtime adapter use — only Setup / CLI surfaces hit this.
    from capa.devices._templates import WATLOW_HEATER_PV, WATLOW_HEATER_SETPOINT  # noqa: PLC0415
    from capa.devices.adapter import Capability  # noqa: PLC0415
    from capa.devices.registry import AdapterDescriptor  # noqa: PLC0415

    return AdapterDescriptor(
        id="capa.devices.watlow",
        label="Watlow PM-series controller",
        family="watlow",
        adapter_factory=WatlowAdapter,
        params_model=WatlowAdapterParams,
        supported_binding_sources=("watlow_parameter",),
        default_params={"protocol": "stdbus", "rate_hz": 1.0},
        channel_templates=(WATLOW_HEATER_PV, WATLOW_HEATER_SETPOINT),
        discoverable=True,
        handshake_available=True,
        capabilities=frozenset(
            {
                Capability.HAS_SETPOINT,
                Capability.HAS_RAMP,
                Capability.READS_PROCESS_VAR,
                Capability.HAS_PARAMETER_CONFIG,
            }
        ),
    )


DESCRIPTOR = _build_descriptor()

from capa.devices.registry import register as _register  # noqa: E402

_register(DESCRIPTOR)
