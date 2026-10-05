"""Declarative settings for Fuji gas analyzers — an experiment's
``device_settings:`` entry for a device whose ``read_state_snapshot()``
returns a :class:`FujiStateSnapshot`.

Per gas, by the gas the channel map asserts (``co2_response_time_s``, not
``CH1``): the response time, the range method, and the range, named by its
span (``{full_scale: 25, unit: vol%}``, not ``2``). For the analyzer as a
whole: output hold and what the outputs hold. The calibration gases are
left out: the next calibration is computed from them, so they are changed
deliberately, on the manual card.

Unlike the Alicat's and the balance's settings, the analyzer keeps these
through a power cycle. Field order is the apply order: a gas's range method
before its range, since a range is selected only while the method is
manual.

Shared by :class:`~capa.devices.fuji.FujiAdapter` and the simulator.
"""

from __future__ import annotations

import math
from typing import Any, Final, Literal, Self, get_args

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from capa.devices.fuji import FujiChannelSettings, FujiRange, FujiStateSnapshot
from capa.devices.fuji_labels import gas_name, range_name
from capa.devices.settings import DeviceSettingsSpec, SettingField, SettingRefusedError

RangeUnit = Literal["vol%", "ppm", "mg/m3", "g/m3"]
RangeMethodLabel = Literal["manual", "auto"]
HoldModeLabel = Literal["last reading", "preset value"]

GASES: Final[tuple[tuple[str, str], ...]] = (
    ("co2", "carbon_dioxide"),
    ("co", "carbon_monoxide"),
    ("o2", "oxygen"),
    ("ch4", "methane"),
    ("so2", "sulfur_dioxide"),
    ("no", "nitric_oxide"),
    ("nox", "nitrogen_oxides"),
)
"""Every gas a channel map can assert, with the form group it is shown in."""

_HOLD_MODES: Final[dict[str, HoldModeLabel]] = {
    "last_value": "last reading",
    "setting": "preset value",
}
"""The adapter's hold mode by the name an experiment declares it with."""


class FujiRangeSpan(BaseModel):
    """A range by its span, as the analyzer's range table gives it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    full_scale: float = Field(gt=0)
    unit: RangeUnit


class _FujiSettingsBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    output_hold: bool | None = Field(
        default=None,
        json_schema_extra={
            "capa_help": "Hold the outputs, and the recorded values, during a calibration."
        },
    )
    hold_mode: HoldModeLabel | None = Field(
        default=None,
        json_schema_extra={
            "capa_help": (
                "What the outputs hold: the last reading before the calibration, "
                "or each channel's preset value."
            ),
        },
    )

    @model_validator(mode="after")
    def _range_needs_manual(self) -> Self:
        for gas, _group in GASES:
            if getattr(self, f"{gas}_range_method") == "auto" and getattr(self, f"{gas}_range"):
                raise ValueError(
                    f"{gas}_range_method is auto but {gas}_range is set; the analyzer "
                    "picks the range itself under auto, so declare one or the other"
                )
        return self


def _gas_fields(gas: str, group: str) -> dict[str, Any]:
    name = gas_name(gas)
    extra = {"capa_group": group, "capa_group_subtitle": name}
    return {
        f"{gas}_response_time_s": (
            int | None,
            Field(
                default=None,
                ge=0,
                le=60,
                title="Response time",
                json_schema_extra={
                    **extra,
                    "capa_unit": "s",
                    "capa_help": f"The {name} reading's response-time filter; 0 switches it off.",
                },
            ),
        ),
        f"{gas}_range_method": (
            RangeMethodLabel | None,
            Field(
                default=None,
                title="Range method",
                json_schema_extra={
                    **extra,
                    "capa_help": (
                        "manual: the range below. auto: up at 90 % of the low "
                        "range, back down below 80 %."
                    ),
                },
            ),
        ),
        f"{gas}_range": (
            FujiRangeSpan | None,
            Field(
                default=None,
                title="Range",
                json_schema_extra={
                    **extra,
                    "capa_help": (
                        f"The {name} range by its span, matched against the analyzer's "
                        "ranges. Needs the range method manual."
                    ),
                },
            ),
        ),
    }


FujiSettings = create_model(
    "FujiSettings",
    __base__=_FujiSettingsBase,
    __doc__="What an experiment can declare for a Fuji analyzer under ``device_settings:``.",
    **{key: value for gas, group in GASES for key, value in _gas_fields(gas, group).items()},
)


# ---------------------------------------------------------------------------
# Reading and setting
# ---------------------------------------------------------------------------


def _channel(gas: str, snapshot: FujiStateSnapshot) -> FujiChannelSettings | None:
    return next((c for c in snapshot.channels if c.gas == gas), None)


def _need_channel(gas: str, snapshot: FujiStateSnapshot) -> FujiChannelSettings:
    channel = _channel(gas, snapshot)
    if channel is not None:
        return channel
    if not snapshot.channels:
        raise SettingRefusedError("the analyzer's settings have not been read")
    raise SettingRefusedError(f"the channel map asserts no measured {gas_name(gas)} channel")


def _current_span(gas: str, snapshot: FujiStateSnapshot) -> FujiRangeSpan | None:
    channel = _channel(gas, snapshot)
    active = channel.range(channel.current_range) if channel is not None else None
    if active is None or active.unit not in get_args(RangeUnit):
        return None
    return FujiRangeSpan.model_validate({"full_scale": active.full_scale, "unit": active.unit})


def _same_span(have: FujiRangeSpan | FujiRange, want: FujiRangeSpan) -> bool:
    return have.unit == want.unit and math.isclose(have.full_scale, want.full_scale, rel_tol=1e-6)


def _span_text(span: FujiRangeSpan) -> str:
    return range_name(span.unit, span.full_scale)


def _gas_setting_fields(gas: str) -> tuple[SettingField, ...]:
    name = gas_name(gas)

    def set_response_time(seconds: int, snapshot: FujiStateSnapshot) -> tuple[str, dict[str, Any]]:
        channel = _need_channel(gas, snapshot)
        return "set_response_time", {"target": channel.channel, "seconds": seconds}

    def set_range_method(method: str, snapshot: FujiStateSnapshot) -> tuple[str, dict[str, Any]]:
        channel = _need_channel(gas, snapshot)
        return "set_range_method", {"channel": channel.channel, "method": method}

    def set_range(want: FujiRangeSpan, snapshot: FujiStateSnapshot) -> tuple[str, dict[str, Any]]:
        channel = _need_channel(gas, snapshot)
        for offered in channel.ranges:
            if _same_span(offered, want):
                return "set_range", {"channel": channel.channel, "range": offered.number}
        offers = "; ".join(r.name for r in channel.ranges) or "none"
        raise SettingRefusedError(
            f"no {name} range is {_span_text(want)} (the analyzer offers: {offers})"
        )

    def response_time(snapshot: FujiStateSnapshot) -> int | None:
        channel = _channel(gas, snapshot)
        return channel.response_time_s if channel is not None else None

    def range_method(snapshot: FujiStateSnapshot) -> str | None:
        channel = _channel(gas, snapshot)
        return channel.range_method if channel is not None else None

    return (
        SettingField(
            name=f"{gas}_response_time_s",
            label=f"{name} response time",
            current=response_time,
            command=set_response_time,
            show=lambda seconds: f"{seconds} s" if seconds else "0 s (filter off)",
        ),
        SettingField(
            name=f"{gas}_range_method",
            label=f"{name} range method",
            current=range_method,
            command=set_range_method,
        ),
        SettingField(
            name=f"{gas}_range",
            label=f"{name} range",
            current=lambda snapshot: _current_span(gas, snapshot),
            command=set_range,
            same=_same_span,
            show=_span_text,
            note="Selected only while the range method is manual.",
        ),
    )


def _set_output_hold(enabled: bool, _snapshot: FujiStateSnapshot) -> tuple[str, dict[str, Any]]:
    return "set_output_hold", {"enabled": enabled}


def _current_hold_mode(snapshot: FujiStateSnapshot) -> str | None:
    mode = snapshot.hold_mode
    return _HOLD_MODES.get(mode, mode) if mode is not None else None


def _set_hold_mode(label: str, _snapshot: FujiStateSnapshot) -> tuple[str, dict[str, Any]]:
    mode = next(mode for mode, shown in _HOLD_MODES.items() if shown == label)
    return "set_hold_mode", {"mode": mode}


FUJI_SETTINGS: Final = DeviceSettingsSpec(
    model=FujiSettings,
    fields=(
        SettingField(
            name="output_hold",
            label="Output hold",
            current=lambda snapshot: snapshot.output_hold,
            command=_set_output_hold,
            show=lambda enabled: "on" if enabled else "off",
        ),
        SettingField(
            name="hold_mode",
            label="Hold mode",
            current=_current_hold_mode,
            command=_set_hold_mode,
        ),
        *(field for gas, _group in GASES for field in _gas_setting_fields(gas)),
    ),
    snapshot_type=FujiStateSnapshot,
)
"""Shared by :class:`~capa.devices.fuji.FujiAdapter` and the simulator."""


__all__ = ["FUJI_SETTINGS", "GASES", "FujiRangeSpan", "FujiSettings"]
