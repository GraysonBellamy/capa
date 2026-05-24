"""CAPA — controlled atmosphere pyrolysis apparatus domain profile.

This is the project's namesake apparatus and the **default** domain profile.
CAPA is a controlled-atmosphere cone-calorimeter-class instrument: a
specimen sits in a holder on a load cell under a radiant heater, swept by
a purge gas to control atmosphere chemistry. The scientific parameter is
the radiant **heat flux** at the specimen surface (kW/m²); operators
achieve a target flux by setting the heater temperature, typically via a
day-of (or per-experiment) flux-vs-setpoint calibration. Most runs are a
single setpoint hold; dynamic programs (ramps) are the minority.

This profile contributes:

- **specimen fields** — id, material, mass, form (disk for ~99% of runs;
  ``other`` for rare non-disk shapes), particle size when relevant,
  specimen-holder description and optional dimensions, conditioning notes
- **method fields** — heater program (target heat flux + heater setpoint,
  optional flux-calibration reference, optional ramp rate), atmosphere
  composition + purge flow target, optional secondary-flow for
  partial-oxidation experiments, exposure / soak time
- **required channel groups**:

  * ``heater_setpoint`` / ``heater_pv`` — the controller pair
  * ``sample_temperature`` — at least one TC inside or close to the sample
  * ``mass`` — load cell reading the specimen mass
  * ``purge_gas_flow`` — the inert/sweep gas MFC
- **gas-analysis metadata** — purge-gas spec (purity grade, supplier,
  cylinder lot), sweep flow target, optional downstream analyzer (FTIR / GC
  / MS) entry-point + serial + sampling-line delay. CAPA does *not* do
  oxygen-depletion calorimetry by default, so the analyzer block is shaped
  for "qualitative product analysis" rather than "quantitative HRR".
- **preflight checks** — heater PV in safe range, purge gas flow
  established and stable, leak-test recency, balance stability when present,
  required channel mappings.

Cone-calorimeter mode (oxygen-depletion HRR) lives separately in
:mod:`capa.experiment.profiles.cone_calorimeter` and is not wired in as a
default; a future operator can opt into it via ``domain_profile.id`` in the
experiment YAML.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from capa.channels.spec import ChannelKind
from capa.experiment.profiles.base import ChannelRequirement, PreflightCheck

PROFILE_ID = "capa.profiles.capa_pyrolysis"
"""Plugin id. Default ``DomainProfileRef.id`` for new experiments."""

DEFAULT_STANDARD_REFS: tuple[str, ...] = ()
"""CAPA-style pyrolysis runs are not bound to a single standard. Lab SOPs
referencing e.g. ASTM E2550 (TGA) or in-house procedures can be added per
experiment via ``DomainProfileRef.standard_refs``."""


# ---------------------------------------------------------------------------
# Specimen / method / atmosphere sub-models.
# ---------------------------------------------------------------------------


SpecimenForm = Literal["disk", "other"]
"""Specimen physical form. ~99% of CAPA runs use a disk; ``other`` is the
escape hatch for rare non-disk geometries (irregular solid, liquid, etc.)
described in ``notes``."""


class CapaSpecimen(BaseModel):
    """CAPA-specific specimen metadata.

    Required at run-arm. Missing fields fail :func:`validate_metadata`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    material: str
    initial_mass_g: float = Field(
        gt=0,
        json_schema_extra={
            "capa_unit": "g",
            "capa_help": "Initial sample mass on the load cell, before heating begins.",
        },
    )
    form: SpecimenForm
    particle_size_um: float | None = Field(
        default=None,
        gt=0,
        json_schema_extra={
            "capa_unit": "µm",
            "capa_help": (
                "Median particle size for powder / granulate runs. Leave unset "
                "for the typical solid disk."
            ),
        },
    )

    specimen_holder: str
    """Specimen-holder description (e.g. ``"stainless steel cup"``). The
    holder geometry varies by run — depth and diameter, plus optional
    insulation, change the exposed surface area."""

    specimen_holder_diameter_mm: float | None = Field(
        default=None,
        gt=0,
        json_schema_extra={
            "capa_unit": "mm",
            "capa_help": "Outside / nominal diameter of the holder cup.",
        },
    )
    specimen_holder_depth_mm: float | None = Field(
        default=None,
        gt=0,
        json_schema_extra={
            "capa_unit": "mm",
            "capa_help": (
                "Internal cup depth. Together with diameter, captures the "
                "cavity geometry that affects exposed surface area."
            ),
        },
    )

    conditioning: str | None = None
    """Free-text description of pre-test conditioning (drying, desiccator,
    storage humidity, etc.)."""

    notes: str | None = None


class HeaterProgram(BaseModel):
    """Operator-declared heater program summary.

    CAPA's experimental parameter is the radiant **heat flux** at the
    specimen surface; operators achieve a target flux by commanding a
    heater temperature, with the flux ↔ setpoint mapping established via
    a day-of (or per-experiment) calibration. This block records what the
    operator intended; the actual command sequence lives in the
    :class:`Method`. The summary is captured here so a downstream analyzer
    can classify the run without parsing the method graph.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_heat_flux_kw_m2: float = Field(
        gt=0,
        json_schema_extra={
            "capa_unit": "kW/m²",
            "capa_help": (
                "Target radiant heat flux at the specimen surface — the "
                "scientific parameter for the run. The heater setpoint is "
                "derived from this via the flux-vs-temperature calibration."
            ),
        },
    )
    heater_setpoint_c: float = Field(
        json_schema_extra={
            "capa_unit": "°C",
            "capa_help": (
                "Heater temperature setpoint chosen to deliver the target "
                "flux. Comes from the flux-vs-temperature calibration."
            ),
        },
    )
    flux_calibration_ref: str | None = None
    """Pointer to the heat-flux ↔ heater-setpoint calibration used
    (date-stamped lookup-table id, lab-notebook entry, etc.). Free-form."""

    ramp_rate_c_per_min: float | None = Field(
        default=None,
        gt=0,
        json_schema_extra={
            "capa_unit": "°C/min",
            "capa_help": (
                "Optional ramp rate for dynamic programs. Leave unset for "
                "the common single-setpoint hold."
            ),
        },
    )


AtmosphereMode = Literal["inert", "oxidative", "reducing", "reactive_blend"]
"""Coarse classification. Drives preflight expectations — an ``inert`` run
should fail preflight if the secondary (O2) MFC is reading non-zero."""


class PurgeGas(BaseModel):
    """Purge / sweep gas spec — the inert atmosphere flow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    species: str
    """Common name. ``"N2"``, ``"Ar"``, ``"He"``, ``"air"``, ``"5% O2/N2"``."""

    purity: str
    """Grade / purity. ``"UHP 5.0"``, ``"99.999%"``, ``"zero-grade air"``."""

    supplier: str | None = None
    cylinder_lot: str | None = None
    target_flow_sccm: float = Field(
        ge=0,
        json_schema_extra={
            "capa_unit": "sccm",
            "capa_help": (
                "Operator's intended purge-flow setpoint at standard "
                "conditions. The MFC channel is the actual source of truth; "
                "set to 0 to opt out of the purge-flow preflight check."
            ),
        },
    )


class ReactiveGas(BaseModel):
    """Optional secondary gas (used for partial-oxidation or doped-purge
    experiments). Set ``None`` for pure-inert runs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    species: str
    """e.g. ``"O2"``, ``"H2"``, ``"CO"``."""

    purity: str
    target_flow_sccm: float = Field(
        ge=0,
        json_schema_extra={
            "capa_unit": "sccm",
            "capa_help": "Operator's intended secondary-gas flow setpoint.",
        },
    )
    target_mole_fraction: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        json_schema_extra={
            "capa_help": (
                "Computed downstream blend fraction, when known. Records the "
                "operator's intent — the actual blend depends on both MFCs."
            ),
        },
    )


class Atmosphere(BaseModel):
    """Atmosphere subsystem metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: AtmosphereMode
    purge: PurgeGas
    reactive: ReactiveGas | None = None
    purge_duration_s: float = Field(
        default=0.0,
        ge=0,
        json_schema_extra={
            "capa_unit": "s",
            "capa_help": (
                "How long the reactor is swept with purge gas before heating "
                "begins. Captured so downstream analysis knows the pre-run "
                "sweep duration."
            ),
        },
    )

    leak_check_at: datetime | None = None
    """Most-recent leak / pressure-decay check timestamp. Preflight
    ``capa.leak_test_recency`` reads this."""


class DownstreamAnalyzer(BaseModel):
    """Optional downstream analyzer attached to the reactor exhaust.

    CAPA pyrolysis is often paired with FTIR / GC / MS for qualitative
    product identification. The analyzer is *not* a capa-controlled device
    — its data lives outside the bundle — but the pedigree fields are
    captured here so the run record cross-references the right external
    file/notebook.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["ftir", "gc", "ms", "gc_ms", "ndir", "other"]
    serial: str | None = None
    sampling_line_delay_s: float = Field(
        default=0.0,
        ge=0,
        json_schema_extra={
            "capa_unit": "s",
            "capa_help": (
                "Transport delay from sample point to analyzer detector. "
                "Used to time-align analyzer output with capa channels."
            ),
        },
    )
    response_time_s: float | None = Field(
        default=None,
        gt=0,
        json_schema_extra={
            "capa_unit": "s",
            "capa_help": (
                "Analyzer 90% step response time — the time-constant of the "
                "instrument's measurement, not the sampling-line delay."
            ),
        },
    )
    external_file_ref: str | None = None
    """Pointer to the analyzer's data file/dataset. Free-form path or URI;
    captured into the bundle so a later analyzer can re-locate the
    correlated data."""


# ---------------------------------------------------------------------------
# Top-level metadata model.
# ---------------------------------------------------------------------------


class CapaPyrolysisMetadata(BaseModel):
    """CAPA profile metadata block.

    Mirrored verbatim into ``profiles/capa_pyrolysis.toml`` at run-arm by
    the bundle writer; the same model validates the inbound config.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    specimen: CapaSpecimen
    program: HeaterProgram
    atmosphere: Atmosphere
    analyzer: DownstreamAnalyzer | None = None
    sop_revision: str | None = None
    """Lab SOP identifier (``"CAPA-SOP-2026-03"``, etc.). Free-form."""


# ---------------------------------------------------------------------------
# Profile object — id, channel requirements, preflight checks.
# ---------------------------------------------------------------------------


REQUIRED_CHANNEL_GROUPS: tuple[ChannelRequirement, ...] = (
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
        group="sample_temperature",
        kinds=(ChannelKind.THERMOCOUPLE.value, ChannelKind.ANALOG_IN.value),
        min_count=1,
    ),
    ChannelRequirement(
        group="purge_gas_flow",
        kinds=(ChannelKind.MFC_FLOW.value, ChannelKind.ANALOG_IN.value),
        min_count=1,
    ),
    ChannelRequirement(
        group="mass",
        kinds=(ChannelKind.MASS.value,),
        min_count=1,
    ),
)
"""Channel groups required by the CAPA profile. Matched against
:attr:`ChannelSpec.metadata['capa_group']` (the operator declares which
channel plays which role when building the hardware profile)."""


OPTIONAL_CHANNEL_GROUPS: tuple[ChannelRequirement, ...] = (
    ChannelRequirement(
        group="reactive_gas_flow",
        kinds=(ChannelKind.MFC_FLOW.value, ChannelKind.ANALOG_IN.value),
        min_count=1,
    ),
)
"""Optional channel groups. Preflight does not block on missing optional
groups but warns if e.g. an ``oxidative`` atmosphere is declared without a
``reactive_gas_flow`` channel — the inconsistency check lives in the
preflight runtime registry."""


PREFLIGHT_CHECKS: tuple[PreflightCheck, ...] = (
    PreflightCheck(
        id="capa.required_channel_mappings",
        description="Every required channel group has at least min_count members.",
        blocking=True,
    ),
    PreflightCheck(
        id="capa.atmosphere_consistency",
        description=(
            "Declared atmosphere mode is consistent with declared channels: "
            "oxidative/reactive_blend modes must declare a reactive_gas_flow."
        ),
        blocking=True,
    ),
    PreflightCheck(
        id="capa.heater_pv_in_safe_range",
        description=(
            "Heater PV reading is within the rig-survival ceiling "
            "(<1000 °C by default). Catches sensor runaway / miswired "
            "channel; not a cold-start gate."
        ),
        blocking=True,
    ),
    PreflightCheck(
        id="capa.purge_flow_established",
        description="Purge gas flow has been seen >= target * 0.5 for >=3 s.",
        blocking=True,
    ),
    PreflightCheck(
        id="capa.leak_test_recency",
        description=(
            "Atmosphere.leak_check_at is within the lab-policy recency window (default 7 days)."
        ),
        blocking=False,
    ),
    PreflightCheck(
        id="capa.flux_calibration_freshness",
        description=(
            "When HeaterProgram.target_heat_flux_kw_m2 is declared, a "
            "flux_calibration_ref is set, and any on-disk tune artifact "
            "it points to is within the lab-policy recency window (default 7 days)."
        ),
        blocking=False,
    ),
    PreflightCheck(
        id="capa.balance_stability",
        description=(
            "When a mass channel is declared, it reports stable for >=5s prior to arming."
        ),
        blocking=False,
    ),
    PreflightCheck(
        id="capa.disk_projection",
        description="Projected bundle size leaves >=1.5x margin on the bundle volume.",
        blocking=True,
    ),
)


# ---------------------------------------------------------------------------
# Validation helpers.
# ---------------------------------------------------------------------------


def validate_metadata(raw: dict[str, object]) -> CapaPyrolysisMetadata:
    """Validate a raw metadata dict against :class:`CapaPyrolysisMetadata`."""
    return CapaPyrolysisMetadata.model_validate(raw)


# Profile object exposed via the DomainProfile Protocol (the plugin runtime
# discovers profiles by importing the module and reading these attributes).
id: str = PROFILE_ID
standard_refs: tuple[str, ...] = DEFAULT_STANDARD_REFS
metadata_model: type[BaseModel] = CapaPyrolysisMetadata
required_channel_groups: tuple[ChannelRequirement, ...] = REQUIRED_CHANNEL_GROUPS
preflight_checks: tuple[PreflightCheck, ...] = PREFLIGHT_CHECKS


__all__ = [
    "DEFAULT_STANDARD_REFS",
    "OPTIONAL_CHANNEL_GROUPS",
    "PREFLIGHT_CHECKS",
    "PROFILE_ID",
    "REQUIRED_CHANNEL_GROUPS",
    "Atmosphere",
    "AtmosphereMode",
    "CapaPyrolysisMetadata",
    "CapaSpecimen",
    "DownstreamAnalyzer",
    "HeaterProgram",
    "PurgeGas",
    "ReactiveGas",
    "SpecimenForm",
    "validate_metadata",
]
