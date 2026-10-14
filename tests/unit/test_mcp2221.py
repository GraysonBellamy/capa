"""Unit tests for :mod:`capa.devices._mcp2221` against the scripted HID handle."""

from __future__ import annotations

import pytest

from capa.devices._mcp2221 import (
    MAX_I2C_SPEED_HZ,
    MIN_I2C_SPEED_HZ,
    BridgeSelector,
    I2cNackError,
    Mcp2221Error,
    Mcp2221I2cBus,
    speed_divider,
)
from tests.fixtures.fake_mcp2221 import FakeMcp2221Hid, opener_for


def _bus(handle: FakeMcp2221Hid, **kwargs: object) -> Mcp2221I2cBus:
    return Mcp2221I2cBus(opener=opener_for(handle), transfer_timeout_s=0.02, **kwargs)  # type: ignore[arg-type]


class TestOpen:
    def test_open_cancels_sets_speed_and_reads_revisions(self) -> None:
        handle = FakeMcp2221Hid()
        bus = _bus(handle, i2c_speed_hz=100_000)
        assert not bus.is_open
        bus.open()
        assert bus.is_open
        assert handle.cancels == 1
        assert handle.speed_divider == speed_divider(100_000) == 117
        assert bus.hardware_revision == "A6"
        assert bus.firmware_revision == "1.2"
        assert bus.actual_speed_hz == pytest.approx(12_000_000 / 120)

    def test_open_is_idempotent(self) -> None:
        handle = FakeMcp2221Hid()
        bus = _bus(handle)
        bus.open()
        bus.open()
        assert handle.cancels == 1

    def test_opener_failure_leaves_bus_closed(self) -> None:
        handle = FakeMcp2221Hid()
        opener = opener_for(handle)
        opener.fail_with = Mcp2221Error("cannot open MCP2221 (first bridge found): no device")
        bus = Mcp2221I2cBus(opener=opener)
        with pytest.raises(Mcp2221Error, match="cannot open"):
            bus.open()
        assert not bus.is_open

    def test_hid_failure_during_open_closes_handle(self) -> None:
        handle = FakeMcp2221Hid()
        handle.fail_hid = OSError("read error")
        bus = _bus(handle)
        with pytest.raises(Mcp2221Error, match="HID I/O failed"):
            bus.open()
        assert not bus.is_open
        assert handle.closed

    def test_selector_describes_itself(self) -> None:
        assert BridgeSelector().describe() == "first bridge found"
        assert BridgeSelector(serial="0006774894").describe() == "serial=0006774894"
        assert "path=" in BridgeSelector(path=b"\\\\?\\HID#x").describe()
        assert Mcp2221I2cBus(serial="abc").selector == BridgeSelector(serial="abc")

    def test_close_is_idempotent_and_never_raises(self) -> None:
        class ExplodingHandle(FakeMcp2221Hid):
            def close(self) -> None:
                raise OSError("already gone")

        handle = ExplodingHandle()
        bus = _bus(handle)
        bus.open()
        bus.close()
        bus.close()
        assert not bus.is_open


class TestSpeed:
    def test_divider_formula(self) -> None:
        assert speed_divider(100_000) == 117
        assert speed_divider(400_000) == 27

    @pytest.mark.parametrize("hz", [MIN_I2C_SPEED_HZ - 1, MAX_I2C_SPEED_HZ + 1, 0])
    def test_out_of_range_speed_rejected_before_any_io(self, hz: int) -> None:
        with pytest.raises(ValueError, match="outside the MCP2221 range"):
            Mcp2221I2cBus(i2c_speed_hz=hz)

    def test_set_speed_refused_while_transfer_runs(self) -> None:
        class BusyHandle(FakeMcp2221Hid):
            def _answer(self, command: bytes) -> bytes:
                response = bytearray(super()._answer(command))
                if command[0] == 0x10 and len(command) > 3 and command[3] == 0x20:
                    response[3] = 0x21
                return bytes(response)

        bus = _bus(BusyHandle())
        with pytest.raises(Mcp2221Error, match="transfer is in progress"):
            bus.open()


class TestTransfers:
    def test_write_then_read_round_trip(self) -> None:
        handle = FakeMcp2221Hid()
        handle.read_data = b"\x12\x34\x56"
        bus = _bus(handle)
        bus.open()
        bus.write(0x44, b"\xfd")
        assert handle.i2c_writes == [(0x44, b"\xfd")]
        assert bus.read(0x44, 3) == b"\x12\x34\x56"
        assert handle.i2c_reads == [(0x44, 3)]
        assert bus.engine_state() == 0x00

    def test_write_to_absent_target_nacks_and_recovers(self) -> None:
        handle = FakeMcp2221Hid()
        bus = _bus(handle)
        bus.open()
        with pytest.raises(I2cNackError, match="no acknowledge"):
            bus.write(0x45, b"\x89")
        # The cancel ran, so the bus is usable without any further action.
        assert handle.cancels == 2
        assert handle.engine_state == 0x00
        bus.write(0x44, b"\x89")

    def test_busy_target_nacks_read_and_recovers(self) -> None:
        handle = FakeMcp2221Hid()
        handle.read_data = b"\x01\x02"
        handle.nack_reads = 1
        bus = _bus(handle)
        bus.open()
        with pytest.raises(I2cNackError, match="target absent or busy"):
            bus.read(0x44, 2)
        assert handle.engine_state == 0x00
        assert bus.read(0x44, 2) == b"\x01\x02"

    def test_refused_transfer_after_unrecovered_nack(self) -> None:
        """A NACK the caller did not clear (the engine refusing the next
        command) is reported as a NACK and cleared then."""
        handle = FakeMcp2221Hid()
        bus = _bus(handle)
        bus.open()
        handle.engine_state = 0x25
        with pytest.raises(I2cNackError, match="previous transfer"):
            bus.write(0x44, b"\xfd")
        assert handle.engine_state == 0x00

    def test_read_timeout_cancels(self) -> None:
        class NeverReady(FakeMcp2221Hid):
            def _answer(self, command: bytes) -> bytes:
                if command[0] == 0x40:
                    response = bytearray(64)
                    response[0] = 0x40
                    response[1] = 0x41
                    response[3] = 0x7F
                    return bytes(response)
                return super()._answer(command)

        handle = NeverReady()
        bus = _bus(handle)
        bus.open()
        with pytest.raises(Mcp2221Error, match="timed out"):
            bus.read(0x44, 6)
        assert handle.cancels == 2

    def test_short_read_is_an_error(self) -> None:
        handle = FakeMcp2221Hid()
        handle.read_data = b"\x01"
        bus = _bus(handle)
        bus.open()
        with pytest.raises(Mcp2221Error, match="returned 1 of 6 bytes"):
            bus.read(0x44, 6)

    def test_hid_io_error_is_wrapped(self) -> None:
        handle = FakeMcp2221Hid()
        bus = _bus(handle)
        bus.open()
        handle.fail_hid = OSError("device disconnected")
        with pytest.raises(Mcp2221Error, match="HID I/O failed: device disconnected"):
            bus.write(0x44, b"\xfd")

    def test_no_response_is_an_error(self) -> None:
        handle = FakeMcp2221Hid()
        bus = _bus(handle)
        bus.open()
        handle.empty_responses = 1
        with pytest.raises(Mcp2221Error, match="no response"):
            bus.status()

    def test_mismatched_response_is_an_error(self) -> None:
        class WrongEcho(FakeMcp2221Hid):
            def _answer(self, command: bytes) -> bytes:
                response = bytearray(super()._answer(command))
                if command[0] == 0x90:
                    response[0] = 0x91
                return bytes(response)

        bus = _bus(WrongEcho())
        bus.open()
        with pytest.raises(Mcp2221Error, match="answered command 0x90"):
            bus.write(0x44, b"\xfd")

    def test_transfer_requires_open(self) -> None:
        bus = _bus(FakeMcp2221Hid())
        with pytest.raises(Mcp2221Error, match="not open"):
            bus.status()

    @pytest.mark.parametrize("address", [0x07, 0x78])
    def test_address_outside_7bit_range_rejected(self, address: int) -> None:
        bus = _bus(FakeMcp2221Hid())
        bus.open()
        with pytest.raises(ValueError, match="7-bit range"):
            bus.write(address, b"\x00")
        with pytest.raises(ValueError, match="7-bit range"):
            bus.read(address, 1)

    def test_transfer_length_limits(self) -> None:
        bus = _bus(FakeMcp2221Hid())
        bus.open()
        with pytest.raises(ValueError, match="exceeds 60"):
            bus.write(0x44, bytes(61))
        with pytest.raises(ValueError, match="outside 1–60"):
            bus.read(0x44, 0)
