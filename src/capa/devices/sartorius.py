"""Real :class:`SartoriusAdapter` — wraps a :class:`sartoriuslib.Balance`.

Architecture:

* ``open`` opens the serial transport via :func:`sartoriuslib.open_device` and
  runs :meth:`Balance.identify` to capture model / serial / firmware. The
  cached :class:`sartoriuslib.DeviceInfo` populates :class:`DeviceSnapshot`.
* ``start`` captures the run :class:`RunClock` and arms the streaming pipeline.
* ``stream`` drives :func:`sartoriuslib.streaming.record` over a single-device
  :class:`PollSource` shim. Every emitted :class:`sartoriuslib.Sample` becomes
  one :class:`SourceRecord` (``shape="single_value_row"``, preserving the
  library-native fields via :func:`sartoriuslib.sinks.sample_to_row`) plus
  zero or more :class:`ChannelSample`\\ s mapped from the configured
  :class:`SartoriusReading` bindings. The balance's stability flag travels
  on the channel sample's :attr:`status` (``"settling"`` while unstable).
* ``snapshot`` returns a :class:`DeviceSnapshot` with cached identity plus
  live health fields (auto-reconnect counter, last-sample age,
  :class:`DeviceHealth` pill).
* ``command`` enforces the authorization gate and dispatches
  :meth:`Balance.tare` / :meth:`Balance.zero`.
"""

from __future__ import annotations

import functools
import logging
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Final, Literal

import sartoriuslib
from pydantic import BaseModel, ConfigDict, Field
from sartoriuslib import (
    Balance,
    CalRecord,
    DeviceInfo,
    DiscoverySummary,
    OverflowPolicy,
    PollSourceAdapter,
    ProtocolKind,
    Reading,
    SartoriusError,
    Unit,
    sample_to_row,
    summarize_discovery,
)
from sartoriuslib.registry.parameters import PARAMETER_TABLE
from sartoriuslib.streaming.recorder import record as sartorius_record
from sartoriuslib.transport import SerialSettings

from capa.channels.spec import ChannelSpec, SartoriusReading
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
from capa.devices.settings import DeviceSettingsSpec, SettingField

if TYPE_CHECKING:
    from capa.devices.registry import AdapterDescriptor

_log = logging.getLogger(__name__)

ADAPTER_ID: Final[str] = "sartorius"


ProtocolName = Literal["xbpi", "sbi", "auto"]
"""Lowercase string form accepted in TOML/YAML configs; mapped to
:class:`ProtocolKind` at adapter construction."""

_PROTOCOL_BY_NAME: Final[dict[ProtocolName, ProtocolKind]] = {
    "xbpi": ProtocolKind.XBPI,
    "sbi": ProtocolKind.SBI,
    "auto": ProtocolKind.AUTO,
}


# ---------------------------------------------------------------------------
# Adapter params (Pydantic) — what shows up under ``[devices.params]`` in TOML.
# ---------------------------------------------------------------------------


class SartoriusAdapterParams(BaseModel):
    """Per-device adapter configuration for a real Sartorius balance.

    adapter-specific knobs live under ``DeviceConfig.params`` and are
    parsed by the adapter at construction time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    port: str
    """Serial-port path: ``/dev/ttyUSB0`` (Linux), ``COM3`` (Windows)."""

    protocol: ProtocolName = "xbpi"
    """Wire protocol. ``auto`` runs the conservative detector
    from sartoriuslib; pin one in production."""

    baudrate: int = 9600
    """Default Sartorius shipment is 9600; some labs reconfigure higher."""

    timeout_s: float = Field(gt=0, default=1.0)
    """Per-call command timeout passed through to
    :func:`sartoriuslib.open_device`."""

    src_sbn: int = Field(ge=0, le=255, default=0x01)
    """Host xBPI bus address."""

    dst_sbn: int = Field(ge=0, le=255, default=0x09)
    """Balance xBPI bus address (factory default ``0x09``)."""

    rate_hz: float = Field(gt=0, le=50.0, default=2.0)
    """Polling cadence. Production runs sit at 1–10 Hz."""

    snapshot_period_s: float = Field(gt=0, default=30.0)
    """Cadence of :class:`DeviceSnapshot` emissions during a run."""

    auto_reconnect: bool = True
    """When ``True``, transient :class:`SartoriusConnectionError`\\ s do not
    terminate the stream — the adapter increments its degradation counter."""

    overflow: Literal["block", "drop_newest"] = "block"
    """Recorder overflow policy. ``BLOCK`` matches """

    def to_serial_settings(self) -> SerialSettings:
        """Build the :class:`SerialSettings` sartoriuslib expects."""
        return SerialSettings(port=self.port, baudrate=self.baudrate)

    def protocol_kind(self) -> ProtocolKind:
        """Wire protocol family (e.g. ``"sbi"``, ``"sbi-pid"``) for this controller."""
        return _PROTOCOL_BY_NAME[self.protocol]

    def overflow_policy(self) -> OverflowPolicy:
        """Translate the user-facing ``overflow`` string to a library :class:`OverflowPolicy`."""
        return OverflowPolicy.BLOCK if self.overflow == "block" else OverflowPolicy.DROP_NEWEST


@dataclass(frozen=True, slots=True)
class SartoriusStateSnapshot:
    """One-shot readback of a balance's menu settings and last calibration.

    Built by :meth:`SartoriusAdapter.read_state_snapshot` on the worker loop
    and consumed by the manual-control card on the qasync loop. Mode values
    are the lowercase spaced names the card offers and sartoriuslib's
    resolvers accept. ``None`` means the balance did not report the value.
    """

    filter_mode: str | None = None
    """``"very stable"`` / ``"stable"`` / ``"unstable"`` / ``"very unstable"``."""

    app_filter: str | None = None
    """``"final reading"`` / ``"filling"`` / ``"reduced"`` / ``"off"``."""

    stability_range: str | None = None
    """``"max accuracy"`` … ``"max fast"`` — how narrow a band counts as stable."""

    stability_delay: str | None = None
    """``"none"`` / ``"short"`` / ``"average"`` / ``"long"``."""

    auto_zero: str | None = None
    """``"on"`` / ``"off"``."""

    display_unit: str | None = None
    """Display unit label, e.g. ``"g"``."""

    tare_behavior: str | None = None
    """``"without stability"`` / ``"with stability"`` / ``"at stability"``."""

    cal_temperature_c: float | None = None
    """Balance temperature at the last calibration."""

    cal_on_record: bool | None = None
    """Whether a calibration is on record. The record's metadata lives in
    RAM, so it is ``False`` after a cold boot until the next calibration."""


# ---------------------------------------------------------------------------
# Menu labels and declarative settings
# ---------------------------------------------------------------------------
#
# Labels are sartoriuslib's mode names as lowercase words — what
# :func:`_mode_label` reports and what the library's resolvers (and, for
# p02–p04, :func:`_encode_mode`) accept.

FilterModeLabel = Literal["very stable", "stable", "unstable", "very unstable"]
AppFilterLabel = Literal["final reading", "filling", "reduced", "off"]
StabilityRangeLabel = Literal[
    "max accuracy", "very accurate", "accurate", "fast", "very fast", "max fast"
]
StabilityDelayLabel = Literal["none", "short", "average", "long"]
AutoZeroLabel = Literal["on", "off"]
TareBehaviorLabel = Literal["without stability", "with stability", "at stability"]

_RAW_MODE_PARAMETERS: Final[dict[str, int]] = {
    "app_filter": 2,
    "stability_range": 3,
    "stability_delay": 4,
}
"""Menu parameters sartoriuslib has no typed accessor for: name → p-index.
Read and written through the raw parameter table."""

_RAW_MODE_COMMANDS: Final[dict[str, int]] = {
    f"set_{name}": index for name, index in _RAW_MODE_PARAMETERS.items()
}


class SartoriusSettings(BaseModel):
    """What an experiment can declare for a balance under ``device_settings:``.

    Written to the runtime menu only — never saved to EEPROM — so the
    experiment re-applies them on every load.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    filter_mode: FilterModeLabel | None = Field(
        default=None,
        json_schema_extra={
            "capa_help": "Ambient conditions (p01): settling time vs. vibration rejection."
        },
    )
    app_filter: AppFilterLabel | None = Field(
        default=None,
        json_schema_extra={"capa_help": "Application filter (p02)."},
    )
    stability_range: StabilityRangeLabel | None = Field(
        default=None,
        json_schema_extra={
            "capa_help": "How narrow a band the reading must stay in to count as stable (p03)."
        },
    )
    stability_delay: StabilityDelayLabel | None = Field(
        default=None,
        json_schema_extra={
            "capa_help": "How long the reading must stay in band before it's stable (p04)."
        },
    )
    auto_zero: AutoZeroLabel | None = Field(
        default=None,
        json_schema_extra={"capa_help": "Automatic zero tracking (p06)."},
    )
    tare_behavior: TareBehaviorLabel | None = Field(
        default=None,
        json_schema_extra={"capa_help": "Whether a tare waits for a stable reading (p05)."},
    )


def _menu_field(name: str, label: str) -> SettingField:
    return SettingField(
        name=name,
        label=label,
        current=lambda snapshot: getattr(snapshot, name),
        command=lambda mode, _snapshot: (f"set_{name}", {"mode": mode}),
    )


SARTORIUS_SETTINGS: Final = DeviceSettingsSpec(
    model=SartoriusSettings,
    fields=(
        _menu_field("filter_mode", "Filter mode"),
        _menu_field("app_filter", "Application filter"),
        _menu_field("stability_range", "Stability range"),
        _menu_field("stability_delay", "Stability delay"),
        _menu_field("auto_zero", "Auto-zero"),
        _menu_field("tare_behavior", "Tare behavior"),
    ),
    snapshot_type=SartoriusStateSnapshot,
)
"""Shared by :class:`SartoriusAdapter` and the balance simulator."""


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


BalanceFactory = Callable[[], Awaitable[Balance]]
"""Test seam: a factory returning an open :class:`Balance`. The adapter calls
it at :meth:`SartoriusAdapter.open` time. Production code leaves this
``None`` and the adapter calls :func:`sartoriuslib.open_device` itself."""


class SartoriusAdapter:
    """Real Sartorius balance adapter (single device per instance).

    Two construction shapes:

    * ``SartoriusAdapter(name=..., **params_kwargs)`` — engine path; per-device
      params from ``DeviceConfig.params`` are forwarded as kwargs and parsed
      into :class:`SartoriusAdapterParams`.
    * ``SartoriusAdapter(name=..., params=SartoriusAdapterParams(...))`` —
      programmatic path used by tests.

    Both shapes accept an optional ``balance_factory`` kwarg as a test seam.
    """

    __slots__ = (
        "_balance",
        "_balance_factory",
        "_channels",
        "_device_info",
        "_interval_max_ms",
        "_interval_min_ms",
        "_interval_narrow_count",
        "_last_monotonic_ns",
        "_state",
        "capabilities",
        "name",
        "params",
    )

    name: str
    params: SartoriusAdapterParams
    capabilities: frozenset[Capability]

    def __init__(
        self,
        *,
        name: str,
        params: SartoriusAdapterParams | None = None,
        balance_factory: BalanceFactory | None = None,
        **params_kwargs: Any,
    ) -> None:
        if params is not None and params_kwargs:
            raise TypeError(
                "SartoriusAdapter accepts either `params=` or per-field kwargs, not both"
            )
        if params is None:
            params = SartoriusAdapterParams.model_validate(params_kwargs)
        self.name = name
        self.params = params
        flags: set[Capability] = {
            Capability.HAS_TARE,
            Capability.HAS_ZERO,
            Capability.EMITS_STABILITY_FLAG,
            Capability.HAS_INTERNAL_CAL,
            Capability.HAS_PARAMETER_CONFIG,
        }
        if params.auto_reconnect:
            flags.add(Capability.SUPPORTS_AUTO_RECONNECT)
        self.capabilities = frozenset(flags)
        self._balance_factory: BalanceFactory | None = balance_factory
        self._balance: Balance | None = None
        self._device_info: DeviceInfo | None = None
        self._channels: list[ChannelSpec] = []
        self._state = AdapterRuntimeState()
        self._last_monotonic_ns: int | None = None
        self._interval_min_ms: float = math.inf
        self._interval_max_ms: float = 0.0
        self._interval_narrow_count: int = 0

    # ------------------------------------------------------------------ wiring

    def configure_channels(self, specs: list[ChannelSpec]) -> None:
        """Bind to :class:`SartoriusReading`-bound channels for this device."""
        self._channels = channels_for_device(
            specs, device=self.name, binding_source="sartorius_reading"
        )

    @property
    def expected_emission_rate_hz(self) -> float:
        # One SourceRecord + one ChannelSample per bound channel per poll.
        """Emission rate hint for queue sizing. See :class:`~capa.devices.adapter.DeviceAdapter`."""
        return self.params.rate_hz * (1 + len(self._channels))

    @property
    def resource_id(self) -> str:
        """Stable contention-domain identifier. See :class:`~capa.devices.adapter.DeviceAdapter`."""
        return serial_resource_id(self.params.port)

    @property
    def device_info(self) -> DeviceInfo | None:
        """The cached :class:`sartoriuslib.DeviceInfo` from :meth:`open`."""
        return self._device_info

    # ------------------------------------------------------------------ lifecycle

    async def open(self) -> None:
        """Open the underlying transport and identify the balance.

        Idempotent: a second call on an already-open adapter is a no-op.
        """
        if self._state.lifecycle.state in ("open", "running"):
            return
        try:
            self._balance = await self._build_balance()
        except SartoriusError as exc:
            await self._safe_close_balance()
            raise AdapterError(
                f"sartorius {self.name!r} open failed: {exc}", device=self.name
            ) from exc
        # ``Balance.info`` is populated by ``open_device(identify=True)``.
        self._device_info = self._balance.info
        self._state.lifecycle.open()

    async def close(self) -> None:
        """Release the bus / handle. Idempotent."""
        if self._state.lifecycle.state == "closed":
            return
        await self._safe_close_balance()
        self._balance = None
        self._state.lifecycle.close()

    async def start(self, ctx: AdapterStartContext) -> None:
        """Capture the :class:`RunClock` anchor and arm the streaming loop."""
        self._state.on_start(ctx.clock)
        # Sartorius-specific: reset wire-spacing jitter tracking on each run.
        self._last_monotonic_ns = None
        self._interval_min_ms = math.inf
        self._interval_max_ms = 0.0
        self._interval_narrow_count = 0

    async def stop(self) -> None:
        """Request the streaming loop to exit cleanly. Idempotent."""
        self._state.request_stop()

    async def snapshot(self) -> DeviceSnapshot:
        """Build a :class:`DeviceSnapshot` from the library snapshot + live health.

        Reads ``balance.snapshot()`` for identity / session counters
        (I/O-free) and projects into capa's emission shape. Capa-only
        fields (protocol, rate, channel-binding count, wire-spacing
        jitter) are added on top.
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

        Drives :func:`sartoriuslib.streaming.record` against a single-device
        :class:`sartoriuslib.PollSourceAdapter`. Successful polls yield one
        :class:`SourceRecord` plus one :class:`ChannelSample` per matching
        :class:`SartoriusReading` binding. Errored polls (sartoriuslib's
        recorder still produces a :class:`Sample` with ``reading=None``)
        increment the degradation counter and are dropped from the
        :class:`ChannelSample` stream — the native row is preserved with the
        error fields populated.
        """
        if self._balance is None:
            raise AdapterError(
                f"sartorius {self.name!r} stream() requires open() first",
                device=self.name,
            )
        if self._state.clock is None:
            raise AdapterError(
                f"sartorius {self.name!r} stream() requires start() first",
                device=self.name,
            )

        snap = await self.snapshot()
        self._state.last_snapshot_t_mono_ns = snap.t_mono_ns
        yield snap

        source = PollSourceAdapter(self.name, self._balance)

        try:
            async with sartorius_record(
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
                        # Source returned no row at all — recorder oddity;
                        # treat as missed tick.
                        continue
                    # Track wire-midpoint spacing so we can diagnose jitter.
                    cur_mono = sample.t_mono_ns
                    if self._last_monotonic_ns is not None:
                        dt_ms = (cur_mono - self._last_monotonic_ns) / 1e6
                        if dt_ms < self._interval_min_ms:
                            self._interval_min_ms = dt_ms
                        if dt_ms > self._interval_max_ms:
                            self._interval_max_ms = dt_ms
                        expected_ms = 1000.0 / self.params.rate_hz
                        if dt_ms < expected_ms * 0.75:
                            self._interval_narrow_count += 1
                    self._last_monotonic_ns = cur_mono
                    record = self._record_for(sample)
                    yield record
                    self._state.last_sample.mark(record.t_mono_ns)
                    if sample.reading is None:
                        # Error sample — native row is preserved (with
                        # error_type / error_message), but no calibrated
                        # ChannelSample can be derived. The session's
                        # recoverable_error_count tracks the transient.
                        if not self.params.auto_reconnect:
                            err = sample.error
                            raise AdapterError(
                                f"sartorius {self.name!r} poll failed and "
                                f"auto_reconnect is disabled: {err}",
                                device=self.name,
                            )
                        continue
                    for cs in self._channel_samples_for(sample, record.record_id):
                        yield cs
                    if self._state.snapshot_due(period_s=self.params.snapshot_period_s):
                        snap = await self.snapshot()
                        self._state.last_snapshot_t_mono_ns = snap.t_mono_ns
                        yield snap
        except* SartoriusError as eg:
            first = next(iter(eg.exceptions))
            raise AdapterError(
                f"sartorius {self.name!r} stream failed: {first}", device=self.name
            ) from first

    # ------------------------------------------------------------------ commands

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        """Issue a generic command. Authorization gate first, then dispatch."""
        clock = self._state.clock or RunClock.now()
        rejection = reject_unless_authorized(
            cmd, adapter_id=ADAPTER_ID, device_name=self.name, clock=clock
        )
        if rejection is not None:
            return rejection
        if self._balance is None:
            return make_not_open_result(adapter_id=ADAPTER_ID, device_name=self.name, clock=clock)

        try:
            detail = await self._dispatch_command(cmd)
        except SartoriusError as exc:
            raise AdapterError(
                f"sartorius {self.name!r} command {cmd.kind!r} failed: {exc}",
                device=self.name,
            ) from exc

        return make_accepted_result(detail=detail, clock=clock)

    async def _dispatch_command(self, cmd: DeviceCommand) -> str:
        """Dispatch a generic :class:`DeviceCommand` to the right typed call.

        Recognized ``cmd.kind`` values:

        Stateful (no library confirm needed):

        * ``"tare"`` — combined-tare (xBPI ``0x14`` / SBI ``ESC T``).
        * ``"zero"`` — zero command (xBPI ``0x18``).

        Persistent / dangerous (CAPA's authorization gate covers the library's
        own ``confirm=True`` gate; we always pass it through here since
        ``reject_unless_authorized`` already accepted the command):

        * ``"internal_adjust"`` — payload ``{"cal_type": int | None}``.
        * ``"set_filter_mode"`` — payload ``{"mode": str | int}``.
        * ``"set_display_unit"`` — payload ``{"unit": str | int}``.
        * ``"set_auto_zero"`` — payload ``{"mode": str | int}``.
        * ``"set_isocal_mode"`` — payload ``{"mode": str | int}`` (Cubis only).
        * ``"set_tare_behavior"`` — payload ``{"mode": str | int}``.
        * ``"set_app_filter"`` / ``"set_stability_range"`` /
          ``"set_stability_delay"`` — payload ``{"mode": str | int}``; p02–p04
          through the raw parameter table (see :func:`_encode_mode`).
        * ``"save_menu"`` — persist current menu to EEPROM.
        * ``"reload_menu"`` — reload saved menu from EEPROM.
        """
        assert self._balance is not None
        kind = cmd.kind
        if kind == "tare":
            await self._balance.tare()
            return "tare"
        if kind == "zero":
            await self._balance.zero()
            return "zero"
        if kind == "internal_adjust":
            cal_type = cmd.payload.get("cal_type")
            await self._balance.internal_adjust(cal_type=cal_type, confirm=True)
            return f"internal_adjust cal_type={cal_type if cal_type is not None else 'default'}"
        if kind == "set_filter_mode":
            mode = cmd.payload["mode"]
            await self._balance.set_filter_mode(mode, confirm=True)
            return f"set_filter_mode mode={mode!r}"
        if kind == "set_display_unit":
            unit = cmd.payload["unit"]
            await self._balance.set_display_unit(unit, confirm=True)
            return f"set_display_unit unit={unit!r}"
        if kind == "set_auto_zero":
            mode = cmd.payload["mode"]
            await self._balance.set_auto_zero(mode, confirm=True)
            return f"set_auto_zero mode={mode!r}"
        if kind == "set_isocal_mode":
            mode = cmd.payload["mode"]
            await self._balance.set_isocal_mode(mode, confirm=True)
            return f"set_isocal_mode mode={mode!r}"
        if kind == "set_tare_behavior":
            mode = cmd.payload["mode"]
            await self._balance.set_tare_behavior(mode, confirm=True)
            return f"set_tare_behavior mode={mode!r}"
        if kind in _RAW_MODE_COMMANDS:
            index = _RAW_MODE_COMMANDS[kind]
            mode = cmd.payload["mode"]
            try:
                wire = _encode_mode(index, mode)
            except ValueError as exc:
                raise AdapterError(
                    f"sartorius {self.name!r} {kind}: {exc}", device=self.name
                ) from exc
            await self._balance.write_parameter(index, wire, confirm=True)
            return f"{kind} mode={mode!r}"
        if kind == "save_menu":
            await self._balance.save_menu(confirm=True)
            return "save_menu"
        if kind == "reload_menu":
            await self._balance.reload_menu(confirm=True)
            return "reload_menu"
        raise AdapterError(
            f"sartorius {self.name!r}: unknown command kind {kind!r}",
            device=self.name,
        )

    async def tare(
        self,
        *,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Tare the balance to zero. Authorization rules match :meth:`command`."""
        return await self.command(
            DeviceCommand(
                kind="tare",
                payload={},
                issued_by=issued_by,
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )

    async def zero(
        self,
        *,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Re-zero the balance (distinct from tare). Authorization rules match :meth:`command`."""
        return await self.command(
            DeviceCommand(
                kind="zero",
                payload={},
                issued_by=issued_by,
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )

    async def internal_adjust(
        self,
        *,
        issued_by: str,
        cal_type: int | None = None,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Run the balance's internal calibration / adjustment routine.

        The Sartorius motorized internal weight is the canonical
        start-of-day calibration; ``cal_type`` selects an external /
        linearization variant (``0x70..0x7B``). Most callers pass
        ``cal_type=None`` and accept the library default.
        """
        return await self.command(
            DeviceCommand(
                kind="internal_adjust",
                payload={"cal_type": cal_type},
                issued_by=issued_by,
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )

    async def set_filter_mode(
        self,
        mode: str | int,
        *,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Write parameter ``p01`` (filter mode). Persists to EEPROM only after
        a subsequent ``save_menu`` — this call writes to the runtime menu."""
        return await self.command(
            DeviceCommand(
                kind="set_filter_mode",
                target="filter_mode",
                payload={"mode": mode},
                issued_by=issued_by,
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )

    async def set_display_unit(
        self,
        unit: str | int,
        *,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Write parameter ``p07`` (display unit)."""
        return await self.command(
            DeviceCommand(
                kind="set_display_unit",
                target="display_unit",
                payload={"unit": unit},
                issued_by=issued_by,
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )

    async def set_auto_zero(
        self,
        mode: str | int,
        *,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Write parameter ``p06`` (auto-zero tracking)."""
        return await self.command(
            DeviceCommand(
                kind="set_auto_zero",
                target="auto_zero",
                payload={"mode": mode},
                issued_by=issued_by,
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )

    async def save_menu(
        self,
        *,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Persist the runtime menu to EEPROM (xBPI ``0x47``).

        audit: a single ``save_menu`` may persist many prior
        parameter writes, so the audit trail captures the save as a
        distinct authorized event.
        """
        return await self.command(
            DeviceCommand(
                kind="save_menu",
                payload={},
                issued_by=issued_by,
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )

    async def reload_menu(
        self,
        *,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Reload the saved menu from EEPROM (xBPI ``0x46``)."""
        return await self.command(
            DeviceCommand(
                kind="reload_menu",
                payload={},
                issued_by=issued_by,
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )

    async def read_mass(self) -> Reading:
        """Read the current net weight (no authorization gate — read-only)."""
        if self._balance is None:
            raise AdapterError(
                f"sartorius {self.name!r} read_mass() requires open() first",
                device=self.name,
            )
        try:
            return await self._balance.poll()
        except SartoriusError as exc:
            raise AdapterError(
                f"sartorius {self.name!r} read_mass failed: {exc}", device=self.name
            ) from exc

    async def read_last_cal_record(self) -> CalRecord:
        """Read the last-calibration record (no authorization gate — read-only).

        The record carries the temperature at the last calibration and
        whether one is on record since the last cold boot — no timestamp.
        """
        if self._balance is None:
            raise AdapterError(
                f"sartorius {self.name!r} read_last_cal_record() requires open() first",
                device=self.name,
            )
        try:
            return await self._balance.last_cal_record()
        except SartoriusError as exc:
            raise AdapterError(
                f"sartorius {self.name!r} read_last_cal_record failed: {exc}",
                device=self.name,
            ) from exc

    async def read_state_snapshot(self) -> SartoriusStateSnapshot | None:
        """One-shot read of the menu settings and last calibration for the
        manual-control card.

        Returns ``None`` when no balance is open. Individual reads that fail
        (a parameter the balance family lacks, SBI framing) are captured as
        ``None`` fields rather than raising.

        No authorization gate (read-only). sartoriuslib serializes I/O on
        the port, so this won't interleave with the streaming recorder.
        """
        balance = self._balance
        if balance is None:
            return None
        filter_mode = await self._read_or_none("filter_mode", balance.get_filter_mode)
        raw_modes = {
            name: await self._read_or_none(name, functools.partial(self._read_mode, index))
            for name, index in _RAW_MODE_PARAMETERS.items()
        }
        auto_zero = await self._read_or_none("auto_zero", balance.get_auto_zero)
        display_unit = await self._read_or_none("display_unit", balance.get_display_unit)
        tare_behavior = await self._read_or_none("tare_behavior", balance.get_tare_behavior)
        cal = await self._read_or_none("last_cal_record", balance.last_cal_record)
        return SartoriusStateSnapshot(
            filter_mode=_mode_label(filter_mode),
            app_filter=_mode_label(raw_modes["app_filter"]),
            stability_range=_mode_label(raw_modes["stability_range"]),
            stability_delay=_mode_label(raw_modes["stability_delay"]),
            auto_zero=_mode_label(auto_zero),
            display_unit=_mode_label(display_unit),
            tare_behavior=_mode_label(tare_behavior),
            cal_temperature_c=cal.temperature_celsius if cal is not None else None,
            cal_on_record=cal.has_metadata if cal is not None else None,
        )

    async def _read_or_none[T](self, what: str, read: Callable[[], Awaitable[T]]) -> T | None:
        try:
            return await read()
        except SartoriusError as exc:
            _log.debug("sartorius %r read %s failed: %s", self.name, what, exc)
            return None

    async def _read_mode(self, index: int) -> Enum:
        """Decode a menu parameter sartoriuslib has no typed getter for."""
        assert self._balance is not None
        entry = await self._balance.read_parameter(index)
        return PARAMETER_TABLE[index].decode(entry.current)

    # ------------------------------------------------------------------ helpers

    async def _build_balance(self) -> Balance:
        """Construct the underlying :class:`Balance`.

        ``sartoriuslib.open_device`` internally swallows the well-known
        cold-open first-byte race (frame underrun / 0-byte read) with a
        bounded retry; capa does not need a retry loop here. Post-open
        transients surface as :class:`SartoriusTransientTransportError`
        and are handled by the stream loop, not by re-opening.
        """
        if self._balance_factory is not None:
            return await self._balance_factory()
        return await sartoriuslib.open_device(
            self.params.port,
            protocol=self.params.protocol_kind(),
            serial_settings=self.params.to_serial_settings(),
            timeout=self.params.timeout_s,
            src_sbn=self.params.src_sbn,
            dst_sbn=self.params.dst_sbn,
        )

    async def _safe_close_balance(self) -> None:
        if self._balance is None:
            return
        try:
            await self._balance.close()
        except SartoriusError:
            return

    def _record_for(self, sample: Any) -> SourceRecord:
        """Convert a sartoriuslib :class:`Sample` into a single-value-row :class:`SourceRecord`.

        Uses the library's own :func:`sartoriuslib.sinks.sample_to_row` helper
        so the row schema matches what an offline ``sartoriuslib`` recorder
        would produce.
        """
        clock = self._state.clock
        assert clock is not None
        row = sample_to_row(sample)
        t_mono_ns = sample.t_mono_ns - clock.started_mono_ns
        self._state.seq += 1
        protocol_str = sample.protocol.value if sample.protocol is not None else None
        return SourceRecord(
            record_id=make_record_id(ADAPTER_ID, self.name, self._state.seq),
            adapter=ADAPTER_ID,
            device=self.name,
            shape="single_value_row",
            t_mono_ns=t_mono_ns,
            t_utc=sample.t_utc,
            row=row,
            metadata={"protocol": protocol_str} if protocol_str is not None else {},
        )

    def _channel_samples_for(self, sample: Any, record_id: str) -> list[DeviceEmission]:
        """Map ``sample`` against the configured :class:`SartoriusReading` bindings."""
        clock = self._state.clock
        assert clock is not None
        reading = sample.reading
        if reading is None:
            return []
        t_mono_ns = sample.t_mono_ns - clock.started_mono_ns
        emissions: list[DeviceEmission] = []
        for spec in self._channels:
            binding = spec.source
            assert isinstance(binding, SartoriusReading)
            field = binding.field
            raw_value = self._extract_field(reading, field)
            if raw_value is None:
                continue
            status = "ok" if reading.stable else "settling"
            if reading.overload:
                status = "overload"
            elif reading.underload:
                status = "underrange"
            emissions.append(
                build_channel_sample(
                    spec=spec,
                    raw_value=float(raw_value),
                    t_mono_ns=t_mono_ns,
                    source_record_id=record_id,
                    source_field=field,
                    status=status,
                )
            )
        return emissions

    @staticmethod
    def _extract_field(reading: Reading, field: str) -> float | None:
        """Pull the requested field out of a :class:`Reading` for channel-sample
        derivation.

        ``"value"`` (the default and most common) returns the numeric mass
        reading. ``"stable"`` returns ``1.0`` / ``0.0`` so a procedure can
        watch settling. Other fields return ``None`` (caller drops the
        sample); the native row in ``device_records/`` still carries the
        full Reading dict.
        """
        if field == "value":
            return reading.value
        if field == "stable":
            return 1.0 if reading.stable else 0.0
        return None

    def watchdog_state(self) -> WatchdogState:
        """Return a compact silence-state view for tests and future policy work."""
        return self._state.watchdog(device=self.name, rate_hz=self.params.rate_hz)

    def _compute_health(self, *, clock: RunClock) -> DeviceHealth:
        """Derive the :class:`DeviceHealth` pill from adapter state.

        Mirrors the session's ``recoverable_error_count`` into the runtime
        state so the shared :meth:`AdapterRuntimeState.compute_health`
        logic still applies. The session is the sole writer; we only
        copy the value through here for the health pill.
        """
        if self._balance is not None:
            self._state.recoverable_error_count = self._balance.session.recoverable_error_count
        return self._state.compute_health(clock=clock, rate_hz=self.params.rate_hz)

    async def _snapshot_fields(self) -> dict[str, float | int | str | bool | None]:
        lib_snap = await self._balance.snapshot() if self._balance is not None else None
        info = self._device_info
        interval_min = (
            None if math.isinf(self._interval_min_ms) else round(self._interval_min_ms, 2)
        )
        recoverable = lib_snap.recoverable_error_count if lib_snap is not None else 0
        out: dict[str, float | int | str | bool | None] = {
            "protocol": self.params.protocol,
            "rate_hz": self.params.rate_hz,
            "channel_count": len(self._channels),
            "state": self._state.lifecycle.state,
            "recoverable_errors": recoverable,
            "wire_interval_min_ms": interval_min,
            "wire_interval_max_ms": round(self._interval_max_ms, 2)
            if self._interval_max_ms
            else None,
            "wire_interval_narrow_count": self._interval_narrow_count,
        }
        if interval_min is not None:
            expected_ms = 1000.0 / self.params.rate_hz
            _log.info(
                "sartorius %r wire-spacing: min=%.1f ms  max=%.1f ms  narrow(<75%% of %.0f ms)=%d",
                self.name,
                self._interval_min_ms,
                self._interval_max_ms,
                expected_ms,
                self._interval_narrow_count,
            )
        if lib_snap is not None:
            if lib_snap.family is not None:
                out["family"] = lib_snap.family.value
            out["lib_protocol"] = lib_snap.protocol.value
        if info is not None:
            out["model"] = info.model
            out["serial"] = info.serial
            out["manufacturer"] = info.manufacturer
            out["software"] = info.software
            if info.firmware is not None:
                out["firmware"] = str(info.firmware)
        return out


def _mode_label(value: Enum | None) -> str | None:
    """Card-facing label for a decoded menu value: a unit's own label, a
    mode's name as lowercase words. ``None`` for absent or ``UNKNOWN``."""
    if value is None or value.name == "UNKNOWN":
        return None
    if isinstance(value, Unit):
        return value.value
    return value.name.lower().replace("_", " ")


def _encode_mode(index: int, mode: str | int) -> int:
    """Wire byte for menu parameter ``index`` set to ``mode``.

    ``mode`` is a label as :func:`_mode_label` reports it (``"very
    accurate"``), the enum member's name, or the raw wire value. Raises
    :class:`ValueError` for anything else, including ``UNKNOWN``.
    """
    spec = PARAMETER_TABLE[index]
    enum_cls = spec.enum
    assert enum_cls is not None
    try:
        member = (
            enum_cls(mode)
            if isinstance(mode, int)
            else enum_cls[mode.strip().upper().replace(" ", "_")]
        )
    except (KeyError, ValueError):
        member = None
    if member is None or member.value == 0:
        choices = ", ".join(repr(m.name.lower().replace("_", " ")) for m in enum_cls if m.value)
        raise ValueError(f"unknown {spec.name} {mode!r}; expected one of {choices}")
    return spec.encode(member)


# ---------------------------------------------------------------------------
# CLI handshake hook (``capa validate --strict``)
# ---------------------------------------------------------------------------


async def handshake(params: dict[str, Any]) -> str:
    """Read-only open + identify + close. Used by ``capa validate --strict``."""
    parsed = SartoriusAdapterParams.model_validate(params)
    try:
        balance = await sartoriuslib.open_device(
            parsed.port,
            protocol=parsed.protocol_kind(),
            serial_settings=parsed.to_serial_settings(),
            timeout=parsed.timeout_s,
            src_sbn=parsed.src_sbn,
            dst_sbn=parsed.dst_sbn,
        )
        try:
            info = balance.info
        finally:
            await balance.close()
    except SartoriusError as exc:
        raise AdapterError(f"sartorius handshake failed at {parsed.port}: {exc}") from exc
    if info is None:
        return f"sartorius port={parsed.port} (no DeviceInfo cached — identify=False?)"
    fw = str(info.firmware) if info.firmware is not None else "?"
    return (
        f"sartorius model={info.model} serial={info.serial or '?'} "
        f"fw={fw} family={info.family.value} protocol={info.protocol.value}"
    )


# ---------------------------------------------------------------------------
# Discovery hook (``capa devices discover``)
# ---------------------------------------------------------------------------


async def discover(
    *,
    ports: list[str] | None = None,
    baudrates: tuple[int, ...] | None = None,
    timeout_s: float = 0.5,
) -> list[dict[str, Any]]:
    """Probe local serial ports for Sartorius balances, sweeping baudrates.

    Thin wrapper over :func:`sartoriuslib.find_devices`. Under the
    unified API ``find_devices`` returns one
    :class:`SartoriusDiscoveryResult` per (port × baudrate) probe;
    capa folds the per-probe rows into per-port summaries via
    :func:`sartoriuslib.summarize_discovery` so the operator sees
    one row per port that responded.
    """
    if ports is not None and not ports:
        return []

    try:
        results = await sartoriuslib.find_devices(
            ports=ports,
            baudrates=baudrates,
            per_probe_timeout_s=timeout_s,
        )
    except SartoriusError:
        return []

    summaries: list[DiscoverySummary] = summarize_discovery(results)
    out: list[dict[str, Any]] = []
    for summary in summaries:
        if not summary.ok or summary.protocol is None:
            continue
        out.append(
            {
                "adapter": ADAPTER_ID,
                "port": summary.port,
                "protocol": summary.protocol.value,
                "baudrate": summary.baudrate,
                "autoprint_active": summary.autoprint_active,
            }
        )
    return out


__all__ = [
    "ADAPTER_ID",
    "DESCRIPTOR",
    "SARTORIUS_SETTINGS",
    "SartoriusAdapter",
    "SartoriusAdapterParams",
    "SartoriusSettings",
    "SartoriusStateSnapshot",
    "discover",
    "handshake",
]


def _build_descriptor() -> AdapterDescriptor:
    from capa.devices._templates import SARTORIUS_MASS  # noqa: PLC0415
    from capa.devices.adapter import Capability  # noqa: PLC0415
    from capa.devices.registry import AdapterDescriptor  # noqa: PLC0415

    return AdapterDescriptor(
        id="capa.devices.sartorius",
        label="Sartorius balance",
        family="sartorius",
        adapter_factory=SartoriusAdapter,
        params_model=SartoriusAdapterParams,
        supported_binding_sources=("sartorius_reading",),
        default_params={"rate_hz": 50.0},
        channel_templates=(SARTORIUS_MASS,),
        discoverable=True,
        handshake_available=True,
        capabilities=frozenset(
            {
                Capability.HAS_TARE,
                Capability.HAS_ZERO,
                Capability.EMITS_STABILITY_FLAG,
                Capability.HAS_INTERNAL_CAL,
                Capability.HAS_PARAMETER_CONFIG,
            }
        ),
        settings=SARTORIUS_SETTINGS,
    )


DESCRIPTOR = _build_descriptor()

from capa.devices.registry import register as _register  # noqa: E402

_register(DESCRIPTOR)
