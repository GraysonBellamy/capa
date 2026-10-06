"""Specimen → sample mirror helpers in :mod:`capa.config.capa_profile`."""

from __future__ import annotations

from capa.config.capa_profile import (
    CAPA_PROFILE_ID,
    clear_per_run_fields,
    profile_model_fields,
    sample_from_specimen,
    sample_specimen_mismatches,
)
from capa.experiment.profiles.capa_pyrolysis import PROFILE_ID


def test_profile_id_matches_profile_module() -> None:
    """The config-layer copy of the id can't drift from the profile's own."""
    assert CAPA_PROFILE_ID == PROFILE_ID


def test_sample_from_specimen_maps_identity_fields() -> None:
    specimen = {
        "id": "P-1",
        "material": "PMMA",
        "initial_mass_g": 5.0,
        "thickness_mm": 6.0,
        "notes": "cast sheet",
        "form": "disk",
        "specimen_holder": "cup",
    }
    assert sample_from_specimen(specimen) == {
        "id": "P-1",
        "material": "PMMA",
        "mass_g": 5.0,
        "thickness_mm": 6.0,
        "notes": "cast sheet",
    }


def test_sample_from_specimen_keeps_extra_and_clears_unset() -> None:
    sample = {"id": "old", "material": "PS", "notes": "stale", "extra": {"lot": 3}}
    out = sample_from_specimen({"id": "P-2", "material": "PMMA"}, sample)
    assert out == {"id": "P-2", "material": "PMMA", "extra": {"lot": 3}}
    # The input is not mutated.
    assert sample["id"] == "old"


def test_sample_from_specimen_drops_values_sample_info_would_reject() -> None:
    """Blank strings and non-positive sizes mirror as unset; the profile's
    own validation reports them, and the sample block stays loadable."""
    out = sample_from_specimen({"id": "", "material": " ", "initial_mass_g": 0.0})
    assert out == {"id": ""}


def test_mismatches_treat_unset_as_equal() -> None:
    specimen = {"id": "P-1", "material": "PMMA", "initial_mass_g": 5.0}
    assert (
        sample_specimen_mismatches({"id": "P-1", "material": "PMMA", "mass_g": 5.0}, specimen) == []
    )
    assert (
        sample_specimen_mismatches(
            {"id": "P-1", "material": "PMMA", "mass_g": 5.0, "notes": None}, specimen
        )
        == []
    )


def test_mismatches_report_each_field() -> None:
    specimen = {"id": "P-1", "material": "PMMA", "initial_mass_g": 5.0}
    sample = {"id": "P-2", "material": "PMMA", "notes": "extra"}
    assert sample_specimen_mismatches(sample, specimen) == [
        ("id", "id", "P-1", "P-2"),
        ("initial_mass_g", "mass_g", 5.0, None),
        ("notes", "notes", None, "extra"),
    ]


def test_profile_model_fields_drops_preflight_knobs() -> None:
    metadata = {"specimen": {}, "_safe_arm": {"max_heater_pv_c": 400.0}}
    assert profile_model_fields(metadata) == {"specimen": {}}


def test_clear_per_run_fields_keeps_rig_level_specimen_fields() -> None:
    payload = {
        "operator": {"id": "abr", "display_name": "A. Researcher"},
        "sample": {"id": "P-1", "material": "PMMA", "mass_g": 5.0, "extra": {"lot": "B7"}},
        "domain_profile": {
            "id": CAPA_PROFILE_ID,
            "metadata": {
                "specimen": {
                    "id": "P-1",
                    "material": "PMMA",
                    "initial_mass_g": 5.0,
                    "thickness_mm": 6.0,
                    "diameter_mm": 70.0,
                    "form": "disk",
                    "specimen_holder": "stainless steel cup",
                    "specimen_holder_diameter_mm": 75.0,
                    "specimen_holder_mass_g": 40.0,
                    "insulation_mass_g": 3.0,
                    "conditioning": "dried 24 h",
                    "notes": "box B",
                },
                "program": {"target_heat_flux_kw_m2": 50.0},
            },
        },
        "tags": ["capa"],
    }
    out = clear_per_run_fields(payload)
    assert out["operator"] == {}
    metadata = out["domain_profile"]["metadata"]
    assert metadata["specimen"] == {
        "form": "disk",
        "specimen_holder": "stainless steel cup",
        "specimen_holder_diameter_mm": 75.0,
    }
    assert metadata["program"] == {"target_heat_flux_kw_m2": 50.0}
    assert out["sample"] == {"id": "", "extra": {"lot": "B7"}}
    assert out["tags"] == ["capa"]
    # The input is left as it was.
    assert payload["operator"]["id"] == "abr"
    assert payload["domain_profile"]["metadata"]["specimen"]["id"] == "P-1"


def test_clear_per_run_fields_without_profile_clears_only_operator() -> None:
    payload = {"operator": {"id": "abr", "display_name": "A. Researcher"}, "sample": {"id": "S-1"}}
    assert clear_per_run_fields(payload) == {"operator": {}, "sample": {"id": "S-1"}}
