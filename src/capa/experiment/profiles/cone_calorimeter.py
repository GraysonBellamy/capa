"""Cone-calorimeter domain profile (ASTM E1354 / ISO 5660-style measurements).

This is the first production profile. It does not make capa a
standards-certification tool; it ensures the run bundle captures the metadata
a researcher or later analyzer needs to interpret a cone run.

The profile contributes:

- specimen fields: id, material, thickness, exposed area, mass, conditioning,
  holder/orientation, backing/retainer
- method fields: target external heat flux, spark/ignition mode, exposure
  timing, test termination criteria
- required channel groups: mass, exhaust flow, oxygen analyzer, smoke optical
  path / laser attenuation when present, heat-flux gauge, relevant
  thermocouples, commanded heater setpoint
- gas-analysis metadata: calibration gases, analyzer serials, zero/span events,
  sampling-line delay, analyzer response time / time constant, baseline-drift
  notes
- preflight checks: active calibration age/traceability, analyzer zero/span
  recency, disk projection, balance stability, heat-flux gauge presence,
  required channel mappings

The profile snapshot lands in ``profiles/cone_calorimeter.toml`` and is
referenced from ``manifest.json.domain_profile``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from capa.channels.spec import ChannelKind
from capa.experiment.profiles.base import ChannelRequirement, PreflightCheck

# ---------------------------------------------------------------------------
# Specimen / method / gas-analysis sub-models.
# ---------------------------------------------------------------------------


SpecimenOrientation = Literal["horizontal", "vertical"]


class Specimen(BaseModel):
    """Cone-specific specimen metadata.

    Required at run-arm. Missing fields fail :func:`validate_metadata`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    material: str
    thickness_mm: float = Field(gt=0)
    exposed_area_cm2: float = Field(gt=0)
    initial_mass_g: float = Field(gt=0)
    orientation: SpecimenOrientation
    conditioning: str
    """Free-text description of pre-test conditioning (e.g. "23°C / 50% RH for
    48 h per ASTM E1354 §10.3")."""
    holder: str
    """Holder/retainer used (e.g. ``"standard frame + grid"``)."""
    backing: str | None = None
    notes: str | None = None


SparkMode = Literal["spark_ignition", "pilot_flame", "no_ignition"]


class MethodSetup(BaseModel):
    """Cone-specific method setup."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_external_flux_kW_m2: float = Field(gt=0)
    spark_mode: SparkMode
    pre_exposure_duration_s: float = Field(default=0.0, ge=0)
    """How long the specimen sits under the cone before the shutter opens
    (or ignition begins). Some procedures use a shorter "establish flux"
    period before opening the shutter."""
    termination_criteria: str
    """Free-text description (``"sustained flameout >30s"``,
    ``"5 min with HRR<5 kW/m^2"``)."""


class AnalyzerCalibration(BaseModel):
    """Gas-analyzer calibration record.

    calibration gases, analyzer serials, zero/span events.
    Recorded so an analyzer five years later can re-derive heat-release
    inputs without trusting human notes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    analyzer: Literal["o2", "co", "co2"]
    serial: str
    zero_at: datetime
    span_at: datetime
    span_gas: str
    """e.g. ``"21.0% O2 in N2 (BOC, lot ABC123)"``."""
    notes: str | None = None


class GasAnalysis(BaseModel):
    """Gas-analyzer subsystem metadata.

    analyzer delay/response is *not* clock sync; it is a
    measurement-model field that belongs in the bundle even though capa
    doesn't perform full analysis.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sampling_line_delay_s: float = Field(ge=0)
    o2_response_time_s: float = Field(gt=0)
    co_response_time_s: float | None = Field(default=None, gt=0)
    co2_response_time_s: float | None = Field(default=None, gt=0)
    analyzer_calibrations: tuple[AnalyzerCalibration, ...] = Field(min_length=1)
    """At minimum one (the O2 analyzer); richer setups include CO/CO2."""
    baseline_drift_notes: str | None = None


class SmokeOptics(BaseModel):
    """Smoke-optics subsystem metadata. Optional — set when a laser
    attenuation path is present on the rig."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    optical_path_length_m: float = Field(gt=0)
    response_time_s: float = Field(gt=0)
    notes: str | None = None


# ---------------------------------------------------------------------------
# Top-level metadata model — what goes inside DomainProfileRef.metadata.
# ---------------------------------------------------------------------------


class ConeCalorimeterMetadata(BaseModel):
    """Cone-profile metadata block.

    Mirrored verbatim into ``profiles/cone_calorimeter.toml`` at run-arm
    The bundle writer mirrors it verbatim; the same model validates the inbound config.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    specimen: Specimen
    setup: MethodSetup
    gas_analysis: GasAnalysis
    smoke_optics: SmokeOptics | None = None
    standard_revision: str = "ASTM E1354-25"


# ---------------------------------------------------------------------------
# Profile object — id, channel requirements, preflight checks.
# ---------------------------------------------------------------------------


PROFILE_ID = "capa.profiles.cone_calorimeter"
DEFAULT_STANDARD_REFS: tuple[str, ...] = ("ASTM E1354-25", "ISO 5660-1:2015")


REQUIRED_CHANNEL_GROUPS: tuple[ChannelRequirement, ...] = (
    ChannelRequirement(
        group="mass",
        kinds=(ChannelKind.MASS.value,),
        min_count=1,
    ),
    ChannelRequirement(
        group="heater_setpoint",
        kinds=(ChannelKind.SETPOINT.value,),
        min_count=1,
    ),
    ChannelRequirement(
        group="heater_pv",
        kinds=(ChannelKind.PROCESS_VAR.value,),
        min_count=1,
    ),
    ChannelRequirement(
        group="exhaust_flow",
        kinds=(ChannelKind.MFC_FLOW.value, ChannelKind.ANALOG_IN.value),
        min_count=1,
    ),
    ChannelRequirement(
        group="oxygen",
        kinds=(ChannelKind.ANALOG_IN.value,),
        min_count=1,
    ),
    ChannelRequirement(
        group="thermocouples",
        kinds=(ChannelKind.THERMOCOUPLE.value,),
        min_count=1,
    ),
    ChannelRequirement(
        group="heat_flux_gauge",
        kinds=(ChannelKind.ANALOG_IN.value,),
        min_count=1,
    ),
)
"""Channel groups required by the cone profile. Matched against
:attr:`ChannelSpec.metadata['cone_group']` (the cone-profile loader sets that
when the operator declares which channel plays which role)."""


PREFLIGHT_CHECKS: tuple[PreflightCheck, ...] = (
    PreflightCheck(
        id="cone.calibration_age",
        description="Active heat-flux gauge calibration is within recency policy.",
        blocking=True,
    ),
    PreflightCheck(
        id="cone.analyzer_zero_span_recent",
        description="O2/CO/CO2 zero/span is within the policy-defined recency window.",
        blocking=True,
    ),
    PreflightCheck(
        id="cone.disk_projection",
        description="Projected bundle size leaves >=1.5x margin on the bundle volume.",
        blocking=True,
    ),
    PreflightCheck(
        id="cone.balance_stability",
        description="Mass channel reports stable for >=5s prior to arming.",
        blocking=True,
    ),
    PreflightCheck(
        id="cone.heat_flux_gauge_present",
        description="A channel mapped to the heat-flux gauge group exists and is healthy.",
        blocking=True,
    ),
    PreflightCheck(
        id="cone.required_channel_mappings",
        description="Every required channel group has at least min_count members.",
        blocking=True,
    ),
)


# ---------------------------------------------------------------------------
# Validation helpers — used by ExperimentConfig._validate_method-equivalent
# logic; exposed here so tests can exercise them directly.
# ---------------------------------------------------------------------------


def validate_metadata(raw: dict[str, object]) -> ConeCalorimeterMetadata:
    """Validate a raw metadata dict against :class:`ConeCalorimeterMetadata`."""
    return ConeCalorimeterMetadata.model_validate(raw)


__all__ = [
    "DEFAULT_STANDARD_REFS",
    "PREFLIGHT_CHECKS",
    "PROFILE_ID",
    "REQUIRED_CHANNEL_GROUPS",
    "AnalyzerCalibration",
    "ConeCalorimeterMetadata",
    "GasAnalysis",
    "MethodSetup",
    "SmokeOptics",
    "SparkMode",
    "Specimen",
    "SpecimenOrientation",
    "validate_metadata",
]
