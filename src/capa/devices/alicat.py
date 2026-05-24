"""Real :class:`AlicatAdapter` — wraps an :class:`alicatlib.devices.base.Device`.

Architecture:

* ``open`` opens the serial transport via :func:`alicatlib.open_device` and runs
  the library's identification + capability probes. The cached
  :class:`alicatlib.DeviceInfo` lands in :class:`DeviceSnapshot` for the
  manifest's equipment block.
* ``start`` captures the run :class:`RunClock` and arms the streaming pipeline.
* ``stream`` drives :func:`alicatlib.streaming.record` over a single-device
  :class:`PollSource` shim. Every emitted :class:`alicatlib.Sample` becomes one
  :class:`SourceRecord` (preserving the wide-row library-native frame via
  :func:`alicatlib.sinks.sample_to_row`) plus zero or more
  :class:`ChannelSample`\\ s mapped from the configured
  :class:`AlicatFrameField` bindings.
* ``snapshot`` returns a :class:`DeviceSnapshot` with cached identity plus
  live health fields (auto-reconnect counter, last-sample age,
  :class:`DeviceHealth` pill).
* ``command`` enforces the authorization gate and dispatches
  :meth:`Device.setpoint`, :meth:`Device.gas`, :meth:`Device.tare_flow` /
  :meth:`Device.tare_absolute_pressure` / :meth:`Device.tare_gauge_pressure`.

For tests, an opt-in ``device_factory`` kwarg lets the adapter run against an
in-process stub without touching a serial port.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal

import alicatlib
from alicatlib import (
    AlicatError,
    OverflowPolicy,
    PollSourceAdapter,
    sample_to_row,
)
from alicatlib.devices.base import Device as AlicatDevice
from alicatlib.devices.flow_controller import FlowController
from alicatlib.devices.models import DeviceInfo, StpNtpMode, TimeUnit, TotalizerId
from alicatlib.devices.pressure_controller import PressureController
from alicatlib.streaming.recorder import record as alicat_record
from alicatlib.transport.base import SerialSettings
from pydantic import BaseModel, ConfigDict, Field

from capa.channels.spec import AlicatFrameField, ChannelSpec
from capa.core.clock import RunClock
from capa.core.errors import AdapterError
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
    DeviceHealth,
    DeviceSnapshot,
    SourceRecord,
)
from capa.devices.runtime_state import AdapterRuntimeState

if TYPE_CHECKING:
    from capa.devices.registry import AdapterDescriptor

ADAPTER_ID: Final[str] = "alicat"


# ---------------------------------------------------------------------------
# Adapter params (Pydantic) — what shows up under ``[devices.params]`` in TOML.
# ---------------------------------------------------------------------------


class AlicatAdapterParams(BaseModel):
    """Per-device adapter configuration for a real Alicat device.

    adapter-specific knobs live under ``DeviceConfig.params`` and are
    parsed by the adapter at construction time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    port: str
    """Serial-port path: ``/dev/ttyUSB0`` (Linux), ``COM3`` (Windows)."""

    unit_id: str = "A"
    """Bus-level single-letter unit id. Multi-drop RS-485 buses use distinct
    letters per device."""

    baudrate: int = 19200
    """Serial baud rate. Most Alicat shipments default to 19200; some labs
    reconfigure to 38400 — set explicitly when you've changed it on the device."""

    timeout_s: float = Field(gt=0, default=0.5)
    """Per-call command timeout passed through to :func:`alicatlib.open_device`."""

    rate_hz: float = Field(gt=0, le=50.0, default=2.0)
    """Polling cadence. Production runs sit at 1–10 Hz: serial round-trips
    bound the achievable rate. The 50 Hz upper bound exists only to catch
    typos."""

    snapshot_period_s: float = Field(gt=0, default=30.0)
    """Cadence of :class:`DeviceSnapshot` emissions during a run."""

    auto_reconnect: bool = True
    """When ``True``, transient :class:`AlicatConnectionError`\\ s do not
    terminate the stream — they are logged and the adapter retries on the
    next tick."""

    overflow: Literal["block", "drop_newest"] = "block"
    """Recorder overflow policy. ``BLOCK`` matches producers
    block on a slow durable sink rather than silently drop."""

    def to_serial_settings(self) -> SerialSettings:
        """Build the :class:`SerialSettings` alicatlib expects."""
        return SerialSettings(port=self.port, baudrate=self.baudrate)

    def overflow_policy(self) -> OverflowPolicy:
        return OverflowPolicy.BLOCK if self.overflow == "block" else OverflowPolicy.DROP_NEWEST


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


DeviceFactory = Callable[[], Awaitable[AlicatDevice]]
"""Test seam: a factory returning an open :class:`AlicatDevice`. The adapter
calls it at :meth:`AlicatAdapter.open` time. Production code leaves this
``None`` and the adapter calls :func:`alicatlib.open_device` itself."""


class AlicatAdapter:
    """Real Alicat adapter (single device per instance).

    Two construction shapes:

    * ``AlicatAdapter(name=..., **params_kwargs)`` — the materialization
      path forwards ``DeviceConfig.params`` as kwargs. They are parsed
      into an :class:`AlicatAdapterParams`.
    * ``AlicatAdapter(name=..., params=AlicatAdapterParams(...))`` — for
      programmatic construction in tests.

    Both shapes accept an optional ``device_factory`` kwarg as a test seam.

    File layout:

    * **Runtime state** — ``__init__``, ``configure_channels``, lifecycle
      (``open``/``close``/``start``/``stop``), ``snapshot``, ``stream``,
      ``watchdog_state`` / ``_snapshot_due`` / ``_compute_health`` /
      ``_snapshot_fields``. Candidates for the shared
      ``AdapterRuntimeState`` helper.
    * **Command surface** — ``command`` (authorization gate),
      ``_dispatch_command``, typed wrappers, and the read-only
      ``read_gas_list``.
    * **Vendor protocol** — alicatlib-specific code: device construction,
      sample → ``SourceRecord`` conversion, channel routing.
    """

    __slots__ = (
        "_channels",
        "_device",
        "_device_factory",
        "_device_info",
        "_state",
        "capabilities",
        "name",
        "params",
    )

    name: str
    params: AlicatAdapterParams
    capabilities: frozenset[Capability]

    def __init__(
        self,
        *,
        name: str,
        params: AlicatAdapterParams | None = None,
        device_factory: DeviceFactory | None = None,
        **params_kwargs: Any,
    ) -> None:
        if params is not None and params_kwargs:
            raise TypeError("AlicatAdapter accepts either `params=` or per-field kwargs, not both")
        if params is None:
            params = AlicatAdapterParams.model_validate(params_kwargs)
        self.name = name
        self.params = params
        # Capabilities are the union of default-on flags for an
        # Alicat (gas-selectable MFC family) plus the auto-reconnect badge.
        # The actual "is this a controller vs. a meter" gate lives in the
        # library's own DeviceInfo.capabilities — capa's flag set is a UI
        # gate, not a re-implementation of the library's probe.
        flags: set[Capability] = {
            Capability.HAS_TARE,
            Capability.HAS_GAS_SELECT,
            Capability.READS_PROCESS_VAR,
            Capability.HAS_PARAMETER_CONFIG,
            Capability.HAS_DISPLAY_CONTROL,
            Capability.HAS_TOTALIZER,
        }
        if params.auto_reconnect:
            flags.add(Capability.SUPPORTS_AUTO_RECONNECT)
        self.capabilities = frozenset(flags)
        self._device_factory: DeviceFactory | None = device_factory
        self._device: AlicatDevice | None = None
        self._device_info: DeviceInfo | None = None
        self._channels: list[ChannelSpec] = []
        self._state = AdapterRuntimeState()

    # =====================================================================
    # SECTION 1 — Runtime state: lifecycle, wiring, snapshot/stream/health
    # =====================================================================

    # ------------------------------------------------------------------ wiring

    def configure_channels(self, specs: list[ChannelSpec]) -> None:
        """Bind the adapter to the channels declared in the experiment config.

        Filters to :class:`AlicatFrameField`-bound specs whose ``device`` name
        matches ours; same shape the sim uses so the routing logic is identical.
        """
        self._channels = channels_for_device(
            specs, device=self.name, binding_source="alicat_frame_field"
        )

    @property
    def expected_emission_rate_hz(self) -> float:
        # One SourceRecord + one ChannelSample per bound channel per poll.
        return self.params.rate_hz * (1 + len(self._channels))

    @property
    def resource_id(self) -> str:
        return serial_resource_id(self.params.port)

    def update_capabilities_from_device(self, device: AlicatDevice) -> None:
        """Refine :attr:`capabilities` from the library's probed
        :class:`alicatlib.DeviceInfo`.

        Adds :class:`Capability.HAS_SETPOINT` and
        :class:`Capability.HAS_VALVE_HOLD` when the device is a controller
        (the ``setpoint`` method and the valve-hold mixin only exist on the
        controller subclasses). Called from :meth:`open` after identification.
        """
        flags = set(self.capabilities)
        if isinstance(device, FlowController | PressureController):
            flags.add(Capability.HAS_SETPOINT)
            flags.add(Capability.HAS_VALVE_HOLD)
        self.capabilities = frozenset(flags)

    @property
    def device_info(self) -> DeviceInfo | None:
        """The cached :class:`alicatlib.DeviceInfo` from :meth:`open`."""
        return self._device_info

    # ------------------------------------------------------------------ lifecycle

    async def open(self) -> None:
        """Open the underlying transport and identify the device.

        Idempotent: a second call on an already-open adapter is a no-op.
        """
        if self._state.lifecycle.state in ("open", "running"):
            return
        try:
            self._device = await self._build_device()
        except AlicatError as exc:
            await self._safe_close_device()
            raise AdapterError(
                f"alicat {self.name!r} open failed: {exc}", device=self.name
            ) from exc
        self._device_info = self._device.info
        self.update_capabilities_from_device(self._device)
        self._state.lifecycle.open()

    async def close(self) -> None:
        """Release the bus / handle. Idempotent."""
        if self._state.lifecycle.state == "closed":
            return
        await self._safe_close_device()
        self._device = None
        self._state.lifecycle.close()

    async def start(self, ctx: AdapterStartContext) -> None:
        """Capture the :class:`RunClock` anchor and arm the streaming loop."""
        self._state.on_start(ctx.clock)

    async def stop(self) -> None:
        """Request the streaming loop to exit cleanly. Idempotent."""
        self._state.request_stop()

    async def snapshot(self) -> DeviceSnapshot:
        """Build a :class:`DeviceSnapshot` from the library snapshot + live health.

        Reads ``device.snapshot()`` (I/O-free) for cached identity and
        the session counters, and projects into capa's emission shape.
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

    # ------------------------------------------------------------------ stream

    async def stream(self) -> AsyncIterator[DeviceEmission]:
        """Yield :class:`DeviceEmission`\\ s while sampling is active.

        Drives :func:`alicatlib.streaming.record` against a single-device
        :class:`alicatlib.PollSourceAdapter`. Every successful poll yields one
        :class:`SourceRecord` plus one :class:`ChannelSample` per matching
        :class:`AlicatFrameField` binding. Errored ticks (transient comm
        failures under ``auto_reconnect=True``) increment the degradation
        counter and surface in the next :class:`DeviceSnapshot`.
        """
        if self._device is None:
            raise AdapterError(
                f"alicat {self.name!r} stream() requires open() first",
                device=self.name,
            )
        if self._state.clock is None:
            raise AdapterError(
                f"alicat {self.name!r} stream() requires start() first",
                device=self.name,
            )

        # Initial snapshot so the manifest's equipment block has something
        # to show before the first poll lands.
        snap = await self.snapshot()
        self._state.last_snapshot_t_mono_ns = snap.t_mono_ns
        yield snap

        source = PollSourceAdapter(self.name, self._device)

        try:
            async with alicat_record(
                source,
                rate_hz=self.params.rate_hz,
                overflow=self.params.overflow_policy(),
                buffer_size=64,
            ) as recording:
                async for batch in recording.stream:
                    if self._state.stop_requested:
                        break
                    sample = batch.get(self.name)
                    if sample is None:
                        # Errored tick — recorder already logged at WARN and
                        # bumped ``device.session.recoverable_error_count``.
                        if not self.params.auto_reconnect:
                            raise AdapterError(
                                f"alicat {self.name!r} poll failed and auto_reconnect is disabled",
                                device=self.name,
                            )
                        continue
                    record = self._record_for(sample)
                    yield record
                    self._state.last_sample.mark(record.t_mono_ns)
                    for cs in self._channel_samples_for(sample, record.record_id):
                        yield cs
                    if self._state.snapshot_due(period_s=self.params.snapshot_period_s):
                        snap = await self.snapshot()
                        self._state.last_snapshot_t_mono_ns = snap.t_mono_ns
                        yield snap
        except* AlicatError as eg:
            first = next(iter(eg.exceptions))
            raise AdapterError(
                f"alicat {self.name!r} stream failed: {first}", device=self.name
            ) from first

    # ---------------------------------------- silence state / snapshot / health

    def watchdog_state(self) -> WatchdogState:
        """Return a compact silence-state view for tests and future policy work."""
        return self._state.watchdog(device=self.name, rate_hz=self.params.rate_hz)

    def _compute_health(self, *, clock: RunClock) -> DeviceHealth:
        """Derive the :class:`DeviceHealth` pill from adapter state.

        Mirrors the session's ``recoverable_error_count`` into the runtime
        state so the shared
        :meth:`AdapterRuntimeState.compute_health` logic still applies.
        ``down`` if the lifecycle reports closed.
        ``degraded`` when streaming-mode idle-window transients have fired,
        *or* the last sample is older than 3× the polling period.
        ``ok`` otherwise.
        """
        if self._device is not None:
            self._state.recoverable_error_count = self._device.session.recoverable_error_count
        return self._state.compute_health(clock=clock, rate_hz=self.params.rate_hz)

    async def _snapshot_fields(self) -> dict[str, float | int | str | bool | None]:
        lib_snap = await self._device.snapshot() if self._device is not None else None
        info = self._device_info
        recoverable = lib_snap.recoverable_error_count if lib_snap is not None else 0
        out: dict[str, float | int | str | bool | None] = {
            "unit_id": self.params.unit_id,
            "rate_hz": self.params.rate_hz,
            "channel_count": len(self._channels),
            "state": self._state.lifecycle.state,
            "recoverable_errors": recoverable,
        }
        if info is not None:
            out["model"] = info.model
            out["serial"] = info.serial
            out["firmware"] = str(info.firmware)
            out["kind"] = info.kind.value
            out["media"] = str(info.media)
        return out

    # =====================================================================
    # SECTION 2 — Command surface: authorization gate + typed wrappers
    # =====================================================================

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        """Issue a generic command. Authorization gate first, then dispatch."""
        clock = self._state.clock or RunClock.now()
        rejection = reject_unless_authorized(
            cmd, adapter_id=ADAPTER_ID, device_name=self.name, clock=clock
        )
        if rejection is not None:
            return rejection
        if self._device is None:
            return make_not_open_result(adapter_id=ADAPTER_ID, device_name=self.name, clock=clock)

        try:
            detail = await self._dispatch_command(cmd)
        except AlicatError as exc:
            raise AdapterError(
                f"alicat {self.name!r} command {cmd.kind!r} failed: {exc}",
                device=self.name,
            ) from exc

        return make_accepted_result(detail=detail, clock=clock)

    async def _dispatch_command(self, cmd: DeviceCommand) -> str:
        """Dispatch a generic :class:`DeviceCommand` to the right typed call.

        Recognized ``cmd.kind`` values:

        Setpoint & gas:

        * ``"set_setpoint"`` / ``"set_flow_setpoint"`` — payload
          ``{"value": float, "unit": str | None}``. Controller-only.
        * ``"set_gas"`` — payload ``{"gas": str | int, "save": bool = False}``.

        Tares:

        * ``"tare"`` / ``"tare_flow"`` — no payload.
        * ``"tare_absolute_pressure"`` / ``"tare_gauge_pressure"`` — no payload.

        Engineering / reference config (V10 10v05+):

        * ``"set_units"`` — payload
          ``{"statistic": str|int, "unit": str|int,
             "apply_to_group": bool = False,
             "override_special_rules": bool = False}``.
        * ``"set_zero_band"`` — payload ``{"zero_band": float}``.
        * ``"set_stp_pressure"`` — payload
          ``{"mode": "S"|"N", "pressure": float, "unit_code": int | None}``.
        * ``"set_stp_temperature"`` — payload
          ``{"mode": "S"|"N", "temperature": float, "unit_code": int | None}``.

        Controller config:

        * ``"set_setpoint_source"`` — payload ``{"mode": "S"|"A"|"U", "save": bool = False}``.
        * ``"set_loop_variable"`` — payload ``{"variable": str | int}``.
        * ``"set_ramp_rate"`` — payload ``{"max_ramp": float, "time_unit": int | str}``.
        * ``"set_deadband"`` — payload ``{"deadband": float, "save": bool = False}``.
        * ``"set_auto_tare"`` — payload ``{"enable": bool, "delay_s": float | None}``.
        * ``"set_power_up_tare"`` — payload ``{"enable": bool}``.

        Display:

        * ``"blink_display"`` — payload ``{"duration_s": int | None}``.
        * ``"lock_display"`` / ``"unlock_display"`` — no payload.

        Valve hold (controller-only):

        * ``"hold_valves"`` — no payload.
        * ``"hold_valves_closed"`` — no payload (DESTRUCTIVE; CAPA's auth gate
          covers the library's confirm gate).
        * ``"cancel_valve_hold"`` — no payload.

        Totalizer (flow devices only):

        * ``"totalizer_reset"`` — payload ``{"totalizer": int = 1}`` (DESTRUCTIVE).
        * ``"totalizer_reset_peak"`` — payload ``{"totalizer": int = 1}`` (DESTRUCTIVE).
        * ``"totalizer_save"`` — payload ``{"enable": bool | None, "save": bool | None}``.
        """
        assert self._device is not None
        kind = cmd.kind
        if kind in ("set_setpoint", "set_flow_setpoint"):
            controller = self._require_controller(kind)
            value = float(cmd.payload["value"])
            unit_arg = cmd.payload.get("unit")
            state = await controller.setpoint(value=value, unit=unit_arg)
            return f"set_setpoint value={value} -> {state.current!r}"
        if kind == "set_gas":
            gas = cmd.payload["gas"]
            gas_save = bool(cmd.payload.get("save", False))
            await self._device.gas(gas, save=gas_save)
            return f"set_gas gas={gas!r} save={gas_save}"
        if kind in ("tare", "tare_flow"):
            await self._device.tare_flow()
            return "tare_flow"
        if kind == "tare_absolute_pressure":
            await self._device.tare_absolute_pressure()
            return "tare_absolute_pressure"
        if kind == "tare_gauge_pressure":
            await self._device.tare_gauge_pressure()
            return "tare_gauge_pressure"

        # ------------------ engineering / reference config

        if kind == "set_units":
            statistic = cmd.payload["statistic"]
            unit = cmd.payload["unit"]
            apply_to_group = bool(cmd.payload.get("apply_to_group", False))
            override = bool(cmd.payload.get("override_special_rules", False))
            await self._device.engineering_units(
                statistic,
                unit,
                apply_to_group=apply_to_group,
                override_special_rules=override,
            )
            return f"set_units stat={statistic!r} unit={unit!r}"
        if kind == "set_zero_band":
            zero_band = float(cmd.payload["zero_band"])
            await self._device.zero_band(zero_band)
            return f"set_zero_band={zero_band}"
        if kind == "set_stp_pressure":
            mode = StpNtpMode(cmd.payload["mode"])
            pressure = float(cmd.payload["pressure"])
            unit_code = cmd.payload.get("unit_code")
            await self._device.stp_ntp_pressure(mode, pressure=pressure, unit_code=unit_code)
            return f"set_stp_pressure mode={mode.value} pressure={pressure}"
        if kind == "set_stp_temperature":
            mode = StpNtpMode(cmd.payload["mode"])
            temperature = float(cmd.payload["temperature"])
            unit_code = cmd.payload.get("unit_code")
            await self._device.stp_ntp_temperature(
                mode, temperature=temperature, unit_code=unit_code
            )
            return f"set_stp_temperature mode={mode.value} temperature={temperature}"

        # ------------------ controller config

        if kind == "set_setpoint_source":
            controller = self._require_controller(kind)
            sp_mode = cmd.payload["mode"]
            sp_save: bool | None = cmd.payload.get("save")
            result = await controller.setpoint_source(sp_mode, save=sp_save)
            return f"set_setpoint_source mode={result!r}"
        if kind == "set_loop_variable":
            controller = self._require_controller(kind)
            variable = cmd.payload["variable"]
            await controller.loop_control_variable(variable)
            return f"set_loop_variable variable={variable!r}"
        if kind == "set_ramp_rate":
            controller = self._require_controller(kind)
            max_ramp = float(cmd.payload["max_ramp"])
            time_unit_arg = cmd.payload["time_unit"]
            time_unit = (
                TimeUnit[time_unit_arg.upper()]
                if isinstance(time_unit_arg, str)
                else TimeUnit(int(time_unit_arg))
            )
            await controller.ramp_rate(max_ramp, time_unit=time_unit)
            return f"set_ramp_rate max={max_ramp} unit={time_unit.name}"
        if kind == "set_deadband":
            controller = self._require_controller(kind)
            deadband = float(cmd.payload["deadband"])
            db_save: bool | None = cmd.payload.get("save")
            await controller.deadband_limit(deadband, save=db_save)
            return f"set_deadband={deadband}"
        if kind == "set_auto_tare":
            controller = self._require_controller(kind)
            enable = bool(cmd.payload["enable"])
            delay_s = cmd.payload.get("delay_s")
            await controller.auto_tare(enable, delay_s=delay_s)
            return f"set_auto_tare enable={enable} delay_s={delay_s}"
        if kind == "set_power_up_tare":
            enable = bool(cmd.payload["enable"])
            await self._device.power_up_tare(enable)
            return f"set_power_up_tare enable={enable}"

        # ------------------ display

        if kind == "blink_display":
            duration_s = cmd.payload.get("duration_s")
            await self._device.blink_display(duration_s)
            return f"blink_display duration_s={duration_s}"
        if kind == "lock_display":
            await self._device.lock_display()
            return "lock_display"
        if kind == "unlock_display":
            await self._device.unlock_display()
            return "unlock_display"

        # ------------------ valve hold (controller-only)

        if kind == "hold_valves":
            controller = self._require_controller(kind)
            await controller.hold_valves()
            return "hold_valves"
        if kind == "hold_valves_closed":
            controller = self._require_controller(kind)
            # CAPA's authorization gate already accepted; pass library confirm.
            await controller.hold_valves_closed(confirm=True)
            return "hold_valves_closed"
        if kind == "cancel_valve_hold":
            controller = self._require_controller(kind)
            await controller.cancel_valve_hold()
            return "cancel_valve_hold"

        # ------------------ totalizer

        if kind == "totalizer_reset":
            totalizer = TotalizerId(int(cmd.payload.get("totalizer", 1)))
            await self._device.totalizer_reset(totalizer, confirm=True)
            return f"totalizer_reset totalizer={totalizer.value}"
        if kind == "totalizer_reset_peak":
            totalizer = TotalizerId(int(cmd.payload.get("totalizer", 1)))
            await self._device.totalizer_reset_peak(totalizer, confirm=True)
            return f"totalizer_reset_peak totalizer={totalizer.value}"
        if kind == "totalizer_save":
            tot_enable: bool | None = cmd.payload.get("enable")
            tot_save: bool | None = cmd.payload.get("save")
            await self._device.totalizer_save(tot_enable, save=tot_save)
            return f"totalizer_save enable={tot_enable} save={tot_save}"

        raise AdapterError(
            f"alicat {self.name!r}: unknown command kind {kind!r}",
            device=self.name,
        )

    def _require_controller(self, kind: str) -> FlowController | PressureController:
        """Reject controller-only ``kind`` on a meter device, return the
        narrowed device handle.

        The ``isinstance`` check runs *here* (not at dispatch sites) so mypy
        can see the narrowed return type — a bare ``None`` return would force
        every caller to ``cast`` or repeat the check.
        """
        if not isinstance(self._device, FlowController | PressureController):
            raise AdapterError(
                f"alicat {self.name!r}: command {kind!r} requires a controller, "
                f"this device is a meter",
                device=self.name,
            )
        return self._device

    # Typed helpers — IDE-friendly parallels to ``.command()``.
    async def set_setpoint(
        self,
        value: float,
        *,
        unit: str | None = None,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        return await self.command(
            DeviceCommand(
                kind="set_setpoint",
                target="setpoint",
                payload={"value": value, "unit": unit},
                issued_by=issued_by,
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )

    async def set_gas(
        self,
        gas: str,
        *,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        return await self.command(
            DeviceCommand(
                kind="set_gas",
                target="gas",
                payload={"gas": gas},
                issued_by=issued_by,
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )

    async def tare_flow(
        self,
        *,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        return await self.command(
            DeviceCommand(
                kind="tare_flow",
                payload={},
                issued_by=issued_by,
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )

    async def set_units(
        self,
        statistic: str | int,
        unit: str | int,
        *,
        issued_by: str,
        apply_to_group: bool = False,
        override_special_rules: bool = False,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Set the engineering unit for one statistic (``DCU``)."""
        return await self.command(
            DeviceCommand(
                kind="set_units",
                target=str(statistic),
                payload={
                    "statistic": statistic,
                    "unit": unit,
                    "apply_to_group": apply_to_group,
                    "override_special_rules": override_special_rules,
                },
                issued_by=issued_by,
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )

    async def hold_valves(
        self,
        *,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Hold valve(s) at their current drive position (``HP``)."""
        return await self.command(
            DeviceCommand(
                kind="hold_valves",
                payload={},
                issued_by=issued_by,
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )

    async def hold_valves_closed(
        self,
        *,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Force valve(s) closed immediately (``HC``). Destructive — interrupts
        closed-loop control. Best wired to a discrete UI confirmation rather
        than a method step."""
        return await self.command(
            DeviceCommand(
                kind="hold_valves_closed",
                payload={},
                issued_by=issued_by,
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )

    async def cancel_valve_hold(
        self,
        *,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Cancel any active valve hold and resume closed-loop control."""
        return await self.command(
            DeviceCommand(
                kind="cancel_valve_hold",
                payload={},
                issued_by=issued_by,
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )

    async def totalizer_reset(
        self,
        *,
        issued_by: str,
        totalizer: int = 1,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Reset a totalizer's accumulated count. Destructive."""
        return await self.command(
            DeviceCommand(
                kind="totalizer_reset",
                payload={"totalizer": totalizer},
                issued_by=issued_by,
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )

    async def lock_display(
        self,
        *,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Lock the front-panel display (``L``)."""
        return await self.command(
            DeviceCommand(
                kind="lock_display",
                payload={},
                issued_by=issued_by,
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )

    async def unlock_display(
        self,
        *,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Unlock the front-panel display (``U``). Always callable as a
        safety escape, even on devices that don't advertise
        :class:`Capability.HAS_DISPLAY_CONTROL`."""
        return await self.command(
            DeviceCommand(
                kind="unlock_display",
                payload={},
                issued_by=issued_by,
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )

    # ------------------------------------------------------------------ read-only helpers
    #
    # No authorization gate — these are pure reads. The manual control panel
    # uses these to render the current device state next to its set / write
    # buttons.

    async def read_gas_list(self) -> Mapping[int, str]:
        """List the gases the device knows about (``GL``).

        Returns a mapping of wire code → label. Custom mixtures occupy slots
        236..255 with operator-defined labels.
        """
        if self._device is None:
            raise AdapterError(
                f"alicat {self.name!r} read_gas_list() requires open() first",
                device=self.name,
            )
        try:
            return await self._device.gas_list()
        except AlicatError as exc:
            raise AdapterError(
                f"alicat {self.name!r} read_gas_list failed: {exc}", device=self.name
            ) from exc

    # =====================================================================
    # SECTION 3 — Vendor protocol: alicatlib-specific device / sample / channels
    # =====================================================================

    async def _build_device(self) -> AlicatDevice:
        """Construct the underlying :class:`AlicatDevice`.

        Default path: :func:`alicatlib.open_device` with the configured port.
        Test path: the injected ``device_factory`` returns a stub.
        """
        if self._device_factory is not None:
            return await self._device_factory()
        return await alicatlib.open_device(
            self.params.port,
            unit_id=self.params.unit_id,
            serial=self.params.to_serial_settings(),
            timeout=self.params.timeout_s,
        )

    async def _safe_close_device(self) -> None:
        if self._device is None:
            return
        try:
            await self._device.close()
        except AlicatError:
            return

    def _record_for(self, sample: Any) -> SourceRecord:
        """Convert an alicatlib :class:`Sample` into a wide-row :class:`SourceRecord`.

        Uses the library's own :func:`alicatlib.sample_to_row` helper so
        the row schema matches what an offline ``alicatlib`` recorder would
        produce — important for ``device_records/alicat.parquet`` parity
        across sim and real bundles.
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
            shape="wide_row",
            t_mono_ns=t_mono_ns,
            t_utc=sample.t_utc,
            row=row,
            metadata={"unit_id": sample.unit_id},
        )

    def _channel_samples_for(self, sample: Any, record_id: str) -> list[DeviceEmission]:
        """Map ``sample`` against the configured :class:`AlicatFrameField` bindings."""
        clock = self._state.clock
        assert clock is not None
        t_mono_ns = sample.t_mono_ns - clock.started_mono_ns
        values = sample.reading.as_dict()
        emissions: list[DeviceEmission] = []
        for spec in self._channels:
            binding = spec.source
            assert isinstance(binding, AlicatFrameField)
            raw_value = values.get(binding.field)
            if raw_value is None or not isinstance(raw_value, int | float):
                # Field absent or non-numeric — preserve native row, drop
                # the derived sample. Status is captured in the row's
                # ``status`` column.
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


# ---------------------------------------------------------------------------
# CLI handshake hook (``capa validate --strict``)
# ---------------------------------------------------------------------------


async def handshake(params: dict[str, Any]) -> str:
    """Read-only open + identify + close. Used by ``capa validate --strict``.

    Returns a one-line summary of the device identity. Raises
    :class:`AdapterError` on any failure so the CLI can surface the wiring
    problem before the operator arms a run.
    """
    parsed = AlicatAdapterParams.model_validate(params)
    try:
        device = await alicatlib.open_device(
            parsed.port,
            unit_id=parsed.unit_id,
            serial=parsed.to_serial_settings(),
            timeout=parsed.timeout_s,
        )
        try:
            info = device.info
        finally:
            await device.close()
    except AlicatError as exc:
        raise AdapterError(f"alicat handshake failed at {parsed.port}: {exc}") from exc
    return (
        f"alicat unit_id={info.unit_id} model={info.model} "
        f"serial={info.serial or '?'} fw={info.firmware} "
        f"kind={info.kind.value}"
    )


# ---------------------------------------------------------------------------
# Discovery hook (``capa devices discover``)
# ---------------------------------------------------------------------------


async def discover(
    *,
    ports: list[str] | None = None,
    unit_ids: tuple[str, ...] = ("A",),
    baudrates: tuple[int, ...] | None = None,
) -> list[dict[str, Any]]:
    """Probe the local serial buses for Alicat devices.

    Wraps :func:`alicatlib.find_devices`. Returns a list of dicts (rather than
    library objects) so the CLI can render them uniformly across adapter
    families. ``ports=None`` enumerates every visible serial port.
    """
    bauds = baudrates if baudrates is not None else alicatlib.DEFAULT_DISCOVERY_BAUDRATES
    results = await alicatlib.find_devices(
        ports=ports,
        unit_ids=unit_ids,
        baudrates=bauds,
    )
    out: list[dict[str, Any]] = []
    for r in results:
        if not r.ok or r.device_info is None:
            continue
        out.append(
            {
                "adapter": ADAPTER_ID,
                "port": r.port,
                "unit_id": r.address,
                "baudrate": r.baudrate,
                "model": r.device_info.model,
                "serial": r.device_info.serial,
                "firmware": str(r.device_info.firmware),
                "kind": r.device_info.kind.value,
            }
        )
    return out


__all__ = [
    "ADAPTER_ID",
    "DESCRIPTOR",
    "AlicatAdapter",
    "AlicatAdapterParams",
    "discover",
    "handshake",
]


def _build_descriptor() -> AdapterDescriptor:
    from capa.devices._templates import ALICAT_PURGE_FLOW  # noqa: PLC0415
    from capa.devices.adapter import Capability  # noqa: PLC0415
    from capa.devices.registry import AdapterDescriptor  # noqa: PLC0415

    return AdapterDescriptor(
        id="capa.devices.alicat",
        label="Alicat MFC / MFM",
        family="alicat",
        adapter_factory=AlicatAdapter,
        params_model=AlicatAdapterParams,
        supported_binding_sources=("alicat_frame_field",),
        default_params={"rate_hz": 2.0},
        channel_templates=(ALICAT_PURGE_FLOW,),
        discoverable=True,
        handshake_available=True,
        capabilities=frozenset(
            {
                Capability.HAS_TARE,
                Capability.HAS_GAS_SELECT,
                Capability.READS_PROCESS_VAR,
                Capability.HAS_PARAMETER_CONFIG,
                Capability.HAS_DISPLAY_CONTROL,
                Capability.HAS_TOTALIZER,
                Capability.HAS_SETPOINT,
                Capability.HAS_VALVE_HOLD,
            }
        ),
    )


DESCRIPTOR = _build_descriptor()

from capa.devices.registry import register as _register  # noqa: E402

_register(DESCRIPTOR)
