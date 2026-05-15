"""Watlow module-level ``discover()``.

These tests verify the in-repo wrapper around
:func:`watlowlib.find_devices` (shipped in watlowlib 0.5.0). The
library iterates the cartesian product of
``ports × baudrates × protocols × addresses`` and emits one
:class:`FindResult` per probe with an explicit ``ok`` flag plus the
baud rate that resolved the device.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from unittest.mock import MagicMock

import pytest
import watlowlib
from watlowlib.protocol.base import ProtocolKind

from capa.devices import watlow


class _StubInfo:
    def __init__(
        self,
        *,
        part_number: str = "PM3R1CA",
        firmware: str = "1.2",
        hardware: str = "9.0",
        family: str = "PM",
    ) -> None:
        self.part_number = MagicMock(raw=part_number)
        self.firmware_id = firmware
        self.hardware_id = hardware
        self.family = MagicMock(value=family)


class _StubFindResult:
    def __init__(
        self,
        *,
        port: str,
        address: int,
        baudrate: int,
        protocol: ProtocolKind,
        ok: bool,
        info: _StubInfo | None = None,
        error: object | None = None,
    ) -> None:
        self.port = port
        self.address = address
        self.baudrate = baudrate
        self.protocol = protocol
        self.ok = ok
        self.info = info
        self.error = error


@pytest.mark.anyio
async def test_watlow_discover_yields_rows_per_responsive_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each ``ok=True`` probe with an ``info`` becomes a row."""

    async def fake_find_devices(**_kwargs: Any) -> list[_StubFindResult]:
        return [
            _StubFindResult(
                port="COM6",
                address=1,
                baudrate=38400,
                protocol=ProtocolKind.MODBUS_RTU,
                ok=True,
                info=_StubInfo(part_number="PM3R1CA"),
            ),
            _StubFindResult(
                port="COM6",
                address=1,
                baudrate=19200,
                protocol=ProtocolKind.MODBUS_RTU,
                ok=False,
                info=None,
            ),
        ]

    monkeypatch.setattr(watlowlib, "find_devices", fake_find_devices)

    rows = await watlow.discover(ports=["COM6"], addresses=(1,))
    assert len(rows) == 1
    row = rows[0]
    # Same convention as alicat.discover: row carries the short
    # ADAPTER_ID, and the dialog re-resolves to descriptor.id when
    # building the device payload.
    assert row["adapter"] == watlow.ADAPTER_ID
    assert row["port"] == "COM6"
    assert row["address"] == 1
    assert row["baudrate"] == 38400
    assert row["protocol"] == "modbus_rtu"
    assert row["model"] == "PM3R1CA"
    assert row["firmware"] == "1.2"


@pytest.mark.anyio
async def test_watlow_discover_dedups_one_physical_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One physical controller surfaces exactly once even if the cartesian
    sweep produces multiple ``ok=True`` rows for the same (port,
    address) at different bauds / protocols.

    ``find_devices`` iterates outermost-port, then baudrate, then
    protocol — so the first hit is the most-likely production config.
    The Discover dialog used to show two rows ("watlow ... stdbus" +
    "watlow ... modbus_rtu") for the same controller; operators read
    that as two devices and the dedup here collapses it.
    """

    async def fake_find_devices(**_kwargs: Any) -> list[_StubFindResult]:
        return [
            _StubFindResult(
                port="COM6",
                address=1,
                baudrate=38400,
                protocol=ProtocolKind.STDBUS,
                ok=True,
                info=_StubInfo(),
            ),
            _StubFindResult(
                port="COM6",
                address=1,
                baudrate=38400,
                protocol=ProtocolKind.MODBUS_RTU,
                ok=True,
                info=_StubInfo(),
            ),
            _StubFindResult(
                port="COM6",
                address=1,
                baudrate=9600,
                protocol=ProtocolKind.STDBUS,
                ok=True,
                info=_StubInfo(),
            ),
        ]

    monkeypatch.setattr(watlowlib, "find_devices", fake_find_devices)

    rows = await watlow.discover(ports=["COM6"], addresses=(1,))
    assert len(rows) == 1
    # First hit wins — 38400 baud, stdbus.
    assert rows[0]["port"] == "COM6"
    assert rows[0]["address"] == 1
    assert rows[0]["baudrate"] == 38400
    assert rows[0]["protocol"] == "stdbus"


@pytest.mark.anyio
async def test_watlow_discover_returns_empty_when_no_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty ``ports`` list short-circuits without hitting the library."""
    called: list[bool] = []

    async def fake_find_devices(**_kwargs: Any) -> list[_StubFindResult]:
        called.append(True)
        return []

    monkeypatch.setattr(watlowlib, "find_devices", fake_find_devices)

    rows = await watlow.discover(ports=[])
    assert rows == []
    assert called == []


@pytest.mark.anyio
async def test_watlow_discover_swallows_library_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``WatlowError`` from ``find_devices`` returns an empty list rather
    than propagating — the Setup Discover dialog renders ``no devices``
    instead of crashing."""

    async def boom(**_kwargs: Any) -> list[_StubFindResult]:
        raise watlowlib.WatlowConfigurationError("bad config")

    monkeypatch.setattr(watlowlib, "find_devices", boom)

    rows = await watlow.discover(ports=["COM6"], addresses=(1,))
    assert rows == []


@pytest.mark.anyio
async def test_watlow_discover_forwards_baudrate_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``baudrates`` are forwarded to ``find_devices`` and the
    successful baud is reflected in the returned row."""
    seen_kwargs: dict[str, Any] = {}

    async def fake_find_devices(**kwargs: Any) -> list[_StubFindResult]:
        seen_kwargs.update(kwargs)
        return [
            _StubFindResult(
                port="COM6",
                address=1,
                baudrate=9600,
                protocol=ProtocolKind.STDBUS,
                ok=True,
                info=_StubInfo(),
            ),
        ]

    monkeypatch.setattr(watlowlib, "find_devices", fake_find_devices)

    baudrates: Sequence[int] = (9600, 19200, 38400)
    rows = await watlow.discover(ports=["COM6"], addresses=(1,), baudrates=tuple(baudrates))
    assert seen_kwargs["baudrates"] == tuple(baudrates)
    assert seen_kwargs["addresses"] == (1,)
    assert rows[0]["baudrate"] == 9600


def test_watlow_descriptor_advertises_discoverable() -> None:
    """The descriptor flag flips to ``discoverable=True`` so the
    DiscoveryDialog runs the sweep automatically."""
    assert watlow.DESCRIPTOR.discoverable is True
    assert watlow.DESCRIPTOR.handshake_available is True
    # Reason no longer needed now that the adapter is scannable.
    assert watlow.DESCRIPTOR.discoverable_reason is None
