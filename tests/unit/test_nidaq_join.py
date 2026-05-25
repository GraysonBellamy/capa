"""Tests for :mod:`capa.devices.nidaq_join` — the NI ↔ capa channel join helper.

Covers both entry points:

* :func:`declared_channels_from_payload` — raw, pre-validation dict payload
  (what the UI sees while the operator is mid-edit).
* :func:`declared_channels_from_config` — validated :class:`HardwareProfile`
  (what the Layer-2 validator sees at config-load time).
"""

from __future__ import annotations

from typing import Any

import pytest

from capa.devices.nidaq_join import (
    DeclaredNIDAQChannel,
    declared_channels_from_config,
    declared_channels_from_payload,
)

# ---------------------------------------------------------------------------
# Payload-side parser (pre-validation dicts)
# ---------------------------------------------------------------------------


def _ni_device(
    name: str = "cdaq1",
    task: str = "ai_task",
    channels: list[dict[str, Any]] | None = None,
    adapter: str = "capa.devices.nidaq",
) -> dict[str, Any]:
    return {
        "name": name,
        "adapter": adapter,
        "params": {
            "task_name": task,
            "channels": channels if channels is not None else [],
        },
    }


def _tc_channel(
    name: str | None, physical: str = "cDAQ1Mod1/ai0", units: str = "DEG_C"
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": "thermocouple",
        "physical_channel": physical,
        "thermocouple_type": "K",
        "min_val": 0.0,
        "max_val": 1000.0,
        "units": units,
    }
    if name is not None:
        out["name"] = name
    return out


def test_payload_extracts_typed_thermocouple() -> None:
    payload = {
        "devices": [
            _ni_device(channels=[_tc_channel(name="TC_top", physical="cDAQ1Mod1/ai0")]),
        ],
        "channels": [],
    }
    declared = declared_channels_from_payload(payload)
    assert declared == [
        DeclaredNIDAQChannel(
            device_name="cdaq1",
            task_name="ai_task",
            field_name="TC_top",
            physical_channel="cDAQ1Mod1/ai0",
            kind="thermocouple",
            units="DEG_C",
            is_bound_to_capa=False,
        )
    ]


def test_payload_field_name_defaults_to_physical_channel_when_name_missing() -> None:
    """NI's display-name fallback: when the channel dict has no ``name``,
    the polled :class:`DaqReading` is keyed by ``physical_channel`` — so
    that's the field a binding must equal.
    """
    payload = {
        "devices": [_ni_device(channels=[_tc_channel(name=None, physical="cDAQ1Mod1/ai3")])],
        "channels": [],
    }
    declared = declared_channels_from_payload(payload)
    assert len(declared) == 1
    assert declared[0].field_name == "cDAQ1Mod1/ai3"


def test_payload_skips_non_nidaq_devices() -> None:
    payload = {
        "devices": [
            {"name": "alicat1", "adapter": "capa.devices.alicat", "params": {}},
            _ni_device(channels=[_tc_channel(name="TC_a")]),
        ],
        "channels": [],
    }
    declared = declared_channels_from_payload(payload)
    assert [d.field_name for d in declared] == ["TC_a"]


def test_payload_handles_voltage_kind_with_optional_unit_string() -> None:
    payload = {
        "devices": [
            _ni_device(
                channels=[
                    {
                        "kind": "ai_voltage",
                        "physical_channel": "cDAQ1Mod2/ai0",
                        "name": "voltage_a",
                        "unit": "V",
                        "min_val": -10.0,
                        "max_val": 10.0,
                    }
                ]
            )
        ],
        "channels": [],
    }
    [d] = declared_channels_from_payload(payload)
    assert d.kind == "ai_voltage"
    assert d.units == "V"


def test_payload_falls_back_to_raw_kind_for_unknown_kinds() -> None:
    payload = {
        "devices": [
            _ni_device(
                channels=[
                    {
                        "kind": "digital_input",
                        "physical_channel": "cDAQ1Mod3/port0/line0",
                        "name": "trig",
                    }
                ]
            )
        ],
        "channels": [],
    }
    [d] = declared_channels_from_payload(payload)
    assert d.kind == "raw"
    assert d.units is None


def test_payload_resolves_is_bound_against_nidaq_reading_field() -> None:
    payload = {
        "devices": [
            _ni_device(
                channels=[
                    _tc_channel(name="TC_a"),
                    _tc_channel(name="TC_b", physical="cDAQ1Mod1/ai1"),
                ]
            )
        ],
        "channels": [
            {
                "name": "TC_sample_top",
                "source": {
                    "source": "nidaq_reading_field",
                    "device": "cdaq1",
                    "task": "ai_task",
                    "field": "TC_a",
                },
            }
        ],
    }
    declared = declared_channels_from_payload(payload)
    by_field = {d.field_name: d.is_bound_to_capa for d in declared}
    assert by_field == {"TC_a": True, "TC_b": False}


def test_payload_resolves_is_bound_against_nidaq_block_channel() -> None:
    payload = {
        "devices": [_ni_device(channels=[_tc_channel(name="TC_a")])],
        "channels": [
            {
                "name": "fast_tc",
                "source": {
                    "source": "nidaq_block_channel",
                    "device": "cdaq1",
                    "task": "ai_task",
                    "channel": "TC_a",
                },
            }
        ],
    }
    [d] = declared_channels_from_payload(payload)
    assert d.is_bound_to_capa is True


def test_payload_typo_in_field_does_not_set_bound() -> None:
    payload = {
        "devices": [_ni_device(channels=[_tc_channel(name="TC_a")])],
        "channels": [
            {
                "name": "TC_sample_top",
                "source": {
                    "source": "nidaq_reading_field",
                    "device": "cdaq1",
                    "task": "ai_task",
                    "field": "TC_A",  # typoed — different case
                },
            }
        ],
    }
    [d] = declared_channels_from_payload(payload)
    assert d.is_bound_to_capa is False


def test_payload_survives_malformed_inputs() -> None:
    """Pre-validation payloads can be partially garbage — the helper must not raise."""
    assert declared_channels_from_payload({}) == []
    assert declared_channels_from_payload({"devices": "nope"}) == []
    assert declared_channels_from_payload({"devices": [{"adapter": "capa.devices.nidaq"}]}) == []
    assert (
        declared_channels_from_payload(
            {
                "devices": [
                    {
                        "name": "cdaq1",
                        "adapter": "capa.devices.nidaq",
                        "params": {"task_name": "ai", "channels": [{"physical_channel": ""}]},
                    }
                ]
            }
        )
        == []
    )


# ---------------------------------------------------------------------------
# Validated-config-side parser
# ---------------------------------------------------------------------------


def _build_hardware_profile_with_tc() -> Any:
    from capa.channels.calibration import Identity
    from capa.channels.spec import ChannelSpec, NIDAQReadingField
    from capa.experiment.config import DeviceConfig, HardwareProfile

    devices = (
        DeviceConfig(
            name="cdaq1",
            adapter="capa.devices.nidaq",
            params={
                "task_name": "ai_task",
                "channels": [
                    {
                        "kind": "thermocouple",
                        "physical_channel": "cDAQ1Mod1/ai0",
                        "name": "TC_top",
                        "thermocouple_type": "K",
                        "min_val": 0.0,
                        "max_val": 1000.0,
                        "units": "DEG_C",
                    }
                ],
            },
        ),
    )
    channels = (
        ChannelSpec(
            name="TC_sample_top",
            kind="tc",
            unit="degC",
            calibration=Identity(input_unit="degC", output_unit="degC"),
            source=NIDAQReadingField(device="cdaq1", task="ai_task", field="TC_top"),
        ),
    )
    return HardwareProfile(name="rig", devices=devices, channels=channels)


def test_config_side_parses_validated_profile_and_resolves_binding() -> None:
    profile = _build_hardware_profile_with_tc()
    declared = declared_channels_from_config(profile)
    assert declared == [
        DeclaredNIDAQChannel(
            device_name="cdaq1",
            task_name="ai_task",
            field_name="TC_top",
            physical_channel="cDAQ1Mod1/ai0",
            kind="thermocouple",
            units="DEG_C",
            is_bound_to_capa=True,
        )
    ]


def test_config_side_returns_empty_when_no_nidaq_devices() -> None:
    from capa.experiment.config import HardwareProfile

    profile = HardwareProfile(name="empty")
    assert declared_channels_from_config(profile) == []


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
