from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from capa.experiment.profiles.cone_calorimeter import (
    DEFAULT_STANDARD_REFS,
    PREFLIGHT_CHECKS,
    PROFILE_ID,
    REQUIRED_CHANNEL_GROUPS,
    ConeCalorimeterMetadata,
    validate_metadata,
)

NOW = datetime.now(UTC)


def _good_metadata() -> dict:
    return {
        "specimen": {
            "id": "S073",
            "material": "paint-A",
            "thickness_mm": 5.0,
            "exposed_area_cm2": 100.0,
            "initial_mass_g": 25.0,
            "orientation": "horizontal",
            "conditioning": "23C/50%RH 48h",
            "holder": "standard frame + grid",
        },
        "setup": {
            "target_external_flux_kW_m2": 50.0,
            "spark_mode": "spark_ignition",
            "termination_criteria": "sustained flameout >30s",
        },
        "gas_analysis": {
            "sampling_line_delay_s": 8.0,
            "o2_response_time_s": 12.0,
            "analyzer_calibrations": [
                {
                    "analyzer": "o2",
                    "serial": "OXY-1234",
                    "zero_at": NOW.isoformat(),
                    "span_at": NOW.isoformat(),
                    "span_gas": "21.0% O2 in N2",
                }
            ],
        },
    }


class TestConeProfile:
    def test_constants(self) -> None:
        assert PROFILE_ID == "capa.profiles.cone_calorimeter"
        assert "ASTM E1354-25" in DEFAULT_STANDARD_REFS
        assert "ISO 5660-1:2015" in DEFAULT_STANDARD_REFS

    def test_required_channel_groups(self) -> None:
        names = {g.group for g in REQUIRED_CHANNEL_GROUPS}
        assert {
            "mass",
            "heater_setpoint",
            "heater_pv",
            "exhaust_flow",
            "oxygen",
            "thermocouples",
            "heat_flux_gauge",
        } <= names

    def test_preflight_check_ids(self) -> None:
        ids = {c.id for c in PREFLIGHT_CHECKS}
        assert {
            "cone.calibration_age",
            "cone.analyzer_zero_span_recent",
            "cone.disk_projection",
            "cone.balance_stability",
            "cone.heat_flux_gauge_present",
            "cone.required_channel_mappings",
        } <= ids

    def test_valid_metadata(self) -> None:
        md = validate_metadata(_good_metadata())
        assert md.specimen.id == "S073"
        assert md.setup.target_external_flux_kW_m2 == 50.0
        assert md.gas_analysis.o2_response_time_s == 12.0

    def test_missing_analyzer_delay_rejected(self) -> None:
        bad = _good_metadata()
        del bad["gas_analysis"]["sampling_line_delay_s"]
        with pytest.raises(ValidationError):
            validate_metadata(bad)

    def test_missing_analyzer_calibrations_rejected(self) -> None:
        bad = _good_metadata()
        bad["gas_analysis"]["analyzer_calibrations"] = []
        with pytest.raises(ValidationError):
            validate_metadata(bad)

    def test_missing_specimen_field_rejected(self) -> None:
        bad = _good_metadata()
        del bad["specimen"]["exposed_area_cm2"]
        with pytest.raises(ValidationError):
            validate_metadata(bad)

    def test_negative_thickness_rejected(self) -> None:
        bad = _good_metadata()
        bad["specimen"]["thickness_mm"] = -1.0
        with pytest.raises(ValidationError):
            validate_metadata(bad)

    def test_round_trip(self) -> None:
        md = validate_metadata(_good_metadata())
        data = md.model_dump(mode="json")
        md2 = ConeCalorimeterMetadata.model_validate(data)
        assert md == md2

    def test_smoke_optics_optional(self) -> None:
        md = validate_metadata(_good_metadata())
        assert md.smoke_optics is None
