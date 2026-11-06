"""MCP2221 USB-to-I2C bridge — the HID surface capa needs to be an I2C master.

The Microchip MCP2221 enumerates as a composite USB device: a CDC serial
port (its UART, unused here) and a vendor HID interface that carries the
I2C engine. Every HID report is 64 bytes; the host writes one command
report and reads one response report, about 1–2 ms per round trip. Four
commands are enough to drive an I2C sensor:

* ``0x10`` Status / Set Parameters — reads the engine state, cancels a
  stuck transfer, sets the bus speed.
* ``0x90`` I2C Write Data — start, address, up to 60 bytes, stop.
* ``0x91`` I2C Read Data — start, address, request N bytes, stop.
* ``0x40`` I2C Get Data — collects the bytes a read produced.

Three quirks the driver owns:

* A NACK (a target that is busy or absent) leaves the engine in state
  ``0x25`` and every later transfer is refused until a cancel.
  :meth:`Mcp2221I2cBus.read` and :meth:`Mcp2221I2cBus.write` cancel
  before raising :class:`I2cNackError`, so the bus is usable again
  straight away.
* A cancel sent to an *idle* engine can wedge it: the chip forces a
  STOP, and on the rig's bus (SHT45 and BMP581 breakouts) that STOP
  times out every time, leaving the engine in state ``0x62``, refusing
  every transfer, every further cancel and the speed setting. So
  :meth:`Mcp2221I2cBus.cancel` only cancels a busy engine.
* A process killed mid-transfer leaves the engine busy across a reopen,
  so :meth:`Mcp2221I2cBus.open` cancels a busy engine before setting the
  speed, and resets the chip (command ``0x70``, as a USB replug would)
  when the cancel does not bring it back to idle.

The class is synchronous: every method blocks for one or more HID
transactions. Adapters call it through :func:`anyio.to_thread.run_sync`
so the worker loop stays free. Nothing here writes the chip's flash:
GPIO, ADC/DAC and USB descriptors are left exactly as found, and a chip
reset only restarts the firmware.

Several sensors on one bridge share one :class:`Mcp2221I2cBus` through
:data:`BUS_POOL`. A second HID handle on the same chip is no
alternative: the OS hands every open handle a copy of every response,
so two handles would read each other's answers. The pool opens the bus
for the first sensor, hands later ones the same bus and a lock to hold
across each multi-transfer transaction, and closes it after the last.

The ``hid`` module (PyPI ``hidapi``) is imported lazily inside the
default opener, so this module and the adapters built on it import
cleanly on a machine without the native library; only opening a bridge
needs it.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

VENDOR_ID: Final = 0x04D8
"""Microchip's USB vendor id."""

PRODUCT_ID: Final = 0x00DD
"""The MCP2221 / MCP2221A product id."""

MIN_I2C_SPEED_HZ: Final = 47_000
"""Slowest bus clock the divider can express (12 MHz / 258)."""

MAX_I2C_SPEED_HZ: Final = 400_000
"""Fast-mode I2C, the fastest the MCP2221 supports."""

MAX_TRANSFER_LEN: Final = 60
"""Bytes one write or read command carries. Enough for any sensor
register exchange; multi-report transfers are not implemented."""

_REPORT_LEN: Final = 64
_CLOCK_HZ: Final = 12_000_000

_CMD_STATUS: Final = 0x10
_CMD_I2C_WRITE: Final = 0x90
_CMD_I2C_READ: Final = 0x91
_CMD_I2C_GET: Final = 0x40

_CMD_RESET: Final = bytes([0x70, 0xAB, 0xCD, 0xEF])
"""Reset Chip: the firmware restarts and the chip re-enumerates on USB.
The chip sends no response."""

_SUBCMD_CANCEL: Final = 0x10
_SUBCMD_SET_SPEED: Final = 0x20
_SPEED_NOT_SET_BUSY: Final = 0x21

_STATE_IDLE: Final = 0x00
_STATE_ADDRESS_NACK: Final = 0x25
"""Engine state after the target failed to acknowledge its address."""

_REOPEN_POLL_S: Final = 0.1

_GET_NO_DATA: Final = 0x7F
"""``I2C Get Data`` length byte while the read is still in flight."""

_STATUS_HW_REV: Final = slice(46, 48)
_STATUS_FW_REV: Final = slice(48, 50)
_STATUS_ENGINE_STATE: Final = 8
_STATUS_SPEED_DIVIDER: Final = 14


class Mcp2221Error(Exception):
    """A bridge failure: HID I/O, a refused transfer, or a timeout."""


class I2cNackError(Mcp2221Error):
    """The target did not acknowledge its address: absent, or busy."""


class I2cEngineStuckError(Mcp2221Error):
    """The I2C engine stayed busy through a cancel; only a chip reset helps."""


class HidHandle(Protocol):
    """The slice of a ``hid.device`` the bus uses; the test seam."""

    def write(self, data: bytes, /) -> int: ...

    def read(self, max_length: int, timeout_ms: int, /) -> list[int]: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class BridgeSelector:
    """Which MCP2221 to open.

    ``path`` is the OS-specific HID path from :func:`enumerate_bridges`
    and wins when set. ``serial`` is the USB serial string, which the
    chip only exposes when serial-number enumeration is enabled in its
    flash (it is off by default, and :func:`enumerate_bridges` then
    reports ``None``). With neither, the first bridge found is opened.
    """

    serial: str | None = None
    path: bytes | None = None

    def describe(self) -> str:
        """Operator-facing form for error messages."""
        if self.path is not None:
            return f"path={self.path!r}"
        if self.serial:
            return f"serial={self.serial}"
        return "first bridge found"


@dataclass(frozen=True, slots=True)
class BridgeInfo:
    """One MCP2221 HID interface as the OS enumerates it."""

    path: bytes
    serial: str | None
    product: str | None
    manufacturer: str | None


HidOpener = Callable[[BridgeSelector], HidHandle]
"""``selector → open handle``. Production uses :func:`open_hid_handle`;
tests inject a scripted handle."""


def enumerate_bridges() -> list[BridgeInfo]:
    """List the MCP2221 HID interfaces present on this machine.

    Pure enumeration, no handle is opened. An empty USB serial string
    (the chip's default) is reported as ``None``.
    """
    import hid  # noqa: PLC0415 — native library, loaded only when a bridge is touched

    out: list[BridgeInfo] = []
    for entry in hid.enumerate(VENDOR_ID, PRODUCT_ID):
        path = entry.get("path")
        if not isinstance(path, bytes):
            continue
        out.append(
            BridgeInfo(
                path=path,
                serial=_text_or_none(entry.get("serial_number")),
                product=_text_or_none(entry.get("product_string")),
                manufacturer=_text_or_none(entry.get("manufacturer_string")),
            )
        )
    return out


def open_hid_handle(selector: BridgeSelector) -> HidHandle:
    """Open the HID interface ``selector`` names via ``hidapi``."""
    import hid  # noqa: PLC0415 — native library, loaded only when a bridge is touched

    device: Any = hid.device()
    try:
        if selector.path is not None:
            device.open_path(selector.path)
        elif selector.serial:
            device.open(VENDOR_ID, PRODUCT_ID, selector.serial)
        else:
            device.open(VENDOR_ID, PRODUCT_ID)
    except (OSError, ValueError) as exc:
        raise Mcp2221Error(f"cannot open MCP2221 ({selector.describe()}): {exc}") from exc
    device.set_nonblocking(False)
    handle: HidHandle = device
    return handle


def speed_divider(hz: int) -> int:
    """The Set Parameters divider for a bus clock of ``hz``.

    The engine clocks the bus at 12 MHz / (divider + 3).
    """
    if not MIN_I2C_SPEED_HZ <= hz <= MAX_I2C_SPEED_HZ:
        raise ValueError(
            f"I2C speed {hz} Hz outside the MCP2221 range {MIN_I2C_SPEED_HZ}–{MAX_I2C_SPEED_HZ} Hz"
        )
    return max(0, min(255, round(_CLOCK_HZ / hz) - 3))


class Mcp2221I2cBus:
    """One MCP2221 as a synchronous I2C master.

    Construction does no I/O; :meth:`open` claims the HID interface,
    clears any stale transfer and sets the bus speed. Every method
    raises :class:`Mcp2221Error` (or its :class:`I2cNackError` subclass)
    rather than letting ``hidapi``'s mixed exception types through.
    """

    __slots__ = (
        "_firmware_revision",
        "_handle",
        "_hardware_revision",
        "_hid_timeout_ms",
        "_i2c_speed_hz",
        "_opener",
        "_reset_settle_s",
        "_reset_timeout_s",
        "_selector",
        "_speed_divider",
        "_transfer_timeout_s",
    )

    def __init__(
        self,
        *,
        serial: str | None = None,
        path: bytes | None = None,
        i2c_speed_hz: int = 100_000,
        opener: HidOpener = open_hid_handle,
        hid_timeout_ms: int = 100,
        transfer_timeout_s: float = 0.1,
        reset_settle_s: float = 0.5,
        reset_timeout_s: float = 5.0,
    ) -> None:
        speed_divider(i2c_speed_hz)  # validate early, before any I/O
        self._selector = BridgeSelector(serial=serial, path=path)
        self._i2c_speed_hz = i2c_speed_hz
        self._opener = opener
        self._hid_timeout_ms = hid_timeout_ms
        self._transfer_timeout_s = transfer_timeout_s
        self._reset_settle_s = reset_settle_s
        self._reset_timeout_s = reset_timeout_s
        self._handle: HidHandle | None = None
        self._hardware_revision: str | None = None
        self._firmware_revision: str | None = None
        self._speed_divider: int | None = None

    # ------------------------------------------------------------------ identity

    @property
    def selector(self) -> BridgeSelector:
        """Which bridge this bus opens."""
        return self._selector

    @property
    def is_open(self) -> bool:
        """Whether :meth:`open` has claimed the HID interface."""
        return self._handle is not None

    @property
    def i2c_speed_hz(self) -> int:
        """The requested bus clock."""
        return self._i2c_speed_hz

    @property
    def hardware_revision(self) -> str | None:
        """Chip hardware revision (``"A6"``) read at :meth:`open`."""
        return self._hardware_revision

    @property
    def firmware_revision(self) -> str | None:
        """Chip firmware revision (``"1.2"``) read at :meth:`open`."""
        return self._firmware_revision

    @property
    def actual_speed_hz(self) -> float | None:
        """The bus clock the chip reports it is using, after :meth:`open`."""
        if self._speed_divider is None:
            return None
        return _CLOCK_HZ / (self._speed_divider + 3)

    # ------------------------------------------------------------------ lifecycle

    def open(self) -> None:
        """Claim the HID interface, clear any stale transfer, set the speed.

        A busy engine is cancelled; one the cancel cannot bring back to
        idle (the ``0x62`` wedge) gets a :meth:`reset_chip`. Idempotent:
        a second call on an open bus is a no-op.
        """
        if self._handle is not None:
            return
        self._handle = self._opener(self._selector)
        try:
            try:
                self.cancel()
            except I2cEngineStuckError:
                self.reset_chip()
            status = self.status()
            self._hardware_revision = _revision(status[_STATUS_HW_REV])
            self._firmware_revision = _revision(status[_STATUS_FW_REV], dotted=True)
            self.set_speed(self._i2c_speed_hz)
        except Mcp2221Error:
            self.close()
            raise

    def close(self) -> None:
        """Release the HID interface. Idempotent; never raises."""
        handle, self._handle = self._handle, None
        if handle is None:
            return
        # A dead handle (bridge unplugged) has nothing left to release.
        with contextlib.suppress(Exception):
            handle.close()

    # ------------------------------------------------------------------ engine control

    def status(self) -> bytes:
        """The raw 64-byte Status / Set Parameters response."""
        return self._transfer(bytes([_CMD_STATUS]))

    def engine_state(self) -> int:
        """The I2C engine's state machine byte; ``0x00`` is idle."""
        return self.status()[_STATUS_ENGINE_STATE]

    def cancel(self) -> None:
        """Abort the current transfer and wait for the engine to go idle.

        The cure for a NACK or an interrupted transfer. A no-op when the
        engine is already idle: cancelling an idle engine makes the chip
        force a STOP that can time out, leaving it in state ``0x62``,
        which no further cancel clears (only :meth:`reset_chip` does). Raises
        :class:`Mcp2221Error` if the engine stays busy, which on this chip
        means the bus itself is stuck (SDA held low by a target).
        """
        if self.engine_state() == _STATE_IDLE:
            return
        self._transfer(bytes([_CMD_STATUS, 0x00, _SUBCMD_CANCEL]))
        deadline = time.perf_counter() + self._transfer_timeout_s
        while True:
            state = self.engine_state()
            if state == _STATE_IDLE:
                return
            if time.perf_counter() > deadline:
                raise I2cEngineStuckError(
                    f"I2C engine did not return to idle after cancel (state 0x{state:02x})"
                )
            time.sleep(0.001)

    def reset_chip(self) -> None:
        """Restart the chip's firmware, as a USB replug would, and reopen it.

        The one cure for an engine no cancel clears. The chip drops off
        USB and re-enumerates in about a second, so the HID handle is
        closed and reopened through the same selector; the bus speed
        reverts to the chip default until :meth:`set_speed`. Nothing in
        flash changes. Raises :class:`Mcp2221Error` if the chip is not
        back, idle, within ``reset_timeout_s``.
        """
        handle = self._handle
        if handle is None:
            raise Mcp2221Error("MCP2221 bridge is not open")
        report = bytes(1) + _CMD_RESET + bytes(_REPORT_LEN - len(_CMD_RESET))
        try:
            handle.write(report)
        except (OSError, ValueError) as exc:
            raise Mcp2221Error(f"MCP2221 reset command failed: {exc}") from exc
        self.close()
        self._speed_divider = None
        time.sleep(self._reset_settle_s)
        deadline = time.perf_counter() + self._reset_timeout_s
        while True:
            try:
                self._handle = self._opener(self._selector)
                state = self.engine_state()
                break
            except Mcp2221Error as exc:
                self.close()
                if time.perf_counter() > deadline:
                    raise Mcp2221Error(
                        f"MCP2221 did not come back after a chip reset: {exc}"
                    ) from exc
                time.sleep(_REOPEN_POLL_S)
        if state != _STATE_IDLE:
            raise Mcp2221Error(f"I2C engine in state 0x{state:02x} even after a chip reset")

    def set_speed(self, hz: int) -> None:
        """Set the bus clock. Refused by the chip while a transfer runs."""
        divider = speed_divider(hz)
        response = self._transfer(bytes([_CMD_STATUS, 0x00, 0x00, _SUBCMD_SET_SPEED, divider]))
        if response[3] == _SPEED_NOT_SET_BUSY:
            raise Mcp2221Error("I2C speed not set: a transfer is in progress")
        self._i2c_speed_hz = hz
        self._speed_divider = response[_STATUS_SPEED_DIVIDER]

    # ------------------------------------------------------------------ transfers

    def write(self, address: int, data: bytes) -> None:
        """Start, address (write), ``data``, stop.

        Waits for the engine to go idle so a NACK surfaces here as
        :class:`I2cNackError` rather than on the next command.
        """
        _check_address(address)
        if len(data) > MAX_TRANSFER_LEN:
            raise ValueError(f"write of {len(data)} bytes exceeds {MAX_TRANSFER_LEN}")
        command = bytes([_CMD_I2C_WRITE, len(data) & 0xFF, len(data) >> 8, address << 1]) + data
        response = self._transfer(command)
        if response[1] != 0x00:
            self._refused("write", address, response[2])
        self._wait_idle("write", address)

    def read(self, address: int, length: int) -> bytes:
        """Start, address (read), ``length`` bytes, stop."""
        _check_address(address)
        if not 1 <= length <= MAX_TRANSFER_LEN:
            raise ValueError(f"read length {length} outside 1–{MAX_TRANSFER_LEN}")
        command = bytes([_CMD_I2C_READ, length & 0xFF, length >> 8, (address << 1) | 1])
        response = self._transfer(command)
        if response[1] != 0x00:
            self._refused("read", address, response[2])
        deadline = time.perf_counter() + self._transfer_timeout_s
        while True:
            response = self._transfer(bytes([_CMD_I2C_GET]))
            if response[1] == 0x00 and response[3] != _GET_NO_DATA:
                got = response[3]
                if got != length:
                    self.cancel()
                    raise Mcp2221Error(
                        f"I2C read from 0x{address:02x} returned {got} of {length} bytes"
                    )
                return bytes(response[4 : 4 + got])
            state = response[2]
            if state == _STATE_ADDRESS_NACK:
                self.cancel()
                raise I2cNackError(
                    f"I2C read from 0x{address:02x}: no acknowledge (target absent or busy)"
                )
            if time.perf_counter() > deadline:
                self.cancel()
                raise Mcp2221Error(
                    f"I2C read from 0x{address:02x} timed out (engine state 0x{state:02x})"
                )
            time.sleep(0.001)

    # ------------------------------------------------------------------ internals

    def _wait_idle(self, what: str, address: int) -> None:
        deadline = time.perf_counter() + self._transfer_timeout_s
        while True:
            state = self.engine_state()
            if state == _STATE_IDLE:
                return
            if state == _STATE_ADDRESS_NACK:
                self.cancel()
                raise I2cNackError(
                    f"I2C {what} to 0x{address:02x}: no acknowledge (target absent or busy)"
                )
            if time.perf_counter() > deadline:
                self.cancel()
                raise Mcp2221Error(
                    f"I2C {what} to 0x{address:02x} timed out (engine state 0x{state:02x})"
                )
            time.sleep(0.001)

    def _refused(self, what: str, address: int, state: int) -> None:
        """The engine would not start a transfer: clear it and report."""
        with contextlib.suppress(Mcp2221Error):
            self.cancel()
        if state == _STATE_ADDRESS_NACK:
            raise I2cNackError(
                f"I2C {what} to 0x{address:02x}: no acknowledge on the previous transfer"
            )
        raise Mcp2221Error(f"I2C {what} to 0x{address:02x} refused (engine state 0x{state:02x})")

    def _transfer(self, command: bytes) -> bytes:
        """Send one command report and return its response report."""
        handle = self._handle
        if handle is None:
            raise Mcp2221Error("MCP2221 bridge is not open")
        report = bytes(1) + command + bytes(_REPORT_LEN - len(command))  # report id 0
        try:
            handle.write(report)
            raw = handle.read(_REPORT_LEN, self._hid_timeout_ms)
        except (OSError, ValueError) as exc:
            raise Mcp2221Error(f"MCP2221 HID I/O failed: {exc}") from exc
        if not raw:
            raise Mcp2221Error(f"MCP2221 gave no response within {self._hid_timeout_ms} ms")
        response = bytes(raw)
        if len(response) < _REPORT_LEN or response[0] != command[0]:
            raise Mcp2221Error(
                f"MCP2221 answered command 0x{command[0]:02x} with {response[:4].hex()!r}"
            )
        return response


@dataclass(eq=False)
class SharedBus:
    """One open bridge as the sensors on it share it.

    Hold :attr:`lock` for the whole of a sensor transaction (command,
    conversion wait, read): the bus interleaves nothing on its own, and a
    second sensor's write landing mid-transaction would be read back as
    the first sensor's answer.
    """

    key: str
    bus: Mcp2221I2cBus
    lock: threading.Lock = field(default_factory=threading.Lock)
    users: int = 0


class BusPool:
    """Opens each bridge once, however many sensor adapters use it.

    Keyed by the adapter's ``resource_id`` (one bridge, one key). Every
    method blocks on HID I/O; adapters call them through
    :func:`anyio.to_thread.run_sync`. Thread-safe.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buses: dict[str, SharedBus] = {}

    def acquire(
        self, key: str, *, i2c_speed_hz: int, factory: Callable[[], Mcp2221I2cBus]
    ) -> SharedBus:
        """The open bus for ``key``, opening it via ``factory`` if no
        sensor holds it yet. Every sensor on a bridge must ask for the same
        bus speed: the chip has one clock."""
        with self._lock:
            shared = self._buses.get(key)
            if shared is None:
                bus = factory()
                bus.open()
                shared = SharedBus(key=key, bus=bus)
                self._buses[key] = shared
            elif shared.bus.i2c_speed_hz != i2c_speed_hz:
                raise Mcp2221Error(
                    f"MCP2221 bridge {key} is already open at {shared.bus.i2c_speed_hz} Hz; "
                    f"every sensor on one bridge must use the same i2c_speed_hz "
                    f"(this one asks for {i2c_speed_hz} Hz)"
                )
            shared.users += 1
            return shared

    def release(self, shared: SharedBus) -> None:
        """Give back a bus from :meth:`acquire`; the last user closes it."""
        with self._lock:
            shared.users -= 1
            if shared.users > 0:
                return
            if self._buses.get(shared.key) is shared:
                del self._buses[shared.key]
        with shared.lock:
            shared.bus.close()

    def reopen(self, shared: SharedBus) -> None:
        """Close and reopen a bus after a HID failure, under its lock so no
        other sensor is mid-transaction. Raises :class:`Mcp2221Error` if the
        bridge does not come back; the bus is then closed and the next
        transaction fails until a later reopen succeeds."""
        with shared.lock:
            shared.bus.close()
            shared.bus.open()

    def open_keys(self) -> list[str]:
        """Keys of the buses currently open, for diagnostics and tests."""
        with self._lock:
            return sorted(self._buses)


BUS_POOL: Final = BusPool()
"""The process-wide pool every MCP2221 sensor adapter shares by default."""


def bridge_revision(shared: SharedBus | None) -> str | None:
    """Hardware / firmware revision of an open bridge (``"A6 / 1.2"``)."""
    if shared is None or shared.bus.hardware_revision is None:
        return None
    return f"{shared.bus.hardware_revision} / {shared.bus.firmware_revision or '?'}"


def _check_address(address: int) -> None:
    if not 0x08 <= address <= 0x77:
        raise ValueError(f"I2C address 0x{address:02x} outside the 7-bit range 0x08–0x77")


def _revision(raw: bytes, *, dotted: bool = False) -> str | None:
    if len(raw) != 2 or not all(0x20 <= b < 0x7F for b in raw):
        return None
    text = raw.decode("ascii")
    return f"{text[0]}.{text[1]}" if dotted else text


def _text_or_none(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


__all__ = [
    "BUS_POOL",
    "MAX_I2C_SPEED_HZ",
    "MAX_TRANSFER_LEN",
    "MIN_I2C_SPEED_HZ",
    "PRODUCT_ID",
    "VENDOR_ID",
    "BridgeInfo",
    "BridgeSelector",
    "BusPool",
    "HidHandle",
    "HidOpener",
    "I2cEngineStuckError",
    "I2cNackError",
    "Mcp2221Error",
    "Mcp2221I2cBus",
    "SharedBus",
    "bridge_revision",
    "enumerate_bridges",
    "open_hid_handle",
    "speed_divider",
]
