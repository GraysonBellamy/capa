"""``capa hardware`` CLI subcommands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from capa.app import app

REPO_ROOT = Path(__file__).resolve().parents[2]
SIM_CAPA_EXP = REPO_ROOT / "configs" / "experiments" / "sim_capa_pyrolysis.yaml"
SIM_CAPA_HW = REPO_ROOT / "configs" / "hardware" / "sim_capa.toml"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# capa hardware validate
# ---------------------------------------------------------------------------


def test_hardware_validate_clean_sim(runner: CliRunner) -> None:
    result = runner.invoke(app, ["hardware", "validate", str(SIM_CAPA_HW)])
    assert result.exit_code == 0, result.stdout


def test_hardware_validate_rejects_broken_file(runner: CliRunner, tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("name = 'broken'\n[[devices]]\n")  # missing 'name' on device
    result = runner.invoke(app, ["hardware", "validate", str(bad)])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# capa hardware new
# ---------------------------------------------------------------------------


def test_hardware_new_writes_minimal_profile(runner: CliRunner, tmp_path: Path) -> None:
    out = tmp_path / "fresh.toml"
    result = runner.invoke(app, ["hardware", "new", str(out)])
    assert result.exit_code == 0, result.stdout
    assert out.exists()
    # Profile is valid: validate should pass.
    result2 = runner.invoke(app, ["hardware", "validate", str(out)])
    assert result2.exit_code == 0, result2.stdout


def test_hardware_new_refuses_to_overwrite(runner: CliRunner, tmp_path: Path) -> None:
    out = tmp_path / "existing.toml"
    out.write_text("name = 'foo'\n")
    result = runner.invoke(app, ["hardware", "new", str(out)])
    assert result.exit_code == 2


def test_hardware_new_honours_name_override(runner: CliRunner, tmp_path: Path) -> None:
    out = tmp_path / "named.toml"
    result = runner.invoke(app, ["hardware", "new", str(out), "--name", "lab_rig_3"])
    assert result.exit_code == 0, result.stdout
    contents = out.read_text()
    assert "lab_rig_3" in contents


# ---------------------------------------------------------------------------
# capa hardware discover
# ---------------------------------------------------------------------------


def test_hardware_discover_runs_and_includes_cameras(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discover routes through the AdapterDescriptor registry so the
    output includes cameras."""
    from capa.devices.camera import webcam
    from capa.devices.sim import flir_ir_sim

    async def fake_webcam() -> list[dict[str, Any]]:
        return [
            {
                "adapter": "capa.devices.camera.webcam",
                "selector": "/dev/video0",
                "model": "FakeCam",
                "serial": "FAKE-1",
                "transport": "usb",
            }
        ]

    async def fake_flir() -> list[dict[str, Any]]:
        return [
            {
                "adapter": "capa.devices.sim.flir_ir_sim",
                "selector": "SIM-IR-0001",
                "model": "FLIR IR sim",
                "serial": "SIM-IR-0001",
                "transport": "sim",
            }
        ]

    # Cancel out serial-port scans so the test doesn't poke real hardware.
    async def empty(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(webcam, "discover_cameras", fake_webcam)
    monkeypatch.setattr(flir_ir_sim, "discover_cameras", fake_flir)
    from capa.devices import alicat, sartorius, watlow

    monkeypatch.setattr(alicat, "discover", empty)
    monkeypatch.setattr(watlow, "discover", empty)
    monkeypatch.setattr(sartorius, "discover", empty)
    try:
        from capa.devices import nidaq

        if hasattr(nidaq, "discover"):
            monkeypatch.setattr(nidaq, "discover", empty)
    except ImportError:
        pass

    result = runner.invoke(app, ["hardware", "discover", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    adapters_seen = {row["adapter"] for row in payload["devices"]}
    assert "capa.devices.camera.webcam" in adapters_seen
    assert "capa.devices.sim.flir_ir_sim" in adapters_seen


def test_hardware_discover_rejects_unknown_family(runner: CliRunner) -> None:
    result = runner.invoke(app, ["hardware", "discover", "--adapter", "no_such_family"])
    assert result.exit_code == 2
