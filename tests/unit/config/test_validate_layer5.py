"""Layer 5 — live checks."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from capa.config import ConfigDocument, validate, validate_live_async
from capa.config.validate import _LIVE_HANDSHAKE_TIMEOUT_S
from capa.devices.registry import (
    ADAPTERS,
    _import_builtins,
    get_descriptor,
)

# The package ``capa.config`` re-exports ``validate`` from its submodule,
# so ``capa.config.validate`` resolves to the function via attribute
# lookup. Pull the submodule out of ``sys.modules`` to monkeypatch its
# module-level constants.
_validate_module = sys.modules["capa.config.validate"]


@pytest.fixture(scope="module", autouse=True)
def _ensure_builtins_loaded() -> None:
    _import_builtins()


@pytest.fixture
def sim_capa_doc(configs_dir: Path) -> ConfigDocument:
    return ConfigDocument.load(configs_dir / "experiments" / "sim_capa_pyrolysis.yaml")


def _make_handshake_available(monkeypatch: pytest.MonkeyPatch, adapter_id: str) -> None:
    """Flip the adapter's descriptor to ``handshake_available=True`` for a test."""
    real = get_descriptor(adapter_id)
    assert real is not None, f"missing descriptor for {adapter_id}"
    patched = replace(real, handshake_available=True)
    monkeypatch.setitem(ADAPTERS, adapter_id, patched)


# ---------------------------------------------------------------------------
# sync entry point
# ---------------------------------------------------------------------------


def test_validate_without_live_runs_no_handshakes(
    sim_capa_doc: ConfigDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``validate(..., with_live_checks=False)`` must not import or call adapters."""
    called: list[str] = []

    async def boom(params: dict[str, Any]) -> str:
        called.append("called")
        return "should not happen"

    _make_handshake_available(monkeypatch, "capa.devices.sim.watlow_sim")
    monkeypatch.setattr("capa.devices.sim.watlow_sim.handshake", boom, raising=False)
    problems = validate(sim_capa_doc, with_live_checks=False)
    assert not any(p.code.startswith("live.") for p in problems)
    assert called == []


def test_validate_with_live_runs_handshake_sync_path(
    sim_capa_doc: ConfigDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The synchronous ``validate(..., with_live_checks=True)`` path drives Layer 5."""

    async def fake(params: dict[str, Any]) -> str:
        return "watlow part=PM3R1CA fw=1"

    _make_handshake_available(monkeypatch, "capa.devices.sim.watlow_sim")
    monkeypatch.setattr("capa.devices.sim.watlow_sim.handshake", fake, raising=False)
    problems = validate(sim_capa_doc, with_live_checks=True)
    oks = [p for p in problems if p.code == "live.handshake_ok"]
    assert any(p.path == ("devices", "heater") for p in oks)
    assert any("PM3R1CA" in p.message for p in oks)


# ---------------------------------------------------------------------------
# async entry point
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_layer5_success_emits_info_problem(
    sim_capa_doc: ConfigDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake(params: dict[str, Any]) -> str:
        return "ok"

    _make_handshake_available(monkeypatch, "capa.devices.sim.watlow_sim")
    monkeypatch.setattr("capa.devices.sim.watlow_sim.handshake", fake, raising=False)
    problems = await validate_live_async(sim_capa_doc)
    matches = [
        p for p in problems if p.code == "live.handshake_ok" and p.path == ("devices", "heater")
    ]
    assert len(matches) == 1
    assert matches[0].severity == "info"
    assert "heater:" in matches[0].message


@pytest.mark.anyio
async def test_layer5_failure_surfaces_error_with_device_path(
    sim_capa_doc: ConfigDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(params: dict[str, Any]) -> str:
        raise RuntimeError("port not present")

    _make_handshake_available(monkeypatch, "capa.devices.sim.watlow_sim")
    monkeypatch.setattr("capa.devices.sim.watlow_sim.handshake", boom, raising=False)
    problems = await validate_live_async(sim_capa_doc)
    fails = [p for p in problems if p.code == "live.handshake_failed"]
    assert len(fails) == 1
    assert fails[0].severity == "error"
    assert fails[0].path == ("devices", "heater")
    assert "port not present" in fails[0].message


@pytest.mark.anyio
async def test_layer5_timeout_surfaces_specific_code(
    sim_capa_doc: ConfigDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handshake that hangs longer than the budget is reported as a timeout."""

    async def hang(params: dict[str, Any]) -> str:
        await asyncio.sleep(60)  # safely above the patched budget
        return "never"

    _make_handshake_available(monkeypatch, "capa.devices.sim.watlow_sim")
    monkeypatch.setattr("capa.devices.sim.watlow_sim.handshake", hang, raising=False)
    # Shrink the budget so the test runs quickly. Original constant lives
    # on the module; the in-flight ``asyncio.wait_for`` reads it on call.
    monkeypatch.setattr(_validate_module, "_LIVE_HANDSHAKE_TIMEOUT_S", 0.05)
    problems = await validate_live_async(sim_capa_doc)
    timeouts = [p for p in problems if p.code == "live.handshake_timeout"]
    assert len(timeouts) == 1
    assert timeouts[0].severity == "error"
    assert timeouts[0].path == ("devices", "heater")


@pytest.mark.anyio
async def test_layer5_skips_descriptors_without_handshake_available(
    sim_capa_doc: ConfigDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sim adapters (default ``handshake_available=False``) are skipped silently."""
    called: list[str] = []

    async def fake(params: dict[str, Any]) -> str:
        called.append("called")
        return "ok"

    monkeypatch.setattr("capa.devices.sim.watlow_sim.handshake", fake, raising=False)
    # Note: NOT calling _make_handshake_available — the descriptor's
    # default flag is False, so Layer 5 should skip every device.
    problems = await validate_live_async(sim_capa_doc)
    assert not any(p.code.startswith("live.handshake") for p in problems)
    assert called == []


@pytest.mark.anyio
async def test_layer5_concurrent_handshakes_run_in_parallel(
    sim_capa_doc: ConfigDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two device handshakes run concurrently — total time ≈ max(per-device)."""
    started = asyncio.Event()
    permit = asyncio.Event()

    async def slow_watlow(params: dict[str, Any]) -> str:
        started.set()
        await permit.wait()
        return "watlow ok"

    async def slow_alicat(params: dict[str, Any]) -> str:
        await started.wait()  # only proceed once watlow has begun
        permit.set()  # release watlow concurrently with self
        return "alicat ok"

    _make_handshake_available(monkeypatch, "capa.devices.sim.watlow_sim")
    _make_handshake_available(monkeypatch, "capa.devices.sim.alicat_sim")
    monkeypatch.setattr("capa.devices.sim.watlow_sim.handshake", slow_watlow, raising=False)
    monkeypatch.setattr("capa.devices.sim.alicat_sim.handshake", slow_alicat, raising=False)
    # If the handshakes ran serially this test would deadlock: alicat
    # waits for watlow to start, watlow waits for alicat to set permit.
    problems = await asyncio.wait_for(validate_live_async(sim_capa_doc), timeout=3.0)
    oks = {p.path for p in problems if p.code == "live.handshake_ok"}
    assert ("devices", "heater") in oks
    assert ("devices", "purge_mfc") in oks


@pytest.mark.anyio
async def test_layer5_reports_schema_failure_without_running_live(
    sim_capa_doc: ConfigDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A draft that fails Layers 1–4 short-circuits before any handshake fires."""
    called: list[str] = []

    async def boom(params: dict[str, Any]) -> str:
        called.append("called")
        return "should not happen"

    _make_handshake_available(monkeypatch, "capa.devices.sim.watlow_sim")
    monkeypatch.setattr("capa.devices.sim.watlow_sim.handshake", boom, raising=False)
    # Break the schema — drop the hardware profile name.
    sim_capa_doc.hardware_payload.pop("name", None)
    problems = await validate_live_async(sim_capa_doc)
    assert any(p.severity == "error" for p in problems)
    assert called == []


def test_live_timeout_default_is_sensible() -> None:
    """Sanity: the default budget is short enough that an operator notices
    a hang quickly, long enough that a real serial probe completes."""
    assert 2.0 <= _LIVE_HANDSHAKE_TIMEOUT_S <= 30.0


# ---------------------------------------------------------------------------
# Camera handshakes.
# ---------------------------------------------------------------------------


class _StubCameraSpec:
    """Minimal stand-in for :class:`CameraSpec` — Layer 5 only reads
    ``name``, ``adapter`` and uses ``model_dump()`` to serialise."""

    def __init__(self, *, name: str, adapter: str, **extra: Any) -> None:
        self.name = name
        self.adapter = adapter
        self.kind = extra.pop("kind", "ir")
        self._extra = extra

    def model_dump(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "adapter": self.adapter,
            "kind": self.kind,
        }
        out.update(self._extra)
        return out


class _StubHardware:
    def __init__(
        self,
        *,
        devices: list[Any] | None = None,
        cameras: list[Any] | None = None,
    ) -> None:
        self.devices = devices or []
        self.cameras = cameras or []


class _StubConfig:
    def __init__(self, hardware: _StubHardware) -> None:
        self.hardware = hardware


@pytest.mark.anyio
async def test_layer5_camera_handshake_success(
    sim_capa_doc: ConfigDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A camera adapter with ``handshake_available=True`` runs through
    the Layer 5 path and surfaces an info-severity ``live.handshake_ok``
    problem under the ``cameras`` section. Plan §7.2 item 1."""
    from capa.config.validate import _layer5_live_async

    cam = _StubCameraSpec(
        name="ir_cam0",
        adapter="capa.devices.sim.flir_ir_sim",
        serial="SIM-IR-0001",
    )
    config = _StubConfig(_StubHardware(cameras=[cam]))

    async def fake(cam_spec: dict[str, Any]) -> str:
        return f"flir_ir_sim serial={cam_spec.get('serial')!r}"

    monkeypatch.setattr("capa.devices.sim.flir_ir_sim.handshake", fake, raising=False)
    problems = await _layer5_live_async(config, sim_capa_doc)
    matches = [
        p for p in problems if p.code == "live.handshake_ok" and p.path == ("cameras", "ir_cam0")
    ]
    assert len(matches) == 1
    assert matches[0].severity == "info"
    assert matches[0].section == "cameras"
    assert "SIM-IR-0001" in matches[0].message


@pytest.mark.anyio
async def test_layer5_camera_handshake_failure_routes_to_cameras_section(
    sim_capa_doc: ConfigDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Camera handshake failures land in the ``cameras`` section so the
    Problems panel navigates correctly."""
    from capa.config.validate import _layer5_live_async

    cam = _StubCameraSpec(
        name="ir_cam0",
        adapter="capa.devices.sim.flir_ir_sim",
        serial="SIM-IR-0001",
    )
    config = _StubConfig(_StubHardware(cameras=[cam]))

    async def boom(cam_spec: dict[str, Any]) -> str:
        raise RuntimeError("camera not enumerated")

    monkeypatch.setattr("capa.devices.sim.flir_ir_sim.handshake", boom, raising=False)
    problems = await _layer5_live_async(config, sim_capa_doc)
    fails = [p for p in problems if p.code == "live.handshake_failed"]
    assert len(fails) == 1
    assert fails[0].path == ("cameras", "ir_cam0")
    assert fails[0].section == "cameras"
    assert "camera not enumerated" in fails[0].message


@pytest.mark.anyio
async def test_layer5_no_more_camera_handshake_stub(
    sim_capa_doc: ConfigDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Previously a stub emitted ``live.camera_handshake_not_implemented`` info
    problems; real handshake routing now replaces that — the stub code must
    no longer appear."""
    from capa.config.validate import _layer5_live_async

    cam = _StubCameraSpec(
        name="ir_cam0",
        adapter="capa.devices.sim.flir_ir_sim",
        serial="SIM-IR-0001",
    )
    config = _StubConfig(_StubHardware(cameras=[cam]))

    async def ok(cam_spec: dict[str, Any]) -> str:
        return "ok"

    monkeypatch.setattr("capa.devices.sim.flir_ir_sim.handshake", ok, raising=False)
    problems = await _layer5_live_async(config, sim_capa_doc)
    assert not any(p.code == "live.camera_handshake_not_implemented" for p in problems)
