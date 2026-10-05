"""Declarative settings for USB webcams — an experiment's ``device_settings:``
entry for a camera whose ``read_state_snapshot()`` returns a
:class:`WebcamStateSnapshot`.

Every UVC control the adapter can set is declarable: framing (zoom, pan,
tilt), focus, exposure, white balance and the image adjustments, plus the
auto toggles for focus, exposure and white balance. The model's field
order is the apply order: zoom before pan and tilt (digital-PTZ cameras
such as the C930e limit pan and tilt to the zoomed view), and each auto
toggle before its value.

A value is checked against the range and step the camera declared before
anything is sent. Setting a value puts that control in manual mode, so a
control the camera is driving itself reads back as ``"auto"``.
"""

from __future__ import annotations

from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from capa.devices.camera.base import WebcamControlState, WebcamStateSnapshot
from capa.devices.settings import DeviceSettingsSpec, SettingField, SettingRefusedError

AUTO: Final = "auto"
"""How a value reads back while the camera is driving that control itself."""

_AUTO_PAIRS: Final = ("focus", "exposure", "white_balance")


def _extra(group: str, help_text: str, unit: str | None = None) -> dict[str, Any]:
    extra: dict[str, Any] = {"capa_group": group, "capa_group_open": True, "capa_help": help_text}
    if unit is not None:
        extra["capa_unit"] = unit
    return extra


_CAMERA_UNITS: Final = "Camera units; the range depends on the camera (the manual card shows it)."


class WebcamSettings(BaseModel):
    """What an experiment can declare for a webcam under ``device_settings:``.

    Field order is the apply order. Values are strict ints so a typo such as
    ``exposure: true`` is an error rather than ``1``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    zoom: int | None = Field(
        default=None,
        strict=True,
        title="Optical zoom",
        json_schema_extra=_extra(
            "framing",
            f"{_CAMERA_UNITS} Applied before pan and tilt, which some cameras "
            "limit to the zoomed view.",
        ),
    )
    digital_zoom: int | None = Field(
        default=None,
        strict=True,
        json_schema_extra=_extra("framing", f"Crop and upscale inside the camera. {_CAMERA_UNITS}"),
    )
    pan: int | None = Field(
        default=None,
        strict=True,
        json_schema_extra=_extra(
            "framing", "Arc-seconds on most cameras (3600 = 1°); 0 is centered."
        ),
    )
    tilt: int | None = Field(
        default=None,
        strict=True,
        json_schema_extra=_extra(
            "framing", "Arc-seconds on most cameras (3600 = 1°); 0 is centered."
        ),
    )
    auto_focus: bool | None = Field(
        default=None,
        strict=True,
        json_schema_extra=_extra(
            "focus", "On: the camera focuses continuously. Off: it holds its focus."
        ),
    )
    focus: int | None = Field(
        default=None,
        strict=True,
        json_schema_extra=_extra(
            "focus", f"Manual focus position. {_CAMERA_UNITS} Setting it turns auto focus off."
        ),
    )
    auto_exposure: bool | None = Field(
        default=None,
        strict=True,
        json_schema_extra=_extra(
            "exposure", "On: the camera picks the exposure. Off: it holds its exposure."
        ),
    )
    exposure: int | None = Field(
        default=None,
        strict=True,
        json_schema_extra=_extra(
            "exposure",
            "UVC log2 seconds: -6 is about 1/64 s. The range depends on the "
            "camera. Setting it turns auto exposure off.",
        ),
    )
    auto_white_balance: bool | None = Field(
        default=None,
        strict=True,
        json_schema_extra=_extra(
            "white_balance",
            "On: the camera picks the white balance. Off: it holds its white balance.",
        ),
    )
    white_balance: int | None = Field(
        default=None,
        strict=True,
        json_schema_extra=_extra(
            "white_balance",
            "Color temperature. Setting it turns auto white balance off.",
            unit="K",
        ),
    )
    brightness: int | None = Field(
        default=None, strict=True, json_schema_extra=_extra("image", _CAMERA_UNITS)
    )
    contrast: int | None = Field(
        default=None, strict=True, json_schema_extra=_extra("image", _CAMERA_UNITS)
    )
    saturation: int | None = Field(
        default=None,
        strict=True,
        json_schema_extra=_extra("image", f"0 is grayscale. {_CAMERA_UNITS}"),
    )
    sharpness: int | None = Field(
        default=None, strict=True, json_schema_extra=_extra("image", _CAMERA_UNITS)
    )
    gamma: int | None = Field(
        default=None, strict=True, json_schema_extra=_extra("image", _CAMERA_UNITS)
    )
    hue: int | None = Field(
        default=None, strict=True, json_schema_extra=_extra("image", _CAMERA_UNITS)
    )
    gain: int | None = Field(
        default=None,
        strict=True,
        json_schema_extra=_extra("image", f"Higher gain raises noise. {_CAMERA_UNITS}"),
    )
    backlight_compensation: int | None = Field(
        default=None,
        strict=True,
        json_schema_extra=_extra("image", f"0 is off. {_CAMERA_UNITS}"),
    )

    @model_validator(mode="after")
    def _auto_or_value(self) -> Self:
        for control in _AUTO_PAIRS:
            if getattr(self, f"auto_{control}") is True and getattr(self, control) is not None:
                raise ValueError(
                    f"auto_{control} is true but {control} is set; setting {control} "
                    f"turns auto off, so declare one or the other"
                )
        return self


def _control(name: str, snapshot: WebcamStateSnapshot) -> WebcamControlState:
    if snapshot.unavailable is not None:
        raise SettingRefusedError(snapshot.unavailable)
    control = snapshot.controls.get(name)
    if control is None:
        raise SettingRefusedError("the camera doesn't have this control")
    return control


def _check_range(want: int, control: WebcamControlState) -> None:
    low, high, step = control.minimum, control.maximum, control.step
    if low is None or high is None:
        return
    if not low <= want <= high:
        raise SettingRefusedError(f"{want} is outside the camera's range, {low} to {high}")
    if step is not None and step > 1 and (want - low) % step:
        below = want - (want - low) % step
        nearest = " or ".join(str(v) for v in (below, below + step) if v <= high)
        raise SettingRefusedError(
            f"the camera takes {low} to {high} in steps of {step}; nearest is {nearest}"
        )


def _value_field(name: str, label: str, unit: str | None) -> SettingField:
    suffix = "" if unit is None else f" {unit}"

    def current(snapshot: WebcamStateSnapshot) -> int | str | None:
        control = snapshot.controls.get(name)
        if control is None or control.value is None:
            return None
        # Not a valid value for the model, so capture leaves it out.
        return AUTO if control.auto else control.value

    def command(want: int, snapshot: WebcamStateSnapshot) -> tuple[str, dict[str, Any]]:
        _check_range(want, _control(name, snapshot))
        return f"set_{name}", {"value": want}

    def show(value: int | str) -> str:
        return value if isinstance(value, str) else f"{value}{suffix}"

    return SettingField(name=name, label=label, current=current, command=command, show=show)


def _auto_field(name: str, label: str) -> SettingField:
    control_name = name.removeprefix("auto_")

    def current(snapshot: WebcamStateSnapshot) -> bool | None:
        control = snapshot.controls.get(control_name)
        return None if control is None else control.auto

    def command(want: bool, snapshot: WebcamStateSnapshot) -> tuple[str, dict[str, Any]]:
        _control(control_name, snapshot)
        return f"set_{name}", {"enable": want}

    return SettingField(
        name=name,
        label=label,
        current=current,
        command=command,
        show=lambda enabled: "on" if enabled else "off",
    )


def _build_fields() -> tuple[SettingField, ...]:
    fields: list[SettingField] = []
    for name, info in WebcamSettings.model_fields.items():
        label = info.title or name.replace("_", " ").capitalize()
        if name.startswith("auto_"):
            fields.append(_auto_field(name, label))
        else:
            extra = info.json_schema_extra
            unit = extra.get("capa_unit") if isinstance(extra, dict) else None
            fields.append(_value_field(name, label, unit if isinstance(unit, str) else None))
    return tuple(fields)


WEBCAM_SETTINGS: Final = DeviceSettingsSpec(
    model=WebcamSettings,
    fields=_build_fields(),
    snapshot_type=WebcamStateSnapshot,
)


__all__ = ["AUTO", "WEBCAM_SETTINGS", "WebcamSettings"]
