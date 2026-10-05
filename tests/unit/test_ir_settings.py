"""IR-camera declarative settings (:mod:`capa.devices.camera.ir_settings`)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from capa.core.clock import RunClock
from capa.devices.adapter import DeviceCommand
from capa.devices.camera.base import (
    CameraSpec,
    CameraTemperatureRange,
    IrCameraStateSnapshot,
    IrRadiometricParams,
)
from capa.devices.camera.ir_settings import (
    IR_CAMERA_SETTINGS,
    IrCameraSettings,
    format_c,
    range_label,
)
from capa.devices.registry import require_descriptor
from capa.devices.settings import capture_settings, plan_settings
from capa.devices.sim.flir_ir_sim import FlirIrSim

pytestmark = pytest.mark.anyio

# The E85 reads its ranges back in Kelvin; converted, they land just off
# the round numbers.
_RANGES = (
    CameraTemperatureRange(min_c=-19.999999999999972, max_c=120.00000000000006),
    CameraTemperatureRange(min_c=0.0, max_c=650.0),
    CameraTemperatureRange(min_c=300.0, max_c=1200.0),
)
_RADIOMETRIC = IrRadiometricParams(
    emissivity=0.95,
    atmospheric_temp_c=20.000000000000057,
    reflected_temp_c=20.0,
    distance_m=1.0,
    relative_humidity=0.5,
    atmospheric_transmission=0.99,
)


def _snapshot(**overrides: Any) -> IrCameraStateSnapshot:
    base: dict[str, Any] = {
        "temperature_ranges": _RANGES,
        "temperature_range_index": 0,
        "radiometric": _RADIOMETRIC,
        "auto_nuc_interval_s": 300,
    }
    base.update(overrides)
    return IrCameraStateSnapshot.model_validate(base)


def _range(min_c: float, max_c: float) -> CameraTemperatureRange:
    return CameraTemperatureRange(min_c=min_c, max_c=max_c)


class TestRange:
    def test_active_range_within_tolerance_needs_no_change(self) -> None:
        desired = IrCameraSettings(temperature_range=_range(-20, 120.5))
        assert plan_settings(IR_CAMERA_SETTINGS, desired, _snapshot()) == ((), ())

    def test_other_range_is_matched_by_celsius(self) -> None:
        desired = IrCameraSettings(temperature_range=_range(0, 650))
        changes, issues = plan_settings(IR_CAMERA_SETTINGS, desired, _snapshot())
        assert issues == ()
        (change,) = changes
        assert (change.kind, dict(change.payload)) == ("set_temperature_range", {"index": 1})
        assert (change.current, change.desired) == ("-20 to 120 °C", "0 to 650 °C")
        assert change.note is not None

    def test_unoffered_range_is_an_issue_listing_the_offers(self) -> None:
        desired = IrCameraSettings(temperature_range=_range(0, 500))
        changes, issues = plan_settings(IR_CAMERA_SETTINGS, desired, _snapshot())
        assert changes == ()
        (issue,) = issues
        assert "no camera range matches 0 to 500 °C" in issue.message
        assert "300 to 1200 °C" in issue.message

    def test_camera_without_ranges_is_an_issue(self) -> None:
        snapshot = _snapshot(temperature_ranges=(), temperature_range_index=None)
        desired = IrCameraSettings(temperature_range=_range(0, 650))
        changes, issues = plan_settings(IR_CAMERA_SETTINGS, desired, snapshot)
        assert changes == ()
        assert "doesn't offer" in issues[0].message


class TestRadiometric:
    def test_readback_noise_needs_no_change(self) -> None:
        desired = IrCameraSettings(atmospheric_temp_c=20.0, emissivity=0.9500001)
        assert plan_settings(IR_CAMERA_SETTINGS, desired, _snapshot()) == ((), ())

    def test_changes_carry_the_command_payloads(self) -> None:
        desired = IrCameraSettings(
            emissivity=0.8,
            atmospheric_temp_c=25.0,
            reflected_temp_c=30.0,
            distance_m=0.5,
            relative_humidity=0.4,
            atmospheric_transmission=0.9,
        )
        changes, _ = plan_settings(IR_CAMERA_SETTINGS, desired, _snapshot())
        assert [(c.kind, dict(c.payload)) for c in changes] == [
            ("set_emissivity", {"emissivity": 0.8}),
            ("set_atmospheric_temp", {"temperature_c": 25.0}),
            ("set_reflected_temp", {"temperature_c": 30.0}),
            ("set_distance_m", {"distance_m": 0.5}),
            ("set_relative_humidity", {"relative_humidity": 0.4}),
            ("set_atmospheric_transmission", {"transmission": 0.9}),
        ]
        assert changes[1].current == "20 °C"

    def test_camera_without_radiometric_params_is_an_issue(self) -> None:
        desired = IrCameraSettings(emissivity=0.8)
        changes, issues = plan_settings(IR_CAMERA_SETTINGS, desired, _snapshot(radiometric=None))
        assert changes == ()
        assert "radiometric" in issues[0].message

    def test_bounds_are_validated(self) -> None:
        with pytest.raises(ValidationError):
            IrCameraSettings(relative_humidity=50.0)  # percent, not a fraction


class TestAutoNuc:
    def test_off_is_shown_as_off(self) -> None:
        changes, _ = plan_settings(
            IR_CAMERA_SETTINGS, IrCameraSettings(auto_nuc_interval_s=0), _snapshot()
        )
        assert [(c.current, c.desired) for c in changes] == [("300 s", "off")]

    def test_camera_without_auto_nuc_is_an_issue(self) -> None:
        changes, issues = plan_settings(
            IR_CAMERA_SETTINGS,
            IrCameraSettings(auto_nuc_interval_s=0),
            _snapshot(auto_nuc_interval_s=None),
        )
        assert changes == ()
        assert len(issues) == 1


def test_range_is_applied_before_radiometric_parameters() -> None:
    desired = IrCameraSettings(
        emissivity=0.8, auto_nuc_interval_s=0, temperature_range=_range(0, 650)
    )
    changes, _ = plan_settings(IR_CAMERA_SETTINGS, desired, _snapshot())
    assert [c.field for c in changes] == ["temperature_range", "emissivity", "auto_nuc_interval_s"]


def test_capture_is_rounded_and_round_trips() -> None:
    captured = capture_settings(IR_CAMERA_SETTINGS, _snapshot())
    assert captured == {
        "temperature_range": {"min_c": -20.0, "max_c": 120.0},
        "emissivity": 0.95,
        "atmospheric_temp_c": 20.0,
        "reflected_temp_c": 20.0,
        "distance_m": 1.0,
        "relative_humidity": 0.5,
        "atmospheric_transmission": 0.99,
        "auto_nuc_interval_s": 300,
    }
    desired = IrCameraSettings.model_validate(captured)
    assert plan_settings(IR_CAMERA_SETTINGS, desired, _snapshot()) == ((), ())


def test_labels() -> None:
    assert range_label(_RANGES[0]) == "-20 to 120 °C"
    assert format_c(-1e-12) == "0"


async def test_applies_to_the_simulator() -> None:
    """Plan → dispatch → read back against the IR simulator: nothing left."""
    assert require_descriptor("capa.devices.sim.flir_ir_sim").settings is IR_CAMERA_SETTINGS
    spec = CameraSpec.model_validate(
        {"name": "ir_cam0", "adapter": "capa.devices.sim.flir_ir_sim", "kind": "ir"}
    )
    sim = FlirIrSim(spec=spec, clock=RunClock.now())
    await sim.open()
    try:
        before = await sim.read_state_snapshot()
        assert before is not None
        target = before.temperature_ranges[-1]
        desired = IrCameraSettings(
            temperature_range=target, emissivity=0.8, distance_m=0.5, auto_nuc_interval_s=0
        )
        changes, issues = plan_settings(IR_CAMERA_SETTINGS, desired, before)
        assert issues == ()
        for change in changes:
            result = await sim.command(
                DeviceCommand(
                    kind=change.kind,
                    payload=dict(change.payload),
                    issued_by="op",
                    confirmed_by="op",
                )
            )
            assert result.accepted, result.detail
        after = await sim.read_state_snapshot()
        assert plan_settings(IR_CAMERA_SETTINGS, desired, after) == ((), ())
    finally:
        await sim.close()
