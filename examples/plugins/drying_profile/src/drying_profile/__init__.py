"""Example domain-profile plugin — see `profile.py`."""

from .profile import (
    PREFLIGHT_CHECKS,
    PROFILE_ID,
    REQUIRED_CHANNEL_GROUPS,
    DryingMetadata,
    DryingSetup,
    DryingSpecimen,
    id,
    metadata_model,
    preflight_checks,
    required_channel_groups,
    standard_refs,
)

__all__ = [
    "PREFLIGHT_CHECKS",
    "PROFILE_ID",
    "REQUIRED_CHANNEL_GROUPS",
    "DryingMetadata",
    "DryingSetup",
    "DryingSpecimen",
    "id",
    "metadata_model",
    "preflight_checks",
    "required_channel_groups",
    "standard_refs",
]
