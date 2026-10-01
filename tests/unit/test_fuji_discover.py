"""Fuji module-level ``discover()``.

Verifies the in-repo wrapper around :func:`fujilib.find_devices`. The library
returns one :class:`fujilib.DiscoveryResult` per (port, station) probed; capa
emits one row per analyzer that answered and identified.
"""

from __future__ import annotations

from typing import Any

import fujilib
import pytest
from fujilib import DeviceInfo, DiscoveryResult, FujiConnectionError, FujiError, ProtocolKind
from fujilib.testing import DEFAULT_ZPA_BANK, MockAnalyzer, mock_transport

from capa.devices import fuji

pytestmark = pytest.mark.anyio


@pytest.fixture
async def info() -> DeviceInfo:
    """The identity of the simulated bench analyzer."""
    async with mock_transport(MockAnalyzer(DEFAULT_ZPA_BANK)) as (transport, _line):
        analyzer = await fujilib.open_device(transport, timeout=0.25)
        try:
            found = analyzer.info
        finally:
            await analyzer.close()
    assert found is not None
    return found


def _probe(
    *,
    port: str,
    address: int = 1,
    ok: bool,
    device_info: DeviceInfo | None = None,
    error: FujiError | None = None,
) -> DiscoveryResult:
    return DiscoveryResult(
        ok=ok,
        port=port,
        address=address,
        baudrate=38400 if ok else None,
        protocol=ProtocolKind.MODBUS_RTU if ok else None,
        device_info=device_info,
        error=error,
        elapsed_s=0.0,
        model="ZPA" if ok else None,
    )


async def test_fuji_discover_yields_a_row_per_identified_analyzer(
    monkeypatch: pytest.MonkeyPatch, info: DeviceInfo
) -> None:
    seen: dict[str, Any] = {}

    async def fake_find_devices(**kwargs: Any) -> list[DiscoveryResult]:
        seen.update(kwargs)
        return [
            _probe(port="COM8", ok=True, device_info=info),
            _probe(port="COM6", ok=False, error=FujiConnectionError("no reply")),
            # Answered as an analyzer, but its identification failed: not a usable row.
            _probe(port="COM4", ok=True, device_info=None),
        ]

    monkeypatch.setattr(fujilib, "find_devices", fake_find_devices)

    rows = await fuji.discover(ports=["COM8", "COM6", "COM4"], addresses=(1, 2), timeout_s=0.2)

    assert rows == [
        {
            "adapter": "fuji",
            "port": "COM8",
            "address": 1,
            "baudrate": 38400,
            "model": "ZPA",
            "serial": "N8A0259T",
            "type_code": "ZPACBJY1MPFYYYYYY2DEYAYAY0",
            "channels": rows[0]["channels"],
            "channel_map": rows[0]["channel_map"],
        }
    ]
    assert rows[0]["channels"].startswith("CH1=")
    # The suggested map never names a gas "unknown": such a channel is left out.
    assert rows[0]["channel_map"]
    assert "unknown" not in rows[0]["channel_map"].values()
    assert seen == {
        "ports": ["COM8", "COM6", "COM4"],
        "addresses": (1, 2),
        "per_probe_timeout_s": 0.2,
    }


async def test_fuji_discover_with_no_ports_probes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(**_kwargs: Any) -> list[DiscoveryResult]:
        raise AssertionError("an empty port list must not be scanned")

    monkeypatch.setattr(fujilib, "find_devices", fail)
    assert await fuji.discover(ports=[]) == []


async def test_fuji_discover_swallows_library_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(**_kwargs: Any) -> list[DiscoveryResult]:
        raise FujiConnectionError("the host's ports cannot be listed")

    monkeypatch.setattr(fujilib, "find_devices", fail)
    assert await fuji.discover() == []
