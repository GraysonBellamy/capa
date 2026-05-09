"""Tests for the CAPA (controlled atmosphere pyrolysis) domain profile."""

from __future__ import annotations

import pytest

from capa.experiment.profiles import capa_pyrolysis as cap


def _good_metadata() -> dict:
    return {
        "specimen": {
            "id": "P-001",
            "material": "PMMA",
            "initial_mass_g": 5.0,
            "form": "pellet",
            "crucible": "alumina 70 uL",
        },
        "program": {
            "initial_temperature_c": 30.0,
            "final_temperature_c": 600.0,
            "ramp_rate_c_per_min": 10.0,
        },
        "atmosphere": {
            "mode": "inert",
            "carrier": {
                "species": "N2",
                "purity": "UHP 5.0",
                "target_flow_sccm": 100.0,
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
    raw["atmosphere"]["reactive"] = {
        "species": "O2",
        "purity": "5.0",
        "target_flow_sccm": 21.0,
        "target_mole_fraction": 0.21,
    }
    meta = cap.validate_metadata(raw)
    assert meta.atmosphere.reactive is not None
    assert meta.atmosphere.reactive.target_mole_fraction == 0.21


def test_required_channel_groups_cover_minimum_capa_rig() -> None:
    groups = {req.group for req in cap.REQUIRED_CHANNEL_GROUPS}
    assert {"heater_setpoint", "heater_pv", "sample_temperature", "carrier_gas_flow"} <= groups


def test_preflight_check_ids_are_unique() -> None:
    ids = [c.id for c in cap.PREFLIGHT_CHECKS]
    assert len(ids) == len(set(ids))


def test_specimen_form_literal() -> None:
    raw = _good_metadata()
    raw["specimen"]["form"] = "not_a_form"
    with pytest.raises(Exception):
        cap.validate_metadata(raw)


def test_profile_module_protocol_attributes_present() -> None:
    """The module exposes the DomainProfile attributes expected by the
    profile-discovery path."""
    assert cap.id == cap.PROFILE_ID  # type: ignore[attr-defined]
    assert cap.metadata_model is cap.CapaPyrolysisMetadata  # type: ignore[attr-defined]
    assert cap.required_channel_groups == cap.REQUIRED_CHANNEL_GROUPS  # type: ignore[attr-defined]
    assert cap.preflight_checks == cap.PREFLIGHT_CHECKS  # type: ignore[attr-defined]
