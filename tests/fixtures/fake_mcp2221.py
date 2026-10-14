"""A scripted MCP2221 HID handle with an SHT4x on its bus, for unit tests.

:class:`FakeMcp2221Hid` answers the four HID commands
:class:`capa.devices._mcp2221.Mcp2221I2cBus` sends, keeping the chip's
I2C engine state so the NACK-then-cancel dance is exercised for real.
:class:`FakeSht4xHid` adds the sensor: measure commands queue a
CRC-protected response, the serial command answers a fixed serial, and
knobs corrupt a response, make the sensor NACK, or fail the HID link.
"""

from __future__ import annotations

from capa.devices._mcp2221 import BridgeSelector, HidHandle
from capa.devices.sht4x import crc8, raw_from_humidity, raw_from_temperature

_CMD_STATUS = 0x10
_CMD_I2C_WRITE = 0x90
_CMD_I2C_READ = 0x91
_CMD_I2C_GET = 0x40
_STATE_IDLE = 0x00
_STATE_NACK = 0x25


def sht4x_payload(temperature_c: float, humidity_pct: float) -> bytes:
    """A 6-byte sensor response for the given values."""
    t_word = raw_from_temperature(temperature_c).to_bytes(2, "big")
    rh_word = raw_from_humidity(humidity_pct).to_bytes(2, "big")
    return t_word + bytes([crc8(t_word)]) + rh_word + bytes([crc8(rh_word)])


def serial_payload(serial: int) -> bytes:
    """A 6-byte serial-number response."""
    high = (serial >> 16).to_bytes(2, "big")
    low = (serial & 0xFFFF).to_bytes(2, "big")
    return high + bytes([crc8(high)]) + low + bytes([crc8(low)])


class FakeMcp2221Hid:
    """The chip side: HID framing plus the I2C engine's state machine.

    ``present`` is the set of 7-bit addresses that acknowledge. A write
    or read to any other address leaves the engine NACKed until a cancel,
    like the real chip.
    """

    def __init__(self, *, present: frozenset[int] = frozenset({0x44})) -> None:
        self.present = set(present)
        self.commands: list[bytes] = []
        """Every command report received (without the report id)."""
        self.i2c_writes: list[tuple[int, bytes]] = []
        self.i2c_reads: list[tuple[int, int]] = []
        self.cancels = 0
        self.speed_divider = 118
        self.closed = False
        self.engine_state = _STATE_IDLE
        self.read_data: bytes = b""
        """What the next accepted I2C read returns."""
        self.nack_reads = 0
        """How many upcoming reads the target NACKs (a busy sensor)."""
        self.fail_hid: OSError | None = None
        """Set to make every HID transaction raise (bridge unplugged)."""
        self.empty_responses = 0
        """How many upcoming HID reads return nothing (a timeout)."""
        self._response: bytes | None = None
        self._read_pending: bytes | None = None

    # -- the HidHandle surface ---------------------------------------------

    def write(self, data: bytes, /) -> int:
        if self.fail_hid is not None:
            raise self.fail_hid
        assert data[0] == 0, "report id 0 expected"
        command = bytes(data[1:])
        self.commands.append(command)
        self._response = self._answer(command)
        return len(data)

    def read(self, max_length: int, timeout_ms: int, /) -> list[int]:
        if self.fail_hid is not None:
            raise self.fail_hid
        if self.empty_responses > 0:
            self.empty_responses -= 1
            return []
        response, self._response = self._response, None
        return list(response or b"")

    def close(self) -> None:
        self.closed = True

    # -- the sensor side, overridable --------------------------------------

    def on_i2c_write(self, address: int, data: bytes) -> None:
        """Hook for a target's reaction to a write; the base does nothing."""

    # -- engine ------------------------------------------------------------

    def _answer(self, command: bytes) -> bytes:
        response = bytearray(64)
        response[0] = command[0]
        kind = command[0]
        if kind == _CMD_STATUS:
            if len(command) > 2 and command[2] == 0x10:
                self.cancels += 1
                self.engine_state = _STATE_IDLE
                self._read_pending = None
                response[2] = 0x10
            if len(command) > 4 and command[3] == 0x20:
                self.speed_divider = command[4]
                response[3] = 0x20
            response[8] = self.engine_state
            response[14] = self.speed_divider
            response[46:48] = b"A6"
            response[48:50] = b"12"
        elif kind == _CMD_I2C_WRITE:
            if self.engine_state != _STATE_IDLE:
                response[1] = 0x01
                response[2] = self.engine_state
            else:
                length = command[1] | (command[2] << 8)
                address = command[3] >> 1
                payload = bytes(command[4 : 4 + length])
                self.i2c_writes.append((address, payload))
                if address in self.present:
                    self.on_i2c_write(address, payload)
                else:
                    self.engine_state = _STATE_NACK
        elif kind == _CMD_I2C_READ:
            if self.engine_state != _STATE_IDLE:
                response[1] = 0x01
                response[2] = self.engine_state
            else:
                length = command[1] | (command[2] << 8)
                address = command[3] >> 1
                self.i2c_reads.append((address, length))
                if address not in self.present or self.nack_reads > 0:
                    if self.nack_reads > 0:
                        self.nack_reads -= 1
                    self.engine_state = _STATE_NACK
                    self._read_pending = None
                else:
                    self._read_pending = self.read_data[:length]
        elif kind == _CMD_I2C_GET:
            if self.engine_state == _STATE_NACK:
                response[1] = 0x41
                response[2] = _STATE_NACK
                response[3] = 0x7F
            elif self._read_pending is None:
                response[1] = 0x41
                response[3] = 0x7F
            else:
                data, self._read_pending = self._read_pending, None
                response[3] = len(data)
                response[4 : 4 + len(data)] = data
        return bytes(response)


class FakeSht4xHid(FakeMcp2221Hid):
    """An MCP2221 with one SHT4x on the bus.

    ``temperature_c`` / ``humidity_pct`` are what the sensor reports;
    change them between polls. ``corrupt_next`` flips a CRC byte on the
    next response once. Heater commands report ``temperature_c`` plus
    ``heater_bump_c``.
    """

    MEASURE_COMMANDS = frozenset({0xFD, 0xF6, 0xE0})
    HEATER_COMMANDS = frozenset({0x39, 0x32, 0x2F, 0x24, 0x1E, 0x15})

    def __init__(
        self,
        *,
        address: int = 0x44,
        serial: int = 0x1643E746,
        temperature_c: float = 22.5,
        humidity_pct: float = 48.0,
    ) -> None:
        super().__init__(present=frozenset({address}))
        self.address = address
        self.serial = serial
        self.temperature_c = temperature_c
        self.humidity_pct = humidity_pct
        self.heater_bump_c = 5.0
        self.corrupt_next = False
        self.sensor_commands: list[int] = []
        """Every command byte the sensor received."""

    def on_i2c_write(self, address: int, data: bytes) -> None:
        if address != self.address or len(data) != 1:
            return
        command = data[0]
        self.sensor_commands.append(command)
        if command in self.MEASURE_COMMANDS:
            self.read_data = sht4x_payload(self.temperature_c, self.humidity_pct)
        elif command in self.HEATER_COMMANDS:
            self.read_data = sht4x_payload(
                self.temperature_c + self.heater_bump_c, self.humidity_pct
            )
        elif command == 0x89:
            self.read_data = serial_payload(self.serial)
        elif command == 0x94:
            self.read_data = b""
        if self.corrupt_next and self.read_data:
            self.corrupt_next = False
            corrupted = bytearray(self.read_data)
            corrupted[2] ^= 0xFF
            self.read_data = bytes(corrupted)


def opener_for(handle: HidHandle) -> OpenerFor:
    """An opener that hands out ``handle`` and records each selector."""
    return OpenerFor(handle)


class OpenerFor:
    """Callable ``selector → handle`` that counts opens; raises after
    :attr:`fail_with` is set, for reopen-failure paths."""

    def __init__(self, handle: HidHandle) -> None:
        self.handle = handle
        self.selectors: list[BridgeSelector] = []
        self.fail_with: Exception | None = None

    def __call__(self, selector: BridgeSelector) -> HidHandle:
        self.selectors.append(selector)
        if self.fail_with is not None:
            raise self.fail_with
        return self.handle


__all__ = [
    "FakeMcp2221Hid",
    "FakeSht4xHid",
    "OpenerFor",
    "opener_for",
    "serial_payload",
    "sht4x_payload",
]
