"""Real :class:`SartoriusAdapter` — wraps a :class:`sartoriuslib.Balance` (P2).

Plan §16 P2 entry: "real ``SartoriusAdapter``. Capability flags. Device
watchdogs and health surfacing. Discovery (``capa devices discover``).
``capa validate --strict``."

Architecture (plan §5.2 / §5.6 / §7.2):

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
* ``command`` enforces the authorization gate (plan §9) and dispatches
  :meth:`Balance.tare` / :meth:`Balance.zero`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Final, Literal

import anyio
import sartoriuslib
from pydantic import BaseModel, ConfigDict, Field
from sartoriuslib.devices.balance import Balance
from sartoriuslib.devices.models import CalRecord, DeviceInfo, Reading
from sartoriuslib.errors import SartoriusError
from sartoriuslib.manager import DeviceResult
from sartoriuslib.protocol.base import ProtocolKind
from sartoriuslib.sinks.base import sample_to_row
from sartoriuslib.streaming import OverflowPolicy
from sartoriuslib.streaming.recorder import record as sartorius_record
from sartoriuslib.transport.base import SerialSettings

from capa.channels.spec import ChannelSpec, SartoriusReading
from capa.core.clock import RunClock
from capa.core.errors import AdapterError
from capa.devices._helpers import (
    LastSampleTracker,
    WatchdogState,
    build_channel_sample,
    channels_for_device,
    make_accepted_result,
    make_not_open_result,
    make_record_id,
    reject_unless_authorized,
)
from capa.devices.adapter import (
    AdapterLifecycle,
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

ADAPTER_ID: Final[str] = "sartorius"

COLD_OPEN_RETRY_ATTEMPTS: Final[int] = 3
"""Total ``_build_balance`` attempts on cold open. Hardware-day §3.4 saw a
single first-byte race after a fresh USB plug; subsequent attempts succeeded.
Three attempts × the backoff schedule below caps the worst-case open at
~1.4 s before giving up — acceptable for a startup-phase operation."""

COLD_OPEN_RETRY_BACKOFF_S: Final[tuple[float, ...]] = (0.2, 0.4, 0.8)
"""Backoff between cold-open attempts. Used positionally — index ``i`` is
the sleep BEFORE attempt ``i+1`` (so attempt 1 has no preceding sleep)."""

_COLD_OPEN_RACE_MARKERS: Final[tuple[str, ...]] = (
    "frame too short",
    "got 0 bytes",
)
"""Substrings that identify the well-known cold-open race in
``sartoriuslib``. Other ``SartoriusError`` shapes (checksum, timeout, bad
device id) re-raise immediately without retry — they're not transient."""


def _is_cold_open_race(exc: SartoriusError) -> bool:
    """True iff ``exc``'s message matches a known transient cold-open race.

    String matching is brittle; a future :class:`sartoriuslib.errors`
    shape with a typed ``FrameTooShortError`` would replace this. Until
    then, the substring set above is the contract with upstream.
    """
    msg = str(exc).lower()
    return any(marker in msg for marker in _COLD_OPEN_RACE_MARKERS)


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

    Plan §5.4: adapter-specific knobs live under ``DeviceConfig.params`` and are
    parsed by the adapter at construction time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    port: str
    """Serial-port path: ``/dev/ttyUSB0`` (Linux), ``COM3`` (Windows)."""

    protocol: ProtocolName = "xbpi"
    """Wire protocol. ``auto`` runs the conservative detector
    (sartoriuslib §6); pin one in production."""

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
    """Recorder overflow policy. ``BLOCK`` matches plan §7.1."""

    def to_serial_settings(self) -> SerialSettings:
        """Build the :class:`SerialSettings` sartoriuslib expects."""
        return SerialSettings(port=self.port, baudrate=self.baudrate)

    def protocol_kind(self) -> ProtocolKind:
        return _PROTOCOL_BY_NAME[self.protocol]

    def overflow_policy(self) -> OverflowPolicy:
        return OverflowPolicy.BLOCK if self.overflow == "block" else OverflowPolicy.DROP_NEWEST


# ---------------------------------------------------------------------------
# Single-device PollSource — adapts one Balance to the recorder's shape.
# ---------------------------------------------------------------------------


class _SingleDevicePollSource:
    """Wrap one :class:`Balance` as the recorder's
    :class:`sartoriuslib.streaming.recorder.PollSource`.

    The recorder expects a ``Mapping[str, DeviceResult[Reading]]`` per tick.
    Errors land in the mapping with ``value=None`` and ``error`` set; the
    recorder builds a :class:`Sample` with ``reading=None`` from that, which
    the adapter treats as a missed tick.
    """

    __slots__ = ("_balance", "_name")

    def __init__(self, name: str, balance: Balance) -> None:
        self._name = name
        self._balance = balance

    async def poll(self, names: Any = None) -> Mapping[str, DeviceResult[Reading]]:
        del names  # single-device, name filter is a no-op
        try:
            reading = await self._balance.poll()
        except SartoriusError as exc:
            return {self._name: DeviceResult(value=None, error=exc)}
        return {self._name: DeviceResult(value=reading, error=None)}


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
        "_clock",
        "_cold_open_retry_count",
        "_device_info",
        "_last_sample",
        "_last_snapshot_t_mono_ns",
        "_lifecycle",
        "_recoverable_error_count",
        "_seq",
        "_stop_requested",
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
        self._clock: RunClock | None = None
        self._lifecycle = AdapterLifecycle()
        self._seq = 0
        self._last_snapshot_t_mono_ns = -(2**62)
        self._last_sample = LastSampleTracker()
        self._recoverable_error_count = 0
        self._stop_requested = False
        self._cold_open_retry_count = 0

    # ------------------------------------------------------------------ wiring

    def configure_channels(self, specs: list[ChannelSpec]) -> None:
        """Bind to :class:`SartoriusReading`-bound channels for this device."""
        self._channels = channels_for_device(
            specs, device=self.name, binding_source="sartorius_reading"
        )

    @property
    def expected_emission_rate_hz(self) -> float:
        # One SourceRecord + one ChannelSample per bound channel per poll.
        return self.params.rate_hz * (1 + len(self._channels))

    @property
    def device_info(self) -> DeviceInfo | None:
        """The cached :class:`sartoriuslib.DeviceInfo` from :meth:`open`."""
        return self._device_info

    # ------------------------------------------------------------------ lifecycle

    async def open(self) -> None:
        """Open the underlying transport and identify the balance.

        Idempotent: a second call on an already-open adapter is a no-op.
        """
        if self._lifecycle.state in ("open", "running"):
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
        self._lifecycle.open()

    async def close(self) -> None:
        """Release the bus / handle. Idempotent."""
        if self._lifecycle.state == "closed":
            return
        await self._safe_close_balance()
        self._balance = None
        self._lifecycle.close()

    async def start(self, clock: RunClock | None = None) -> None:
        """Capture the :class:`RunClock` anchor and arm the streaming loop."""
        self._lifecycle.start()
        self._clock = clock or RunClock.now()
        self._stop_requested = False
        self._last_sample.reset()
        self._recoverable_error_count = 0
        self._last_snapshot_t_mono_ns = -(2**62)

    async def stop(self) -> None:
        """Request the streaming loop to exit cleanly. Idempotent."""
        if self._lifecycle.state != "running":
            return
        self._stop_requested = True
        self._lifecycle.stop()

    async def snapshot(self) -> DeviceSnapshot:
        """Build a :class:`DeviceSnapshot` from cached identity + live health."""
        clock = self._clock or RunClock.now()
        return DeviceSnapshot(
            adapter=ADAPTER_ID,
            device=self.name,
            t_mono_ns=clock.t_mono_ns(),
            t_utc=datetime.now(UTC),
            healthy=self._lifecycle.state in ("open", "running"),
            health=self._compute_health(clock=clock),
            fields=self._snapshot_fields(),
        )

    # ------------------------------------------------------------------ stream

    async def stream(self) -> AsyncIterator[DeviceEmission]:
        """Yield :class:`DeviceEmission`\\ s while sampling is active.

        Drives :func:`sartoriuslib.streaming.record` against a single-device
        :class:`_SingleDevicePollSource`. Successful polls yield one
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
        if self._clock is None:
            raise AdapterError(
                f"sartorius {self.name!r} stream() requires start() first",
                device=self.name,
            )

        snap = await self.snapshot()
        self._last_snapshot_t_mono_ns = snap.t_mono_ns
        yield snap

        source = _SingleDevicePollSource(self.name, self._balance)

        try:
            async with sartorius_record(
                source,
                rate_hz=self.params.rate_hz,
                overflow=self.params.overflow_policy(),
                buffer_size=64,
            ) as batches:
                async for batch in batches:
                    if self._stop_requested:
                        break
                    sample = batch.get(self.name)
                    if sample is None:
                        # Source returned no row at all — recorder oddity;
                        # treat as missed tick.
                        self._recoverable_error_count += 1
                        continue
                    record = self._record_for(sample)
                    yield record
                    self._last_sample.mark(record.t_mono_ns)
                    if sample.reading is None:
                        # Error sample — native row is preserved (with
                        # error_type / error_message), but no calibrated
                        # ChannelSample can be derived.
                        self._recoverable_error_count += 1
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
                    if self._snapshot_due():
                        snap = await self.snapshot()
                        self._last_snapshot_t_mono_ns = snap.t_mono_ns
                        yield snap
        except* SartoriusError as eg:
            first = next(iter(eg.exceptions))
            raise AdapterError(
                f"sartorius {self.name!r} stream failed: {first}", device=self.name
            ) from first

    # ------------------------------------------------------------------ commands

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        """Issue a generic command. Authorization gate first, then dispatch."""
        clock = self._clock or RunClock.now()
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

        Plan §9 audit: a single ``save_menu`` may persist many prior
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
        """Read the last-calibration snapshot (no authorization gate — read-only).

        Used by the manual-control panel to display when the balance was last
        calibrated and what the result was.
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

    # ------------------------------------------------------------------ helpers

    async def _build_balance(self) -> Balance:
        """Construct the underlying :class:`Balance`, retrying past the
        cold-open first-byte race.

        Hardware-day §3.4: a fresh USB plug consistently produced
        ``frame too short: got 1 bytes (min 4)`` on the first identify;
        every retry succeeded. The retry policy here is bounded — see
        :data:`COLD_OPEN_RETRY_ATTEMPTS` and
        :data:`COLD_OPEN_RETRY_BACKOFF_S`. Non-cold-open ``SartoriusError``
        shapes (checksum, timeout, bad-device-id) re-raise immediately.
        """
        last_exc: SartoriusError | None = None
        for attempt in range(COLD_OPEN_RETRY_ATTEMPTS):
            try:
                return await self._build_balance_once()
            except SartoriusError as exc:
                if not _is_cold_open_race(exc):
                    raise
                last_exc = exc
                self._cold_open_retry_count += 1
                if attempt + 1 < COLD_OPEN_RETRY_ATTEMPTS:
                    await anyio.sleep(COLD_OPEN_RETRY_BACKOFF_S[attempt])
        assert last_exc is not None
        raise last_exc

    async def _build_balance_once(self) -> Balance:
        """One open attempt — the test seam path or the real
        :func:`sartoriuslib.open_device` call.
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
        assert self._clock is not None
        row = sample_to_row(sample)
        t_mono_ns = sample.monotonic_ns - self._clock.started_mono_ns
        self._seq += 1
        protocol_str = sample.protocol.value if sample.protocol is not None else None
        return SourceRecord(
            record_id=make_record_id(ADAPTER_ID, self.name, self._seq),
            adapter=ADAPTER_ID,
            device=self.name,
            shape="single_value_row",
            t_mono_ns=t_mono_ns,
            t_utc=sample.midpoint_at,
            row=row,
            metadata={"protocol": protocol_str} if protocol_str is not None else {},
        )

    def _channel_samples_for(self, sample: Any, record_id: str) -> list[DeviceEmission]:
        """Map ``sample`` against the configured :class:`SartoriusReading` bindings."""
        assert self._clock is not None
        reading = sample.reading
        if reading is None:
            return []
        t_mono_ns = sample.monotonic_ns - self._clock.started_mono_ns
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
        """Watchdog view for the engine's silent-device task (plan §13.2)."""
        return WatchdogState(
            device=self.name,
            last_t_mono_ns=self._last_sample.last_t_mono_ns,
            expected_period_ns=int(1e9 / self.params.rate_hz),
            lifecycle_state=self._lifecycle.state,
        )

    def _snapshot_due(self) -> bool:
        if self._clock is None:
            return False
        elapsed_ns = self._clock.t_mono_ns() - self._last_snapshot_t_mono_ns
        return elapsed_ns >= int(self.params.snapshot_period_s * 1e9)

    def _compute_health(self, *, clock: RunClock) -> DeviceHealth:
        """Derive the :class:`DeviceHealth` pill from adapter state."""
        if self._lifecycle.state == "closed":
            return "down"
        if self._lifecycle.state == "open":
            return "ok"
        if self._recoverable_error_count > 0:
            return "degraded"
        age_ns = self._last_sample.age_ns(now_t_mono_ns=clock.t_mono_ns())
        if age_ns is not None:
            stale_threshold_ns = int(3.0 * (1e9 / self.params.rate_hz))
            if age_ns > stale_threshold_ns:
                return "degraded"
        return "ok"

    def _snapshot_fields(self) -> dict[str, float | int | str | bool | None]:
        info = self._device_info
        out: dict[str, float | int | str | bool | None] = {
            "protocol": self.params.protocol,
            "rate_hz": self.params.rate_hz,
            "channel_count": len(self._channels),
            "state": self._lifecycle.state,
            "recoverable_errors": self._recoverable_error_count,
        }
        if info is not None:
            out["model"] = info.model
            out["serial"] = info.serial
            out["manufacturer"] = info.manufacturer
            out["family"] = info.family.value
            out["software"] = info.software
            if info.firmware is not None:
                out["firmware"] = str(info.firmware)
        return out


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


async def discover(*, ports: list[str] | None = None) -> list[dict[str, Any]]:
    """Probe local serial ports for Sartorius balances.

    Wraps :func:`sartoriuslib.discover_port` for each visible port. The
    library's discover only resolves the wire protocol (xBPI vs SBI) — it
    does not identify the balance. Use ``capa validate --strict`` after
    wiring the device into a config to get model / serial / firmware.

    Returns one dict per port that responded with a recognised protocol.
    """
    if ports is None:
        # sartoriuslib does not (yet) ship a shared ``list_serial_ports``;
        # alicatlib does. Fall back to anyserial directly to avoid a hard
        # cross-library dep.
        try:
            import anyserial  # noqa: PLC0415
        except ImportError:
            return []
        ports = [p.device for p in await anyserial.list_serial_ports()]

    out: list[dict[str, Any]] = []
    for port in ports:
        try:
            result = await sartoriuslib.discover_port(port)
        except SartoriusError:
            continue
        if not result.ok or result.protocol is None:
            continue
        out.append(
            {
                "adapter": ADAPTER_ID,
                "port": port,
                "protocol": result.protocol.value,
                "baudrate": result.baudrate,
                "autoprint_active": result.autoprint_active,
            }
        )
    return out


__all__ = [
    "ADAPTER_ID",
    "SartoriusAdapter",
    "SartoriusAdapterParams",
    "discover",
    "handshake",
]
