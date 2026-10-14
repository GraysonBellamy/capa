"""Real :class:`Sht4xAdapter` — a Sensirion SHT4x ambient temperature /
humidity sensor behind an MCP2221 USB-I2C bridge.

The SHT40, SHT41 and SHT45 share one protocol; the chip cannot say which
it is, so the adapter is named for the family. Everything on the wire is
in this module: the sensor speaks six single-byte commands, answers with
two CRC-protected 16-bit words, and that is the whole device. The bridge
side lives in :mod:`capa.devices._mcp2221`.

Architecture:

* ``open`` claims the bridge (cancel any stale transfer, set the bus
  speed), soft-resets the sensor and reads its serial number.
* ``start`` captures the run :class:`RunClock`; ``stream`` then polls at
  ``rate_hz``. Each poll is one measure command, a wait of the
  precision's maximum conversion time, and one 6-byte read — about
  15 ms over HID — run on a worker thread via
  :func:`anyio.to_thread.run_sync` so the loop stays free. Every poll
  yields one :class:`SourceRecord` (``shape="wide_row"``: both values,
  both raw words, latency) plus one :class:`ChannelSample` per bound
  :class:`Sht4xReading`.
* A failed poll (NACK, CRC, bridge I/O) keeps its row with the error
  fields populated, emits no channel samples, and counts as a
  recoverable error. With ``auto_reconnect`` a bridge I/O failure also
  reopens the bridge after a short back-off.
* ``command`` enforces the authorization gate and dispatches
  ``set_precision``, ``heater_pulse`` and ``soft_reset``. The heater is
  the one write that changes what the sensor reports: a pulse warms the
  element for up to a second and the reading stays skewed for a while
  after, so it is refused above the datasheet's ambient limit and held
  to a ten-percent duty cycle.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal

import anyio
from pydantic import BaseModel, ConfigDict, Field

from capa.channels.spec import ChannelSpec, Sht4xReading
from capa.core.clock import RunClock
from capa.core.errors import AdapterError
from capa.devices._helpers import (
    WatchdogState,
    build_channel_sample,
    channels_for_device,
    hid_resource_id,
    make_accepted_result,
    make_not_open_result,
    make_record_id,
    reject_unless_authorized,
)
from capa.devices._mcp2221 import (
    MAX_I2C_SPEED_HZ,
    MIN_I2C_SPEED_HZ,
    BridgeInfo,
    HidOpener,
    I2cNackError,
    Mcp2221Error,
    Mcp2221I2cBus,
    enumerate_bridges,
    open_hid_handle,
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
from capa.devices.settings import DeviceSettingsSpec, SettingField

if TYPE_CHECKING:
    from capa.devices.registry import AdapterDescriptor

_log = logging.getLogger(__name__)

ADAPTER_ID: Final[str] = "sht4x"


# ---------------------------------------------------------------------------
# Sensor protocol — Sensirion SHT4x datasheet, section 4.
# ---------------------------------------------------------------------------


Precision = Literal["high", "medium", "low"]
"""Measurement repeatability setting. Higher precision is a longer
conversion (8.3 / 4.5 / 1.7 ms) and more of the sensor's self-heating."""

PRECISIONS: Final[tuple[Precision, ...]] = ("high", "medium", "low")

HEATER_POWERS_MW: Final[tuple[int, ...]] = (200, 110, 20)
"""Heater powers the sensor offers, in milliwatts."""
HEATER_DURATIONS_S: Final[tuple[float, ...]] = (1.0, 0.1)
"""Heater pulse lengths the sensor offers, in seconds."""

HEATER_MAX_AMBIENT_C: Final[float] = 65.0
"""The datasheet allows heater use only below this ambient temperature."""

HEATER_MAX_DUTY: Final[float] = 0.10
"""Heater-on time may be at most this fraction of elapsed time: after a
pulse of ``d`` seconds the next one waits ``d / HEATER_MAX_DUTY - d``."""

_MEASURE_COMMANDS: Final[dict[str, tuple[int, float]]] = {
    "high": (0xFD, 0.0083),
    "medium": (0xF6, 0.0045),
    "low": (0xE0, 0.0017),
}
"""precision → (command byte, maximum conversion time in seconds)."""

_HEATER_COMMANDS: Final[dict[tuple[int, float], tuple[int, float]]] = {
    (200, 1.0): (0x39, 1.1),
    (200, 0.1): (0x32, 0.11),
    (110, 1.0): (0x2F, 1.1),
    (110, 0.1): (0x24, 0.11),
    (20, 1.0): (0x1E, 1.1),
    (20, 0.1): (0x15, 0.11),
}
"""(power mW, duration s) → (command byte, maximum time until the
high-precision measurement taken at the end of the pulse is ready)."""

_CMD_READ_SERIAL: Final = 0x89
_CMD_SOFT_RESET: Final = 0x94
_SOFT_RESET_S: Final = 0.001
_RESPONSE_LEN: Final = 6
_READ_RETRY_S: Final = 0.001
_READ_RETRY_BUDGET_S: Final = 0.02
"""Extra time to keep retrying a NACKed read after the datasheet maximum
has elapsed, before the poll counts as failed."""
_RECONNECT_BACKOFF_S: Final = 1.0


class Sht4xError(Exception):
    """A sensor-level failure: bad CRC, or no answer from the sensor."""


class Sht4xCrcError(Sht4xError):
    """A word's checksum did not match: a corrupted transfer."""


def crc8(data: bytes) -> int:
    """Sensirion's CRC-8: polynomial 0x31, init 0xFF, no reflection."""
    crc = 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def decode_words(payload: bytes) -> tuple[int, int]:
    """Split a 6-byte response into its two 16-bit words, checking both CRCs."""
    if len(payload) != _RESPONSE_LEN:
        raise Sht4xError(f"expected {_RESPONSE_LEN} bytes from the sensor, got {len(payload)}")
    first, second = payload[0:2], payload[3:5]
    if crc8(first) != payload[2] or crc8(second) != payload[5]:
        raise Sht4xCrcError(f"checksum mismatch in {payload.hex()}")
    return int.from_bytes(first, "big"), int.from_bytes(second, "big")


def convert_temperature(raw: int) -> float:
    """Raw temperature word → °C (datasheet 4.6)."""
    return -45.0 + 175.0 * raw / 65535.0


def convert_humidity(raw: int) -> float:
    """Raw humidity word → %RH, clamped to the physical range. The
    datasheet notes values a little outside 0–100 can occur at the ends
    of the range and are to be clipped; the raw word keeps the overshoot."""
    value = -6.0 + 125.0 * raw / 65535.0
    return min(max(value, 0.0), 100.0)


def raw_from_temperature(temperature_c: float) -> int:
    """Inverse of :func:`convert_temperature`, for the simulator."""
    return min(max(round((temperature_c + 45.0) / 175.0 * 65535.0), 0), 65535)


def raw_from_humidity(humidity_pct: float) -> int:
    """Inverse of :func:`convert_humidity`, for the simulator."""
    return min(max(round((humidity_pct + 6.0) / 125.0 * 65535.0), 0), 65535)


@dataclass(frozen=True, slots=True)
class Measurement:
    """One temperature / humidity pair as the sensor reported it."""

    temperature_c: float
    humidity_pct: float
    temperature_raw: int
    humidity_raw: int
    precision: str
    """``"high"`` / ``"medium"`` / ``"low"``; a heater pulse's reading is
    always ``"high"``."""
    latency_s: float
    """Wall time from the measure command to the data in hand."""

    @classmethod
    def from_payload(cls, payload: bytes, *, precision: str, latency_s: float) -> Measurement:
        """Decode a 6-byte sensor response."""
        temperature_raw, humidity_raw = decode_words(payload)
        return cls(
            temperature_c=convert_temperature(temperature_raw),
            humidity_pct=convert_humidity(humidity_raw),
            temperature_raw=temperature_raw,
            humidity_raw=humidity_raw,
            precision=precision,
            latency_s=latency_s,
        )


def sht4x_row(
    measurement: Measurement | None,
    *,
    precision: str,
    latency_s: float,
    error: BaseException | None = None,
) -> dict[str, float | int | str | bool | None]:
    """The library-native wide row for one poll, shared with the simulator.

    A failed poll keeps every column, with the values ``None`` and the
    error fields populated, so ``device_records/sht4x.parquet`` records
    the outage in place.
    """
    return {
        "temperature_c": measurement.temperature_c if measurement else None,
        "humidity_pct": measurement.humidity_pct if measurement else None,
        "temperature_raw": measurement.temperature_raw if measurement else None,
        "humidity_raw": measurement.humidity_raw if measurement else None,
        "precision": precision,
        "latency_ms": round(latency_s * 1e3, 3),
        "error_type": type(error).__name__ if error is not None else None,
        "error_message": str(error) if error is not None else None,
    }


# ---------------------------------------------------------------------------
# Adapter params (Pydantic) — what shows up under ``[devices.params]`` in TOML.
# ---------------------------------------------------------------------------


class Sht4xAdapterParams(BaseModel):
    """Per-device adapter configuration for an SHT4x on an MCP2221."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    serial: str | None = None
    """USB serial of the MCP2221 to use. ``None`` opens the first bridge
    found, which is the only option until serial-number enumeration is
    enabled in the chip's flash (off by default)."""

    i2c_address: int = Field(ge=0x08, le=0x77, default=0x44)
    """The sensor's 7-bit address: ``0x44`` for SHT40-A / SHT41-A / SHT45,
    ``0x45`` for the SHT40-B / SHT41-B variants, ``0x46`` for SHT40-C."""

    precision: Precision = "high"
    """Measurement repeatability the stream starts with; ``set_precision``
    changes it live and an experiment's ``device_settings`` can declare it."""

    rate_hz: float = Field(gt=0, le=10.0, default=1.0)
    """Polling cadence. Capped at 10 Hz: the datasheet keeps the
    measurement duty cycle under ten percent to limit self-heating, and a
    high-precision conversion is 8.3 ms."""

    i2c_speed_hz: int = Field(ge=MIN_I2C_SPEED_HZ, le=MAX_I2C_SPEED_HZ, default=100_000)
    """Bus clock. The sensor accepts up to 1 MHz; the bridge tops out at 400 kHz."""

    snapshot_period_s: float = Field(gt=0, default=30.0)
    """Cadence of :class:`DeviceSnapshot` emissions during a run."""

    auto_reconnect: bool = True
    """When ``True`` a failed poll does not end the stream: the row keeps
    its error fields, the degradation counter increments, and a bridge
    I/O failure reopens the bridge after a back-off."""

    def measure_period_s(self) -> float:
        """Seconds between polls."""
        return 1.0 / self.rate_hz


# ---------------------------------------------------------------------------
# State snapshot + declarative settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Sht4xStateSnapshot:
    """What the manual card and ``device_settings`` read back.

    Built by :meth:`Sht4xAdapter.read_state_snapshot` on the worker loop.
    """

    precision: str | None = None
    """The precision the stream measures at."""

    sensor_serial: str | None = None
    """The sensor's 32-bit serial as ``0x…``; ``None`` before :meth:`open`."""

    bridge_serial: str | None = None
    """The MCP2221's USB serial, when the chip exposes one."""

    bridge_firmware: str | None = None
    """MCP2221 hardware and firmware revision, e.g. ``"A6 / 1.2"``."""

    last_temperature_c: float | None = None
    last_humidity_pct: float | None = None
    """The most recent good reading, from the stream or a one-shot read."""

    heater_pulses: int = 0
    """Heater pulses issued since the adapter was constructed."""

    seconds_since_heater_pulse: float | None = None
    """Age of the last pulse's end; ``None`` if none has been issued."""


class Sht4xSettings(BaseModel):
    """What an experiment can declare for an SHT4x under ``device_settings:``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    precision: Precision | None = Field(
        default=None,
        json_schema_extra={
            "capa_help": (
                "Measurement repeatability: high (8.3 ms conversion, the default), "
                "medium or low. Lower precision means less self-heating at high rates."
            )
        },
    )


SHT4X_SETTINGS: Final = DeviceSettingsSpec(
    model=Sht4xSettings,
    fields=(
        SettingField(
            name="precision",
            label="Measurement precision",
            current=lambda snapshot: snapshot.precision,
            command=lambda want, _snapshot: ("set_precision", {"precision": want}),
        ),
    ),
    snapshot_type=Sht4xStateSnapshot,
)
"""Shared by :class:`Sht4xAdapter` and the simulator."""


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


BusFactory = Callable[[Sht4xAdapterParams], Mcp2221I2cBus]
"""Test seam: builds the (unopened) bridge the adapter will drive. Production
leaves it ``None`` and the adapter builds a :class:`Mcp2221I2cBus` itself."""


def _default_bus_factory(params: Sht4xAdapterParams) -> Mcp2221I2cBus:
    return Mcp2221I2cBus(serial=params.serial, i2c_speed_hz=params.i2c_speed_hz)


class Sht4xAdapter:
    """Real SHT4x adapter (one sensor on one bridge per instance).

    Two construction shapes:

    * ``Sht4xAdapter(name=..., **params_kwargs)`` — engine path; per-device
      params from ``DeviceConfig.params`` are forwarded as kwargs and parsed
      into :class:`Sht4xAdapterParams`.
    * ``Sht4xAdapter(name=..., params=Sht4xAdapterParams(...))`` —
      programmatic path used by tests.

    Both shapes accept an optional ``bus_factory`` kwarg as a test seam.
    """

    __slots__ = (
        "_bus",
        "_bus_factory",
        "_bus_lock",
        "_channels",
        "_events",
        "_heater_free_at",
        "_heater_pulses",
        "_last_heater_end",
        "_last_measurement",
        "_precision",
        "_sensor_serial",
        "_state",
        "capabilities",
        "name",
        "params",
    )

    name: str
    params: Sht4xAdapterParams
    capabilities: frozenset[Capability]

    def __init__(
        self,
        *,
        name: str,
        params: Sht4xAdapterParams | None = None,
        bus_factory: BusFactory | None = None,
        **params_kwargs: Any,
    ) -> None:
        if params is not None and params_kwargs:
            raise TypeError("Sht4xAdapter accepts either `params=` or per-field kwargs, not both")
        if params is None:
            params = Sht4xAdapterParams.model_validate(params_kwargs)
        self.name = name
        self.params = params
        flags: set[Capability] = {
            Capability.HAS_PARAMETER_CONFIG,
            Capability.HAS_SENSOR_HEATER,
            Capability.SUPPORTS_DISCOVERY,
        }
        if params.auto_reconnect:
            flags.add(Capability.SUPPORTS_AUTO_RECONNECT)
        self.capabilities = frozenset(flags)
        self._bus_factory: BusFactory = bus_factory or _default_bus_factory
        self._bus: Mcp2221I2cBus | None = None
        # Created at open(), on the worker loop: an anyio lock binds to the
        # backend it is first used under.
        self._bus_lock: anyio.Lock | None = None
        self._channels: list[ChannelSpec] = []
        self._state = AdapterRuntimeState()
        self._precision: str = params.precision
        self._sensor_serial: str | None = None
        self._last_measurement: Measurement | None = None
        self._events: list[DeviceEvent] = []
        self._heater_pulses: int = 0
        self._last_heater_end: float | None = None
        self._heater_free_at: float = 0.0

    # ------------------------------------------------------------------ wiring

    def configure_channels(self, specs: list[ChannelSpec]) -> None:
        """Bind to :class:`Sht4xReading`-bound channels for this device."""
        self._channels = channels_for_device(
            specs, device=self.name, binding_source="sht4x_reading"
        )

    @property
    def expected_emission_rate_hz(self) -> float:
        """Emission rate hint for queue sizing. See :class:`~capa.devices.adapter.DeviceAdapter`."""
        return self.params.rate_hz * (1 + len(self._channels))

    @property
    def resource_id(self) -> str:
        """Stable contention-domain identifier: the bridge. See :class:`~capa.devices.adapter.DeviceAdapter`."""
        return hid_resource_id(self.params.serial)

    @property
    def precision(self) -> str:
        """The precision the stream measures at (``set_precision`` changes it)."""
        return self._precision

    @property
    def sensor_serial(self) -> str | None:
        """The sensor's serial number as ``0x…``, read at :meth:`open`."""
        return self._sensor_serial

    @property
    def last_measurement(self) -> Measurement | None:
        """The most recent good reading, from the stream or a one-shot read."""
        return self._last_measurement

    # ------------------------------------------------------------------ lifecycle

    async def open(self) -> None:
        """Claim the bridge, reset the sensor and read its serial number.

        Idempotent: a second call on an already-open adapter is a no-op.
        """
        if self._state.lifecycle.state in ("open", "running"):
            return
        if self._bus_lock is None:
            self._bus_lock = anyio.Lock()
        bus = self._bus_factory(self.params)
        try:
            await anyio.to_thread.run_sync(bus.open)
            self._sensor_serial = await anyio.to_thread.run_sync(self._identify_blocking, bus)
        except (Mcp2221Error, Sht4xError) as exc:
            await anyio.to_thread.run_sync(bus.close)
            raise AdapterError(f"sht4x {self.name!r} open failed: {exc}", device=self.name) from exc
        self._bus = bus
        self._state.lifecycle.open()

    async def close(self) -> None:
        """Release the bridge. Idempotent. The sensor needs no safe state: it
        has no outputs, and the heater is only ever pulsed."""
        if self._state.lifecycle.state == "closed":
            return
        bus, self._bus = self._bus, None
        if bus is not None:
            await anyio.to_thread.run_sync(bus.close)
        self._state.lifecycle.close()

    async def start(self, ctx: AdapterStartContext) -> None:
        """Capture the :class:`RunClock` anchor and arm the streaming loop."""
        self._state.on_start(ctx.clock)

    async def stop(self) -> None:
        """Request the streaming loop to exit cleanly. Idempotent."""
        self._state.request_stop()

    async def snapshot(self) -> DeviceSnapshot:
        """Build a :class:`DeviceSnapshot` from cached identity plus live health."""
        clock = self._state.clock or RunClock.now()
        return DeviceSnapshot(
            adapter=ADAPTER_ID,
            device=self.name,
            t_mono_ns=clock.t_mono_ns(),
            t_utc=datetime.now(UTC),
            health=self._compute_health(clock=clock),
            fields=self._snapshot_fields(),
        )

    # ------------------------------------------------------------------ stream

    async def stream(self) -> AsyncIterator[DeviceEmission]:
        """Yield :class:`DeviceEmission`\\ s while sampling is active.

        One poll per ``1 / rate_hz``, each on a worker thread. Good polls
        yield the record then one :class:`ChannelSample` per bound
        channel; failed polls yield the record with its error fields and
        count as recoverable (or end the stream when ``auto_reconnect``
        is off). Events queued by commands (a heater pulse, a reconnect)
        follow the poll that drained them.
        """
        if self._bus is None or self._bus_lock is None:
            raise AdapterError(
                f"sht4x {self.name!r} stream() requires open() first", device=self.name
            )
        clock = self._state.clock
        if clock is None:
            raise AdapterError(
                f"sht4x {self.name!r} stream() requires start() first", device=self.name
            )

        snap = await self.snapshot()
        self._state.last_snapshot_t_mono_ns = snap.t_mono_ns
        yield snap

        period_ns = int(self.params.measure_period_s() * 1e9)
        next_tick_ns = clock.t_mono_ns()
        while not self._state.stop_requested:
            t_start_ns = clock.t_mono_ns()
            measurement: Measurement | None = None
            failure: Exception | None = None
            try:
                async with self._bus_lock:
                    measurement = await anyio.to_thread.run_sync(
                        self._measure_blocking, self._precision
                    )
            except (Mcp2221Error, Sht4xError) as exc:
                failure = exc
            t_end_ns = clock.t_mono_ns()

            record = self._record_for(measurement, failure, t_start_ns, t_end_ns)
            if measurement is None:
                # Count before the row goes out, so a snapshot taken on seeing
                # the error row already reads degraded.
                self._state.recoverable_error_count += 1
                _log.warning("sht4x %r poll failed: %s", self.name, failure)
            yield record
            if measurement is None:
                if not self.params.auto_reconnect:
                    raise AdapterError(
                        f"sht4x {self.name!r} poll failed and auto_reconnect is disabled: "
                        f"{failure}",
                        device=self.name,
                    ) from failure
                await self._recover(failure)
            else:
                self._state.last_sample.mark(record.t_mono_ns)
                self._last_measurement = measurement
                for sample in self._channel_samples_for(measurement, record):
                    yield sample
            while self._events:
                yield self._events.pop(0)
            if self._state.snapshot_due(period_s=self.params.snapshot_period_s):
                snap = await self.snapshot()
                self._state.last_snapshot_t_mono_ns = snap.t_mono_ns
                yield snap

            next_tick_ns += period_ns
            delay_s = (next_tick_ns - clock.t_mono_ns()) / 1e9
            if delay_s > 0:
                await anyio.sleep(delay_s)
            else:
                # Fell behind (a heater pulse held the bus, or the host
                # stalled): resync rather than burst to catch up.
                next_tick_ns = clock.t_mono_ns()

    async def _recover(self, failure: Exception | None) -> None:
        """After a failed poll: a NACK or CRC leaves the bus usable (the
        bridge already cancelled), so only a bridge I/O failure is worth
        a reopen, after a back-off."""
        if not isinstance(failure, Mcp2221Error) or isinstance(failure, I2cNackError):
            return
        await anyio.sleep(_RECONNECT_BACKOFF_S)
        bus = self._bus
        if bus is None or self._state.stop_requested:
            return
        try:
            await anyio.to_thread.run_sync(bus.close)
            await anyio.to_thread.run_sync(bus.open)
            self._sensor_serial = await anyio.to_thread.run_sync(self._identify_blocking, bus)
        except (Mcp2221Error, Sht4xError) as exc:
            _log.warning("sht4x %r bridge reopen failed: %s", self.name, exc)
            return
        self._events.append(
            self._event(
                kind="reconnect",
                severity="warning",
                message=f"MCP2221 bridge reopened after: {failure}",
            )
        )

    # ------------------------------------------------------------------ commands

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        """Issue a generic command. Authorization gate first, then dispatch."""
        clock = self._state.clock or RunClock.now()
        rejection = reject_unless_authorized(
            cmd, adapter_id=ADAPTER_ID, device_name=self.name, clock=clock
        )
        if rejection is not None:
            return rejection
        if self._bus is None or self._bus_lock is None:
            return make_not_open_result(adapter_id=ADAPTER_ID, device_name=self.name, clock=clock)
        try:
            outcome = await self._dispatch_command(cmd)
        except (Mcp2221Error, Sht4xError) as exc:
            raise AdapterError(
                f"sht4x {self.name!r} command {cmd.kind!r} failed: {exc}", device=self.name
            ) from exc
        if isinstance(outcome, CommandResult):
            return outcome
        return make_accepted_result(detail=outcome, clock=clock)

    async def _dispatch_command(self, cmd: DeviceCommand) -> str | CommandResult:
        """Dispatch a generic :class:`DeviceCommand`.

        Recognized ``cmd.kind`` values:

        * ``"set_precision"`` — payload ``{"precision": "high" | "medium" | "low"}``.
          Changes which measure command the stream sends; nothing is
          written to the sensor.
        * ``"heater_pulse"`` — payload ``{"power_mw": 200 | 110 | 20,
          "duration_s": 1.0 | 0.1}``. Runs the sensor's heater once and
          returns the high-precision reading taken at the end of the
          pulse. Refused (``accepted=False``) above
          :data:`HEATER_MAX_AMBIENT_C` or inside the duty-cycle hold-off.
        * ``"soft_reset"`` — the sensor's reset command; its serial is re-read.
        """
        kind = cmd.kind
        if kind == "set_precision":
            precision = str(cmd.payload["precision"]).strip().lower()
            if precision not in PRECISIONS:
                raise AdapterError(
                    f"sht4x {self.name!r} set_precision: unknown precision {precision!r}; "
                    f"expected one of {', '.join(PRECISIONS)}",
                    device=self.name,
                )
            self._precision = precision
            return f"set_precision precision={precision!r}"
        if kind == "heater_pulse":
            return await self._heater_pulse(cmd.payload)
        if kind == "soft_reset":
            bus = self._bus
            assert bus is not None and self._bus_lock is not None
            async with self._bus_lock:
                self._sensor_serial = await anyio.to_thread.run_sync(self._identify_blocking, bus)
            return f"soft_reset serial={self._sensor_serial}"
        raise AdapterError(f"sht4x {self.name!r}: unknown command kind {kind!r}", device=self.name)

    async def _heater_pulse(self, payload: dict[str, Any]) -> str | CommandResult:
        clock = self._state.clock or RunClock.now()
        try:
            power_mw = int(payload["power_mw"])
            duration_s = float(payload["duration_s"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AdapterError(
                f"sht4x {self.name!r} heater_pulse needs power_mw and duration_s: {exc}",
                device=self.name,
            ) from exc
        if (power_mw, duration_s) not in _HEATER_COMMANDS:
            raise AdapterError(
                f"sht4x {self.name!r} heater_pulse: no {power_mw} mW / {duration_s} s pulse; "
                f"power is one of {HEATER_POWERS_MW} mW, duration one of {HEATER_DURATIONS_S} s",
                device=self.name,
            )
        last = self._last_measurement
        if last is not None and last.temperature_c > HEATER_MAX_AMBIENT_C:
            return CommandResult(
                accepted=False,
                detail=(
                    f"heater refused: sensor reads {last.temperature_c:.1f} °C, above the "
                    f"{HEATER_MAX_AMBIENT_C:.0f} °C limit for heater use"
                ),
                t_mono_ns=clock.t_mono_ns(),
                t_utc=datetime.now(UTC),
            )
        remaining = self._heater_free_at - time.monotonic()
        if remaining > 0:
            return CommandResult(
                accepted=False,
                detail=(
                    f"heater refused: duty-cycle hold-off, {remaining:.0f} s until the next "
                    f"pulse is allowed"
                ),
                t_mono_ns=clock.t_mono_ns(),
                t_utc=datetime.now(UTC),
            )
        bus = self._bus
        assert bus is not None and self._bus_lock is not None
        async with self._bus_lock:
            measurement = await anyio.to_thread.run_sync(
                self._heater_blocking, bus, power_mw, duration_s
            )
        now = time.monotonic()
        self._heater_pulses += 1
        self._last_heater_end = now
        self._heater_free_at = now + duration_s / HEATER_MAX_DUTY - duration_s
        self._events.append(
            self._event(
                kind="heater_pulse",
                severity="info",
                message=(
                    f"heater pulse {power_mw} mW for {duration_s:g} s; reading at end of pulse "
                    f"{measurement.temperature_c:.2f} °C, {measurement.humidity_pct:.1f} %RH"
                ),
                metadata={
                    "power_mw": power_mw,
                    "duration_s": duration_s,
                    "temperature_c": measurement.temperature_c,
                    "humidity_pct": measurement.humidity_pct,
                },
            )
        )
        return (
            f"heater_pulse power_mw={power_mw} duration_s={duration_s:g} "
            f"end_of_pulse={measurement.temperature_c:.2f}C/{measurement.humidity_pct:.1f}%RH"
        )

    async def set_precision(
        self,
        precision: str,
        *,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Change the measurement precision. Authorization rules match :meth:`command`."""
        return await self.command(
            DeviceCommand(
                kind="set_precision",
                payload={"precision": precision},
                issued_by=issued_by,
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )

    async def heater_pulse(
        self,
        *,
        power_mw: int,
        duration_s: float,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Run the sensor's heater once. Authorization rules match :meth:`command`."""
        return await self.command(
            DeviceCommand(
                kind="heater_pulse",
                payload={"power_mw": power_mw, "duration_s": duration_s},
                issued_by=issued_by,
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )

    async def soft_reset(
        self,
        *,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Soft-reset the sensor. Authorization rules match :meth:`command`."""
        return await self.command(
            DeviceCommand(
                kind="soft_reset",
                payload={},
                issued_by=issued_by,
                authorization_id=authorization_id,
                confirmed_by=confirmed_by,
            )
        )

    # ------------------------------------------------------------------ read-only

    async def read_once(self) -> Measurement:
        """One measurement outside the stream. Read-only, not gated; used by
        the handshake, the hardware smoke test and the manual card."""
        bus = self._bus
        if bus is None or self._bus_lock is None:
            raise AdapterError(f"sht4x {self.name!r} is not open", device=self.name)
        try:
            async with self._bus_lock:
                measurement = await anyio.to_thread.run_sync(
                    self._measure_blocking, self._precision
                )
        except (Mcp2221Error, Sht4xError) as exc:
            raise AdapterError(f"sht4x {self.name!r} read failed: {exc}", device=self.name) from exc
        self._last_measurement = measurement
        return measurement

    async def read_state_snapshot(self) -> Sht4xStateSnapshot | None:
        """The precision, identity and last reading, for the manual card and
        ``device_settings``. ``None`` before :meth:`open`. No I/O: every
        field is already in hand."""
        if self._state.lifecycle.state == "closed":
            return None
        bus = self._bus
        last = self._last_measurement
        return Sht4xStateSnapshot(
            precision=self._precision,
            sensor_serial=self._sensor_serial,
            bridge_serial=self.params.serial,
            bridge_firmware=_bridge_revision(bus),
            last_temperature_c=last.temperature_c if last else None,
            last_humidity_pct=last.humidity_pct if last else None,
            heater_pulses=self._heater_pulses,
            seconds_since_heater_pulse=(
                time.monotonic() - self._last_heater_end
                if self._last_heater_end is not None
                else None
            ),
        )

    # ------------------------------------------------------------------ blocking I/O (worker thread)

    def _measure_blocking(self, precision: str) -> Measurement:
        """One measure command, the conversion wait, one read. Runs off-loop."""
        bus = self._bus
        if bus is None:
            raise Mcp2221Error("bridge not open")
        command, conversion_s = _MEASURE_COMMANDS[precision]
        started = time.perf_counter()
        bus.write(self.params.i2c_address, bytes([command]))
        time.sleep(conversion_s)
        payload = self._read_when_ready(bus, started + conversion_s + _READ_RETRY_BUDGET_S)
        return Measurement.from_payload(
            payload, precision=precision, latency_s=time.perf_counter() - started
        )

    def _heater_blocking(self, bus: Mcp2221I2cBus, power_mw: int, duration_s: float) -> Measurement:
        """One heater command, the pulse, then the reading it produced."""
        command, ready_s = _HEATER_COMMANDS[(power_mw, duration_s)]
        started = time.perf_counter()
        bus.write(self.params.i2c_address, bytes([command]))
        time.sleep(ready_s)
        payload = self._read_when_ready(bus, started + ready_s + _READ_RETRY_BUDGET_S)
        return Measurement.from_payload(
            payload, precision="high", latency_s=time.perf_counter() - started
        )

    def _identify_blocking(self, bus: Mcp2221I2cBus) -> str:
        """Soft reset, then the serial number. Runs off-loop."""
        address = self.params.i2c_address
        bus.write(address, bytes([_CMD_SOFT_RESET]))
        time.sleep(_SOFT_RESET_S)
        bus.write(address, bytes([_CMD_READ_SERIAL]))
        payload = self._read_when_ready(bus, time.perf_counter() + _READ_RETRY_BUDGET_S)
        high, low = decode_words(payload)
        return f"0x{(high << 16) | low:08x}"

    def _read_when_ready(self, bus: Mcp2221I2cBus, deadline: float) -> bytes:
        """Read the 6-byte response, retrying while the sensor NACKs
        (it does so until its conversion is done) up to ``deadline``."""
        while True:
            try:
                return bus.read(self.params.i2c_address, _RESPONSE_LEN)
            except I2cNackError:
                if time.perf_counter() > deadline:
                    raise Sht4xError(
                        f"sensor at 0x{self.params.i2c_address:02x} did not answer"
                    ) from None
                time.sleep(_READ_RETRY_S)

    # ------------------------------------------------------------------ records

    def _record_for(
        self,
        measurement: Measurement | None,
        failure: Exception | None,
        t_start_ns: int,
        t_end_ns: int,
    ) -> SourceRecord:
        clock = self._state.clock
        assert clock is not None
        t_mono_ns = (t_start_ns + t_end_ns) // 2
        self._state.seq += 1
        latency_s = (
            measurement.latency_s if measurement is not None else (t_end_ns - t_start_ns) / 1e9
        )
        return SourceRecord(
            record_id=make_record_id(ADAPTER_ID, self.name, self._state.seq),
            adapter=ADAPTER_ID,
            device=self.name,
            shape="wide_row",
            t_mono_ns=t_mono_ns,
            t_utc=clock.to_wall_ns(t_mono_ns),
            row=sht4x_row(
                measurement, precision=self._precision, latency_s=latency_s, error=failure
            ),
            metadata={
                "sensor_serial": self._sensor_serial,
                "i2c_address": self.params.i2c_address,
            },
        )

    def _channel_samples_for(
        self, measurement: Measurement, record: SourceRecord
    ) -> list[DeviceEmission]:
        """Map ``measurement`` against the configured :class:`Sht4xReading` bindings."""
        emissions: list[DeviceEmission] = []
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
                    t_mono_ns=record.t_mono_ns,
                    source_record_id=record.record_id,
                    source_field=binding.field,
                )
            )
        return emissions

    def _event(
        self,
        *,
        kind: str,
        severity: Literal["info", "warning", "error"],
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> DeviceEvent:
        clock = self._state.clock or RunClock.now()
        return DeviceEvent(
            adapter=ADAPTER_ID,
            device=self.name,
            t_mono_ns=clock.t_mono_ns(),
            t_utc=datetime.now(UTC),
            kind=kind,
            message=message,
            severity=severity,
            metadata=metadata or {},
        )

    # ------------------------------------------------------------------ health

    def watchdog_state(self) -> WatchdogState:
        """Return a compact silence-state view for tests and future policy work."""
        return self._state.watchdog(device=self.name, rate_hz=self.params.rate_hz)

    def _compute_health(self, *, clock: RunClock) -> DeviceHealth:
        return self._state.compute_health(clock=clock, rate_hz=self.params.rate_hz)

    def _snapshot_fields(self) -> dict[str, float | int | str | bool | None]:
        bus = self._bus
        last = self._last_measurement
        return {
            "precision": self._precision,
            "rate_hz": self.params.rate_hz,
            "i2c_address": self.params.i2c_address,
            "i2c_speed_hz": bus.actual_speed_hz if bus is not None else None,
            "channel_count": len(self._channels),
            "state": self._state.lifecycle.state,
            "recoverable_errors": self._state.recoverable_error_count,
            "sensor_serial": self._sensor_serial,
            "bridge_serial": self.params.serial,
            "bridge_revision": _bridge_revision(bus),
            "last_temperature_c": last.temperature_c if last else None,
            "last_humidity_pct": last.humidity_pct if last else None,
            "last_latency_ms": round(last.latency_s * 1e3, 2) if last else None,
            "heater_pulses": self._heater_pulses,
        }


def _bridge_revision(bus: Mcp2221I2cBus | None) -> str | None:
    if bus is None or bus.hardware_revision is None:
        return None
    return f"{bus.hardware_revision} / {bus.firmware_revision or '?'}"


# ---------------------------------------------------------------------------
# CLI handshake hook (``capa validate --strict``)
# ---------------------------------------------------------------------------


async def handshake(params: dict[str, Any], *, bus_factory: BusFactory | None = None) -> str:
    """Open, read the serial and one measurement, close. Used by ``capa validate --strict``."""
    parsed = Sht4xAdapterParams.model_validate(params)
    adapter = Sht4xAdapter(name="handshake", params=parsed, bus_factory=bus_factory)
    await adapter.open()
    try:
        measurement = await adapter.read_once()
        snapshot = await adapter.read_state_snapshot()
    finally:
        await adapter.close()
    revision = snapshot.bridge_firmware if snapshot is not None else None
    return (
        f"sht4x serial={adapter.sensor_serial} addr=0x{parsed.i2c_address:02x} "
        f"bridge=MCP2221 rev={revision or '?'} "
        f"T={measurement.temperature_c:.2f}C RH={measurement.humidity_pct:.1f}%"
    )


# ---------------------------------------------------------------------------
# Discovery hook (``capa devices discover``)
# ---------------------------------------------------------------------------


DISCOVERY_ADDRESSES: Final[tuple[int, ...]] = (0x44, 0x45, 0x46)
"""Every address an SHT4x variant ships at."""


async def discover(
    *,
    addresses: tuple[int, ...] = DISCOVERY_ADDRESSES,
    bridges: Callable[[], list[BridgeInfo]] = enumerate_bridges,
    opener: HidOpener = open_hid_handle,
) -> list[dict[str, Any]]:
    """Probe every MCP2221 on the machine for an SHT4x at each ``addresses``.

    One row per sensor found: ``adapter``, the bridge's ``serial`` (``None``
    when the chip exposes none) and ``path``, the ``i2c_address`` and the
    ``sensor_serial``. Bridges that cannot be opened (another process
    holds them) are skipped. ``bridges`` and ``opener`` are test seams.
    """
    found_bridges = await anyio.to_thread.run_sync(bridges)
    rows: list[dict[str, Any]] = []
    for info in found_bridges:
        found = await anyio.to_thread.run_sync(_probe_bridge_blocking, info.path, addresses, opener)
        for address, sensor_serial in found:
            rows.append(
                {
                    "adapter": ADAPTER_ID,
                    "serial": info.serial,
                    "path": info.path.decode("utf-8", "replace"),
                    "i2c_address": address,
                    "sensor_serial": sensor_serial,
                }
            )
    return rows


def _probe_bridge_blocking(
    path: bytes, addresses: tuple[int, ...], opener: HidOpener
) -> list[tuple[int, str]]:
    bus = Mcp2221I2cBus(path=path, opener=opener)
    try:
        bus.open()
    except Mcp2221Error:
        return []
    found: list[tuple[int, str]] = []
    try:
        for address in addresses:
            try:
                bus.write(address, bytes([_CMD_READ_SERIAL]))
                payload = bus.read(address, _RESPONSE_LEN)
                high, low = decode_words(payload)
            except (Mcp2221Error, Sht4xError):
                continue
            found.append((address, f"0x{(high << 16) | low:08x}"))
    finally:
        bus.close()
    return found


__all__ = [
    "ADAPTER_ID",
    "DESCRIPTOR",
    "DISCOVERY_ADDRESSES",
    "HEATER_DURATIONS_S",
    "HEATER_MAX_AMBIENT_C",
    "HEATER_MAX_DUTY",
    "HEATER_POWERS_MW",
    "PRECISIONS",
    "SHT4X_SETTINGS",
    "Measurement",
    "Precision",
    "Sht4xAdapter",
    "Sht4xAdapterParams",
    "Sht4xCrcError",
    "Sht4xError",
    "Sht4xSettings",
    "Sht4xStateSnapshot",
    "convert_humidity",
    "convert_temperature",
    "crc8",
    "decode_words",
    "discover",
    "handshake",
    "raw_from_humidity",
    "raw_from_temperature",
    "sht4x_row",
]


def _build_descriptor() -> AdapterDescriptor:
    from capa.devices._templates import (  # noqa: PLC0415
        SHT4X_AMBIENT_HUMIDITY,
        SHT4X_AMBIENT_TEMPERATURE,
    )
    from capa.devices.registry import AdapterDescriptor  # noqa: PLC0415

    return AdapterDescriptor(
        id="capa.devices.sht4x",
        label="Sensirion SHT4x (MCP2221 bridge)",
        family="sht4x",
        adapter_factory=Sht4xAdapter,
        params_model=Sht4xAdapterParams,
        supported_binding_sources=("sht4x_reading",),
        default_params={"rate_hz": 1.0, "precision": "high"},
        channel_templates=(SHT4X_AMBIENT_TEMPERATURE, SHT4X_AMBIENT_HUMIDITY),
        discoverable=True,
        handshake_available=True,
        capabilities=frozenset(
            {
                Capability.HAS_PARAMETER_CONFIG,
                Capability.HAS_SENSOR_HEATER,
                Capability.SUPPORTS_DISCOVERY,
                Capability.SUPPORTS_AUTO_RECONNECT,
            }
        ),
        settings=SHT4X_SETTINGS,
    )


DESCRIPTOR = _build_descriptor()

from capa.devices.registry import register as _register  # noqa: E402

_register(DESCRIPTOR)
