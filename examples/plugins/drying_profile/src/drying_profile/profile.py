"""Worked example: a minimal drying-loss study profile.

Captures the specimen and method metadata for an isothermal drying study —
a real, small scientific scope that is not covered by the CAPA pyrolysis
profile (which assumes a heat-flux target) or by the cone calorimeter
profile (which assumes oxygen-depletion HRR).

Pair this module with the [Writing a profile](../../../../docs/extending/writing-a-profile.md)
tutorial.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from capa.channels.spec import ChannelKind
from capa.experiment.profiles.base import ChannelRequirement, PreflightCheck

PROFILE_ID = "drying.profile"


class DryingSpecimen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    material: str
    initial_mass_g: float = Field(gt=0)
    initial_moisture_pct: float = Field(ge=0, le=100)


class DryingSetup(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    target_temperature_c: float = Field(gt=0)
    soak_duration_s: float = Field(gt=0)


class DryingMetadata(BaseModel):
    """Top-level metadata mirrored into `profiles/drying.profile.toml`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    specimen: DryingSpecimen
    setup: DryingSetup
    notes: str | None = None


REQUIRED_CHANNEL_GROUPS: tuple[ChannelRequirement, ...] = (
    ChannelRequirement(
        group="mass",
        kinds=(ChannelKind.MASS.value,),
        min_count=1,
    ),
    ChannelRequirement(
        group="sample_temperature",
        kinds=(ChannelKind.THERMOCOUPLE.value,),
        min_count=1,
    ),
)


PREFLIGHT_CHECKS: tuple[PreflightCheck, ...] = (
    PreflightCheck(
        id="drying.required_channel_mappings",
        description="Mass and sample temperature channels are bound.",
        blocking=True,
    ),
)


# Module-level attributes the engine reads to satisfy the
# `DomainProfile` Protocol contract.
id: str = PROFILE_ID
standard_refs: tuple[str, ...] = ()
metadata_model: type[BaseModel] = DryingMetadata
required_channel_groups: tuple[ChannelRequirement, ...] = REQUIRED_CHANNEL_GROUPS
preflight_checks: tuple[PreflightCheck, ...] = PREFLIGHT_CHECKS
