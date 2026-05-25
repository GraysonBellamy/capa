"""Unit tests for :mod:`capa.runtime.recording`.

Covers the resolution pipeline (default → policy override →
procedure plan_capture) and the rename-resilience contract on
:meth:`HeatFluxTune.plan_capture` — the most important test in the
recording-filter feature.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from capa.experiment.config import RecordingPolicy
from capa.experiment.procedures.base import Procedure
from capa.experiment.procedures.builtin.heat_flux_tune.controller import HeatFluxTune
from capa.runtime.recording import (
    ResolvedRecordingPlan,
    default_recording_plan,
    resolve_recording_plan,
)


@dataclass
class _FakeHardware:
    """Stub satisfying the two methods :func:`default_recording_plan` reads."""

    channels: tuple[str, ...]
    cameras: tuple[str, ...]

    def channel_names(self) -> tuple[str, ...]:
        return self.channels

    def camera_names(self) -> tuple[str, ...]:
        return self.cameras


class _PlanCaptureProcedure:
    """Minimal procedure stub that returns a narrowed plan."""

    def __init__(self, plan: ResolvedRecordingPlan | None) -> None:
        self._plan = plan

    def plan_capture(self, default_plan: ResolvedRecordingPlan) -> ResolvedRecordingPlan | None:
        return self._plan


class _NoOverrideProcedure:
    """Procedure with no plan_capture — falls through to full-rig."""


# ---------------------------------------------------------------------------
# default_recording_plan
# ---------------------------------------------------------------------------


class TestDefaultRecordingPlan:
    def test_records_all_channels_and_cameras(self) -> None:
        hw = _FakeHardware(
            channels=("a", "b", "c"),
            cameras=("ir", "visible"),
        )
        plan = default_recording_plan(hw)
        assert plan.channel_mode == "all"
        assert plan.recorded_channels == ("a", "b", "c")
        assert plan.camera_mode == "all"
        assert plan.recorded_cameras == ("ir", "visible")
        assert plan.source == "procedure_default"

    def test_allows_predicates_pass_everything(self) -> None:
        hw = _FakeHardware(channels=("a",), cameras=("ir",))
        plan = default_recording_plan(hw)
        assert plan.allows_channel("a") is True
        assert plan.allows_channel("not-in-hw") is True  # mode='all' accepts anything
        assert plan.allows_camera("ir") is True
        assert plan.allows_camera("not-in-hw") is True


# ---------------------------------------------------------------------------
# resolve_recording_plan
# ---------------------------------------------------------------------------


class TestResolveRecordingPlan:
    def test_no_procedure_returns_default(self) -> None:
        hw = _FakeHardware(channels=("a", "b"), cameras=("ir",))
        plan = resolve_recording_plan(
            hardware=hw,
            procedure=None,
            policy=RecordingPolicy(),
        )
        assert plan == default_recording_plan(hw)
        assert plan.source == "procedure_default"

    def test_record_all_policy_overrides_procedure(self) -> None:
        """`record_all` skips plan_capture entirely and stamps source=override."""
        hw = _FakeHardware(channels=("a", "b"), cameras=("ir",))
        narrowed = ResolvedRecordingPlan(
            channel_mode="only",
            recorded_channels=("a",),
            camera_mode="none",
            source="procedure_default",
        )
        proc = _PlanCaptureProcedure(narrowed)
        plan = resolve_recording_plan(
            hardware=hw,
            procedure=cast(Procedure, proc),
            policy=RecordingPolicy(mode="record_all"),
        )
        assert plan.channel_mode == "all"
        assert plan.camera_mode == "all"
        assert plan.source == "operator_override"

    def test_procedure_plan_capture_honoured(self) -> None:
        hw = _FakeHardware(channels=("a", "b", "c"), cameras=("ir",))
        narrowed = ResolvedRecordingPlan(
            channel_mode="only",
            recorded_channels=("a",),
            camera_mode="none",
            source="procedure_default",
        )
        plan = resolve_recording_plan(
            hardware=hw,
            procedure=cast(Procedure, _PlanCaptureProcedure(narrowed)),
            policy=RecordingPolicy(),
        )
        assert plan == narrowed

    def test_procedure_returning_none_falls_through_to_default(self) -> None:
        hw = _FakeHardware(channels=("a",), cameras=("ir",))
        plan = resolve_recording_plan(
            hardware=hw,
            procedure=cast(Procedure, _PlanCaptureProcedure(None)),
            policy=RecordingPolicy(),
        )
        assert plan == default_recording_plan(hw)

    def test_procedure_without_plan_capture_falls_through(self) -> None:
        """A procedure plugin that doesn't implement plan_capture still works."""
        hw = _FakeHardware(channels=("a",), cameras=("ir",))
        plan = resolve_recording_plan(
            hardware=hw,
            procedure=cast(Procedure, _NoOverrideProcedure()),
            policy=RecordingPolicy(),
        )
        assert plan == default_recording_plan(hw)


# ---------------------------------------------------------------------------
# HeatFluxTune.plan_capture — the rename-resilience contract
# ---------------------------------------------------------------------------


class TestHeatFluxTunePlanCapture:
    def test_default_channels_named(self) -> None:
        """Defaults — three canonical channels, no cameras."""
        hw = _FakeHardware(channels=("any",), cameras=("any",))
        proc = HeatFluxTune.from_config({"targets_kw_m2": (50.0,), "t_set_max_c": 900.0})
        plan = proc.plan_capture(default_recording_plan(hw))
        assert plan.channel_mode == "only"
        assert plan.recorded_channels == (
            "heat_flux_gauge",
            "heater.pv",
            "heater.setpoint",
        )
        assert plan.camera_mode == "none"
        assert plan.recorded_cameras == ()

    def test_rename_resilience(self) -> None:
        """Rebinding ``flux_channel`` produces the rebound name in the plan.

        The filter must derive from config fields, not string literals.
        A plugin author who hardcodes ``"heat_flux_gauge"`` will fail this
        test.
        """
        hw = _FakeHardware(channels=("flux_b", "pv_b", "sp_b"), cameras=())
        proc = HeatFluxTune.from_config(
            {
                "targets_kw_m2": (50.0,),
                "t_set_max_c": 900.0,
                "flux_channel": "flux_b",
                "heater_pv_channel": "pv_b",
                "heater_setpoint_channel": "sp_b",
            }
        )
        plan = proc.plan_capture(default_recording_plan(hw))
        assert plan.recorded_channels == ("flux_b", "pv_b", "sp_b")
        # Critically: none of the *default* names appear.
        assert "heat_flux_gauge" not in plan.recorded_channels
        assert "heater.pv" not in plan.recorded_channels
        assert "heater.setpoint" not in plan.recorded_channels

    def test_resolve_end_to_end_with_heat_flux_tune(self) -> None:
        """The full pipeline against a real procedure instance."""
        hw = _FakeHardware(
            channels=("heat_flux_gauge", "heater.pv", "heater.setpoint", "balance.mass"),
            cameras=("ir", "visible"),
        )
        proc = HeatFluxTune.from_config({"targets_kw_m2": (50.0,), "t_set_max_c": 900.0})
        plan = resolve_recording_plan(hardware=hw, procedure=proc, policy=RecordingPolicy())
        # Procedure-narrowed: only three channels, no cameras.
        assert plan.channel_mode == "only"
        assert plan.recorded_channels == (
            "heat_flux_gauge",
            "heater.pv",
            "heater.setpoint",
        )
        assert plan.camera_mode == "none"
        # `balance.mass` would be in the default but the plan excludes it.
        assert plan.allows_channel("balance.mass") is False
        assert plan.allows_camera("ir") is False
