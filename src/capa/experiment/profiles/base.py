""":class:`DomainProfile` Protocol.

Plan §5.4.1: domain profiles are optional schema/preflight bundles layered on
top of the generic engine. Each profile declares the metadata it requires,
the channel groups that must be present, and a list of preflight checks to
run before arming a run. P0a ships the Protocol + the cone-calorimeter
implementation; the executor that *runs* the preflight checks lands in P3
alongside ``Procedure.preflight``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class ChannelRequirement(BaseModel):
    """Required channel group inside a domain profile.

    A run is preflight-rejected when the active :class:`HardwareProfile` does
    not declare *at least* ``min_count`` channels matching ``kinds`` and
    tagged with ``group``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    group: str
    """Group name. Matches :attr:`ChannelSpec.plot_group` or
    :attr:`ChannelSpec.metadata` key for grouping."""
    kinds: tuple[str, ...]
    """Acceptable :class:`ChannelKind` enum values (as strings)."""
    min_count: int = 1


class PreflightCheck(BaseModel):
    """Declarative preflight check.

    The check ``id`` is resolved by the profile's runtime (P3) to a concrete
    callable. P0a stores the schema only; the registry of checks lands with
    the executor.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    description: str = ""
    blocking: bool = True
    """If ``True``, the run cannot be armed until the check passes."""


@runtime_checkable
class DomainProfile(Protocol):
    """A domain profile contributes required metadata, channel groups, and
    preflight checks. Plan §5.4.1.
    """

    id: str
    """e.g. ``"capa.profiles.cone_calorimeter"``."""

    standard_refs: tuple[str, ...]
    """Standards this profile aligns with (``"ASTM E1354-25"``, …)."""

    metadata_model: type[BaseModel]
    """Pydantic model that validates the profile-specific metadata block of
    :attr:`DomainProfileRef.metadata`."""

    required_channel_groups: tuple[ChannelRequirement, ...]

    preflight_checks: tuple[PreflightCheck, ...]


__all__ = ["ChannelRequirement", "DomainProfile", "PreflightCheck"]
