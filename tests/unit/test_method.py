from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from capa.experiment.method import (
    ChannelRef,
    EndCondition,
    HoldStep,
    Method,
    PromptStep,
    RampStep,
    SafeShutdownStep,
    SetpointStep,
    WaitStep,
)


class TestSteps:
    def test_hold_requires_duration_or_end_condition(self) -> None:
        with pytest.raises(ValidationError):
            HoldStep(target=ChannelRef(name="x"), value=1.0)

    def test_hold_with_duration(self) -> None:
        step = HoldStep(target=ChannelRef(name="x"), value=1.0, duration_s=10.0)
        assert step.kind == "hold"

    def test_hold_with_end_condition(self) -> None:
        step = HoldStep(
            target=ChannelRef(name="heater.pv"),
            value=1.0,
            end_condition=EndCondition(channel="heater.pv", op=">=", value=400),
        )
        assert step.end_condition is not None
        assert step.end_condition.channel == "heater.pv"

    def test_ramp_requires_rate_or_duration(self) -> None:
        with pytest.raises(ValidationError):
            RampStep(target=ChannelRef(name="x"), end_value=100)

    def test_ramp_with_rate(self) -> None:
        step = RampStep(
            target=ChannelRef(name="heater.setpoint"),
            end_value=600.0,
            rate_per_second=2.0,
        )
        assert step.kind == "ramp"

    def test_setpoint(self) -> None:
        step = SetpointStep(target=ChannelRef(name="x"), value=42.0)
        assert step.value == 42.0

    def test_wait_requires_condition_or_duration(self) -> None:
        with pytest.raises(ValidationError):
            WaitStep()

    def test_wait_with_duration(self) -> None:
        step = WaitStep(duration_s=30.0)
        assert step.on_timeout == "warn"

    def test_prompt(self) -> None:
        step = PromptStep(message="Ignite sample, then press Continue")
        assert step.title == "Operator confirmation"

    def test_safe_shutdown(self) -> None:
        step = SafeShutdownStep(cool_target={"heater.setpoint": 100.0})
        assert step.kind == "safe_shutdown"


class TestMethod:
    def test_minimal(self) -> None:
        m = Method(
            name="t",
            steps=(HoldStep(target=ChannelRef(name="x"), value=1, duration_s=1),),
        )
        assert len(m.steps) == 1

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Method(name="t", steps=())

    def test_round_trip_through_dict(self) -> None:
        m = Method(
            name="round_trip",
            steps=(
                HoldStep(target=ChannelRef(name="x"), value=1, duration_s=1),
                RampStep(
                    target=ChannelRef(name="x"),
                    end_value=10,
                    rate_per_second=0.5,
                ),
                SafeShutdownStep(),
            ),
        )
        data = m.model_dump()
        m2 = Method.model_validate(data)
        assert m2 == m

    def test_load_from_fixture(self, configs_dir: Path) -> None:
        with open(configs_dir / "methods/sim_basic.method.toml", "rb") as f:
            data = tomllib.load(f)
        m = Method.model_validate(data)
        assert m.name == "sim_basic"
        assert [s.kind for s in m.steps] == [
            "hold",
            "ramp",
            "wait",
            "safe_shutdown",
        ]
