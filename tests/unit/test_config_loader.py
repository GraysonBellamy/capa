"""Tests for :meth:`ExperimentConfig.load`."""

from __future__ import annotations

from pathlib import Path

import pytest

from capa.core.errors import ConfigError
from capa.experiment.config import ExperimentConfig

_FREE_RUN_TOML = """
procedure = { id = "capa.builtin.free_run", config = { duration_s = 0.1 } }
calibration_set = { name = "default" }
operator = { id = "abr" }
sample = { id = "S-1" }

hardware = "hardware.toml"
"""

_HARDWARE_TOML = """
name = "tiny"
[[devices]]
name = "heater"
adapter = "capa.devices.sim.watlow_sim"
"""


def test_load_yaml_with_inline_hardware(configs_dir: Path) -> None:
    ec = ExperimentConfig.load(configs_dir / "experiments" / "sim_freerun.yaml")
    assert ec.procedure.id == "capa.builtin.free_run"
    assert ec.hardware.name == "sim_minimal"
    assert len(ec.hardware.devices) == 2


def test_load_toml_with_external_hardware_ref(tmp_path: Path) -> None:
    (tmp_path / "experiment.toml").write_text(_FREE_RUN_TOML, encoding="utf-8")
    (tmp_path / "hardware.toml").write_text(_HARDWARE_TOML, encoding="utf-8")
    ec = ExperimentConfig.load(tmp_path / "experiment.toml")
    assert ec.hardware.name == "tiny"
    assert ec.hardware.devices[0].name == "heater"


def test_load_unknown_suffix_raises(tmp_path: Path) -> None:
    (tmp_path / "x.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigError, match="unsupported config suffix"):
        ExperimentConfig.load(tmp_path / "x.json")


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="file not found"):
        ExperimentConfig.load(tmp_path / "nope.yaml")


def test_load_external_ref_resolves_relative(tmp_path: Path) -> None:
    sub = tmp_path / "configs"
    sub.mkdir()
    (sub / "exp.toml").write_text(
        'procedure = { id = "capa.builtin.free_run" }\n'
        'calibration_set = { name = "default" }\n'
        'operator = { id = "abr" }\n'
        'sample = { id = "S-1" }\n'
        'hardware = "../hw.toml"\n',
        encoding="utf-8",
    )
    (tmp_path / "hw.toml").write_text(_HARDWARE_TOML, encoding="utf-8")
    ec = ExperimentConfig.load(sub / "exp.toml")
    assert ec.hardware.name == "tiny"
