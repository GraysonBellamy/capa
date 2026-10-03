"""Batch child configs keep the profile specimen and the sample in step."""

from __future__ import annotations

from pathlib import Path

from capa.config.capa_profile import sample_specimen_mismatches
from capa.experiment.config import ExperimentConfig, ProcedureRef
from capa.experiment.procedures.builtin.batch import _build_child_config


def test_child_config_templates_sample_and_specimen_id(configs_dir: Path) -> None:
    parent = ExperimentConfig.load(configs_dir / "experiments" / "sim_capa_pyrolysis.yaml")
    child = _build_child_config(
        parent=parent,
        inner=ProcedureRef(id="capa.builtin.free_run"),
        child_sample_id="SIM-CAPA-001_002",
        batch_id="batch-1",
        iteration=2,
    )

    assert child.sample.id == "SIM-CAPA-001_002"
    assert child.domain_profile is not None
    specimen = child.domain_profile.metadata["specimen"]
    assert specimen["id"] == "SIM-CAPA-001_002"
    assert sample_specimen_mismatches(child.sample.model_dump(), specimen) == []
    # The parent is untouched.
    assert parent.domain_profile is not None
    assert parent.domain_profile.metadata["specimen"]["id"] == "SIM-CAPA-001"
    assert child.custom["batch"]["parent_sample_id"] == "SIM-CAPA-001"
