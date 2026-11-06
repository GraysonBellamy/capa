"""Real :class:`Bmp58xAdapter` — a Bosch BMP580 / BMP581 / BMP585 barometric
pressure sensor behind an MCP2221 USB-I2C bridge.

The three parts share one register map and protocol; the chip id tells
the BMP585 (``0x51``) from the BMP580 / BMP581 (``0x50``), which cannot
be told apart, so the adapter is named for the family. The sensor
compensates on-chip: pressure and temperature come out as 24-bit words
already in Pa (``/ 64``) and °C (``/ 65536``), with no trimming
coefficients to read. The sensor side lives here; the bridge side in
:mod:`capa.devices._mcp2221`.

Architecture:

* ``open`` takes the bridge from :data:`~capa.devices._mcp2221.BUS_POOL`
  (an SHT4x on the same bridge shares the bus), soft-resets the sensor,
  checks its chip id and NVM status, then configures it: pressure on,
  the oversampling, deep standby off, the data-ready flag on (the
  interrupt pin stays off).
* ``start`` captures the run :class:`RunClock`; ``stream`` then polls at
  ``rate_hz``. Each poll is a forced-mode conversion: one register write
  starts it, the adapter waits the oversampling's conversion time, polls
  the data-ready flag, and reads the six data bytes — roughly
  20 ms of bridge traffic plus the conversion (about 45 ms at the
  default 64× / 4×), on a worker thread via
  :func:`anyio.to_thread.run_sync`. Every poll yields one
  :class:`SourceRecord` (``shape="wide_row"``: both values, both raw
  words, the oversampling, latency) plus one :class:`ChannelSample` per
  bound :class:`Bmp58xReading`.
* A failed poll (no data ready, a power-on reset, an implausible value,
  bridge I/O) keeps its row with the error fields populated, emits no
  channel samples, counts as a recoverable error, and marks the sensor
  for a reset-and-reconfigure before the next poll. With
  ``auto_reconnect`` a bridge I/O failure also reopens the bridge.
* ``command`` enforces the authorization gate and dispatches
  ``set_oversampling`` and ``soft_reset``. Nothing the sensor offers
  changes what it measures beyond noise and conversion time, so neither
  needs confirmation.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal, get_args

import anyio
from pydantic import BaseModel, ConfigDict, Field

from capa.channels.spec import Bmp58xReading, ChannelSpec
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
    BUS_POOL,
    MAX_I2C_SPEED_HZ,
    MIN_I2C_SPEED_HZ,
    BridgeInfo,
    BusPool,
    HidOpener,
    I2cNackError,
    Mcp2221Error,
    Mcp2221I2cBus,
    SharedBus,
    bridge_revision,
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

ADAPTER_ID: Final[str] = "bmp58x"


# ---------------------------------------------------------------------------
# Sensor protocol — Bosch BMP581 datasheet, section 7 (register map).
# ---------------------------------------------------------------------------


Oversampling = Literal[1, 2, 4, 8, 16, 32, 64, 128]
"""Samples averaged per conversion. More is less noise and a longer
conversion; pressure dominates the conversion time."""

OVERSAMPLINGS: Final[tuple[int, ...]] = get_args(Oversampling)

CHIP_IDS: Final[dict[int, str]] = {0x50: "BMP580/BMP581", 0x51: "BMP585"}
"""Chip id register value → part. The BMP580 and BMP581 share an id."""

I2C_ADDRESSES: Final[tuple[int, ...]] = (0x46, 0x47)
"""``0x46`` with SDO tied low, ``0x47`` with SDO high (most breakouts)."""

PRESSURE_RANGE_PA: Final[tuple[float, float]] = (30_000.0, 125_000.0)
"""The datasheet's operating range. A reading outside it is a fault, not
weather: the data registers' reset pattern decodes to about 130.6 kPa."""

_REG_CHIP_ID: Final = 0x01
_REG_REV_ID: Final = 0x02
_REG_INT_SOURCE: Final = 0x15
_REG_DATA: Final = 0x1D
"""Six bytes from here: temperature then pressure, each 24-bit little-endian."""
_REG_INT_STATUS: Final = 0x27
"""Clear-on-read."""
_REG_STATUS: Final = 0x28
_REG_OSR_CONFIG: Final = 0x36
_REG_ODR_CONFIG: Final = 0x37
_REG_CMD: Final = 0x7E

_INT_DRDY: Final = 0x01
_INT_POR: Final = 0x10
_STATUS_NVM_RDY: Final = 0x02
_STATUS_NVM_ERR: Final = 0x04
_OSR_PRESS_EN: Final = 0x40
_ODR_DEEP_DIS: Final = 0x80
_ODR_1HZ: Final = 0x1C << 2
"""The output-data-rate field is unused in forced mode; 1 Hz is the reset value."""
_ODR_STANDBY: Final = _ODR_DEEP_DIS | _ODR_1HZ | 0x00
_ODR_FORCED: Final = _ODR_DEEP_DIS | _ODR_1HZ | 0x02
_CMD_SOFT_RESET: Final = 0xB6

_SOFT_RESET_S: Final = 0.002
_READY_POLL_S: Final = 0.001
_READY_BUDGET_S: Final = 0.05
"""Extra time to keep polling the data-ready flag past the estimated
conversion time before the poll counts as failed."""
_RECONNECT_BACKOFF_S: Final = 1.0


class Bmp58xError(Exception):
    """A sensor-level failure: wrong chip, no data ready, an implausible value."""


def osr_code(oversampling: int) -> int:
    """Oversampling ratio → the 3-bit register code (``1×`` → 0, ``128×`` → 7)."""
    try:
        return OVERSAMPLINGS.index(oversampling)
    except ValueError:
        raise ValueError(
            f"oversampling {oversampling} is not one of {', '.join(map(str, OVERSAMPLINGS))}"
        ) from None


def osr_config(pressure_oversampling: int, temperature_oversampling: int) -> int:
    """The ``OSR_CONFIG`` byte: pressure enabled plus both ratios."""
    return (
        _OSR_PRESS_EN | (osr_code(pressure_oversampling) << 3) | osr_code(temperature_oversampling)
    )


def conversion_time_s(pressure_oversampling: int, temperature_oversampling: int) -> float:
    """Estimated forced-mode conversion time. Fitted to a BMP581 on the
    rig (1× / 1× is about 1 ms, 64× / 4× about 45 ms, 128× / 8× about
    86 ms); the stream polls the data-ready flag after this wait, so the
    estimate only needs to be close."""
    return 0.001 + 0.00065 * pressure_oversampling + 0.00027 * temperature_oversampling


def decode_data(payload: bytes) -> tuple[int, int]:
    """Split the six data bytes into ``(temperature_raw, pressure_raw)``:
    a signed and an unsigned 24-bit little-endian word."""
    if len(payload) != 6:
        raise Bmp58xError(f"expected 6 data bytes from the sensor, got {len(payload)}")
    temperature_raw = int.from_bytes(payload[0:3], "little", signed=True)
    pressure_raw = int.from_bytes(payload[3:6], "little", signed=False)
    return temperature_raw, pressure_raw


def convert_temperature(raw: int) -> float:
    """Raw temperature word → °C."""
    return raw / 65536.0


def convert_pressure(raw: int) -> float:
    """Raw pressure word → Pa."""
    return raw / 64.0


def raw_from_temperature(temperature_c: float) -> int:
    """Inverse of :func:`convert_temperature`, for the simulator and fakes."""
    return max(-(1 << 23), min(round(temperature_c * 65536.0), (1 << 23) - 1))


def raw_from_pressure(pressure_pa: float) -> int:
    """Inverse of :func:`convert_pressure`, for the simulator and fakes."""
    return max(0, min(round(pressure_pa * 64.0), (1 << 24) - 1))


def encode_data(temperature_c: float, pressure_pa: float) -> bytes:
    """The six data bytes for the given values, for the simulator and fakes."""
    t_raw = raw_from_temperature(temperature_c)
    p_raw = raw_from_pressure(pressure_pa)
    return t_raw.to_bytes(3, "little", signed=True) + p_raw.to_bytes(3, "little")


@dataclass(frozen=True, slots=True)
class Measurement:
    """One pressure / temperature pair as the sensor reported it."""

    pressure_pa: float
    temperature_c: float
    pressure_raw: int
    temperature_raw: int
    pressure_oversampling: int
    temperature_oversampling: int
    latency_s: float
    """Wall time from the conversion trigger to the data in hand."""

    @classmethod
    def from_payload(
        cls,
        payload: bytes,
        *,
        pressure_oversampling: int,
        temperature_oversampling: int,
        latency_s: float,
    ) -> Measurement:
        """Decode the six data bytes, rejecting a pressure outside the
        sensor's operating range."""
        temperature_raw, pressure_raw = decode_data(payload)
        pressure_pa = convert_pressure(pressure_raw)
        low, high = PRESSURE_RANGE_PA
        if not low <= pressure_pa <= high:
            raise Bmp58xError(
                f"pressure {pressure_pa:.0f} Pa is outside the sensor's "
                f"{low:.0f}–{high:.0f} Pa range (data {payload.hex()})"
            )
        return cls(
            pressure_pa=pressure_pa,
            temperature_c=convert_temperature(temperature_raw),
            pressure_raw=pressure_raw,
            temperature_raw=temperature_raw,
            pressure_oversampling=pressure_oversampling,
            temperature_oversampling=temperature_oversampling,
            latency_s=latency_s,
        )


def bmp58x_row(
    measurement: Measurement | None,
    *,
    pressure_oversampling: int,
    temperature_oversampling: int,
    latency_s: float,
    error: BaseException | None = None,
) -> dict[str, float | int | str | bool | None]:
    """The library-native wide row for one poll, shared with the simulator.

    A failed poll keeps every column, with the values ``None`` and the
    error fields populated, so ``device_records/bmp58x.parquet`` records
    the outage in place.
    """
    return {
        "pressure_pa": measurement.pressure_pa if measurement else None,
        "temperature_c": measurement.temperature_c if measurement else None,
        "pressure_raw": measurement.pressure_raw if measurement else None,
        "temperature_raw": measurement.temperature_raw if measurement else None,
        "pressure_oversampling": pressure_oversampling,
        "temperature_oversampling": temperature_oversampling,
        "latency_ms": round(latency_s * 1e3, 3),
        "error_type": type(error).__name__ if error is not None else None,
        "error_message": str(error) if error is not None else None,
    }


# ---------------------------------------------------------------------------
# Adapter params (Pydantic) — what shows up under ``[devices.params]`` in TOML.
# ---------------------------------------------------------------------------


class Bmp58xAdapterParams(BaseModel):
    """Per-device adapter configuration for a BMP58x on an MCP2221."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    serial: str | None = None
    """USB serial of the MCP2221 to use. ``None`` opens the first bridge
    found. Must match any other sensor's on the same bridge."""

    i2c_address: Literal[0x46, 0x47] = 0x47
    """The sensor's 7-bit address: ``0x47`` with SDO high (the usual
    breakout default), ``0x46`` with SDO low."""

    pressure_oversampling: Oversampling = 64
    """Pressure samples per conversion. 64× is about 0.1 Pa of noise."""

    temperature_oversampling: Oversampling = 4
    """Temperature samples per conversion."""

    rate_hz: float = Field(gt=0, le=10.0, default=1.0)
    """Polling cadence; one forced conversion per poll."""

    i2c_speed_hz: int = Field(ge=MIN_I2C_SPEED_HZ, le=MAX_I2C_SPEED_HZ, default=100_000)
    """Bus clock. Every sensor on one bridge must declare the same speed."""

    snapshot_period_s: float = Field(gt=0, default=30.0)
    """Cadence of :class:`DeviceSnapshot` emissions during a run."""

    auto_reconnect: bool = True
    """When ``True`` a failed poll does not end the stream: the row keeps
    its error fields, the degradation counter increments, the sensor is
    reset and reconfigured before the next poll, and a bridge I/O failure
    reopens the bridge after a back-off."""

    def measure_period_s(self) -> float:
        """Seconds between polls."""
        return 1.0 / self.rate_hz


# ---------------------------------------------------------------------------
# State snapshot + declarative settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bmp58xStateSnapshot:
    """What ``device_settings`` reads back.

    Built by :meth:`Bmp58xAdapter.read_state_snapshot` on the worker loop.
    """

    pressure_oversampling: int | None = None
    temperature_oversampling: int | None = None

    chip: str | None = None
    """``"BMP580/BMP581"`` or ``"BMP585"``; ``None`` before :meth:`open`."""

    revision: str | None = None
    """The sensor's revision id as ``0x…``."""

    bridge_serial: str | None = None
    bridge_firmware: str | None = None

    last_pressure_pa: float | None = None
    last_temperature_c: float | None = None
    """The most recent good reading, from the stream or a one-shot read."""


class Bmp58xSettings(BaseModel):
    """What an experiment can declare for a BMP58x under ``device_settings:``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pressure_oversampling: Oversampling | None = Field(
        default=None,
        json_schema_extra={
            "capa_help": (
                "Pressure samples averaged per conversion (1–128). 64 (the default) "
                "is about 0.1 Pa of noise and a 45 ms conversion."
            )
        },
    )
    temperature_oversampling: Oversampling | None = Field(
        default=None,
        json_schema_extra={"capa_help": "Temperature samples averaged per conversion (1–128)."},
    )


BMP58X_SETTINGS: Final = DeviceSettingsSpec(
    model=Bmp58xSettings,
    fields=(
        SettingField(
            name="pressure_oversampling",
            label="Pressure oversampling",
            current=lambda snapshot: snapshot.pressure_oversampling,
            command=lambda want, _snapshot: ("set_oversampling", {"pressure": want}),
            show=lambda value: f"{value}×",
        ),
        SettingField(
            name="temperature_oversampling",
            label="Temperature oversampling",
            current=lambda snapshot: snapshot.temperature_oversampling,
            command=lambda want, _snapshot: ("set_oversampling", {"temperature": want}),
            show=lambda value: f"{value}×",
        ),
    ),
    snapshot_type=Bmp58xStateSnapshot,
)
"""Shared by :class:`Bmp58xAdapter` and the simulator."""


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


BusFactory = Callable[[Bmp58xAdapterParams], Mcp2221I2cBus]
"""Test seam: builds the (unopened) bridge the adapter will drive when no
other sensor has opened it yet."""


def _default_bus_factory(params: Bmp58xAdapterParams) -> Mcp2221I2cBus:
    return Mcp2221I2cBus(serial=params.serial, i2c_speed_hz=params.i2c_speed_hz)


@dataclass(frozen=True, slots=True)
class _Identity:
    chip_id: int
    revision: int


def _read_register(bus: Mcp2221I2cBus, address: int, register: int, length: int) -> bytes:
    """Set the register pointer, then read ``length`` bytes from it."""
    bus.write(address, bytes([register]))
    return bus.read(address, length)


class Bmp58xAdapter:
    """Real BMP58x adapter (one sensor per instance; the bridge may carry others).

    Two construction shapes:

    * ``Bmp58xAdapter(name=..., **params_kwargs)`` — engine path; per-device
      params from ``DeviceConfig.params`` are forwarded as kwargs and parsed
      into :class:`Bmp58xAdapterParams`.
    * ``Bmp58xAdapter(name=..., params=Bmp58xAdapterParams(...))`` —
      programmatic path used by tests.

    Both shapes accept optional ``bus_factory`` and ``bus_pool`` kwargs as
    test seams.
    """

    __slots__ = (
        "_bus_factory",
        "_bus_pool",
        "_channels",
        "_events",
        "_identity",
        "_last_measurement",
        "_needs_configure",
        "_pressure_oversampling",
        "_sensor_lock",
        "_shared",
        "_state",
        "_temperature_oversampling",
        "capabilities",
        "name",
        "params",
    )

    name: str
    params: Bmp58xAdapterParams
    capabilities: frozenset[Capability]

    def __init__(
        self,
        *,
        name: str,
        params: Bmp58xAdapterParams | None = None,
        bus_factory: BusFactory | None = None,
        bus_pool: BusPool | None = None,
        **params_kwargs: Any,
    ) -> None:
        if params is not None and params_kwargs:
            raise TypeError("Bmp58xAdapter accepts either `params=` or per-field kwargs, not both")
        if params is None:
            params = Bmp58xAdapterParams.model_validate(params_kwargs)
        self.name = name
        self.params = params
        flags: set[Capability] = {
            Capability.HAS_PARAMETER_CONFIG,
            Capability.SUPPORTS_DISCOVERY,
        }
        if params.auto_reconnect:
            flags.add(Capability.SUPPORTS_AUTO_RECONNECT)
        self.capabilities = frozenset(flags)
        self._bus_factory: BusFactory = bus_factory or _default_bus_factory
        self._bus_pool: BusPool = bus_pool if bus_pool is not None else BUS_POOL
        self._shared: SharedBus | None = None
        # Held across a whole sensor transaction (trigger, wait, read) so a
        # command never reconfigures the sensor mid-conversion.
        self._sensor_lock = threading.RLock()
        self._channels: list[ChannelSpec] = []
        self._state = AdapterRuntimeState()
        self._pressure_oversampling: int = params.pressure_oversampling
        self._temperature_oversampling: int = params.temperature_oversampling
        self._identity: _Identity | None = None
        self._needs_configure = False
        self._last_measurement: Measurement | None = None
        self._events: list[DeviceEvent] = []

    # ------------------------------------------------------------------ wiring

    def configure_channels(self, specs: list[ChannelSpec]) -> None:
        """Bind to :class:`Bmp58xReading`-bound channels for this device."""
        self._channels = channels_for_device(
            specs, device=self.name, binding_source="bmp58x_reading"
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
    def oversampling(self) -> tuple[int, int]:
        """``(pressure, temperature)`` oversampling the stream converts at."""
        return self._pressure_oversampling, self._temperature_oversampling

    @property
    def chip(self) -> str | None:
        """The part, from the chip id read at :meth:`open`."""
        return CHIP_IDS.get(self._identity.chip_id) if self._identity else None

    @property
    def last_measurement(self) -> Measurement | None:
        """The most recent good reading, from the stream or a one-shot read."""
        return self._last_measurement

    # ------------------------------------------------------------------ lifecycle

    async def open(self) -> None:
        """Take the bridge, reset the sensor, check it and configure it.

        Idempotent: a second call on an already-open adapter is a no-op.
        """
        if self._state.lifecycle.state in ("open", "running"):
            return
        pool = self._bus_pool
        try:
            shared = await anyio.to_thread.run_sync(self._acquire_blocking)
        except Mcp2221Error as exc:
            raise AdapterError(
                f"bmp58x {self.name!r} open failed: {exc}", device=self.name
            ) from exc
        try:
            self._identity = await anyio.to_thread.run_sync(self._setup_blocking, shared)
        except (Mcp2221Error, Bmp58xError) as exc:
            await anyio.to_thread.run_sync(pool.release, shared)
            raise AdapterError(
                f"bmp58x {self.name!r} open failed: {exc}", device=self.name
            ) from exc
        self._shared = shared
        self._needs_configure = False
        self._state.lifecycle.open()

    async def close(self) -> None:
        """Release the bridge (closed once no other sensor holds it).
        Idempotent. The sensor needs no safe state: it has no outputs, and
        it idles in standby between forced conversions."""
        if self._state.lifecycle.state == "closed":
            return
        shared, self._shared = self._shared, None
        if shared is not None:
            await anyio.to_thread.run_sync(self._bus_pool.release, shared)
        self._state.lifecycle.close()

    def _acquire_blocking(self) -> SharedBus:
        return self._bus_pool.acquire(
            self.resource_id,
            i2c_speed_hz=self.params.i2c_speed_hz,
            factory=lambda: self._bus_factory(self.params),
        )

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

        One forced conversion per ``1 / rate_hz``, each on a worker
        thread. Good polls yield the record then one
        :class:`ChannelSample` per bound channel; failed polls yield the
        record with its error fields and count as recoverable (or end the
        stream when ``auto_reconnect`` is off).
        """
        if self._shared is None:
            raise AdapterError(
                f"bmp58x {self.name!r} stream() requires open() first", device=self.name
            )
        clock = self._state.clock
        if clock is None:
            raise AdapterError(
                f"bmp58x {self.name!r} stream() requires start() first", device=self.name
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
                measurement = await anyio.to_thread.run_sync(self._measure_blocking)
            except (Mcp2221Error, Bmp58xError) as exc:
                failure = exc
            t_end_ns = clock.t_mono_ns()

            record = self._record_for(measurement, failure, t_start_ns, t_end_ns)
            if measurement is None:
                self._state.recoverable_error_count += 1
                _log.warning("bmp58x %r poll failed: %s", self.name, failure)
            yield record
            if measurement is None:
                if not self.params.auto_reconnect:
                    raise AdapterError(
                        f"bmp58x {self.name!r} poll failed and auto_reconnect is disabled: "
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
                # Fell behind (a long conversion at a high rate, or the
                # host stalled): resync rather than burst to catch up.
                next_tick_ns = clock.t_mono_ns()

    async def _recover(self, failure: Exception | None) -> None:
        """After a failed poll the sensor is reconfigured before the next
        one (``_needs_configure``); a bridge I/O failure additionally
        reopens the shared bus after a back-off."""
        self._needs_configure = True
        if not isinstance(failure, Mcp2221Error) or isinstance(failure, I2cNackError):
            return
        await anyio.sleep(_RECONNECT_BACKOFF_S)
        shared = self._shared
        if shared is None or self._state.stop_requested:
            return
        try:
            await anyio.to_thread.run_sync(self._bus_pool.reopen, shared)
        except Mcp2221Error as exc:
            _log.warning("bmp58x %r bridge reopen failed: %s", self.name, exc)
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
        if self._shared is None:
            return make_not_open_result(adapter_id=ADAPTER_ID, device_name=self.name, clock=clock)
        try:
            detail = await self._dispatch_command(cmd)
        except (Mcp2221Error, Bmp58xError) as exc:
            raise AdapterError(
                f"bmp58x {self.name!r} command {cmd.kind!r} failed: {exc}", device=self.name
            ) from exc
        return make_accepted_result(detail=detail, clock=clock)

    async def _dispatch_command(self, cmd: DeviceCommand) -> str:
        """Dispatch a generic :class:`DeviceCommand`.

        Recognized ``cmd.kind`` values:

        * ``"set_oversampling"`` — payload ``{"pressure": n, "temperature": n}``,
          either key optional, each one of 1, 2, 4 … 128. Written to the
          sensor between conversions.
        * ``"soft_reset"`` — the sensor's reset command, then the
          configuration is written again.
        """
        shared = self._shared
        assert shared is not None
        if cmd.kind == "set_oversampling":
            pressure = self._oversampling_from(cmd.payload, "pressure")
            temperature = self._oversampling_from(cmd.payload, "temperature")
            if pressure is None and temperature is None:
                raise AdapterError(
                    f"bmp58x {self.name!r} set_oversampling needs 'pressure' and/or 'temperature'",
                    device=self.name,
                )
            await anyio.to_thread.run_sync(
                self._set_oversampling_blocking,
                shared,
                pressure or self._pressure_oversampling,
                temperature or self._temperature_oversampling,
            )
            return (
                f"set_oversampling pressure={self._pressure_oversampling}x "
                f"temperature={self._temperature_oversampling}x"
            )
        if cmd.kind == "soft_reset":
            self._identity = await anyio.to_thread.run_sync(self._setup_blocking, shared)
            self._needs_configure = False
            return f"soft_reset chip={self.chip} rev=0x{self._identity.revision:02x}"
        raise AdapterError(
            f"bmp58x {self.name!r}: unknown command kind {cmd.kind!r}", device=self.name
        )

    def _oversampling_from(self, payload: dict[str, Any], key: str) -> int | None:
        if payload.get(key) is None:
            return None
        try:
            value = int(payload[key])
            osr_code(value)
        except (TypeError, ValueError) as exc:
            raise AdapterError(
                f"bmp58x {self.name!r} set_oversampling {key}: {exc}", device=self.name
            ) from exc
        return value

    async def set_oversampling(
        self,
        *,
        pressure: int | None = None,
        temperature: int | None = None,
        issued_by: str,
        authorization_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> CommandResult:
        """Change the oversampling. Authorization rules match :meth:`command`."""
        return await self.command(
            DeviceCommand(
                kind="set_oversampling",
                payload={"pressure": pressure, "temperature": temperature},
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
        """Soft-reset and reconfigure the sensor. Authorization rules match :meth:`command`."""
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
        """One conversion outside the stream. Read-only, not gated; used by
        the handshake and the hardware smoke test."""
        if self._shared is None:
            raise AdapterError(f"bmp58x {self.name!r} is not open", device=self.name)
        try:
            measurement = await anyio.to_thread.run_sync(self._measure_blocking)
        except (Mcp2221Error, Bmp58xError) as exc:
            self._needs_configure = True
            raise AdapterError(
                f"bmp58x {self.name!r} read failed: {exc}", device=self.name
            ) from exc
        self._last_measurement = measurement
        return measurement

    async def read_state_snapshot(self) -> Bmp58xStateSnapshot | None:
        """The oversampling, identity and last reading, for
        ``device_settings``. ``None`` before :meth:`open`. No I/O."""
        if self._state.lifecycle.state == "closed":
            return None
        last = self._last_measurement
        identity = self._identity
        return Bmp58xStateSnapshot(
            pressure_oversampling=self._pressure_oversampling,
            temperature_oversampling=self._temperature_oversampling,
            chip=self.chip,
            revision=f"0x{identity.revision:02x}" if identity else None,
            bridge_serial=self.params.serial,
            bridge_firmware=bridge_revision(self._shared),
            last_pressure_pa=last.pressure_pa if last else None,
            last_temperature_c=last.temperature_c if last else None,
        )

    # ------------------------------------------------------------------ blocking I/O (worker thread)

    def _setup_blocking(self, shared: SharedBus) -> _Identity:
        """Soft reset, identity and NVM checks, then the configuration."""
        address = self.params.i2c_address
        with self._sensor_lock:
            with shared.lock:
                shared.bus.write(address, bytes([_REG_CMD, _CMD_SOFT_RESET]))
            time.sleep(_SOFT_RESET_S)
            with shared.lock:
                chip_id, revision = _read_register(shared.bus, address, _REG_CHIP_ID, 2)
                status = _read_register(shared.bus, address, _REG_STATUS, 1)[0]
                # Clear-on-read: drops the power-on flag the reset just raised.
                _read_register(shared.bus, address, _REG_INT_STATUS, 1)
            if chip_id not in CHIP_IDS:
                raise Bmp58xError(
                    f"device at 0x{address:02x} has chip id 0x{chip_id:02x}, not a BMP58x "
                    f"(expected {', '.join(f'0x{c:02x}' for c in CHIP_IDS)})"
                )
            if not status & _STATUS_NVM_RDY or status & _STATUS_NVM_ERR:
                raise Bmp58xError(
                    f"sensor at 0x{address:02x} reports an NVM fault (status 0x{status:02x})"
                )
            self._configure_blocking(shared)
        return _Identity(chip_id=chip_id, revision=revision)

    def _configure_blocking(self, shared: SharedBus) -> None:
        """Data-ready flag on, pressure on with the oversampling, standby
        with deep standby disabled. The caller holds the sensor lock."""
        address = self.params.i2c_address
        osr = osr_config(self._pressure_oversampling, self._temperature_oversampling)
        with shared.lock:
            shared.bus.write(address, bytes([_REG_INT_SOURCE, _INT_DRDY]))
            shared.bus.write(address, bytes([_REG_OSR_CONFIG, osr]))
            shared.bus.write(address, bytes([_REG_ODR_CONFIG, _ODR_STANDBY]))
            readback = _read_register(shared.bus, address, _REG_OSR_CONFIG, 2)
        if readback != bytes([osr, _ODR_STANDBY]):
            raise Bmp58xError(
                f"sensor at 0x{address:02x} did not take its configuration "
                f"(wrote {osr:02x} {_ODR_STANDBY:02x}, read back {readback.hex(' ')})"
            )

    def _set_oversampling_blocking(
        self, shared: SharedBus, pressure: int, temperature: int
    ) -> None:
        with self._sensor_lock:
            previous = (self._pressure_oversampling, self._temperature_oversampling)
            self._pressure_oversampling, self._temperature_oversampling = pressure, temperature
            try:
                self._configure_blocking(shared)
            except (Mcp2221Error, Bmp58xError):
                self._pressure_oversampling, self._temperature_oversampling = previous
                self._needs_configure = True
                raise

    def _measure_blocking(self) -> Measurement:
        """One forced conversion: trigger, wait, poll data-ready, read. Runs off-loop."""
        shared = self._shared
        if shared is None:
            raise Mcp2221Error("bridge not open")
        address = self.params.i2c_address
        with self._sensor_lock:
            if self._needs_configure:
                self._identity = self._setup_blocking(shared)
                self._needs_configure = False
            osr_p, osr_t = self._pressure_oversampling, self._temperature_oversampling
            wait_s = conversion_time_s(osr_p, osr_t)
            started = time.perf_counter()
            with shared.lock:
                shared.bus.write(address, bytes([_REG_ODR_CONFIG, _ODR_FORCED]))
            time.sleep(wait_s)
            deadline = started + wait_s + _READY_BUDGET_S
            while True:
                with shared.lock:
                    status = _read_register(shared.bus, address, _REG_INT_STATUS, 1)[0]
                    payload = (
                        _read_register(shared.bus, address, _REG_DATA, 6)
                        if status & _INT_DRDY
                        else None
                    )
                if status & _INT_POR:
                    self._needs_configure = True
                    raise Bmp58xError(
                        f"sensor at 0x{address:02x} restarted (power-on reset) mid-run; "
                        "reconfiguring"
                    )
                if payload is not None:
                    break
                if time.perf_counter() > deadline:
                    self._needs_configure = True
                    raise Bmp58xError(
                        f"sensor at 0x{address:02x} had no data ready "
                        f"{(time.perf_counter() - started) * 1e3:.0f} ms after the trigger"
                    )
                time.sleep(_READY_POLL_S)
        return Measurement.from_payload(
            payload,
            pressure_oversampling=osr_p,
            temperature_oversampling=osr_t,
            latency_s=time.perf_counter() - started,
        )

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
        identity = self._identity
        return SourceRecord(
            record_id=make_record_id(ADAPTER_ID, self.name, self._state.seq),
            adapter=ADAPTER_ID,
            device=self.name,
            shape="wide_row",
            t_mono_ns=t_mono_ns,
            t_utc=clock.to_wall_ns(t_mono_ns),
            row=bmp58x_row(
                measurement,
                pressure_oversampling=self._pressure_oversampling,
                temperature_oversampling=self._temperature_oversampling,
                latency_s=latency_s,
                error=failure,
            ),
            metadata={
                "chip": self.chip,
                "revision": f"0x{identity.revision:02x}" if identity else None,
                "i2c_address": self.params.i2c_address,
            },
        )

    def _channel_samples_for(
        self, measurement: Measurement, record: SourceRecord
    ) -> list[DeviceEmission]:
        """Map ``measurement`` against the configured :class:`Bmp58xReading` bindings."""
        emissions: list[DeviceEmission] = []
        for spec in self._channels:
            binding = spec.source
            assert isinstance(binding, Bmp58xReading)
            raw_value = (
                measurement.pressure_pa
                if binding.field == "pressure"
                else measurement.temperature_c
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
        shared = self._shared
        last = self._last_measurement
        identity = self._identity
        return {
            "pressure_oversampling": self._pressure_oversampling,
            "temperature_oversampling": self._temperature_oversampling,
            "rate_hz": self.params.rate_hz,
            "i2c_address": self.params.i2c_address,
            "i2c_speed_hz": shared.bus.actual_speed_hz if shared is not None else None,
            "channel_count": len(self._channels),
            "state": self._state.lifecycle.state,
            "recoverable_errors": self._state.recoverable_error_count,
            "chip": self.chip,
            "revision": f"0x{identity.revision:02x}" if identity else None,
            "bridge_serial": self.params.serial,
            "bridge_revision": bridge_revision(shared),
            "last_pressure_pa": last.pressure_pa if last else None,
            "last_temperature_c": last.temperature_c if last else None,
            "last_latency_ms": round(last.latency_s * 1e3, 2) if last else None,
        }


# ---------------------------------------------------------------------------
# CLI handshake hook (``capa validate --strict``)
# ---------------------------------------------------------------------------


async def handshake(
    params: dict[str, Any],
    *,
    bus_factory: BusFactory | None = None,
    bus_pool: BusPool | None = None,
) -> str:
    """Open, identify, take one reading, close. Used by ``capa validate --strict``."""
    parsed = Bmp58xAdapterParams.model_validate(params)
    adapter = Bmp58xAdapter(
        name="handshake", params=parsed, bus_factory=bus_factory, bus_pool=bus_pool
    )
    await adapter.open()
    try:
        measurement = await adapter.read_once()
        snapshot = await adapter.read_state_snapshot()
    finally:
        await adapter.close()
    revision = snapshot.bridge_firmware if snapshot is not None else None
    chip_rev = snapshot.revision if snapshot is not None else None
    return (
        f"bmp58x chip={adapter.chip} rev={chip_rev} addr=0x{parsed.i2c_address:02x} "
        f"bridge=MCP2221 rev={revision or '?'} "
        f"P={measurement.pressure_pa:.1f}Pa T={measurement.temperature_c:.2f}C"
    )


# ---------------------------------------------------------------------------
# Discovery hook (``capa devices discover``)
# ---------------------------------------------------------------------------


async def discover(
    *,
    addresses: tuple[int, ...] = I2C_ADDRESSES,
    bridges: Callable[[], list[BridgeInfo]] = enumerate_bridges,
    opener: HidOpener = open_hid_handle,
) -> list[dict[str, Any]]:
    """Probe every MCP2221 on the machine for a BMP58x at each ``addresses``.

    One row per sensor found: ``adapter``, the bridge's ``serial`` (``None``
    when the chip exposes none) and ``path``, the ``i2c_address`` and the
    ``chip``. Only the chip id register is read; the sensor is not reset.
    Bridges that cannot be opened are skipped. ``bridges`` and ``opener``
    are test seams.
    """
    found_bridges = await anyio.to_thread.run_sync(bridges)
    rows: list[dict[str, Any]] = []
    for info in found_bridges:
        found = await anyio.to_thread.run_sync(_probe_bridge_blocking, info.path, addresses, opener)
        for address, chip in found:
            rows.append(
                {
                    "adapter": ADAPTER_ID,
                    "serial": info.serial,
                    "path": info.path.decode("utf-8", "replace"),
                    "i2c_address": address,
                    "chip": chip,
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
                chip_id = _read_register(bus, address, _REG_CHIP_ID, 1)[0]
            except Mcp2221Error:
                continue
            if chip_id in CHIP_IDS:
                found.append((address, CHIP_IDS[chip_id]))
    finally:
        bus.close()
    return found


__all__ = [
    "ADAPTER_ID",
    "BMP58X_SETTINGS",
    "CHIP_IDS",
    "DESCRIPTOR",
    "I2C_ADDRESSES",
    "OVERSAMPLINGS",
    "PRESSURE_RANGE_PA",
    "Bmp58xAdapter",
    "Bmp58xAdapterParams",
    "Bmp58xError",
    "Bmp58xSettings",
    "Bmp58xStateSnapshot",
    "Measurement",
    "Oversampling",
    "bmp58x_row",
    "conversion_time_s",
    "convert_pressure",
    "convert_temperature",
    "decode_data",
    "discover",
    "encode_data",
    "handshake",
    "osr_code",
    "osr_config",
    "raw_from_pressure",
    "raw_from_temperature",
]


def _build_descriptor() -> AdapterDescriptor:
    from capa.devices._templates import (  # noqa: PLC0415
        BMP58X_AMBIENT_PRESSURE,
        BMP58X_AMBIENT_TEMPERATURE,
    )
    from capa.devices.registry import AdapterDescriptor  # noqa: PLC0415

    return AdapterDescriptor(
        id="capa.devices.bmp58x",
        label="Bosch BMP58x barometer (MCP2221 bridge)",
        family="bmp58x",
        adapter_factory=Bmp58xAdapter,
        params_model=Bmp58xAdapterParams,
        supported_binding_sources=("bmp58x_reading",),
        default_params={"rate_hz": 1.0, "pressure_oversampling": 64, "temperature_oversampling": 4},
        channel_templates=(BMP58X_AMBIENT_PRESSURE, BMP58X_AMBIENT_TEMPERATURE),
        discoverable=True,
        handshake_available=True,
        capabilities=frozenset(
            {
                Capability.HAS_PARAMETER_CONFIG,
                Capability.SUPPORTS_DISCOVERY,
                Capability.SUPPORTS_AUTO_RECONNECT,
            }
        ),
        settings=BMP58X_SETTINGS,
    )


DESCRIPTOR = _build_descriptor()

from capa.devices.registry import register as _register  # noqa: E402

_register(DESCRIPTOR)
