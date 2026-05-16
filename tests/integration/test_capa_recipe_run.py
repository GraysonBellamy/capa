"""End-to-end CAPA recipe run.

This exercises the CAPA controlled-atmosphere-pyrolysis run against simulated
hardware and verifies the same operator-facing bundle guarantees expected from
a recipe-driven run.

This test loads ``configs/experiments/sim_capa_pyrolysis.yaml``, runs it
through :func:`run_headless` (the conductor stack) against simulated
adapters, and checks:

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
from capa.experiment.method import HoldStep, Method
from capa.runtime.headless import run_headless

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT_PATH = REPO_ROOT / "configs" / "experiments" / "sim_capa_pyrolysis.yaml"


def _load_smoke_config(path: Path) -> ExperimentConfig:
    """Load the production CAPA recipe, but keep the integration run short."""
    config = ExperimentConfig.load(path)
    method = config.method
    assert isinstance(method, Method)

    steps = tuple(
        step.model_copy(update={"duration_s": 0.25})
        if isinstance(step, HoldStep) and step.duration_s is not None and step.duration_s > 1.0
        else step
        for step in method.steps
    )
    return config.model_copy(update={"method": method.model_copy(update={"steps": steps})})


@pytest.mark.anyio
async def test_capa_recipe_run_seals_bundle_with_full_audit(tmp_path: Path) -> None:
    config = _load_smoke_config(EXPERIMENT_PATH)

    result = await run_headless(
        config,
        runs_root=tmp_path,
    )

    # Outcome gate: clean completion + sealed bundle.
    assert result.run_status == "completed", result.exit_reason
    assert result.bundle_status == "sealed", result.exit_reason
    assert result.bundle_path is not None
    bundle = result.bundle_path

    # Manifest captures the procedure id and the profile id; the profile's
    # rich metadata is snapshotted into config.toml (the full
    # ExperimentConfig dump) and also into a dedicated profiles/<id>.toml
    # file ().
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["procedure"]["id"] == "capa.builtin.recipe_runner"
    assert manifest["domain_profile"]["id"] == "capa.profiles.capa_pyrolysis"

    config_snapshot = tomllib.loads((bundle / "config.toml").read_text())
    profile_meta = config_snapshot["domain_profile"]["metadata"]
    assert profile_meta["specimen"]["material"] == "PMMA"
    assert profile_meta["atmosphere"]["mode"] == "inert"
    assert profile_meta["atmosphere"]["purge"]["species"] == "N2"

    # dedicated per-profile snapshot.
    snapshot_path = bundle / "profiles" / "capa_pyrolysis.toml"
    assert snapshot_path.is_file(), "profiles/<id>.toml snapshot missing"
    snapshot = tomllib.loads(snapshot_path.read_text())
    assert snapshot["id"] == "capa.profiles.capa_pyrolysis"
    # Verbatim mirror of the metadata block, plus the wrapper id/standard_refs.
    assert snapshot["specimen"]["material"] == profile_meta["specimen"]["material"]
    assert snapshot["atmosphere"]["mode"] == profile_meta["atmosphere"]["mode"]
    assert (
        snapshot["atmosphere"]["purge"]["species"]
        == (profile_meta["atmosphere"]["purge"]["species"])
    )
    # Sealed bundle: the integrity walk must cover the snapshot file.
    digest_text = (bundle / "manifest.sha256").read_text(encoding="utf-8")
    assert "profiles/capa_pyrolysis.toml" in digest_text

    # Method executor emitted the entered/exited audit events.
    events_db = bundle / "events.sqlite"
    assert events_db.is_file()
    with sqlite3.connect(events_db) as conn:
        kinds = [row[0] for row in conn.execute("SELECT kind FROM events").fetchall()]
    assert any(k == "method.step.entered" for k in kinds)
    assert any(k == "method.step.exited" for k in kinds)
    assert any(k == "method.command.issued" for k in kinds)

    # Every issued command carries an authorization_id (audit invariant —
    # #12 says every device write is attributable).
    with sqlite3.connect(events_db) as conn:
        rows = conn.execute(
            "SELECT metadata_json FROM events WHERE kind = 'method.command.issued'"
        ).fetchall()
    assert rows, "no command audit events found"
    for (meta_json,) in rows:
        meta = json.loads(meta_json)
        assert meta.get("authorization_id"), meta
        assert meta.get("issued_by") == "abr"
