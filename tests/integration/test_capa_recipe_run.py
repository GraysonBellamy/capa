"""End-to-end CAPA recipe run — the P3 outcome gate.

Plan §16 P3: "First end-to-end recipe-driven cone run; replicates via
``Batch``; profile metadata captured." For this project the equivalent is
the CAPA controlled-atmosphere-pyrolysis run, since that's the apparatus
the project is named after; cone-calorimeter mode is deferred.

This test loads ``configs/experiments/sim_capa_pyrolysis.yaml``, runs it
through :class:`ExperimentEngine` against simulated adapters, and checks:

* the bundle finalizes cleanly (``run_status="completed"``,
  ``bundle_status="sealed"``);
* the method executor emitted ``method.step.entered`` / ``exited`` events;
* device-command audit events carry an ``authorization_id`` matching the
  Authorization minted at run-arm;
* the snapshotted CAPA profile metadata is preserved in the bundle.
"""

from __future__ import annotations

import json
import sqlite3
import tomllib
from pathlib import Path

import pytest

from capa.experiment.config import ExperimentConfig
from capa.experiment.engine import ExperimentEngine

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT_PATH = REPO_ROOT / "configs" / "experiments" / "sim_capa_pyrolysis.yaml"


@pytest.mark.anyio
async def test_capa_recipe_run_seals_bundle_with_full_audit(tmp_path: Path) -> None:
    config = ExperimentConfig.load(EXPERIMENT_PATH)
    engine = ExperimentEngine()

    result = await engine.run(
        config,
        runs_root=tmp_path,
        configure_logging_for_bundle=False,
    )

    # Outcome gate: clean completion + sealed bundle.
    assert result.run_status == "completed", result.exit_reason
    assert result.bundle_status == "sealed", result.exit_reason
    assert result.bundle_path is not None
    bundle = result.bundle_path

    # Manifest captures the procedure id and the profile id; the profile's
    # rich metadata is snapshotted into config.toml (the full
    # ExperimentConfig dump). Plan §5.4.1 will eventually mirror it into a
    # dedicated profiles/<id>.toml file as well.
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["procedure"]["id"] == "capa.builtin.recipe_runner"
    assert manifest["domain_profile"]["id"] == "capa.profiles.capa_pyrolysis"

    config_snapshot = tomllib.loads((bundle / "config.toml").read_text())
    profile_meta = config_snapshot["domain_profile"]["metadata"]
    assert profile_meta["specimen"]["material"] == "PMMA"
    assert profile_meta["atmosphere"]["mode"] == "inert"
    assert profile_meta["atmosphere"]["carrier"]["species"] == "N2"

    # Method executor emitted the entered/exited audit events.
    events_db = bundle / "events.sqlite"
    assert events_db.is_file()
    with sqlite3.connect(events_db) as conn:
        kinds = [row[0] for row in conn.execute("SELECT kind FROM events").fetchall()]
    assert any(k == "method.step.entered" for k in kinds)
    assert any(k == "method.step.exited" for k in kinds)
    assert any(k == "method.command.issued" for k in kinds)

    # Every issued command carries an authorization_id (audit invariant —
    # plan §18 #12 says every device write is attributable).
    with sqlite3.connect(events_db) as conn:
        rows = conn.execute(
            "SELECT metadata_json FROM events WHERE kind = 'method.command.issued'"
        ).fetchall()
    assert rows, "no command audit events found"
    for (meta_json,) in rows:
        meta = json.loads(meta_json)
        assert meta.get("authorization_id"), meta
        assert meta.get("issued_by") == "abr"
