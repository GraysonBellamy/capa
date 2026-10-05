"""Shared CAPA profile metadata.

Single source of truth for:

* the required-channel groups the CAPA pyrolysis profile expects (used
  by Layer 3 validation and by the Setup editor's required-mapping
  panel);
* a helper that walks a config's channels and reports which CAPA group
  is currently mapped to which channel (used by the Overview pane and
  by the CAPA Profile section's status chips);
* the specimen → ``sample`` mirror. The profile's ``specimen`` block is
  where the operator describes the specimen; the experiment's top-level
  ``sample`` block (which names the run id and the catalog row) is
  derived from it. Layer 3 flags any drift between the two.

Pure data + pure functions — no Qt, no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

CAPA_PROFILE_ID = "capa.profiles.capa_pyrolysis"
"""``domain_profile.id`` of the CAPA pyrolysis profile. Mirrors
:data:`capa.experiment.profiles.capa_pyrolysis.PROFILE_ID` without
importing the profile module (and its channel-spec dependencies)."""

# Required CAPA pyrolysis groups → acceptable :class:`ChannelKind` values
# (StrEnum lower-case form). Layer 3 errors when a required group isn't
# mapped; the section's chip row flips red until it is.
CAPA_REQUIRED_GROUPS: dict[str, tuple[str, ...]] = {
    "heater_setpoint": ("setpoint",),
    "heater_pv": ("process_var",),
    "purge_gas_flow": ("mfc_flow",),
    "mass": ("mass",),
}

# Optional groups — surfaced for completeness but never block Apply.
CAPA_OPTIONAL_GROUPS: dict[str, tuple[str, ...]] = {
    "reactive_gas_flow": ("mfc_flow",),
}

SPECIMEN_SAMPLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("id", "id"),
    ("material", "material"),
    ("initial_mass_g", "mass_g"),
    ("thickness_mm", "thickness_mm"),
    ("notes", "notes"),
)
"""``(specimen_key, sample_key)`` pairs mirrored from
``domain_profile.metadata.specimen`` into the experiment's ``sample``
block. ``sample.extra`` is not mirrored; it stays as authored."""

_POSITIVE_SAMPLE_KEYS: frozenset[str] = frozenset({"mass_g", "thickness_mm"})
"""``sample`` keys that :class:`~capa.experiment.config.SampleInfo`
constrains to ``> 0``. A non-positive specimen value mirrors as unset
so an in-progress edit can't break schema validation of the whole
config — the profile's own validation reports the bad value."""


def current_capa_mappings(channels: Iterable[object]) -> dict[str, list[str]]:
    """Walk raw channel dicts; return ``{group_name: [channel_name, ...]}``.

    A group mapped to several channels appears with every matched
    channel; single-channel groups appear with a length-1 list. Groups with no mapping aren't included in the return — callers
    that want a complete picture should iterate :data:`CAPA_REQUIRED_GROUPS`
    and look up by key.

    Accepts raw dicts (the payload shape the Setup editor edits) and
    Pydantic :class:`~capa.channels.spec.ChannelSpec` instances
    (returned by ``model_dump``); the function only reads ``name`` and
    ``metadata.capa_group``.
    """
    out: dict[str, list[str]] = {}
    for entry in channels:
        if not isinstance(entry, Mapping):
            continue
        metadata = entry.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            continue
        group = metadata.get("capa_group")
        if not isinstance(group, str) or not group:
            continue
        name = entry.get("name", "")
        if not isinstance(name, str):
            continue
        out.setdefault(group, []).append(name)
    return out


# ---------------------------------------------------------------------------
# Specimen → sample mirror.
# ---------------------------------------------------------------------------


def profile_model_fields(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``metadata`` without its underscore-prefixed preflight knobs.

    Keys such as ``_safe_arm`` and ``_flux_calibration_window_days`` tune the
    profile's preflight checks (:mod:`capa.experiment.profiles.runtime`).
    They share the ``domain_profile.metadata`` block but are not fields
    of the profile's metadata model, so model validation skips them.
    """
    return {k: v for k, v in metadata.items() if not str(k).startswith("_")}


def is_capa_profile(experiment_payload: Mapping[str, Any]) -> bool:
    """``True`` when the experiment payload declares the CAPA pyrolysis profile."""
    profile = experiment_payload.get("domain_profile")
    return isinstance(profile, Mapping) and profile.get("id") == CAPA_PROFILE_ID


def profile_specimen(experiment_payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return ``domain_profile.metadata.specimen`` when it is a mapping."""
    profile = experiment_payload.get("domain_profile")
    if not isinstance(profile, Mapping):
        return None
    metadata = profile.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    specimen = metadata.get("specimen")
    return specimen if isinstance(specimen, Mapping) else None


def _mirrored(sample_key: str, value: Any) -> Any:
    """Normalise one specimen value into its ``sample`` form; ``None`` = unset."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if sample_key in _POSITIVE_SAMPLE_KEYS and (
        isinstance(value, bool) or not isinstance(value, int | float) or value <= 0
    ):
        return None
    return value


def sample_from_specimen(
    specimen: Mapping[str, Any],
    sample: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return ``sample`` with its mirrored fields replaced from ``specimen``.

    Keys outside :data:`SPECIMEN_SAMPLE_FIELDS` (``extra``) are kept. An
    unset specimen value removes the matching ``sample`` key, except
    ``id``, which :class:`~capa.experiment.config.SampleInfo` requires
    and is always written (empty when the specimen has none yet).
    """
    out = {k: v for k, v in (sample or {}).items()}
    for specimen_key, sample_key in SPECIMEN_SAMPLE_FIELDS:
        value = _mirrored(sample_key, specimen.get(specimen_key))
        if value is None:
            out.pop(sample_key, None)
        else:
            out[sample_key] = value
    if "id" not in out:
        out["id"] = ""
    return out


def sample_specimen_mismatches(
    sample: Mapping[str, Any],
    specimen: Mapping[str, Any],
) -> list[tuple[str, str, Any, Any]]:
    """List mirrored fields whose ``sample`` value differs from the specimen.

    Returns ``(specimen_key, sample_key, specimen_value, sample_value)``
    tuples. Unset on both sides (missing, ``None`` or blank) compares
    equal, so a specimen without ``notes`` matches a sample without
    ``notes``.
    """
    out: list[tuple[str, str, Any, Any]] = []
    for specimen_key, sample_key in SPECIMEN_SAMPLE_FIELDS:
        expected = _mirrored(sample_key, specimen.get(specimen_key))
        actual = _mirrored(sample_key, sample.get(sample_key))
        if expected != actual:
            out.append((specimen_key, sample_key, expected, actual))
    return out


__all__ = [
    "CAPA_OPTIONAL_GROUPS",
    "CAPA_PROFILE_ID",
    "CAPA_REQUIRED_GROUPS",
    "SPECIMEN_SAMPLE_FIELDS",
    "current_capa_mappings",
    "is_capa_profile",
    "profile_model_fields",
    "profile_specimen",
    "sample_from_specimen",
    "sample_specimen_mismatches",
]
