"""Tests for the CAPA (controlled atmosphere pyrolysis) domain profile."""

from __future__ import annotations

from typing import Any

import pytest

from capa.experiment.profiles import capa_pyrolysis as cap


def _good_metadata() -> dict[str, Any]:
    return {
        "specimen": {
            "id": "P-001",
            "material": "PMMA",
            "initial_mass_g": 5.0,
            "form": "disk",
            "specimen_holder": "stainless steel cup",
        },
        "program": {
            "target_heat_flux_kw_m2": 50.0,
            "heater_setpoint_c": 600.0,
        },
        "atmosphere": {
            "mode": "inert",
            "purge": {
                "species": "N2",
                "purity": "UHP 5.0",
                "target_flow_slpm": 100.0,
            },
        },
    }


def test_validate_metadata_accepts_minimal_inert_run() -> None:
    meta = cap.validate_metadata(_good_metadata())
    assert meta.specimen.material == "PMMA"
    assert meta.atmosphere.mode == "inert"
    assert meta.atmosphere.reactive is None


def test_validate_metadata_rejects_negative_mass() -> None:
    raw = _good_metadata()
    raw["specimen"]["initial_mass_g"] = 0.0
    with pytest.raises(Exception):
        cap.validate_metadata(raw)


def test_validate_metadata_oxidative_with_reactive() -> None:
    raw = _good_metadata()
    raw["atmosphere"]["mode"] = "oxidative"
    raw["atmosphere"]["reactive"] = {  # secondary gas alongside the inert purge
        "species": "O2",
        "purity": "5.0",
        "target_flow_slpm": 21.0,
        "target_mole_fraction": 0.21,
    }
    meta = cap.validate_metadata(raw)
    assert meta.atmosphere.reactive is not None
    assert meta.atmosphere.reactive.target_mole_fraction == 0.21


def test_required_channel_groups_cover_minimum_capa_rig() -> None:
    groups = {req.group for req in cap.REQUIRED_CHANNEL_GROUPS}
    assert {"heater_setpoint", "heater_pv", "purge_gas_flow", "mass"} == groups
    optional = {req.group for req in cap.OPTIONAL_CHANNEL_GROUPS}
    assert "sample_temperature" not in groups | optional


def test_preflight_check_ids_are_unique() -> None:
    ids = [c.id for c in cap.PREFLIGHT_CHECKS]
    assert len(ids) == len(set(ids))


def test_specimen_form_literal() -> None:
    raw = _good_metadata()
    raw["specimen"]["form"] = "not_a_form"
    with pytest.raises(Exception):
        cap.validate_metadata(raw)


def test_specimen_form_accepts_other() -> None:
    raw = _good_metadata()
    raw["specimen"]["form"] = "other"
    raw["specimen"]["notes"] = "thin film 200um, irregular edges"
    meta = cap.validate_metadata(raw)
    assert meta.specimen.form == "other"


def test_profile_module_protocol_attributes_present() -> None:
    """The module exposes the DomainProfile attributes expected by the
    profile-discovery path."""
    assert cap.id == cap.PROFILE_ID
    assert cap.metadata_model is cap.CapaPyrolysisMetadata
    assert cap.required_channel_groups == cap.REQUIRED_CHANNEL_GROUPS
    assert cap.preflight_checks == cap.PREFLIGHT_CHECKS


def test_gas_sampling_is_optional() -> None:
    assert cap.validate_metadata(_good_metadata()).gas_sampling is None


def test_gas_sampling_accepts_probe_and_flow() -> None:
    raw = _good_metadata()
    raw["gas_sampling"] = {
        "probe_location": "exhaust duct, centerline",
        "probe_height_mm": 450.0,
        "probe_radial_offset_mm": 0.0,
        "sample_flow_slpm": 1.0,
        "line_length_m": 3.0,
        "line_material": "PTFE",
        "conditioning": "particulate filter, chiller",
        "transport_delay_s": 12.5,
    }
    sampling = cap.validate_metadata(raw).gas_sampling
    assert sampling is not None
    assert sampling.probe_radial_offset_mm == 0.0
    assert sampling.transport_delay_s == 12.5
    assert sampling.line_temperature_c is None


@pytest.mark.parametrize(
    "gas_sampling",
    [
        {"sample_flow_slpm": 1.0},
        {"probe_location": "", "sample_flow_slpm": 1.0},
        {"probe_location": "exhaust duct"},
        {"probe_location": "exhaust duct", "sample_flow_slpm": 0.0},
        {"probe_location": "exhaust duct", "sample_flow_slpm": 1.0, "flow_sccm": 1000},
    ],
    ids=["no-location", "blank-location", "no-flow", "zero-flow", "unknown-key"],
)
def test_gas_sampling_rejects_incomplete_block(gas_sampling: dict[str, Any]) -> None:
    raw = _good_metadata()
    raw["gas_sampling"] = gas_sampling
    with pytest.raises(Exception):
        cap.validate_metadata(raw)
