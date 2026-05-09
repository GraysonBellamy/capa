from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from capa.channels.calibration import Identity
from capa.channels.spec import (
    ChannelKind,
    ChannelSpec,
    NIDAQReadingField,
    WatlowParameter,
)
from capa.core.errors import ConfigError
from capa.experiment.config import (
    CalibrationSetRef,
    DeviceConfig,
    ExperimentConfig,
    HardwareProfile,
    OperatorRef,
    ProcedureRef,
    SampleInfo,
)
from capa.experiment.method import (
    ChannelRef,
    HoldStep,
    Method,
    SafeShutdownStep,
)


def _hp_min() -> HardwareProfile:
    return HardwareProfile(
        name="t",
        devices=(DeviceConfig(name="heater", adapter="x"),),
        channels=(
            ChannelSpec(
                name="heater.pv",
                kind=ChannelKind.PROCESS_VAR,
                source=WatlowParameter(device="heater", parameter="process_value", instance=1),
                unit="degC",
                derived_unit="degC",
                calibration=Identity(input_unit="degC", output_unit="degC"),
            ),
            ChannelSpec(
                name="heater.setpoint",
                kind=ChannelKind.SETPOINT,
                source=WatlowParameter(device="heater", parameter="setpoint", instance=1),
                unit="degC",
                derived_unit="degC",
                calibration=Identity(input_unit="degC", output_unit="degC"),
            ),
        ),
    )


class TestHardwareProfile:
    def test_minimal(self) -> None:
        hp = _hp_min()
        assert hp.channel_names() == ("heater.pv", "heater.setpoint")

    def test_duplicate_device_rejected(self) -> None:
        with pytest.raises(ConfigError):
            HardwareProfile(
                name="t",
                devices=(
                    DeviceConfig(name="heater", adapter="x"),
                    DeviceConfig(name="heater", adapter="x"),
                ),
            )

    def test_duplicate_channel_rejected(self) -> None:
        spec = ChannelSpec(
            name="dup",
            kind=ChannelKind.ANALOG_IN,
            source=NIDAQReadingField(device="ni", task="t", field="f"),
            unit="V",
            derived_unit="V",
            calibration=Identity(input_unit="V", output_unit="V"),
        )
        with pytest.raises(ConfigError):
            HardwareProfile(
                name="t",
                devices=(DeviceConfig(name="ni", adapter="x"),),
                channels=(spec, spec),
            )

    def test_orphan_channel_rejected(self) -> None:
        with pytest.raises(ConfigError):
            HardwareProfile(
                name="t",
                devices=(),
                channels=(
                    ChannelSpec(
                        name="x",
                        kind=ChannelKind.PROCESS_VAR,
                        source=WatlowParameter(device="ghost", parameter="pv", instance=1),
                        unit="K",
                        derived_unit="K",
                        calibration=Identity(input_unit="K", output_unit="K"),
                    ),
                ),
            )


class TestExperimentConfig:
    def _make(self, method: Method | None) -> ExperimentConfig:
        return ExperimentConfig(
            hardware=_hp_min(),
            method=method,
            procedure=ProcedureRef(id="capa.builtin.recipe_runner"),
            calibration_set=CalibrationSetRef(name="default"),
            operator=OperatorRef(id="abr"),
            sample=SampleInfo(id="S001"),
        )

    def test_method_can_be_none(self) -> None:
        ec = self._make(None)
        assert ec.method is None

    def test_method_step_unknown_target_rejected(self) -> None:
        m = Method(
            name="bad",
            steps=(HoldStep(target=ChannelRef(name="ghost"), value=1, duration_s=1),),
        )
        with pytest.raises(ConfigError):
            self._make(m)

    def test_method_safe_shutdown_no_target(self) -> None:
        # SafeShutdownStep has no target, so the validator should not blow up.
        m = Method(name="ok", steps=(SafeShutdownStep(),))
        ec = self._make(m)
        assert ec.method is not None

    def test_load_minimal_hardware_fixture(self, configs_dir: Path) -> None:
        with open(configs_dir / "hardware/sim_minimal.toml", "rb") as f:
            data = tomllib.load(f)
        hp = HardwareProfile.model_validate(data)
        assert hp.name == "sim_minimal"
        assert len(hp.devices) == 4
        assert len(hp.channels) == 5

    def test_load_cone_hardware_fixture(self, configs_dir: Path) -> None:
        with open(configs_dir / "hardware/sim_cone.toml", "rb") as f:
            data = tomllib.load(f)
        hp = HardwareProfile.model_validate(data)
        assert hp.name == "sim_cone"
        # cone profile groups should all be present in metadata
        groups = {
            ch.metadata.get("cone_group") for ch in hp.channels if "cone_group" in ch.metadata
        }
        assert {
            "heater_pv",
            "heater_setpoint",
            "exhaust_flow",
            "mass",
            "thermocouples",
            "heat_flux_gauge",
            "oxygen",
        } <= groups

    def test_load_yaml_experiment_fixture(self, configs_dir: Path) -> None:
        yaml = YAML(typ="safe")
        data = yaml.load((configs_dir / "experiments/sim_freerun.yaml").read_text())
        ec = ExperimentConfig.model_validate(data)
        assert ec.procedure.id == "capa.builtin.free_run"
        assert ec.method is None
