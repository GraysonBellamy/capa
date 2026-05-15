"""Camera module-level discover + handshake.

These tests verify the ``discover_cameras()`` / ``handshake()``
entry points on :mod:`capa.devices.camera.webcam` and
:mod:`capa.devices.sim.flir_ir_sim`. The webcam path is OS-portable
but tested via monkeypatched enumerators so it runs on any CI host.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from capa.devices.camera import webcam
from capa.devices.sim import flir_ir_sim

# ---------------------------------------------------------------------------
# FLIR IR sim — easy case (no platform dependency).
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_flir_ir_sim_discover_returns_one_row() -> None:
    rows = await flir_ir_sim.discover_cameras()
    assert len(rows) == 1
    row = rows[0]
    assert row["adapter"] == "capa.devices.sim.flir_ir_sim"
    assert row["transport"] == "sim"
    assert row["serial"].startswith("SIM-IR-")


@pytest.mark.anyio
async def test_flir_ir_sim_handshake_returns_summary() -> None:
    cam_spec = {
        "name": "ir_cam0",
        "adapter": "capa.devices.sim.flir_ir_sim",
        "kind": "ir",
        "serial": "SIM-IR-0001",
    }
    summary = await flir_ir_sim.handshake(cam_spec)
    assert "flir_ir_sim" in summary
    assert "SIM-IR-0001" in summary


# ---------------------------------------------------------------------------
# Webcam — platform-portable via monkeypatched enumerators.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_webcam_discover_on_unsupported_platform_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS / other unsupported platforms return ``[]`` rather than raise."""
    # Force the "neither linux nor win32" branch.
    monkeypatch.setattr(sys, "platform", "darwin", raising=False)
    rows = await webcam.discover_cameras()
    assert rows == []


@pytest.mark.anyio
async def test_webcam_discover_linux_walks_sysfs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux", raising=False)
    fake_rows = [
        {
            "adapter": "capa.devices.camera.webcam",
            "selector": "/dev/video0",
            "model": "Logitech C920",
            "serial": "ABC123",
            "transport": "usb",
        }
    ]

    def fake_enum() -> list[dict[str, Any]]:
        return fake_rows

    monkeypatch.setattr(webcam, "_enumerate_v4l2_sync", fake_enum)
    rows = await webcam.discover_cameras()
    assert rows == fake_rows


@pytest.mark.anyio
async def test_webcam_discover_windows_walks_dshow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32", raising=False)
    fake_rows = [
        {
            "adapter": "capa.devices.camera.webcam",
            "selector": "video=Logitech C920",
            "model": "Logitech C920",
            "serial": r"\\?\usb#vid_046d&pid_082d",
            "transport": "directshow",
        }
    ]

    async def fake_enum() -> list[dict[str, Any]]:
        return fake_rows

    monkeypatch.setattr(webcam, "_enumerate_directshow", fake_enum)
    rows = await webcam.discover_cameras()
    assert rows == fake_rows


# ---------------------------------------------------------------------------
# Webcam handshake — selector / serial / model_hint match rules.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_webcam_handshake_matches_on_serial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_discover() -> list[dict[str, Any]]:
        return [
            {
                "model": "C920",
                "serial": "ABC123",
                "selector": "/dev/video0",
                "adapter": "capa.devices.camera.webcam",
            },
            {
                "model": "C930",
                "serial": "XYZ999",
                "selector": "/dev/video1",
                "adapter": "capa.devices.camera.webcam",
            },
        ]

    monkeypatch.setattr(webcam, "discover_cameras", fake_discover)
    summary = await webcam.handshake({"serial": "XYZ999", "model_hint": None})
    assert "XYZ999" in summary
    assert "C930" in summary


@pytest.mark.anyio
async def test_webcam_handshake_matches_on_model_hint_when_no_serial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_discover() -> list[dict[str, Any]]:
        return [
            {
                "model": "C920",
                "serial": "ABC",
                "selector": "/dev/video0",
                "adapter": "capa.devices.camera.webcam",
            },
            {
                "model": "C930",
                "serial": "XYZ",
                "selector": "/dev/video1",
                "adapter": "capa.devices.camera.webcam",
            },
        ]

    monkeypatch.setattr(webcam, "discover_cameras", fake_discover)
    summary = await webcam.handshake({"serial": None, "model_hint": "C920"})
    assert "C920" in summary


@pytest.mark.anyio
async def test_webcam_handshake_unique_no_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exactly one camera + no selector = accept (mirrors UvcController.find)."""

    async def fake_discover() -> list[dict[str, Any]]:
        return [
            {
                "model": "C920",
                "serial": "ABC",
                "selector": "/dev/video0",
                "adapter": "capa.devices.camera.webcam",
            },
        ]

    monkeypatch.setattr(webcam, "discover_cameras", fake_discover)
    summary = await webcam.handshake({"serial": None, "model_hint": None})
    assert "C920" in summary


@pytest.mark.anyio
async def test_webcam_handshake_raises_on_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from capa.core.errors import AdapterError

    async def fake_discover() -> list[dict[str, Any]]:
        return [
            {
                "model": "C920",
                "serial": "ABC",
                "selector": "/dev/video0",
                "adapter": "capa.devices.camera.webcam",
            },
        ]

    monkeypatch.setattr(webcam, "discover_cameras", fake_discover)
    with pytest.raises(AdapterError):
        await webcam.handshake({"serial": "NOPE", "model_hint": None})


@pytest.mark.anyio
async def test_webcam_handshake_raises_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from capa.core.errors import AdapterError

    async def fake_discover() -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(webcam, "discover_cameras", fake_discover)
    with pytest.raises(AdapterError):
        await webcam.handshake({"serial": "anything", "model_hint": None})


# ---------------------------------------------------------------------------
# Descriptor flag sanity (regression).
# ---------------------------------------------------------------------------


def test_webcam_descriptor_advertises_discoverable_and_handshake() -> None:
    desc = webcam.DESCRIPTOR
    assert desc.discoverable is True
    assert desc.handshake_available is True
    # Now scannable — no need for a "not scannable" reason.
    assert desc.discoverable_reason is None


def test_flir_ir_sim_descriptor_advertises_discoverable_and_handshake() -> None:
    desc = flir_ir_sim.DESCRIPTOR
    assert desc.discoverable is True
    assert desc.handshake_available is True
    assert desc.discoverable_reason is None
