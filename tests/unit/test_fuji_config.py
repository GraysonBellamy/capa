"""The Fuji analyzer's place in capa's configuration surface.

The ``fuji_channel`` binding and the ``gas_concentration`` kind, the binding
policy, the channel templates, the storage layout, and the Layer-2 join
between an analyzer's ``channel_map`` and the channels bound to it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from capa.channels.calibration import Identity
from capa.channels.spec import ChannelKind, ChannelSpec, FujiChannel, SourceBinding
from capa.config import ConfigDocument, ConfigProblem, validate
from capa.config.binding_policy import (
    ALL_BINDING_SOURCES,
    filter_bindings_for_family,
    ordered_bindings_for_kind,
)
from capa.devices._templates import FUJI_CO, FUJI_CO2, FUJI_O2
from capa.devices.adapter import Capability
from capa.devices.registry import _import_builtins, get_descriptor
from capa.storage.finalize import _infer_layout_for_adapter


@pytest.fixture(scope="module", autouse=True)
def _ensure_builtins_loaded() -> None:
    _import_builtins()


# ---------------------------------------------------------------------------
# Binding and kind.
# ---------------------------------------------------------------------------


class TestBinding:
    def test_default_field_is_the_value(self) -> None:
        binding = FujiChannel(device="analyzer", channel="CH3")
        assert (binding.source, binding.field) == ("fuji_channel", "value")

    def test_validity_field(self) -> None:
        assert FujiChannel(device="analyzer", channel="CH12", field="valid").field == "valid"

    @pytest.mark.parametrize(
        "fields",
        [
            {"channel": "CH0"},
            {"channel": "CH13"},
            {"channel": "ch3"},
            {"channel": "CH3", "field": "stable"},
            {"channel": "CH3", "gas": "o2"},
        ],
    )
    def test_bad_bindings_are_refused(self, fields: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            FujiChannel(device="analyzer", **fields)

    def test_the_union_discriminates_on_source(self) -> None:
        adapter: TypeAdapter[SourceBinding] = TypeAdapter(SourceBinding)
        binding = adapter.validate_python(
            {"source": "fuji_channel", "device": "analyzer", "channel": "CH1"}
        )
        assert isinstance(binding, FujiChannel)

    def test_a_gas_channel_spec(self) -> None:
        spec = ChannelSpec(
            name="gas.o2",
            kind=ChannelKind.GAS_CONCENTRATION,
            source=FujiChannel(device="analyzer", channel="CH3"),
            unit="percent",
            derived_unit="percent",
            calibration=Identity(input_unit="percent", output_unit="percent"),
        )
        assert spec.kind.value == "gas_concentration"
        assert spec.output_unit() == "percent"


class TestPolicy:
    def test_gas_concentration_prefers_the_analyzer_binding(self) -> None:
        ordered = ordered_bindings_for_kind(ChannelKind.GAS_CONCENTRATION)
        assert ordered[:2] == ("fuji_channel", "nidaq_reading_field")
        assert set(ordered) == set(ALL_BINDING_SOURCES)

    def test_a_fuji_device_offers_only_its_binding(self) -> None:
        descriptor = get_descriptor("capa.devices.fuji")
        assert descriptor is not None
        offered = filter_bindings_for_family(
            ordered_bindings_for_kind(ChannelKind.GAS_CONCENTRATION),
            descriptor.supported_binding_sources,
        )
        assert offered == ("fuji_channel", "derived")


class TestTemplatesAndLayout:
    @pytest.mark.parametrize(
        ("template", "channel"), [(FUJI_CO2, "CH1"), (FUJI_CO, "CH2"), (FUJI_O2, "CH3")]
    )
    def test_templates_build_valid_channels(self, template: Any, channel: str) -> None:
        source = template.source_factory("analyzer")
        assert source == {
            "source": "fuji_channel",
            "device": "analyzer",
            "channel": channel,
            "field": "value",
        }
        spec = ChannelSpec.model_validate(
            {
                "name": template.id,
                "kind": template.kind,
                "source": source,
                "unit": template.default_unit,
                "derived_unit": template.default_derived_unit,
                "calibration": template.default_calibration,
                "plot_group": template.plot_group,
            }
        )
        assert spec.kind is ChannelKind.GAS_CONCENTRATION
        assert spec.plot_group == "gases"

    def test_the_records_file_is_a_wide_row_layout(self) -> None:
        assert _infer_layout_for_adapter("fuji") == "wide_row"

    def test_the_capability_for_a_gas_calibration_exists(self) -> None:
        assert Capability.HAS_GAS_CALIBRATION not in (
            Capability.HAS_INTERNAL_CAL,
            Capability.HAS_PARAMETER_CONFIG,
        )


# ---------------------------------------------------------------------------
# Layer 2 — the join between channel_map and the bound channels.
# ---------------------------------------------------------------------------


def _load(configs_dir: Path) -> ConfigDocument:
    return ConfigDocument.load(configs_dir / "experiments" / "fuji_real_freerun.yaml")


def _with_code(problems: list[ConfigProblem], code: str) -> list[ConfigProblem]:
    return [p for p in problems if p.code == code]


def test_the_real_rig_config_validates(configs_dir: Path) -> None:
    assert validate(_load(configs_dir)) == []


def test_a_channel_outside_the_channel_map_is_flagged(configs_dir: Path) -> None:
    doc = _load(configs_dir)
    doc.hardware_payload["devices"][0]["params"]["channel_map"].pop("CH3")
    problems = _with_code(validate(doc), "channels.fuji_channel_unmapped")
    # Both channels bound to CH3: its value and its validity.
    assert len(problems) == 2
    problem = problems[0]
    assert problem.severity == "error"
    assert problem.section == "channels"
    assert problem.path[0] == "channels"
    assert problem.path[-2:] == ("source", "channel")
    assert "CH3" in problem.message
    assert "['CH1', 'CH2']" in problem.message


def test_channel_map_keys_are_matched_whatever_their_case(configs_dir: Path) -> None:
    doc = _load(configs_dir)
    params = doc.hardware_payload["devices"][0]["params"]
    params["channel_map"] = {"ch1": "co2", "ch2": "co", "ch3": "o2"}
    assert _with_code(validate(doc), "channels.fuji_channel_unmapped") == []


def test_two_analyzers_on_one_port_are_flagged(configs_dir: Path) -> None:
    doc = _load(configs_dir)
    doc.hardware_payload["devices"].append(
        {
            "name": "analyzer_b",
            "adapter": "capa.devices.fuji",
            "params": {"port": "com8", "address": 2, "channel_map": {"CH1": "co2"}},
        }
    )
    problems = _with_code(validate(doc), "devices.fuji.shared_port")
    assert len(problems) == 1
    assert problems[0].path == ("devices", "analyzer_b", "params", "port")
    assert "'analyzer'" in problems[0].message


def test_a_binding_to_another_family_is_left_to_the_family_check(configs_dir: Path) -> None:
    doc = _load(configs_dir)
    doc.hardware_payload["devices"].append(
        {"name": "balance", "adapter": "capa.devices.sim.sartorius_sim", "params": {}}
    )
    doc.hardware_payload["channels"].append(
        {
            "name": "gas.stray",
            "kind": "gas_concentration",
            "unit": "percent",
            "derived_unit": "percent",
            "source": {"source": "fuji_channel", "device": "balance", "channel": "CH1"},
            "calibration": {"kind": "identity", "input_unit": "percent", "output_unit": "percent"},
        }
    )
    problems = validate(doc)
    assert len(_with_code(problems, "channels.binding_family_mismatch")) == 1
    assert _with_code(problems, "channels.fuji_channel_unmapped") == []
