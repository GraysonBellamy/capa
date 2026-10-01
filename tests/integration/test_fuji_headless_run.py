"""End-to-end test for a headless free-run with a gas analyzer on the rig.

A simulated Fuji analyzer runs through the whole stack (adapter → worker →
conductor → writer) and the sealed bundle carries both of its surfaces:

* ``device_records/fuji.parquet`` — the analyzer's native wide rows, with
  each reading's validity state;
* ``scalars.parquet`` — the bound gas channels, value and validity.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq
from typer.testing import CliRunner

from capa.cli import app
from capa.storage.manifest import BundleManifest

_EXPERIMENT_TOML = """
procedure = { id = "capa.builtin.free_run", config = { duration_s = 0.6 } }
calibration_set = { name = "default" }
operator = { id = "abr", display_name = "A. Researcher" }
sample = { id = "FUJI-SIM-1" }
tags = ["sim", "fuji"]

hardware = "hardware.toml"
"""

_HARDWARE_TOML = """
name = "fuji-sim"
[[devices]]
name = "analyzer"
adapter = "capa.devices.sim.fuji_sim"
[devices.params]
tick_period_s = 0.05
hold_from_s = 0.3
[devices.params.signals.CH3]
kind = "constant"
value = 20.5

[[channels]]
name = "gas.o2"
kind = "gas_concentration"
unit = "percent"
derived_unit = "percent"
[channels.source]
source = "fuji_channel"
device = "analyzer"
channel = "CH3"
[channels.calibration]
kind = "identity"
input_unit = "percent"
output_unit = "percent"

[[channels]]
name = "gas.o2_valid"
kind = "gas_concentration"
unit = "dimensionless"
derived_unit = "dimensionless"
[channels.source]
source = "fuji_channel"
device = "analyzer"
channel = "CH3"
field = "valid"
[channels.calibration]
kind = "identity"
input_unit = "dimensionless"
output_unit = "dimensionless"
"""


def test_a_simulated_run_writes_the_analyzers_records_and_channels(tmp_path: Path) -> None:
    (tmp_path / "experiment.toml").write_text(_EXPERIMENT_TOML, encoding="utf-8")
    (tmp_path / "hardware.toml").write_text(_HARDWARE_TOML, encoding="utf-8")
    runs = tmp_path / "runs"

    result = CliRunner().invoke(
        app, ["run", "--headless", "--runs-root", str(runs), str(tmp_path / "experiment.toml")]
    )
    assert result.exit_code == 0, result.stdout

    bundles = [p for p in runs.iterdir() if p.is_dir() and (p / "manifest.json").exists()]
    assert len(bundles) == 1
    bundle = bundles[0]
    manifest = BundleManifest.read(bundle / "manifest.json")
    assert (manifest.run_status, manifest.bundle_status) == ("completed", "sealed")
    assert manifest.integrity.status == "ok"

    records = pq.read_table(bundle / "device_records" / "fuji.parquet").to_pylist()
    assert len(records) >= 5
    assert {"ch3_value", "ch3_state", "ch3_valid", "ch1_gas", "error_type"} <= set(records[0])
    assert {row["ch3_value"] for row in records} == {20.5}
    # Output hold comes on part-way through: the rows say so.
    assert {row["ch3_state"] for row in records} == {"ok", "hold"}

    scalars = pq.read_table(bundle / "scalars.parquet").to_pylist()
    by_channel: dict[str, list[dict[str, object]]] = {}
    for row in scalars:
        by_channel.setdefault(str(row["channel"]), []).append(row)
    assert set(by_channel) == {"gas.o2", "gas.o2_valid"}
    assert {row["value"] for row in by_channel["gas.o2"]} == {20.5}
    assert {row["status"] for row in by_channel["gas.o2"]} == {"ok", "hold"}
    assert {row["value"] for row in by_channel["gas.o2_valid"]} == {0.0, 1.0}
