"""Declarative settings for IR cameras — an experiment's ``device_settings:``
entry for any camera whose ``read_state_snapshot()`` returns an
:class:`IrCameraStateSnapshot`.

Shared by the in-tree IR simulator and the ``capa-flir`` adapter. The
temperature range is declared in °C and matched against the camera's own
list (its position is camera-specific); the range is applied before the
radiometric parameters so nothing the camera does on a range switch can
undo them.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from capa.devices.camera.base import (
    CameraTemperatureRange,
    IrCameraStateSnapshot,
    IrRadiometricParams,
)
from capa.devices.settings import (
    DeviceSettingsSpec,
    SettingField,
    SettingRefusedError,
    close_to,
)

RANGE_MATCH_TOLERANCE_C: Final[float] = 1.0
"""How far a declared range's ends may sit from the camera's own range
(read back in Kelvin and converted) and still count as that range."""


class IrCameraSettings(BaseModel):
    """What an experiment can declare for an IR camera under ``device_settings:``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    temperature_range: CameraTemperatureRange | None = Field(
        default=None,
        json_schema_extra={
            "capa_help": (
                "Measurement range in °C, matched against the camera's own "
                "ranges. Switching makes the camera recalibrate for a few seconds."
            ),
        },
    )
    emissivity: float | None = Field(default=None, ge=0.001, le=1.0)
    atmospheric_temp_c: float | None = Field(default=None, json_schema_extra={"capa_unit": "°C"})
    reflected_temp_c: float | None = Field(default=None, json_schema_extra={"capa_unit": "°C"})
    distance_m: float | None = Field(default=None, gt=0, json_schema_extra={"capa_unit": "m"})
    relative_humidity: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        json_schema_extra={"capa_help": "Fraction 0–1, not percent."},
    )
    atmospheric_transmission: float | None = Field(default=None, ge=0.0, le=1.0)
    auto_nuc_interval_s: int | None = Field(
        default=None,
        ge=0,
        json_schema_extra={
            "capa_unit": "s",
            "capa_help": "Seconds between automatic NUCs; 0 = off.",
        },
    )


def range_label(temperature_range: CameraTemperatureRange) -> str:
    """``"-20 to 120 °C"`` — the range as the operator picks it."""
    return f"{format_c(temperature_range.min_c)} to {format_c(temperature_range.max_c)} °C"


def format_c(value: float) -> str:
    """A Celsius value for display: one decimal, no ``-0``."""
    # A Kelvin read-back converts to e.g. -19.999999999999972; round it
    # back, and fold a rounded -0.0 into 0.
    rounded = round(value, 1) + 0.0
    return f"{rounded:g}"


def _active_range(snapshot: IrCameraStateSnapshot) -> CameraTemperatureRange | None:
    index = snapshot.temperature_range_index
    if index is None or not 0 <= index < len(snapshot.temperature_ranges):
        return None
    found = snapshot.temperature_ranges[index]
    return CameraTemperatureRange(
        min_c=round(found.min_c, 1) + 0.0, max_c=round(found.max_c, 1) + 0.0
    )


def _same_range(have: CameraTemperatureRange, want: CameraTemperatureRange) -> bool:
    return (
        abs(have.min_c - want.min_c) <= RANGE_MATCH_TOLERANCE_C
        and abs(have.max_c - want.max_c) <= RANGE_MATCH_TOLERANCE_C
    )


def _set_range(
    want: CameraTemperatureRange, snapshot: IrCameraStateSnapshot
) -> tuple[str, dict[str, Any]]:
    for index, offered in enumerate(snapshot.temperature_ranges):
        if _same_range(offered, want):
            return "set_temperature_range", {"index": index}
    if not snapshot.temperature_ranges:
        raise SettingRefusedError("the camera doesn't offer a temperature-range choice")
    offered_labels = "; ".join(range_label(r) for r in snapshot.temperature_ranges)
    raise SettingRefusedError(
        f"no camera range matches {range_label(want)} (offers: {offered_labels})"
    )


def _radiometric_field(
    name: str,
    label: str,
    *,
    kind: str,
    payload_key: str,
    tolerance: float,
    digits: int,
    unit: str = "",
) -> SettingField:
    def current(snapshot: IrCameraStateSnapshot) -> float | None:
        params: IrRadiometricParams | None = snapshot.radiometric
        return None if params is None else round(getattr(params, name), digits) + 0.0

    def command(want: float, snapshot: IrCameraStateSnapshot) -> tuple[str, dict[str, Any]]:
        if snapshot.radiometric is None:
            raise SettingRefusedError("the camera doesn't report radiometric parameters")
        return kind, {payload_key: want}

    return SettingField(
        name=name,
        label=label,
        current=current,
        command=command,
        same=close_to(tolerance),
        show=lambda value: f"{value:g}{unit}",
    )


def _set_auto_nuc(seconds: int, snapshot: IrCameraStateSnapshot) -> tuple[str, dict[str, Any]]:
    if snapshot.auto_nuc_interval_s is None:
        raise SettingRefusedError("the camera has no automatic-NUC interval")
    return "set_auto_nuc_interval", {"seconds": seconds}


IR_CAMERA_SETTINGS: Final = DeviceSettingsSpec(
    model=IrCameraSettings,
    fields=(
        SettingField(
            name="temperature_range",
            label="Temperature range",
            current=_active_range,
            command=_set_range,
            same=_same_range,
            show=range_label,
            note="The camera recalibrates for a few seconds.",
        ),
        _radiometric_field(
            "emissivity",
            "Emissivity",
            kind="set_emissivity",
            payload_key="emissivity",
            tolerance=1e-3,
            digits=3,
        ),
        _radiometric_field(
            "atmospheric_temp_c",
            "Atmospheric temperature",
            kind="set_atmospheric_temp",
            payload_key="temperature_c",
            tolerance=0.05,
            digits=2,
            unit=" °C",
        ),
        _radiometric_field(
            "reflected_temp_c",
            "Reflected temperature",
            kind="set_reflected_temp",
            payload_key="temperature_c",
            tolerance=0.05,
            digits=2,
            unit=" °C",
        ),
        _radiometric_field(
            "distance_m",
            "Object distance",
            kind="set_distance_m",
            payload_key="distance_m",
            tolerance=1e-3,
            digits=3,
            unit=" m",
        ),
        _radiometric_field(
            "relative_humidity",
            "Relative humidity",
            kind="set_relative_humidity",
            payload_key="relative_humidity",
            tolerance=1e-3,
            digits=3,
        ),
        _radiometric_field(
            "atmospheric_transmission",
            "Atmospheric transmission",
            kind="set_atmospheric_transmission",
            payload_key="transmission",
            tolerance=1e-3,
            digits=3,
        ),
        SettingField(
            name="auto_nuc_interval_s",
            label="Auto-NUC interval",
            current=lambda snapshot: snapshot.auto_nuc_interval_s,
            command=_set_auto_nuc,
            show=lambda seconds: "off" if seconds == 0 else f"{seconds} s",
        ),
    ),
    snapshot_type=IrCameraStateSnapshot,
)


__all__ = [
    "IR_CAMERA_SETTINGS",
    "RANGE_MATCH_TOLERANCE_C",
    "IrCameraSettings",
    "format_c",
    "range_label",
]
