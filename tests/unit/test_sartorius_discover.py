"""Sartorius module-level ``discover()``.

Verifies the in-repo wrapper around :func:`sartoriuslib.find_devices`.
Under the unified API the library returns one :class:`SartoriusDiscoveryResult`
per probe (port × baudrate); capa folds these into per-port summaries via
:func:`sartoriuslib.summarize_discovery` before emitting rows.
"""

from __future__ import annotations

from typing import Any

import pytest
import sartoriuslib
from sartoriuslib import ProtocolKind, SartoriusDiscoveryResult

from capa.devices import sartorius


def _probe(
    *,
    port: str,
    baudrate: int,
    protocol: ProtocolKind | None,
    ok: bool,
    autoprint_active: bool = False,
    error: Exception | None = None,
) -> SartoriusDiscoveryResult:
    return SartoriusDiscoveryResult(
        ok=ok,
        port=port,
        address=None,
        baudrate=baudrate,
        protocol=protocol,
        device_info=None,
        error=error,
        elapsed_s=0.0,
        parity="O",
        stopbits=1,
        autoprint_active=autoprint_active,
        pending_lines=(),
    )


@pytest.mark.anyio
async def test_sartorius_discover_yields_row_per_successful_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each ``ok=True`` port becomes one row carrying the resolved baud
    and protocol."""

    async def fake_find_devices(**_kwargs: Any) -> list[SartoriusDiscoveryResult]:
        return [
            _probe(port="COM3", baudrate=19200, protocol=ProtocolKind.XBPI, ok=True),
            _probe(port="COM4", baudrate=115200, protocol=None, ok=False),
        ]

    monkeypatch.setattr(sartoriuslib, "find_devices", fake_find_devices)

    rows = await sartorius.discover(ports=["COM3", "COM4"])
    assert len(rows) == 1
    row = rows[0]
    assert row["adapter"] == sartorius.ADAPTER_ID
    assert row["port"] == "COM3"
    assert row["baudrate"] == 19200
    assert row["protocol"] == "xbpi"
    assert row["autoprint_active"] is False


@pytest.mark.anyio
async def test_sartorius_discover_forwards_baudrate_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``baudrates`` are forwarded to ``find_devices``."""
    seen_kwargs: dict[str, Any] = {}

    async def fake_find_devices(**kwargs: Any) -> list[SartoriusDiscoveryResult]:
        seen_kwargs.update(kwargs)
        return [
            _probe(
                port="COM3",
                baudrate=38400,
                protocol=ProtocolKind.SBI,
                ok=True,
                autoprint_active=True,
            ),
        ]

    monkeypatch.setattr(sartoriuslib, "find_devices", fake_find_devices)

    rows = await sartorius.discover(ports=["COM3"], baudrates=(9600, 38400, 115200), timeout_s=0.25)
    assert seen_kwargs["baudrates"] == (9600, 38400, 115200)
    assert seen_kwargs["per_probe_timeout_s"] == 0.25
    assert rows[0]["protocol"] == "sbi"
    assert rows[0]["autoprint_active"] is True


@pytest.mark.anyio
async def test_sartorius_discover_returns_empty_when_no_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty ``ports`` list short-circuits without hitting the library."""
    called: list[bool] = []

    async def fake_find_devices(**_kwargs: Any) -> list[SartoriusDiscoveryResult]:
        called.append(True)
        return []

    monkeypatch.setattr(sartoriuslib, "find_devices", fake_find_devices)

    rows = await sartorius.discover(ports=[])
    assert rows == []
    assert called == []


@pytest.mark.anyio
async def test_sartorius_discover_swallows_library_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``SartoriusError`` from ``find_devices`` returns an empty list
    rather than propagating."""

    async def boom(**_kwargs: Any) -> list[SartoriusDiscoveryResult]:
        raise sartoriuslib.SartoriusConfigurationError("bad config")

    monkeypatch.setattr(sartoriuslib, "find_devices", boom)

    rows = await sartorius.discover(ports=["COM3"])
    assert rows == []


def test_sartorius_descriptor_advertises_discoverable() -> None:
    """The descriptor flag stays ``discoverable=True`` after the bump."""
    assert sartorius.DESCRIPTOR.discoverable is True
    assert sartorius.DESCRIPTOR.handshake_available is True
