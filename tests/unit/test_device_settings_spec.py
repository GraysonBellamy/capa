"""Declarative device settings: the :mod:`capa.devices.settings` framework
and the Alicat / Sartorius specs (the IR camera's lives in
``test_ir_settings.py``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from capa.devices.alicat import ALICAT_SETTINGS, AlicatSettings, AlicatStateSnapshot
from capa.devices.registry import ensure_adapters_loaded, require_descriptor
from capa.devices.sartorius import SARTORIUS_SETTINGS, SartoriusSettings, SartoriusStateSnapshot
from capa.devices.settings import (
    DeviceSettingsSpec,
    SettingChange,
    SettingField,
    SettingIssue,
    SettingRefusedError,
    capture_settings,
    close_to,
    plan_settings,
)

# ---------------------------------------------------------------------------
# Framework, against a toy spec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Snapshot:
    level: float | None = None
    mode: str | None = None


class _Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    level: float | None = Field(default=None, ge=0)
    mode: str | None = None


def _refuse_loud(mode: str, _snapshot: Any) -> tuple[str, dict[str, Any]]:
    if mode == "loud":
        raise SettingRefusedError("too loud")
    return "set_mode", {"mode": mode}


_SPEC = DeviceSettingsSpec(
    model=_Settings,
    fields=(
        SettingField(
            name="level",
            label="Level",
            current=lambda s: s.level,
            command=lambda v, _s: ("set_level", {"value": v}),
            same=close_to(0.01),
            show=lambda v: f"{v:g} u",
        ),
        SettingField(
            name="mode",
            label="Mode",
            current=lambda s: s.mode,
            command=_refuse_loud,
            note="takes a moment",
        ),
    ),
    snapshot_type=_Snapshot,
)


class TestPlanSettings:
    def test_unset_fields_are_left_alone(self) -> None:
        assert plan_settings(_SPEC, _Settings(), _Snapshot(level=1.0, mode="a")) == ((), ())

    def test_matching_values_need_no_change(self) -> None:
        desired = _Settings(level=1.004, mode="a")
        assert plan_settings(_SPEC, desired, _Snapshot(level=1.0, mode="a")) == ((), ())

    def test_differing_values_become_changes_in_field_order(self) -> None:
        changes, issues = plan_settings(
            _SPEC, _Settings(level=2.0, mode="b"), _Snapshot(level=1.0, mode="a")
        )
        assert issues == ()
        assert changes == (
            SettingChange(
                field="level",
                label="Level",
                current="1 u",
                desired="2 u",
                kind="set_level",
                payload={"value": 2.0},
            ),
            SettingChange(
                field="mode",
                label="Mode",
                current="a",
                desired="b",
                kind="set_mode",
                payload={"mode": "b"},
                note="takes a moment",
            ),
        )

    def test_unreported_current_value_is_a_change(self) -> None:
        changes, _ = plan_settings(_SPEC, _Settings(mode="b"), _Snapshot())
        assert [(c.field, c.current) for c in changes] == [("mode", None)]

    def test_refused_value_is_an_issue_not_a_change(self) -> None:
        changes, issues = plan_settings(
            _SPEC, _Settings(level=2.0, mode="loud"), _Snapshot(level=1.0, mode="a")
        )
        assert [c.field for c in changes] == ["level"]
        assert issues == (SettingIssue(field="mode", label="Mode", message="too loud"),)


class TestCaptureSettings:
    def test_reported_values_only(self) -> None:
        assert capture_settings(_SPEC, _Snapshot(level=1.5)) == {"level": 1.5}

    def test_value_the_model_rejects_is_left_out(self) -> None:
        assert capture_settings(_SPEC, _Snapshot(level=-1.0, mode="a")) == {"mode": "a"}

    def test_capture_then_plan_is_a_no_op(self) -> None:
        snapshot = _Snapshot(level=3.0, mode="c")
        desired = _Settings.model_validate(capture_settings(_SPEC, snapshot))
        assert plan_settings(_SPEC, desired, snapshot) == ((), ())


def test_close_to() -> None:
    same = close_to(0.05)
    assert same(20.0, 20.04)
    assert not same(20.0, 20.06)


# ---------------------------------------------------------------------------
# Alicat
# ---------------------------------------------------------------------------


class TestAlicatSettings:
    def test_gas_is_canonicalized(self) -> None:
        assert AlicatSettings(gas="nitrogen").gas == "N2"
        assert AlicatSettings(gas="air").gas == "Air"

    def test_unknown_gas_suggests(self) -> None:
        with pytest.raises(ValidationError, match="did you mean"):
            AlicatSettings(gas="N3")

    def test_unknown_key_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AlicatSettings.model_validate({"gass": "N2"})

    def test_same_gas_any_spelling_needs_no_change(self) -> None:
        snapshot = AlicatStateSnapshot(gas="N2", gas_list=("Air", "N2"))
        assert plan_settings(ALICAT_SETTINGS, AlicatSettings(gas="n2"), snapshot) == ((), ())

    def test_gas_change_is_session_only(self) -> None:
        snapshot = AlicatStateSnapshot(gas="Air", gas_list=("Air", "N2"))
        changes, issues = plan_settings(ALICAT_SETTINGS, AlicatSettings(gas="N2"), snapshot)
        assert issues == ()
        assert [(c.current, c.desired, c.kind, dict(c.payload)) for c in changes] == [
            ("Air", "N2", "set_gas", {"gas": "N2", "save": False})
        ]

    def test_gas_the_device_does_not_offer_is_an_issue(self) -> None:
        snapshot = AlicatStateSnapshot(gas="Air", gas_list=("Air", "Ar"))
        changes, issues = plan_settings(ALICAT_SETTINGS, AlicatSettings(gas="N2"), snapshot)
        assert changes == ()
        assert [i.message for i in issues] == ["the device doesn't offer N2"]

    def test_unknown_current_gas_and_no_gas_list_still_plans(self) -> None:
        # Legacy firmware: no GS query, gas list unreadable.
        changes, issues = plan_settings(
            ALICAT_SETTINGS, AlicatSettings(gas="N2"), AlicatStateSnapshot()
        )
        assert issues == ()
        assert [(c.current, c.desired) for c in changes] == [(None, "N2")]

    def test_custom_mixture_compares_case_folded(self) -> None:
        snapshot = AlicatStateSnapshot(gas="MyMix", gas_list=("Air", "MyMix"))
        assert ALICAT_SETTINGS.fields[0].same("MyMix", "mymix")
        # A custom mixture isn't a registry gas, so capture leaves it out.
        assert capture_settings(ALICAT_SETTINGS, snapshot) == {}

    def test_capture(self) -> None:
        snapshot = AlicatStateSnapshot(gas="N2", gas_list=("Air", "N2"), setpoint=1.0)
        assert capture_settings(ALICAT_SETTINGS, snapshot) == {"gas": "N2"}


# ---------------------------------------------------------------------------
# Sartorius
# ---------------------------------------------------------------------------


class TestSartoriusSettings:
    def test_labels_are_validated(self) -> None:
        with pytest.raises(ValidationError):
            SartoriusSettings.model_validate({"stability_range": "sloppy"})

    def test_plan_sets_each_differing_menu_entry(self) -> None:
        snapshot = SartoriusStateSnapshot(
            filter_mode="stable",
            app_filter="final reading",
            stability_range="fast",
            stability_delay="short",
            auto_zero="on",
            tare_behavior="with stability",
        )
        desired = SartoriusSettings(
            filter_mode="very stable", stability_range="fast", stability_delay="long"
        )
        changes, issues = plan_settings(SARTORIUS_SETTINGS, desired, snapshot)
        assert issues == ()
        assert [(c.kind, dict(c.payload), c.current) for c in changes] == [
            ("set_filter_mode", {"mode": "very stable"}, "stable"),
            ("set_stability_delay", {"mode": "long"}, "short"),
        ]

    def test_capture_leaves_out_display_unit_and_calibration(self) -> None:
        snapshot = SartoriusStateSnapshot(
            filter_mode="stable",
            stability_range="accurate",
            display_unit="g",
            cal_temperature_c=22.0,
            cal_on_record=True,
        )
        assert capture_settings(SARTORIUS_SETTINGS, snapshot) == {
            "filter_mode": "stable",
            "stability_range": "accurate",
        }


# ---------------------------------------------------------------------------
# Descriptors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("adapter_id", "spec"),
    [
        ("capa.devices.alicat", ALICAT_SETTINGS),
        ("capa.devices.sim.alicat_sim", ALICAT_SETTINGS),
        ("capa.devices.sartorius", SARTORIUS_SETTINGS),
        ("capa.devices.sim.sartorius_sim", SARTORIUS_SETTINGS),
        ("capa.devices.watlow", None),
        ("capa.devices.sim.watlow_sim", None),
    ],
)
def test_descriptor_settings(adapter_id: str, spec: DeviceSettingsSpec | None) -> None:
    ensure_adapters_loaded()
    assert require_descriptor(adapter_id).settings is spec
