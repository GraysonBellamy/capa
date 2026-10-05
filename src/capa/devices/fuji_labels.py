"""Operator-facing names for a Fuji analyzer's channels, ranges and settings.

The analyzer numbers its channels (``CH1``..``CH12``) and each channel's
ranges (1, 2); neither number means anything at the bench. capa names a
channel by the gas its ``channel_map`` asserts and a range by its span, and
the enumerated settings by what they do.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Final

from fujilib import ChannelId, FujiValidationError, RangeInfo
from fujilib.registry.channels import MEASURED_CHANNELS, coerce_channel

_GAS_NAMES: Final[Mapping[str, str]] = {"nox": "NOx"}

RANGE_METHOD_NAMES: Final[Mapping[str, str]] = {
    "manual": "Manual",
    "auto": "Auto",
    "remote": "Remote (contact input)",
}
"""How a channel changes range, by the name the adapter reports and takes.
``remote`` follows a contact input and cannot be written."""

HOLD_MODE_NAMES: Final[Mapping[str, str]] = {
    "last_value": "Last reading",
    "setting": "Preset value",
}
"""What the outputs hold during a calibration, by the adapter's name."""


def gas_name(gas: str) -> str:
    """``"CO2"``, ``"NOx"``: an asserted gas (``"co2"``) as the operator reads it."""
    return _GAS_NAMES.get(gas.lower(), gas.upper())


def channel_names(channel_map: Mapping[str, str]) -> dict[str, str]:
    """``{"CH1": "CO2", "CH3": "O2"}``: each analyzer channel by its gas.

    Keyed by canonical channel id. A gas asserted on two channels (a
    measured CO and its O2-corrected value, say) names each with its
    channel too: ``"CO (CH2)"``.
    """
    gases = {_channel_id(channel): gas_name(gas) for channel, gas in channel_map.items()}
    counts = Counter(gases.values())
    return {
        channel: name if counts[name] == 1 else f"{name} ({channel})"
        for channel, name in gases.items()
    }


def is_measured(channel: str) -> bool:
    """Whether ``channel`` is one of the five measured channels, the ones
    with ranges, response times and calibration gases. Channels 6-12 carry
    O2-corrected values and averages."""
    return _channel_id(channel) in {c.value for c in MEASURED_CHANNELS}


def range_name(unit: str, full_scale: float) -> str:
    """``"0–25 vol%"``: a range by its span."""
    return f"0–{full_scale:g} {unit}"


def range_of(table: RangeInfo | None, number: int) -> str:
    """Range ``number`` of a channel by its span in the channel's range
    ``table``; ``"range 2"`` when the table does not say."""
    if table is None or not 1 <= number <= min(table.count, len(table.units)):
        return f"range {number}"
    unit, full_scale, _decimals = table.of(number)
    return range_name(unit.value, full_scale)


def _channel_id(channel: str | ChannelId) -> str:
    try:
        return coerce_channel(channel).value
    except FujiValidationError:
        return str(channel).upper()


__all__ = [
    "HOLD_MODE_NAMES",
    "RANGE_METHOD_NAMES",
    "channel_names",
    "gas_name",
    "is_measured",
    "range_name",
    "range_of",
]
