"""End-to-end test for the P0c outcome gate (plan §16):

    capa run --headless freerun.yaml writes a completed + sealed bundle
    with full software-environment provenance and catalog entry.

Drives the typer CLI in-process and asserts:

* exit code 0,
* bundle exists, manifest deserializes,
* sha256 verifies cleanly,
* catalog row reflects ``completed`` + ``sealed`` + ``ok``,
* ``run.log`` contains JSON lines with a bound ``run_id``,
* ``manifest.queue_health`` was populated by the engine metrics.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from capa.app import app
from capa.storage.catalog import RunCatalog
from capa.storage.integrity import verify
from capa.storage.manifest import BundleManifest

_FREE_RUN_TOML = """
procedure = {{ id = "capa.builtin.free_run", config = {{ duration_s = {duration} }} }}
calibration_set = {{ name = "default" }}
operator = {{ id = "abr", display_name = "A. Researcher" }}
sample = {{ id = "{sample_id}" }}
tags = ["sim", "p0c"]

hardware = "hardware.toml"
"""

_HARDWARE_TOML = """
name = "p0c-sim"
[[devices]]
name = "heater"
adapter = "capa.devices.sim.watlow_sim"

[[channels]]
name = "heater.pv"
kind = "process_var"
unit = "degC"
derived_unit = "degC"
[channels.source]
source = "watlow_parameter"
device = "heater"
parameter = "process_value"
instance = 1
[channels.calibration]
kind = "identity"
input_unit = "degC"
output_unit = "degC"
"""


def _write_example(tmp_path: Path, *, duration: float = 0.1) -> Path:
    (tmp_path / "experiment.toml").write_text(
        _FREE_RUN_TOML.format(duration=duration, sample_id="P0C-1"), encoding="utf-8"
    )
    (tmp_path / "hardware.toml").write_text(_HARDWARE_TOML, encoding="utf-8")
    return tmp_path / "experiment.toml"


def test_p0c_outcome_gate(tmp_path: Path) -> None:
    runner = CliRunner()
    config = _write_example(tmp_path, duration=0.1)
    runs = tmp_path / "runs"

    result = runner.invoke(
        app,
        ["run", "--headless", "--runs-root", str(runs), str(config)],
    )
    assert result.exit_code == 0, result.stdout

    bundles = [p for p in runs.iterdir() if p.is_dir() and (p / "manifest.json").exists()]
    assert len(bundles) == 1
    bundle = bundles[0]

    manifest = BundleManifest.read(bundle / "manifest.json")
    assert manifest.run_status == "completed"
    assert manifest.bundle_status == "sealed"
    assert manifest.integrity.status == "ok"
    assert manifest.ended_utc is not None
    assert manifest.queue_health, "engine should populate queue_health"
    assert manifest.capa.engine_version is not None

    # sha256 verification is the storage layer's responsibility — re-run
    # against the on-disk bundle.
    verify_result = verify(bundle)
    assert verify_result.status == "ok"
    assert verify_result.mismatches == ()

    # Catalog row must mirror manifest.
    with RunCatalog(runs) as cat:
        rows = cat.list()
        assert len(rows) == 1
        row = rows[0]
        assert row.run_status == "completed"
        assert row.bundle_status == "sealed"
        assert row.integrity_status == "ok"
        assert row.operator_id == "abr"
        assert row.sample_id == "P0C-1"

    # run.log was captured into the bundle.
    log_text = (bundle / "run.log").read_text(encoding="utf-8")
    log_lines = [ln for ln in log_text.splitlines() if ln.strip()]
    assert log_lines, "run.log should contain at least one event"
    parsed = [json.loads(ln) for ln in log_lines]
    events = {p.get("event") for p in parsed}
    assert "engine.run.start" in events
    assert "engine.run.end" in events
    assert all(p.get("run_id") for p in parsed if "run_id" in p)


def test_finalize_recovers_open_bundle(tmp_path: Path) -> None:
    """If a bundle is left ``open`` (engine never finalized), ``capa
    finalize RUN_ID`` must seal it."""
    runner = CliRunner()
    config = _write_example(tmp_path, duration=0.1)
    runs = tmp_path / "runs"

    # First, write a real bundle.
    runner.invoke(app, ["run", "--headless", "--runs-root", str(runs), str(config)])
    bundle = next(p for p in runs.iterdir() if p.is_dir() and (p / "manifest.json").exists())

    # Simulate a "left open" bundle by rewriting the manifest's
    # bundle_status to "open" and clearing ended_utc + manifest.sha256.
    manifest = BundleManifest.read(bundle / "manifest.json")
    manifest = manifest.model_copy(update={"bundle_status": "open", "ended_utc": None})
    manifest.write(bundle / "manifest.json")
    (bundle / "manifest.sha256").unlink()

    result = runner.invoke(app, ["finalize", bundle.name, "--runs-root", str(runs)])
    assert result.exit_code == 0, result.stdout

    refreshed = BundleManifest.read(bundle / "manifest.json")
    assert refreshed.bundle_status == "sealed"
    assert refreshed.integrity.status == "ok"
    assert (bundle / "manifest.sha256").is_file()
