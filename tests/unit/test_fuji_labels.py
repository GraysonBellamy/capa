""":mod:`capa.devices.fuji_labels` — the names an operator reads for a Fuji
analyzer's channels and ranges."""

from __future__ import annotations

from fujilib import ChannelId, RangeInfo, Unit

from capa.devices.fuji_labels import channel_names, gas_name, is_measured, range_name, range_of


def test_a_gas_is_named_as_written() -> None:
    assert [gas_name(g) for g in ("co2", "co", "o2", "nox", "ch4")] == [
        "CO2",
        "CO",
        "O2",
        "NOx",
        "CH4",
    ]


def test_each_channel_is_named_by_its_gas() -> None:
    assert channel_names({"CH1": "co2", "ch3": "o2"}) == {"CH1": "CO2", "CH3": "O2"}
    # The same gas on two channels: each says which.
    assert channel_names({"CH2": "co", "CH6": "co", "CH3": "o2"}) == {
        "CH2": "CO (CH2)",
        "CH6": "CO (CH6)",
        "CH3": "O2",
    }


def test_only_channels_one_to_five_are_measured() -> None:
    assert is_measured("CH5")
    assert not is_measured("CH6")


def test_a_range_is_named_by_its_span() -> None:
    assert range_name("vol%", 25.0) == "0–25 vol%"
    assert range_name("ppm", 2000.0) == "0–2000 ppm"
    table = RangeInfo(
        channel=ChannelId.CH4,
        count=2,
        units=(Unit.PPM, Unit.PPM),
        full_scale=(200.0, 2000.0),
        decimals=(1, 0),
    )
    assert [range_of(table, n) for n in (1, 2)] == ["0–200 ppm", "0–2000 ppm"]
    # Without a table to say, the analyzer's number.
    assert range_of(None, 2) == "range 2"
    one = RangeInfo(
        channel=ChannelId.CH1,
        count=1,
        units=(Unit.VOL_PERCENT, Unit.VOL_PERCENT),
        full_scale=(10.0, 10.0),
        decimals=(2, 2),
    )
    assert range_of(one, 2) == "range 2"
