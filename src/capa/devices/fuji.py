"""Real :class:`FujiAdapter` — wraps a :class:`fujilib.Analyzer`.

A Fuji ZP-series NDIR gas analyzer (ZPA and its siblings; also sold as the CAI
ZPA) reports up to twelve display channels over Modbus RTU, typically CO2, CO
and O2.

Architecture:

* ``open`` opens the serial port via :func:`fujilib.open_device`, which
  identifies the analyzer, and reads the settings snapshot
  (:meth:`fujilib.Analyzer.read_metadata`): ranges, response times, hold and
  calibration gases. Which gas each channel carries is asserted by the
  ``channel_map`` parameter; the analyzer's type code only suggests it.
* ``start`` captures the run :class:`RunClock` and resets the per-run state.
* ``stream`` drives :func:`fujilib.record` over a single-analyzer
  :class:`fujilib.PollSourceAdapter`. Every poll becomes one
  :class:`SourceRecord` (``shape="wide_row"``, the row from
  :func:`fujilib.sample_to_row`) plus one :class:`ChannelSample` per bound
  :class:`FujiChannel`. A failed poll yields the record, with its error
  columns filled, and no channel samples. The reading's validity state
  (``ok``, ``hold``, ``calibrating``, ``settling``, ...) travels on the channel
  sample's :attr:`status`.
* A reading whose unit is not the unit its channel declares quarantines that
  channel for the rest of the run and emits one :class:`DeviceEvent`, so a
  range change from vol% to ppm cannot produce silently wrong values.
* Changes are reported as :class:`DeviceEvent`\\ s: the connection lost and
  restored, an instrument error, output hold, and a zero or span calibration
  made at the analyzer's front panel.
* ``snapshot`` returns a :class:`DeviceSnapshot` with the cached identity and
  settings plus live health; it does no I/O.
* ``command`` enforces the authorization gate, then a rule by fujilib's
  safety tier: the settings writes and return-to-measurement pass with either
  authorization, and everything ``DANGEROUS`` (a calibration gas, starting an
  automatic calibration) needs a person's confirmation even inside an
  authorized run. A refusal by fujilib or the analyzer is a result, not an
  exception; a write whose outcome is uncertain is a failed result and an
  error event.
* A manual zero or span is driven from the manual control panel in three
  commands (``calibration_begin``, ``calibration_commit``,
  ``calibration_cancel``) over one background task that owns the run
  (:mod:`capa.devices.fuji_calibration`). The operator switches the gas and
  names it; fujilib presses the analyzer's calibration keys, and sends the
  key that calibrates only while the reading is steady on that gas.

Readings over Modbus are the analyzer's display values: O2 arrives in steps
of 0.01 vol%, and every value has passed the analyzer's response-time filter.
The analyzer needs its warm-up time after power-on and does not say when it
is still warming up.
"""

from __future__ import annotations

import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Final, Literal

import fujilib
from fujilib import (
    Analyzer,
    AnalyzerMetadata,
    ChannelId,
    DeviceInfo,
    Frame,
    FujiAnalyzerStateError,
    FujiCapabilityError,
    FujiConfirmationRequiredError,
    FujiError,
    FujiModbusError,
    FujiModbusTimeoutError,
    FujiValidationError,
    FujiVerificationError,
    FujiWriteOutcomeUnknownError,
    Gas,
    LabelSource,
    OverflowPolicy,
    PollSourceAdapter,
    RangeInfo,
    Reading,
    ReconnectPolicy,
    RegisterValue,
    SafetyTier,
    Sample,
    sample_to_row,
    to_pint,
)
from fujilib import Capability as FujiCapability
from fujilib.devices.capability import OPTION_CAPABILITIES
from fujilib.devices.keys import CalibrationGas
from fujilib.devices.panel import (
    ManualCalibrationEvent,
    ManualCalibrationOutcome,
    ManualCalibrationPlan,
    ManualCalibrationTracker,
    PanelObservation,
    calibration_record,
)
from fujilib.devices.steadiness import SteadinessRule
from fujilib.devices.writes import WriteResult, describe
from fujilib.registry.channels import MEASURED_CHANNELS, coerce_channel_map
from fujilib.registry.enums import HoldMode, RangeIndex, RangeMethod
from fujilib.streaming.recorder import record as fuji_record
from pydantic import BaseModel, ConfigDict, Field, field_validator

from capa.channels.spec import ChannelSpec, FujiChannel
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
from capa.devices.fuji_calibration import IDLE, CalibrationRun, CalibrationStatus, plan_summary
from capa.devices.fuji_labels import (
    HOLD_MODE_NAMES,
    RANGE_METHOD_NAMES,
    channel_names,
    range_name,
    range_of,
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

ADAPTER_ID: Final[str] = "fuji"

Scalar = float | int | str | bool | None

_AUTO_CALIBRATION_OPTIONS: Final = FujiCapability.AUTO_CALIBRATION | FujiCapability.AUTO_ZERO

SETTINGS_MAX_AGE_S: Final[float] = 10.0
"""How old the cached settings may be when :meth:`FujiAdapter.read_state_snapshot`
answers: older ones are read again, so a change made at the front panel
reaches the manual card within this time. A change made through capa is
read back at once."""

# What fujilib or the analyzer declines before anything changes: a value that
# does not fit, the analyzer busy calibrating or in a menu, an option that is
# not fitted, an exception reply.
_REFUSALS: Final = (
    FujiValidationError,
    FujiAnalyzerStateError,
    FujiCapabilityError,
    FujiConfirmationRequiredError,
    FujiModbusError,
)


class _RefusedError(Exception):
    """A command the adapter declines before fujilib is asked."""


class _UncertainError(Exception):
    """A command that was sent but did not end as asked."""


# ---------------------------------------------------------------------------
# Operator-facing readback snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FujiReadback:
    """One channel's reading for the manual-control card."""

    channel: str
    gas: str
    value: float | None
    unit: str
    state: str
    """The reading's validity state: ``ok``, ``hold``, ``calibrating``, ..."""


@dataclass(frozen=True, slots=True)
class FujiRange:
    """One range of a measured channel, with its calibration gases."""

    number: int
    """1 or 2, as the analyzer numbers it."""
    unit: str
    full_scale: float
    zero_gas: float | None = None
    """The zero calibration gas, in :attr:`unit`; ``None`` if unreadable."""
    span_gas: float | None = None
    """The span calibration gas, in :attr:`unit`; ``None`` if unreadable."""

    @property
    def name(self) -> str:
        """``"0–25 vol%"``: the range by its span."""
        return range_name(self.unit, self.full_scale)


@dataclass(frozen=True, slots=True)
class FujiChannelSettings:
    """The settings of one measured channel the channel map asserts."""

    channel: str
    """``"CH1"``."""
    gas: str
    """The asserted gas, ``"co2"``."""
    name: str
    """The channel as the operator reads it, ``"CO2"``."""
    ranges: tuple[FujiRange, ...] = ()
    current_range: int | None = None
    """The range the channel measures on now."""
    range_method: str | None = None
    """``"manual"``, ``"auto"`` or ``"remote"``; ``None`` if not read."""
    response_time_s: int | None = None
    """The response time of the channel's slot; ``None`` when the channel
    map does not say which slot is the channel's."""

    def range(self, number: int | None) -> FujiRange | None:
        """Range ``number``, if the channel has it."""
        return next((r for r in self.ranges if r.number == number), None)


@dataclass(frozen=True, slots=True)
class FujiStateSnapshot:
    """One-shot readback of the operator-facing state of a Fuji analyzer.

    Built by :meth:`FujiAdapter.read_state_snapshot` on the worker loop and
    consumed by the manual-control card on the qasync loop.
    """

    calibration: CalibrationStatus = IDLE
    """The calibration under way, or the last one that ended."""
    readings: tuple[FujiReadback, ...] = ()
    """The latest reading of each established channel; empty when the
    analyzer could not be read."""
    channels: tuple[FujiChannelSettings, ...] = ()
    """The settings of each measured channel the channel map asserts, in
    channel order; empty until the settings have been read."""
    output_hold: bool | None = None
    """Whether the outputs, and the Modbus values, hold during a calibration."""
    hold_mode: str | None = None
    """What they hold: ``"last_value"`` or ``"setting"``."""


# ---------------------------------------------------------------------------
# Adapter params (Pydantic) — what shows up under ``[devices.params]`` in TOML.
# ---------------------------------------------------------------------------


class FujiAdapterParams(BaseModel):
    """Per-device adapter configuration for a real Fuji ZP-series analyzer.

    adapter-specific knobs live under ``DeviceConfig.params`` and are
    parsed by the adapter at construction time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    port: str
    """Serial-port path: ``/dev/ttyUSB0`` (Linux), ``COM8`` (Windows). The
    analyzer speaks Modbus RTU at a fixed 38400 8-N-1; one analyzer per
    port."""

    address: int = Field(ge=1, le=31, default=1)
    """Station number, as set on the analyzer's front panel."""

    channel_map: dict[str, str]
    """The gas on each analyzer channel, e.g.
    ``{CH1 = "co2", CH2 = "co", CH3 = "o2"}``. Asserted by the operator: the
    analyzer's type code only suggests the gases and can be out of date.
    Only channels named here can be bound."""

    rate_hz: float = Field(gt=0, le=5.0, default=1.0)
    """Polling cadence. One poll is two Modbus transactions and takes about
    0.12 s; the analyzer's response-time filter makes more than a few polls a
    second pointless."""

    timeout_s: float = Field(gt=0, default=0.5)
    """Per-reply timeout passed through to :func:`fujilib.open_device`."""

    snapshot_period_s: float = Field(gt=0, default=30.0)
    """Cadence of :class:`DeviceSnapshot` emissions during a run."""

    auto_reconnect: bool = True
    """When ``True``, a connection failure does not terminate the stream:
    every tick of the outage is an error row and the port is reopened on
    fujilib's back-off schedule. Readings in the 90 s after a reopen have the
    status ``settling``."""

    overflow: Literal["block", "drop_newest"] = "block"
    """Recorder overflow policy: block the poll loop on a slow consumer
    rather than silently drop."""

    options: tuple[str, ...] = ()
    """Analyzer options the operator asserts are fitted, by fujilib
    capability name, e.g. ``["auto_calibration", "auto_zero"]`` for a unit
    whose calibration gases are plumbed through its valve contacts."""

    @field_validator("channel_map")
    @classmethod
    def _check_channel_map(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError("channel_map must name the gas of at least one channel")
        try:
            resolved = coerce_channel_map({channel: gas for channel, gas in value.items()})
        except FujiValidationError as exc:
            raise ValueError(str(exc)) from exc
        return {channel.value: gas.value for channel, gas in resolved.items()}

    @field_validator("options")
    @classmethod
    def _check_options(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        names = tuple(name.strip().lower() for name in value)
        for name in names:
            member = FujiCapability.__members__.get(name.upper())
            if member is None or not member & OPTION_CAPABILITIES:
                allowed = sorted(
                    m.lower()
                    for m, flag in FujiCapability.__members__.items()
                    if flag and flag & OPTION_CAPABILITIES == flag
                )
                raise ValueError(f"unknown analyzer option {name!r}; expected one of {allowed}")
        return names

    def gases(self) -> Mapping[ChannelId, Gas]:
        """The asserted channel map as fujilib types."""
        return coerce_channel_map(self.asserted())

    def asserted(self) -> dict[ChannelId | str, Gas | str]:
        """The asserted channel map as :func:`fujilib.open_device` takes it."""
        return {channel: gas for channel, gas in self.channel_map.items()}

    def option_flags(self) -> FujiCapability:
        """The asserted options as one fujilib :class:`Capability` flag."""
        flags = FujiCapability.NONE
        for name in self.options:
            flags |= FujiCapability[name.upper()]
        return flags

    def overflow_policy(self) -> OverflowPolicy:
        """Translate the user-facing ``overflow`` string to a library :class:`OverflowPolicy`."""
        return OverflowPolicy.BLOCK if self.overflow == "block" else OverflowPolicy.DROP_NEWEST


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


AnalyzerFactory = Callable[[], Awaitable[Analyzer]]
"""Test seam: a factory returning an open, identified :class:`Analyzer`. The
adapter calls it at :meth:`FujiAdapter.open` time. Production code leaves
this ``None`` and the adapter calls :func:`fujilib.open_device` itself."""


class FujiAdapter:
    """Real Fuji ZP-series gas-analyzer adapter (single analyzer per instance).

    Two construction shapes:

    * ``FujiAdapter(name=..., **params_kwargs)`` — engine path; per-device
      params from ``DeviceConfig.params`` are forwarded as kwargs and parsed
      into :class:`FujiAdapterParams`.
    * ``FujiAdapter(name=..., params=FujiAdapterParams(...))`` — programmatic
      path used by tests.

    Both shapes accept an optional ``analyzer_factory`` kwarg as a test seam.

    File layout:

    * **Runtime state** — ``__init__``, ``configure_channels``, lifecycle,
      ``snapshot``, ``stream``, health.
    * **Command surface** — ``command`` (authorization gate, then the tier
      rule), ``_dispatch_command``, typed wrappers, and the read-only
      ``read_settings`` / ``plan_calibration``.
    * **Vendor protocol** — fujilib-specific code: analyzer construction,
      sample → ``SourceRecord`` conversion, channel routing, the unit check,
      and the change events.
    """

    __slots__ = (
        "_analyzer",
        "_analyzer_factory",
        "_calibration",
        "_calibration_interval_s",
        "_calibration_timeout_s",
        "_channels",
        "_device_info",
        "_events",
        "_hold_channels",
        "_instrument_errors",
        "_metadata",
        "_names",
        "_outage",
        "_quarantined",
        "_range_methods",
        "_settings_read_at",
        "_state",
        "_steadiness_rule",
        "_tracker",
        "capabilities",
        "name",
        "params",
    )

    name: str
    params: FujiAdapterParams
    capabilities: frozenset[Capability]

    def __init__(
        self,
        *,
        name: str,
        params: FujiAdapterParams | None = None,
        analyzer_factory: AnalyzerFactory | None = None,
        **params_kwargs: Any,
    ) -> None:
        if params is not None and params_kwargs:
            raise TypeError("FujiAdapter accepts either `params=` or per-field kwargs, not both")
        if params is None:
            params = FujiAdapterParams.model_validate(params_kwargs)
        self.name = name
        self.params = params
        flags: set[Capability] = {
            Capability.READS_PROCESS_VAR,
            Capability.HAS_PARAMETER_CONFIG,
            Capability.HAS_GAS_CALIBRATION,
        }
        if params.auto_reconnect:
            flags.add(Capability.SUPPORTS_AUTO_RECONNECT)
        if params.option_flags() & _AUTO_CALIBRATION_OPTIONS:
            # The analyzer drives its own calibration-gas valves only where
            # the operator asserts that option.
            flags.add(Capability.HAS_INTERNAL_CAL)
        self.capabilities = frozenset(flags)
        self._analyzer_factory: AnalyzerFactory | None = analyzer_factory
        self._analyzer: Analyzer | None = None
        self._device_info: DeviceInfo | None = None
        # Each channel by its gas, for what the operator reads.
        self._names: dict[str, str] = channel_names(params.channel_map)
        # The settings as last read, and when (monotonic seconds).
        self._metadata: AnalyzerMetadata | None = None
        self._range_methods: dict[ChannelId, str] = {}
        self._settings_read_at: float = -math.inf
        self._channels: list[ChannelSpec] = []
        self._state = AdapterRuntimeState()
        # Channels whose reading's unit was not the unit the channel declares:
        # no ChannelSample is derived for them until the next start().
        self._quarantined: set[str] = set()
        # DeviceEvents queued while a batch is handled, yielded after it.
        self._events: list[DeviceEvent] = []
        self._tracker = ManualCalibrationTracker()
        # Per-run edge state, so each change is reported once.
        self._outage: _Outage | None = None
        self._instrument_errors: frozenset[int] | None = None
        self._hold_channels: frozenset[str] | None = None
        # The manual calibration under way, or the last one.
        self._calibration: CalibrationRun | None = None
        # fujilib's steadiness rule and wait-step timing; ``None`` is its default
        # rule (0.5 %FS over at least 30 s, within 10 %FS of the gas).
        self._steadiness_rule: SteadinessRule | None = None
        self._calibration_interval_s = 0.5
        self._calibration_timeout_s = 900.0

    # =====================================================================
    # SECTION 1 — Runtime state: lifecycle, wiring, snapshot/stream/health
    # =====================================================================

    # ------------------------------------------------------------------ wiring

    def configure_channels(self, specs: list[ChannelSpec]) -> None:
        """Bind to the :class:`FujiChannel`-bound channels for this device."""
        self._channels = channels_for_device(specs, device=self.name, binding_source="fuji_channel")

    @property
    def expected_emission_rate_hz(self) -> float:
        """Emission rate hint for queue sizing. See :class:`~capa.devices.adapter.DeviceAdapter`."""
        # One SourceRecord + one ChannelSample per bound channel per poll.
        return self.params.rate_hz * (1 + len(self._channels))

    @property
    def resource_id(self) -> str:
        """``serial:<port>`` — one analyzer per port."""
        return serial_resource_id(self.params.port)

    @property
    def device_info(self) -> DeviceInfo | None:
        """The cached :class:`fujilib.DeviceInfo` from :meth:`open`."""
        return self._device_info

    @property
    def metadata(self) -> AnalyzerMetadata | None:
        """The analyzer's settings as last read: at :meth:`open`, at the
        start of each :meth:`stream`, after each setting written through
        capa, and by :meth:`read_state_snapshot` when they are old."""
        return self._metadata

    # ------------------------------------------------------------------ lifecycle

    async def open(self) -> None:
        """Open the port, identify the analyzer and read its settings.

        Idempotent: a second call on an already-open adapter is a no-op.
        """
        if self._state.lifecycle.state in ("open", "running"):
            return
        try:
            self._analyzer = await self._build_analyzer()
            self._device_info = self._analyzer.info
            await self._read_settings(self._analyzer)
        except FujiError as exc:
            await self._safe_close_analyzer()
            self._analyzer = None
            raise AdapterError(f"fuji {self.name!r} open failed: {exc}", device=self.name) from exc
        self._state.lifecycle.open()

    async def close(self) -> None:
        """Release the port. Idempotent."""
        if self._state.lifecycle.state == "closed":
            return
        try:
            if self._calibration is not None:
                # The run's owner task returns the panel to measurement
                # before the port goes away.
                await self._calibration.close()
        finally:
            # The port is released even when the wait above is cut short.
            await self._safe_close_analyzer()
            self._analyzer = None
            self._state.lifecycle.close()

    async def start(self, ctx: AdapterStartContext) -> None:
        """Capture the :class:`RunClock` anchor and arm the streaming loop.

        A calibration still on its wait step is cancelled: the manual
        control panel cannot reach it during a run, so it would hold the
        readings ``calibrating`` until it timed out.
        """
        self._state.on_start(ctx.clock)
        # New run, fresh state: a unit mismatch fixed between runs should not
        # stay quarantined, and every condition is reported again.
        self._quarantined.clear()
        self._events.clear()
        self._tracker = ManualCalibrationTracker()
        self._outage = None
        self._instrument_errors = None
        self._hold_channels = None
        if self._calibration is not None and self._calibration.active:
            # Its end is then the run's first event.
            _ = await self._calibration.cancel(reason="cancelled because a run started")

    async def stop(self) -> None:
        """Request the streaming loop to exit cleanly. Idempotent.

        The loop sees the request at its next poll.
        """
        self._state.request_stop()

    async def snapshot(self) -> DeviceSnapshot:
        """Build a :class:`DeviceSnapshot` from cached identity and settings
        plus live health. No I/O."""
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

        Drives :func:`fujilib.record` against a single-analyzer
        :class:`fujilib.PollSourceAdapter`. A successful poll yields one
        :class:`SourceRecord` plus one :class:`ChannelSample` per matching
        :class:`FujiChannel` binding. fujilib's recorder yields a sample for
        a failed poll too (``frame=None``): its row is emitted with the error
        columns filled and no channel sample is derived. With
        ``auto_reconnect`` the recorder rides out a connection failure and
        reopens the port; without it any failed poll ends the stream.
        """
        analyzer = self._analyzer
        if analyzer is None:
            raise AdapterError(
                f"fuji {self.name!r} stream() requires open() first", device=self.name
            )
        clock = self._state.clock
        if clock is None:
            raise AdapterError(
                f"fuji {self.name!r} stream() requires start() first", device=self.name
            )

        # Each run records the settings in force when it began.
        await self._refresh_metadata(analyzer)
        snap = await self.snapshot()
        self._state.last_snapshot_t_mono_ns = snap.t_mono_ns
        yield snap
        for event in self._label_events(clock):
            yield event

        source = PollSourceAdapter(self.name, analyzer)
        # Only a port fujilib opened by name can be reopened.
        reopenable = self.params.auto_reconnect and analyzer.session.reopenable
        try:
            async with fuji_record(
                source,
                rate_hz=self.params.rate_hz,
                overflow=self.params.overflow_policy(),
                buffer_size=64,
                reconnect=ReconnectPolicy() if reopenable else None,
            ) as recording:
                async for batch in recording.stream:
                    if self._state.stop_requested:
                        break
                    sample = batch.get(self.name)
                    if sample is None:
                        continue
                    record = self._record_for(sample)
                    yield record
                    if sample.frame is None:
                        self._note_failed_poll(sample, record.t_mono_ns)
                        if not self.params.auto_reconnect:
                            raise AdapterError(
                                f"fuji {self.name!r} poll failed and auto_reconnect is "
                                f"disabled: {sample.error}",
                                device=self.name,
                            )
                    else:
                        # Only a good poll counts as an emission for health:
                        # an outage must read as stale, not as live.
                        self._state.last_sample.mark(record.t_mono_ns)
                        self._note_good_poll(sample.frame, record.t_mono_ns)
                        for cs in self._channel_samples_for(sample.frame, record):
                            yield cs
                    # Emitting events after the batch keeps record/sample
                    # ordering deterministic.
                    while self._events:
                        yield self._events.pop(0)
                    if self._state.snapshot_due(period_s=self.params.snapshot_period_s):
                        snap = await self.snapshot()
                        self._state.last_snapshot_t_mono_ns = snap.t_mono_ns
                        yield snap
        except* FujiError as eg:
            first = next(iter(eg.exceptions))
            raise AdapterError(
                f"fuji {self.name!r} stream failed: {first}", device=self.name
            ) from first

    # ---------------------------------------- silence state / snapshot / health

    def watchdog_state(self) -> WatchdogState:
        """Return a compact silence-state view for tests and future policy work."""
        return self._state.watchdog(device=self.name, rate_hz=self.params.rate_hz)

    def _compute_health(self, *, clock: RunClock) -> DeviceHealth:
        """Derive the :class:`DeviceHealth` pill from adapter state.

        Mirrors the session's ``recoverable_error_count`` into the runtime
        state so the shared :meth:`AdapterRuntimeState.compute_health` logic
        applies. ``degraded`` also covers an outage: only good polls mark the
        last-sample time, so it goes stale while the analyzer is silent.
        """
        if self._analyzer is not None:
            self._state.recoverable_error_count = self._analyzer.session.recoverable_error_count
        return self._state.compute_health(clock=clock, rate_hz=self.params.rate_hz)

    async def _snapshot_fields(self) -> dict[str, Scalar]:
        analyzer = self._analyzer
        lib_snap = await analyzer.snapshot() if analyzer is not None else None
        out: dict[str, Scalar] = {
            "address": self.params.address,
            "rate_hz": self.params.rate_hz,
            "channel_count": len(self._channels),
            "state": self._state.lifecycle.state,
            "recoverable_errors": lib_snap.recoverable_error_count if lib_snap is not None else 0,
            "channel_map": ", ".join(f"{c}={g}" for c, g in self.params.channel_map.items()),
        }
        if lib_snap is not None:
            out["connected"] = lib_snap.connected
        if analyzer is not None:
            until = analyzer.session.settling_until
            out["settling_until"] = until.isoformat() if until is not None else None
        info = self._device_info
        if info is not None:
            out["model"] = info.model
            out["serial"] = info.serial_number
            out["type_code"] = info.type_code.raw
        if self._metadata is not None:
            out.update(_metadata_fields(self._metadata, self.params.gases(), self._range_methods))
        return out

    # =====================================================================
    # SECTION 2 — Command surface: authorization gate + tier rule + wrappers
    # =====================================================================

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        """Issue a generic command. Authorization gate first, then dispatch.

        Three ways out besides success:

        * **Refused** — capa's gate, the tier rule, a bad payload, or a
          refusal by fujilib or the analyzer (a value that does not fit, the
          analyzer calibrating or in a menu, an option not fitted, an
          exception reply). Nothing changed. ``accepted=False``.
        * **Uncertain** — a write that did not read back as written, or whose
          outcome is unknown. ``accepted=False`` with the outcome in the
          detail, and an ``error`` event: the analyzer may hold something
          other than what was asked for.
        * **Failed** — the connection. Raises :class:`AdapterError`.
        """
        clock = self._state.clock or RunClock.now()
        rejection = reject_unless_authorized(
            cmd, adapter_id=ADAPTER_ID, device_name=self.name, clock=clock
        )
        if rejection is not None:
            return rejection
        analyzer = self._analyzer
        if analyzer is None:
            return make_not_open_result(adapter_id=ADAPTER_ID, device_name=self.name, clock=clock)

        try:
            detail = await self._dispatch_command(analyzer, cmd)
        except _RefusedError as exc:
            return _not_accepted(f"{cmd.kind} refused: {exc}", clock)
        except _UncertainError as exc:
            self._settings_read_at = -math.inf
            detail = f"{cmd.kind}: {exc}"
            self._queue_while_running(
                self._event(
                    "calibration_uncertain",
                    detail,
                    severity="error",
                    metadata={"command": cmd.kind},
                )
            )
            return _not_accepted(detail, clock)
        except (FujiVerificationError, FujiWriteOutcomeUnknownError) as exc:
            # The analyzer may hold something else than the cache: the next
            # read-back reads it.
            self._settings_read_at = -math.inf
            outcome = "unknown" if isinstance(exc, FujiWriteOutcomeUnknownError) else "not verified"
            detail = f"{cmd.kind}: outcome {outcome}: {exc}"
            self._queue_while_running(
                self._event(
                    "write_uncertain",
                    detail,
                    severity="error",
                    metadata={"command": cmd.kind, "outcome": outcome},
                )
            )
            return _not_accepted(detail, clock)
        except FujiModbusTimeoutError as exc:
            raise self._command_failed(cmd, exc) from exc
        except _REFUSALS as exc:
            return _not_accepted(f"{cmd.kind} refused: {exc}", clock)
        except FujiError as exc:
            raise self._command_failed(cmd, exc) from exc

        return make_accepted_result(detail=detail, clock=clock)

    def _command_failed(self, cmd: DeviceCommand, exc: FujiError) -> AdapterError:
        return AdapterError(
            f"fuji {self.name!r} command {cmd.kind!r} failed: {exc}", device=self.name
        )

    async def _dispatch_command(self, analyzer: Analyzer, cmd: DeviceCommand) -> str:
        """Dispatch a generic :class:`DeviceCommand` to the right fujilib call.

        Recognized ``cmd.kind`` values, with fujilib's safety tier. capa's
        authorization gate covers fujilib's ``confirm=True``; the tier adds
        that every ``DANGEROUS`` verb needs ``confirmed_by``, a person at the
        interface, even when a run authorization is present, so a method step
        cannot change what the analyzer calibrates against.

        Settings, ``PERSISTENT`` (written once, read back, kept by the
        analyzer through a power cycle):

        * ``"set_response_time"`` — payload ``{"target": str, "seconds": int}``;
          ``target`` is a channel or a slot (``"o2"``, ``"ndir1"``..).
        * ``"set_output_hold"`` — payload ``{"enabled": bool}``.
        * ``"set_hold_mode"`` — payload ``{"mode": "last_value" | "setting"}``.
        * ``"set_hold_value"`` — payload ``{"channel": str, "percent_fs": int}``.
        * ``"set_range"`` — payload ``{"channel": str, "range": 1 | 2}``.
        * ``"set_range_method"`` — payload ``{"channel": str, "method": "manual" | "auto"}``.

        Settings by name, at the tier of what they would write:

        * ``"write_parameter"`` — payload ``{"name": str, "value": ..., "unit": str | None}``.
        * ``"apply_settings"`` — payload ``{"document": dict}``, a
          ``fujilib-settings/1`` document.

        ``DANGEROUS``:

        * ``"set_calibration_gas"`` — payload ``{"channel": str, "range": 1 | 2,
          "kind": "zero" | "span", "value": float, "unit": str}``.
        * ``"start_auto_calibration"`` / ``"start_auto_zero_calibration"`` —
          no payload; refused unless the option is asserted in ``options``.

        ``STATEFUL``:

        * ``"return_to_measurement"`` — no payload.

        A manual zero or span (see :mod:`capa.devices.fuji_calibration`):

        * ``"calibration_plan"`` — payload ``{"channel": str, "kind": "zero" |
          "span"}``. Reads only: the result's detail says every channel and
          range the calibration would reach, and the gas of each.
        * ``"calibration_begin"`` — payload ``{"channel": str, "kind": "zero" |
          "span", "gas_value": float, "gas_unit": str | None, "gas_label": str
          | None}``. Presses the keys up to the panel's wait step. Needs
          ``confirmed_by``: the operator is stating which gas is at the inlet.
          The gas named must be the analyzer's calibration-gas setting.
        * ``"calibration_commit"`` — no payload. ``DANGEROUS``: sends the key
          that calibrates. Refused unless the reading is steady on the gas.
        * ``"calibration_cancel"`` — no payload. Leaves the wait step.
        """
        kind = cmd.kind
        if kind == "set_response_time":
            target, seconds = _need(cmd, "target", "seconds")
            return await self._written(
                analyzer, await analyzer.set_response_time(target, seconds, confirm=True)
            )
        if kind == "set_output_hold":
            (enabled,) = _need(cmd, "enabled")
            return await self._written(
                analyzer, await analyzer.set_output_hold(enabled, confirm=True)
            )
        if kind == "set_hold_mode":
            (mode,) = _need(cmd, "mode")
            return await self._written(analyzer, await analyzer.set_hold_mode(mode, confirm=True))
        if kind == "set_hold_value":
            channel, percent_fs = _need(cmd, "channel", "percent_fs")
            return await self._written(
                analyzer, await analyzer.set_hold_value(channel, percent_fs, confirm=True)
            )
        if kind == "set_range":
            channel, number = _need(cmd, "channel", "range")
            return await self._written(
                analyzer, await analyzer.set_range(channel, number, confirm=True)
            )
        if kind == "set_range_method":
            channel, method = _need(cmd, "channel", "method")
            return await self._written(
                analyzer, await analyzer.set_range_method(channel, method, confirm=True)
            )
        if kind == "write_parameter":
            name, value = _need(cmd, "name", "value")
            spec = analyzer.session.profile.registry.resolve(str(name))
            _require_person(cmd, spec.safety)
            return await self._written(
                analyzer,
                await analyzer.write_parameter(
                    str(name), value, unit=cmd.payload.get("unit"), confirm=True
                ),
            )
        if kind == "apply_settings":
            (document,) = _need(cmd, "document")
            return await self._apply_settings(analyzer, cmd, document)
        if kind == "set_calibration_gas":
            channel, number, gas_kind, value, unit = _need(
                cmd, "channel", "range", "kind", "value", "unit"
            )
            _require_person(cmd, SafetyTier.DANGEROUS)
            return await self._written(
                analyzer,
                await analyzer.set_calibration_gas(
                    channel, number, gas_kind, value, unit=unit, confirm=True
                ),
            )
        if kind == "return_to_measurement":
            done = await analyzer.return_to_measurement(confirm=True)
            return f"return_to_measurement: {done.outcome.value}"
        if kind in ("start_auto_calibration", "start_auto_zero_calibration"):
            _require_person(cmd, SafetyTier.DANGEROUS)
            start = (
                analyzer.start_auto_calibration
                if kind == "start_auto_calibration"
                else analyzer.start_auto_zero_calibration
            )
            started = await start(confirm=True)
            self._queue_while_running(
                self._event(
                    "calibration_command",
                    f"{kind}: {started.outcome.value}",
                    severity="warning",
                    metadata={"command": kind, "outcome": started.outcome.value},
                )
            )
            return f"{kind}: {started.outcome.value}"
        if kind == "calibration_plan":
            channel, plan_kind = _need(cmd, "channel", "kind")
            plan = await analyzer.plan_manual_calibration(channel, plan_kind)
            notes = "".join(f". {note}" for note in plan.notes)
            return f"calibration_plan: {plan_summary(plan, self._names, self._ranges())}{notes}"
        if kind == "calibration_begin":
            return await self._begin_calibration(analyzer, cmd)
        if kind == "calibration_commit":
            _require_person(cmd, SafetyTier.DANGEROUS)
            status = await self._active_calibration().commit()
            return _calibration_result(status, committing=True)
        if kind == "calibration_cancel":
            status = await self._active_calibration().cancel()
            return _calibration_result(status)
        raise AdapterError(
            f"fuji {self.name!r}: unknown command kind {kind!r}",
            device=self.name,
        )

    async def _begin_calibration(self, analyzer: Analyzer, cmd: DeviceCommand) -> str:
        channel, kind, gas_value = _need(cmd, "channel", "kind", "gas_value")
        operator = cmd.confirmed_by
        if operator is None:
            raise _RefusedError(
                "a calibration is begun by a person at the interface, who names the gas "
                "at the inlet; a run's authorization does not cover it"
            )
        if self._calibration is not None and self._calibration.active:
            raise _RefusedError("a calibration is already under way; commit or cancel it first")
        gas = CalibrationGas(gas_value, cmd.payload.get("gas_unit"), cmd.payload.get("gas_label"))
        plan = await analyzer.plan_manual_calibration(channel, kind)
        run = CalibrationRun(
            analyzer,
            plan,
            gas,
            operator=operator,
            info=self._device_info,
            port=self.params.port,
            address=self.params.address,
            rule=self._steadiness_rule,
            interval_s=self._calibration_interval_s,
            timeout_s=self._calibration_timeout_s,
            on_end=self._calibration_ended,
            names=self._names,
            ranges=self._ranges(),
        )
        self._calibration = run
        status = await run.begin()
        return f"calibration_begin: {status.plan}; on the wait step"

    def _active_calibration(self) -> CalibrationRun:
        run = self._calibration
        if run is None or not run.active:
            raise _RefusedError("no calibration is under way")
        return run

    def _calibration_ended(self, status: CalibrationStatus) -> None:
        """Report a calibration driven from here, with its record, like one
        watched at the panel."""
        ok = status.outcome in {"completed", "cancelled"} and status.clean is not False
        record = dict(status.record) if status.record is not None else None
        self._queue_while_running(
            self._event(
                "calibration",
                f"a manual {status.kind} calibration of {status.channel_name or status.channel} "
                f"from the host ended: {status.outcome or 'not started'}"
                + (f" ({status.error})" if status.error else ""),
                severity="info" if ok else "warning",
                metadata={
                    "kind": status.kind,
                    "outcome": status.outcome,
                    "channels": status.channel,
                    "source": "remote",
                    "record": record,
                },
            )
        )

    @property
    def calibration(self) -> CalibrationStatus:
        """The calibration under way, or the last one that ended."""
        return self._calibration.status if self._calibration is not None else IDLE

    def _describe_write(self, result: WriteResult) -> str:
        """``"O2 response time: 15 s -> 10 s"``, ``"CO2 range: 0–10 vol% ->
        0–25 vol%"``: a write as the operator reads it, each channel by its
        gas and each range by its span. A setting the card does not offer
        keeps its register name."""
        ranges = {info.channel: info for info in self._ranges()}
        previous = _value_text(result.previous, ranges)
        written = _value_text(result.requested, ranges)
        return f"{self._setting_text(result.name)}: {previous} -> {written}"

    def _setting_text(self, register: str) -> str:
        """``"O2 response time"`` for ``"response_time.o2"``."""
        parts = register.split(".")
        if parts[0] == "response_time" and len(parts) == 2:
            slot = _response_slots(self.params.gases()).get(parts[1])
            who = self._names.get(slot.value, slot.value) if slot else parts[1].upper()
            return f"{who} response time"
        if register == "output_hold.enabled":
            return "output hold"
        if register == "hold.mode":
            return "hold mode"
        if len(parts) < 3 or not parts[1].startswith("ch") or not parts[1][2:].isdigit():
            return register
        channel = ChannelId.from_number(int(parts[1][2:])).value
        gas = self._names.get(channel, channel)
        if parts[0] == "range" and parts[2] == "selected":
            return f"{gas} range"
        if parts[0] == "range" and parts[2] == "method":
            return f"{gas} range method"
        if parts[0] == "hold" and parts[2] == "value":
            return f"{gas} hold value"
        if parts[0] == "calibration_gas" and len(parts) == 4 and parts[2].startswith("range"):
            table = next((t for t in self._ranges() if t.channel.value == channel), None)
            span = range_of(table, int(parts[2].removeprefix("range")))
            return f"{gas} {span} {parts[3]} gas"
        return register

    async def _written(self, analyzer: Analyzer, result: WriteResult) -> str:
        """Take in a verified setting write: refresh the cached settings and
        report the change."""
        await self._refresh_metadata(analyzer)
        change = self._describe_write(result)
        self._queue_while_running(
            self._event(
                "setting_changed",
                change,
                metadata={
                    "setting": result.name,
                    "previous": _shown(result.previous),
                    "written": _shown(result.requested),
                },
            )
        )
        return change

    async def _apply_settings(self, analyzer: Analyzer, cmd: DeviceCommand, document: Any) -> str:
        """Apply a settings document, no higher than the tier the command's
        authorization allows. fujilib compares the whole document first and
        writes nothing if any of it is refused or above that tier."""
        if not isinstance(document, Mapping):
            raise _RefusedError("payload 'document' must be a fujilib-settings/1 document")
        allowed = SafetyTier.DANGEROUS if cmd.confirmed_by is not None else SafetyTier.PERSISTENT
        report = await analyzer.apply_settings(document, confirm=True, max_tier=allowed)
        if report.completed:
            await self._refresh_metadata(analyzer)
        for result in report.completed:
            self._queue_while_running(
                self._event(
                    "setting_changed",
                    self._describe_write(result),
                    metadata={
                        "setting": result.name,
                        "previous": _shown(result.previous),
                        "written": _shown(result.requested),
                    },
                )
            )
        error = report.error
        if error is not None:
            # A write failed part-way: fujilib rolls nothing back, and says so.
            # The same kind of error is raised, so it is sorted like any
            # other write's, with what was and was not written in its message.
            left = ", ".join(report.not_attempted) or "none"
            reason = error.args[0] if error.args else type(error).__name__
            raise type(error)(
                f"applied {len(report.completed)} settings, then {report.failed} failed: "
                f"{reason}; not attempted: {left}",
                context=error.context,
            ) from error
        if not report.completed:
            return "apply_settings: nothing to change"
        names = ", ".join(result.name for result in report.completed)
        return f"apply_settings: wrote {len(report.completed)} ({names})"

    def _queue_while_running(self, event: DeviceEvent) -> None:
        """Queue ``event`` for the stream to yield. Outside a run there is no
        stream, and the caller's own log is the record."""
        if self._state.lifecycle.state == "running":
            self._events.append(event)

    # Typed helpers — IDE-friendly parallels to ``.command()``.

    async def _issue(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        issued_by: str,
        authorization_id: str | None,
        confirmed_by: str | None,
        target: str | None = None,
    ) -> CommandResult:
        return await self.command(
            DeviceCommand(
                kind=kind,
                target=target,
                payload=payload,
                issued_by=issued_by,
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )

    async def set_response_time(
        self,
        target: str,
        seconds: int,
        *,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Set a response time, 0-60 s, by channel or slot (``"o2"``,
        ``"ndir1"``..). 0 switches the analyzer's filter off. Authorization
        rules match :meth:`command`."""
        return await self._issue(
            "set_response_time",
            {"target": target, "seconds": seconds},
            target=f"response_time:{target}",
            issued_by=issued_by,
            authorization_id=authorization_id,
            confirmed_by=confirmed_by,
        )

    async def set_output_hold(
        self,
        enabled: bool,
        *,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Hold the outputs, and the Modbus concentrations, during a
        calibration, or not."""
        return await self._issue(
            "set_output_hold",
            {"enabled": enabled},
            target="output_hold",
            issued_by=issued_by,
            authorization_id=authorization_id,
            confirmed_by=confirmed_by,
        )

    async def set_hold_mode(
        self,
        mode: str,
        *,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """What the outputs hold during a calibration: ``"last_value"`` or
        ``"setting"``."""
        return await self._issue(
            "set_hold_mode",
            {"mode": mode},
            target="hold_mode",
            issued_by=issued_by,
            authorization_id=authorization_id,
            confirmed_by=confirmed_by,
        )

    async def set_hold_value(
        self,
        channel: str,
        percent_fs: int,
        *,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """The value a channel holds in ``setting`` mode, 0-100 % of full scale."""
        return await self._issue(
            "set_hold_value",
            {"channel": channel, "percent_fs": percent_fs},
            target=f"hold_value:{channel}",
            issued_by=issued_by,
            authorization_id=authorization_id,
            confirmed_by=confirmed_by,
        )

    async def set_range(
        self,
        channel: str,
        range_number: int,
        *,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Select range 1 or 2 of a channel whose range method is manual. A
        channel's unit can differ between its ranges: a bound capa channel
        whose declared unit no longer matches is quarantined."""
        return await self._issue(
            "set_range",
            {"channel": channel, "range": range_number},
            target=f"range:{channel}",
            issued_by=issued_by,
            authorization_id=authorization_id,
            confirmed_by=confirmed_by,
        )

    async def set_range_method(
        self,
        channel: str,
        method: str,
        *,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """How a channel changes range: ``"manual"`` or ``"auto"``."""
        return await self._issue(
            "set_range_method",
            {"channel": channel, "method": method},
            target=f"range_method:{channel}",
            issued_by=issued_by,
            authorization_id=authorization_id,
            confirmed_by=confirmed_by,
        )

    async def set_calibration_gas(
        self,
        channel: str,
        range_number: int,
        kind: str,
        value: float,
        *,
        unit: str,
        issued_by: str,
        confirmed_by: str | None = None,
        authorization_id: str | None = None,
    ) -> CommandResult:
        """Set the zero or span calibration gas of a channel's range.
        Destructive: the next calibration is computed from it. It needs
        ``confirmed_by``; a run authorization alone is refused."""
        return await self._issue(
            "set_calibration_gas",
            {"channel": channel, "range": range_number, "kind": kind, "value": value, "unit": unit},
            target=f"calibration_gas:{channel}:{range_number}:{kind}",
            issued_by=issued_by,
            authorization_id=authorization_id,
            confirmed_by=confirmed_by,
        )

    async def return_to_measurement(
        self,
        *,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Bring the analyzer's front panel back to the measurement screen.
        Refused while a calibration is under way: it does not cancel one."""
        return await self._issue(
            "return_to_measurement",
            {},
            issued_by=issued_by,
            authorization_id=authorization_id,
            confirmed_by=confirmed_by,
        )

    async def begin_calibration(
        self,
        channel: str,
        kind: str,
        *,
        gas_value: float,
        gas_unit: str | None = None,
        gas_label: str | None = None,
        issued_by: str,
        confirmed_by: str,
    ) -> CommandResult:
        """Begin a manual zero or span of ``channel`` against the gas the
        operator has put at the inlet. The gas must be the analyzer's
        calibration-gas setting; ``gas_label`` (a cylinder, a lot) goes into
        the record. Returns once the panel is on its wait step."""
        return await self._issue(
            "calibration_begin",
            {
                "channel": channel,
                "kind": kind,
                "gas_value": gas_value,
                "gas_unit": gas_unit,
                "gas_label": gas_label,
            },
            target=f"calibration:{channel}:{kind}",
            issued_by=issued_by,
            authorization_id=None,
            confirmed_by=confirmed_by,
        )

    async def commit_calibration(self, *, issued_by: str, confirmed_by: str) -> CommandResult:
        """Send the key that calibrates. Destructive: it overwrites the
        channel's calibration, and is right only if the named gas is flowing.
        Refused unless the reading is steady on it."""
        return await self._issue(
            "calibration_commit",
            {},
            issued_by=issued_by,
            authorization_id=None,
            confirmed_by=confirmed_by,
        )

    async def cancel_calibration(
        self,
        *,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Leave the wait step without calibrating."""
        return await self._issue(
            "calibration_cancel",
            {},
            issued_by=issued_by,
            authorization_id=authorization_id,
            confirmed_by=confirmed_by,
        )

    # ------------------------------------------------------------------ read-only helpers
    #
    # No authorization gate — these are pure reads. The manual control panel
    # uses them to render the analyzer's state next to its write buttons.

    async def read_state_snapshot(self) -> FujiStateSnapshot | None:
        """One-shot readback for the manual-control card: the calibration's
        state, the latest readings and the settings.

        Returns ``None`` when no analyzer is open. During a run the readings
        are the stream's latest, and during a calibration the latest of its
        own reads of the wait step; otherwise the analyzer is polled once,
        and a poll that fails leaves the readings empty rather than raising.
        After a good poll, settings older than :data:`SETTINGS_MAX_AGE_S`
        are read again; the snapshot shows them as last read.
        """
        analyzer = self._analyzer
        if analyzer is None:
            return None
        frame = analyzer.last_frame
        calibrating = self._calibration is not None and self._calibration.active
        if self._state.lifecycle.state != "running" and not calibrating:
            try:
                frame = await analyzer.poll()
            except FujiError:
                frame = None
            if frame is not None and (
                time.monotonic() - self._settings_read_at > SETTINGS_MAX_AGE_S
            ):
                await self._refresh_metadata(analyzer)
        readings = (
            tuple(
                FujiReadback(
                    channel=reading.channel.value,
                    gas=reading.gas.value,
                    value=reading.value,
                    unit=reading.unit.value,
                    state=reading.state.value,
                )
                for reading in frame.readings
            )
            if frame is not None
            else ()
        )
        metadata = self._metadata
        if metadata is None:
            return FujiStateSnapshot(calibration=self.calibration, readings=readings)
        return FujiStateSnapshot(
            calibration=self.calibration,
            readings=readings,
            channels=_channel_settings(
                metadata, self.params.gases(), self._range_methods, self._names
            ),
            output_hold=metadata.output_hold,
            hold_mode=_enum_text(metadata.hold_mode),
        )

    async def read_settings(self) -> Mapping[str, RegisterValue]:
        """Read every setting of the analyzer, by its fujilib register name."""
        analyzer = self._require_open("read_settings")
        try:
            return await analyzer.read_settings()
        except FujiError as exc:
            raise AdapterError(
                f"fuji {self.name!r} read_settings failed: {exc}", device=self.name
            ) from exc

    async def plan_calibration(self, channel: str, kind: str) -> ManualCalibrationPlan:
        """What a manual zero or span of ``channel`` would calibrate: every
        channel and range it reaches, and the calibration gas of each. Reads
        only."""
        analyzer = self._require_open("plan_calibration")
        try:
            return await analyzer.plan_manual_calibration(channel, kind)
        except FujiError as exc:
            raise AdapterError(
                f"fuji {self.name!r} plan_calibration failed: {exc}", device=self.name
            ) from exc

    def _require_open(self, operation: str) -> Analyzer:
        if self._analyzer is None:
            raise AdapterError(
                f"fuji {self.name!r} {operation}() requires open() first", device=self.name
            )
        return self._analyzer

    # =====================================================================
    # SECTION 3 — Vendor protocol: fujilib-specific analyzer / sample / events
    # =====================================================================

    async def _build_analyzer(self) -> Analyzer:
        """Construct the underlying :class:`Analyzer`, open and identified.

        Default path: :func:`fujilib.open_device` with the configured port.
        Test path: the injected ``analyzer_factory``.
        """
        if self._analyzer_factory is not None:
            return await self._analyzer_factory()
        return await fujilib.open_device(
            self.params.port,
            address=self.params.address,
            timeout=self.params.timeout_s,
            channel_map=self.params.asserted(),
            options=self.params.option_flags(),
        )

    async def _safe_close_analyzer(self) -> None:
        if self._analyzer is None:
            return
        try:
            await self._analyzer.close()
        except FujiError:
            # Cleanup path: don't mask whatever the original failure was.
            return

    async def _read_settings(self, analyzer: Analyzer) -> None:
        """Read the settings snapshot and the measured channels' range
        methods, which it does not carry.

        Raises:
            FujiError: a read failed; the cached settings are unchanged.
        """
        metadata = await analyzer.read_metadata()
        measured = [c for c in self.params.gases() if c in MEASURED_CHANNELS]
        names = [f"range.ch{c.number}.method" for c in measured]
        methods = await analyzer.read_parameters(names) if names else {}
        self._metadata = metadata
        self._range_methods = {
            channel: str(_shown(methods[name]))
            for channel, name in zip(measured, names, strict=True)
        }
        self._settings_read_at = time.monotonic()

    def _ranges(self) -> tuple[RangeInfo, ...]:
        """The range tables as last read; empty before the first read."""
        return self._metadata.ranges if self._metadata is not None else ()

    async def _refresh_metadata(self, analyzer: Analyzer) -> None:
        """Read the settings again; keep the last ones if the read fails."""
        try:
            await self._read_settings(analyzer)
        except FujiError as exc:
            self._queue_while_running(
                self._event(
                    "metadata_stale",
                    "the analyzer's settings could not be read; the snapshots show them "
                    f"as last read: {exc}",
                    severity="warning",
                    metadata={"error_type": type(exc).__name__},
                )
            )

    def _record_for(self, sample: Sample) -> SourceRecord:
        """Convert a fujilib :class:`Sample` into a wide-row :class:`SourceRecord`.

        Uses the library's own :func:`fujilib.sample_to_row` helper so the
        row schema matches what an offline ``fujilib`` recording would
        produce. A failed poll's row has the same keys as a good one.
        """
        clock = self._state.clock
        assert clock is not None
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
            row=sample_to_row(sample),
            metadata={"address": sample.address},
        )

    def _channel_samples_for(self, frame: Frame, record: SourceRecord) -> list[DeviceEmission]:
        """Map ``frame`` against the configured :class:`FujiChannel` bindings.

        A value whose decimal point did not decode, and a validity the
        analyzer did not report, yield no sample; the native row keeps what
        was read. The unit check runs on every reading, since a range change
        at the analyzer can change a channel's unit mid-run.
        """
        readings = {reading.channel.value: reading for reading in frame.readings}
        emissions: list[DeviceEmission] = []
        for spec in self._channels:
            binding = spec.source
            assert isinstance(binding, FujiChannel)
            reading = readings.get(binding.channel)
            if reading is None:
                continue
            column = f"ch{reading.channel.number}_{binding.field}"
            if binding.field == "valid":
                valid = reading.valid
                if valid is None:
                    continue
                raw_value = 1.0 if valid else 0.0
            else:
                if reading.value is None or spec.name in self._quarantined:
                    continue
                if _unit_mismatch(spec, reading):
                    self._quarantined.add(spec.name)
                    self._events.append(self._unit_event(spec, reading, record.t_mono_ns))
                    continue
                raw_value = reading.value
            emissions.append(
                build_channel_sample(
                    spec=spec,
                    raw_value=raw_value,
                    t_mono_ns=record.t_mono_ns,
                    source_record_id=record.record_id,
                    source_field=column,
                    status=reading.state.value,
                )
            )
        return emissions

    # ------------------------------------------------------------------ events

    def _event(
        self,
        kind: str,
        message: str,
        *,
        severity: Literal["info", "warning", "error"] = "info",
        t_mono_ns: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DeviceEvent:
        clock = self._state.clock or RunClock.now()
        return DeviceEvent(
            adapter=ADAPTER_ID,
            device=self.name,
            t_mono_ns=t_mono_ns if t_mono_ns is not None else clock.t_mono_ns(),
            t_utc=datetime.now(UTC),
            kind=kind,
            message=message,
            severity=severity,
            metadata=metadata or {},
        )

    def _label_events(self, clock: RunClock) -> list[DeviceEvent]:
        """One warning per channel whose type code suggests another gas than
        the one asserted. The type code is only a hint, so nothing is
        quarantined: the operator's assertion stands."""
        info = self._device_info
        if info is None:
            return []
        events: list[DeviceEvent] = []
        for channel in info.channels:
            suggested = channel.suggested_gas
            if (
                channel.label_source is not LabelSource.ASSERTED
                or suggested is None
                or suggested is Gas.UNKNOWN
                or suggested is channel.gas
            ):
                continue
            events.append(
                self._event(
                    "label_disagreement",
                    f"{channel.channel.value} is asserted as {channel.gas.value}, but the "
                    f"analyzer's type code suggests {suggested.value}; the assertion is used",
                    severity="warning",
                    t_mono_ns=clock.t_mono_ns(),
                    metadata={
                        "channel": channel.channel.value,
                        "asserted": channel.gas.value,
                        "suggested": suggested.value,
                    },
                )
            )
        return events

    def _unit_event(self, spec: ChannelSpec, reading: Reading, t_mono_ns: int) -> DeviceEvent:
        wire_unit = to_pint(reading.unit) or reading.unit.value
        return self._event(
            "unit_mismatch",
            f"channel {spec.name!r} declares unit {spec.unit!r} but "
            f"{reading.channel.value} reads in {reading.unit.value!r} ({wire_unit!r}); "
            "ChannelSample derivation skipped for the rest of the run. Fix the channel "
            "config or the analyzer's range to recover.",
            severity="error",
            t_mono_ns=t_mono_ns,
            metadata={
                "channel": spec.name,
                "analyzer_channel": reading.channel.value,
                "declared_unit": spec.unit,
                "wire_unit": reading.unit.value,
            },
        )

    def _note_failed_poll(self, sample: Sample, t_mono_ns: int) -> None:
        """Start, or extend, an outage; its first failed poll is reported."""
        outage = self._outage
        if outage is not None:
            outage.failed_polls += 1
            return
        error = sample.error
        error_type = type(error).__name__ if error is not None else "unknown"
        self._outage = _Outage(started_t_mono_ns=t_mono_ns, error_type=error_type)
        self._events.append(
            self._event(
                "comm_lost",
                f"the analyzer stopped answering: {error}",
                severity="warning",
                t_mono_ns=t_mono_ns,
                metadata={"error_type": error_type, "error_message": str(error)},
            )
        )

    def _note_good_poll(self, frame: Frame, t_mono_ns: int) -> None:
        """Report what changed since the last good poll."""
        outage, self._outage = self._outage, None
        if outage is not None:
            until = self._analyzer.session.settling_until if self._analyzer is not None else None
            outage_s = (t_mono_ns - outage.started_t_mono_ns) / 1e9
            settling = f"; readings are settling until {until.isoformat()}" if until else ""
            self._events.append(
                self._event(
                    "comm_restored",
                    f"the analyzer answers again after {outage_s:.1f} s "
                    f"({outage.failed_polls} failed polls){settling}",
                    t_mono_ns=t_mono_ns,
                    metadata={
                        "outage_s": outage_s,
                        "failed_polls": outage.failed_polls,
                        "error_type": outage.error_type,
                        "settling_until": until.isoformat() if until else None,
                    },
                )
            )
        self._note_instrument_errors(frame, t_mono_ns)
        self._note_hold(frame, t_mono_ns)
        if self._calibration is not None and self._calibration.active:
            # A calibration driven from here reports itself when it ends.
            self._tracker = ManualCalibrationTracker()
            return
        observation = PanelObservation.from_frame(frame)
        if observation is not None:
            calibration = self._tracker.feed(observation)
            if calibration is not None:
                self._events.append(self._calibration_event(calibration, t_mono_ns))

    def _note_instrument_errors(self, frame: Frame, t_mono_ns: int) -> None:
        status = frame.analyzer
        if status is None:
            return
        errors = frozenset(int(code) for code in status.errors)
        previous, self._instrument_errors = self._instrument_errors, errors
        if errors == (previous or frozenset()):
            return
        codes = ",".join(str(code) for code in sorted(errors))
        if errors:
            message = f"the analyzer reports instrument error {codes}; its readings are not valid"
        else:
            message = "the analyzer's instrument error cleared"
        self._events.append(
            self._event(
                "instrument_error",
                message,
                severity="error" if errors else "info",
                t_mono_ns=t_mono_ns,
                metadata={"active": bool(errors), "errors": codes},
            )
        )

    def _note_hold(self, frame: Frame, t_mono_ns: int) -> None:
        held = frozenset(
            reading.channel.value
            for reading in frame.readings
            if reading.status is not None and reading.status.hold
        )
        previous, self._hold_channels = self._hold_channels, held
        if held == (previous or frozenset()):
            return
        ordered = sorted(held, key=lambda name: ChannelId(name).number)
        channels = ",".join(ordered)
        if held:
            gases = ", ".join(self._names.get(channel, channel) for channel in ordered)
            message = f"output hold is on for {gases}; the values are frozen"
        else:
            message = "output hold is off"
        self._events.append(
            self._event(
                "hold",
                message,
                severity="warning" if held else "info",
                t_mono_ns=t_mono_ns,
                metadata={"active": bool(held), "channels": channels},
            )
        )

    def _calibration_event(
        self, calibration: ManualCalibrationEvent, t_mono_ns: int
    ) -> DeviceEvent:
        """A zero or span made at the analyzer's front panel, with its
        ``fujilib-calibration/1`` record."""
        channels = ",".join(channel.value for channel in calibration.channels)
        gases = ", ".join(self._names.get(c.value, c.value) for c in calibration.channels)
        ok = calibration.outcome in {
            ManualCalibrationOutcome.COMPLETED,
            ManualCalibrationOutcome.CANCELLED,
        }
        return self._event(
            "calibration",
            f"a manual {calibration.kind.value} calibration of {gases} at the front panel "
            f"ended: {calibration.outcome.value}",
            severity="info" if ok else "warning",
            t_mono_ns=t_mono_ns,
            metadata={
                "kind": calibration.kind.value,
                "outcome": calibration.outcome.value,
                "channels": channels,
                "record": calibration_record(
                    calibration,
                    info=self._device_info,
                    port=self.params.port,
                    address=self.params.address,
                ),
            },
        )


class _Outage:
    """The run of failed polls in progress."""

    __slots__ = ("error_type", "failed_polls", "started_t_mono_ns")

    def __init__(self, *, started_t_mono_ns: int, error_type: str) -> None:
        self.started_t_mono_ns = started_t_mono_ns
        self.error_type = error_type
        self.failed_polls = 1


def _not_accepted(detail: str, clock: RunClock) -> CommandResult:
    """The ``accepted=False`` result of a refused or uncertain command."""
    return CommandResult(
        accepted=False,
        detail=detail,
        t_mono_ns=clock.t_mono_ns(),
        t_utc=datetime.now(UTC),
    )


def _need(cmd: DeviceCommand, *keys: str) -> tuple[Any, ...]:
    """The payload values of ``keys``; a missing one refuses the command."""
    missing = [key for key in keys if key not in cmd.payload]
    if missing:
        raise _RefusedError(f"payload is missing {', '.join(missing)}")
    return tuple(cmd.payload[key] for key in keys)


def _require_person(cmd: DeviceCommand, tier: SafetyTier) -> None:
    """Refuse a ``DANGEROUS`` command that no person confirmed."""
    if tier >= SafetyTier.DANGEROUS and cmd.confirmed_by is None:
        raise _RefusedError(
            "it is DANGEROUS and needs a person's confirmation at the interface; "
            "a run's authorization does not cover it"
        )


def _calibration_result(status: CalibrationStatus, *, committing: bool = False) -> str:
    """The detail of a calibration that ended as asked; anything else is loud.

    ``committing`` is for the command that calibrates: a run that ended
    cancelled, by another command or at the panel, did not do what it asked.
    """
    what = f"{status.kind} of {status.channel_name or status.channel}"
    if status.error is not None or status.clean is False or status.outcome is None:
        problem = status.error or "the front panel was not left clean"
        raise _UncertainError(f"{what} ended {status.outcome or 'without a result'}: {problem}")
    if status.outcome not in {"completed", "cancelled"}:
        raise _UncertainError(f"{what} ended {status.outcome}; check the analyzer")
    if committing and status.outcome != "completed":
        raise _RefusedError(f"{what} ended {status.outcome}; nothing was calibrated")
    return f"{what}: {status.outcome}"


def _shown(value: RegisterValue) -> Scalar:
    """A register value as an event scalar: the number, flag or enum name."""
    shown = value.value if value.value is not None else value.raw
    return shown.name.lower() if isinstance(shown, Enum) else shown


def _response_slots(gases: Mapping[ChannelId, Gas]) -> dict[str, ChannelId]:
    """The measured channel each response-time slot serves, by fujilib's
    rule: the O2 channel has the ``o2`` slot, and the n-th other channel
    ``ndirn``, which needs every channel before it asserted."""
    slots: dict[str, ChannelId] = {}
    ndir = 0
    gap = False
    for channel in MEASURED_CHANNELS:
        gas = gases.get(channel)
        if gas is None:
            gap = True
        elif gas is Gas.O2:
            slots["o2"] = channel
        elif not gap:
            ndir += 1
            slots[f"ndir{ndir}"] = channel
    return slots


def _value_text(value: RegisterValue, ranges: Mapping[ChannelId, RangeInfo]) -> str:
    """A setting's value as the operator reads it: a range by its span, a
    method or hold mode by what it does, a flag as on or off."""
    shown = value.value if value.value is not None else value.raw
    if isinstance(shown, RangeIndex):
        channel = value.spec.channel
        return range_of(ranges.get(channel) if channel is not None else None, shown.number)
    if isinstance(shown, RangeMethod):
        return RANGE_METHOD_NAMES.get(shown.name.lower(), shown.name.lower()).lower()
    if isinstance(shown, HoldMode):
        return HOLD_MODE_NAMES.get(shown.name.lower(), shown.name.lower()).lower()
    if isinstance(shown, bool):
        return "on" if shown else "off"
    if isinstance(shown, float):
        return f"{shown:g}" + (f" {value.unit}" if value.unit else "")
    return describe(value)


def _unit_mismatch(spec: ChannelSpec, reading: Reading) -> bool:
    """``True`` when the reading's unit is not the unit ``spec`` declares.

    Compares canonical pint names. Dimensional compatibility is not enough:
    vol% and ppm are both dimensionless, and a channel declared in percent
    fed ppm values would be wrong by a factor of 10,000. A unit fujilib does
    not know is not checked.
    """
    pint_unit = to_pint(reading.unit)
    if pint_unit is None:
        return False
    if not units_compatible(pint_unit, spec.unit):
        return True
    return canonicalize_unit(pint_unit) != canonicalize_unit(spec.unit)


def _enum_text(value: object) -> str:
    """An enum member's lower-case name, or the raw number as text."""
    name = getattr(value, "name", None)
    return name.lower() if isinstance(name, str) else str(value)


def _metadata_fields(
    metadata: AnalyzerMetadata,
    gases: Mapping[ChannelId, Gas],
    range_methods: Mapping[ChannelId, str],
) -> dict[str, Scalar]:
    """Flatten the analyzer's settings to snapshot scalars.

    Per-channel fields are given for the asserted channels that have range
    registers (channels 1-5).
    """
    out: dict[str, Scalar] = {
        "response_time_o2_s": metadata.response_time_o2_s,
        "hold_mode": _enum_text(metadata.hold_mode),
        "output_hold": metadata.output_hold,
        "analyzer_clock": metadata.clock.isoformat() if metadata.clock is not None else None,
        "metadata_captured_at": metadata.captured_at.isoformat(),
    }
    for slot, seconds in enumerate(metadata.response_time_ndir_s, start=1):
        out[f"response_time_ndir{slot}_s"] = seconds
    ranges = {info.channel: info for info in metadata.ranges}
    for channel in gases:
        info = ranges.get(channel)
        current = metadata.current_range.get(channel)
        if info is None or current is None:
            continue
        prefix = f"ch{channel.number}"
        out[f"{prefix}_range"] = current
        if channel in range_methods:
            out[f"{prefix}_range_method"] = range_methods[channel]
        if 1 <= current <= len(info.units):
            unit, full_scale, _decimals = info.of(current)
            out[f"{prefix}_unit"] = unit.value
            out[f"{prefix}_full_scale"] = full_scale
        for number in range(1, info.count + 1):
            gas = metadata.calibration_gas.get((channel, number))
            if gas is None:
                continue
            out[f"{prefix}_range{number}_zero_gas"], out[f"{prefix}_range{number}_span_gas"] = gas
    return out


def _channel_settings(
    metadata: AnalyzerMetadata,
    gases: Mapping[ChannelId, Gas],
    range_methods: Mapping[ChannelId, str],
    names: Mapping[str, str],
) -> tuple[FujiChannelSettings, ...]:
    """The settings of each asserted channel that has range registers
    (channels 1-5), in channel order."""
    tables = {info.channel: info for info in metadata.ranges}
    out: list[FujiChannelSettings] = []
    for channel in sorted(gases, key=lambda c: c.number):
        table = tables.get(channel)
        if table is None:
            continue
        ranges: list[FujiRange] = []
        for number in range(1, min(table.count, len(table.units)) + 1):
            unit, full_scale, _decimals = table.of(number)
            zero, span = metadata.calibration_gas.get((channel, number), (None, None))
            ranges.append(FujiRange(number, unit.value, full_scale, zero, span))
        out.append(
            FujiChannelSettings(
                channel=channel.value,
                gas=gases[channel].value,
                name=names.get(channel.value, channel.value),
                ranges=tuple(ranges),
                current_range=metadata.current_range.get(channel),
                range_method=range_methods.get(channel),
                response_time_s=metadata.response_time_s.get(channel),
            )
        )
    return tuple(out)


def _suggested_channel_map(info: DeviceInfo) -> dict[str, str]:
    """The gas the analyzer's type code and channel layout suggest for each
    channel; channels with no suggestion are left out."""
    return {
        c.channel.value: c.suggested_gas.value
        for c in info.channels
        if c.suggested_gas is not None and c.suggested_gas is not Gas.UNKNOWN
    }


def _channels_text(info: DeviceInfo) -> str:
    """``"CH1=co2, CH2=co, CH3=o2"``: the established channels and their gases."""
    return ", ".join(f"{c.channel.value}={c.gas.value}" for c in info.channels)


# ---------------------------------------------------------------------------
# CLI handshake hook (``capa validate --strict``)
# ---------------------------------------------------------------------------


async def handshake(params: dict[str, Any]) -> str:
    """Read-only open + identify + close. Used by ``capa validate --strict``.

    Returns a one-line summary of the analyzer's identity. Raises
    :class:`AdapterError` on any failure so the CLI can surface the wiring
    problem before the operator arms a run.
    """
    parsed = FujiAdapterParams.model_validate(params)
    try:
        analyzer = await fujilib.open_device(
            parsed.port,
            address=parsed.address,
            timeout=parsed.timeout_s,
            channel_map=parsed.asserted(),
            options=parsed.option_flags(),
        )
        try:
            info = analyzer.info
        finally:
            await analyzer.close()
    except FujiError as exc:
        raise AdapterError(f"fuji handshake failed at {parsed.port}: {exc}") from exc
    if info is None:
        return f"fuji port={parsed.port} station={parsed.address} (not identified)"
    return (
        f"fuji model={info.model} serial={info.serial_number or '?'} "
        f"type_code={info.type_code.raw} station={info.address} "
        f"channels=[{_channels_text(info)}]"
    )


# ---------------------------------------------------------------------------
# Discovery hook (``capa devices discover`` / Setup editor scan)
# ---------------------------------------------------------------------------


async def discover(
    *,
    ports: list[str] | None = None,
    addresses: tuple[int, ...] = (1,),
    timeout_s: float = 0.3,
) -> list[dict[str, Any]]:
    """Probe local serial ports for Fuji ZP-series analyzers.

    Thin wrapper over :func:`fujilib.find_devices`, which sends a Modbus RTU
    read at 38400 8-N-1 to each station of ``addresses`` on each port and
    identifies whatever answers as an analyzer. Reads only, but every port
    scanned receives the probe frames, so the Setup tab scans serial
    families one at a time. One row per analyzer found. ``channels`` and
    ``channel_map`` are the type code's suggestion of each channel's gas, to
    be confirmed by the operator: the type code can be out of date.
    """
    if ports is not None and not ports:
        return []
    try:
        results = await fujilib.find_devices(
            ports=ports, addresses=addresses, per_probe_timeout_s=timeout_s
        )
    except FujiError:
        return []
    rows: list[dict[str, Any]] = []
    for result in results:
        info = result.device_info
        if not result.ok or info is None:
            continue
        suggested = _suggested_channel_map(info)
        rows.append(
            {
                "adapter": ADAPTER_ID,
                "port": result.port,
                "address": result.address,
                "baudrate": result.baudrate,
                "model": info.model,
                "serial": info.serial_number,
                "type_code": info.type_code.raw,
                "channels": ", ".join(f"{c}={g}" for c, g in suggested.items()),
                "channel_map": suggested,
            }
        )
    return rows


__all__ = [
    "ADAPTER_ID",
    "DESCRIPTOR",
    "SETTINGS_MAX_AGE_S",
    "FujiAdapter",
    "FujiAdapterParams",
    "FujiChannelSettings",
    "FujiRange",
    "FujiReadback",
    "FujiStateSnapshot",
    "discover",
    "handshake",
]


def _build_descriptor() -> AdapterDescriptor:
    from capa.devices._templates import FUJI_CO, FUJI_CO2, FUJI_O2  # noqa: PLC0415
    from capa.devices.fuji_settings import FUJI_SETTINGS  # noqa: PLC0415
    from capa.devices.registry import AdapterDescriptor  # noqa: PLC0415

    return AdapterDescriptor(
        id="capa.devices.fuji",
        label="Fuji ZP gas analyzer",
        family="fuji",
        adapter_factory=FujiAdapter,
        params_model=FujiAdapterParams,
        supported_binding_sources=("fuji_channel",),
        default_params={"rate_hz": 1.0, "address": 1},
        channel_templates=(FUJI_CO2, FUJI_CO, FUJI_O2),
        discoverable=True,
        handshake_available=True,
        capabilities=frozenset(
            {
                Capability.READS_PROCESS_VAR,
                Capability.HAS_PARAMETER_CONFIG,
                Capability.HAS_GAS_CALIBRATION,
                Capability.HAS_INTERNAL_CAL,
            }
        ),
        settings=FUJI_SETTINGS,
    )


DESCRIPTOR = _build_descriptor()

from capa.devices.registry import register as _register  # noqa: E402

_register(DESCRIPTOR)
