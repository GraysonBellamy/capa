"""Specimen → sample mirror helpers in :mod:`capa.config.capa_profile`."""

from __future__ import annotations

from capa.config.capa_profile import (
    CAPA_PROFILE_ID,
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
