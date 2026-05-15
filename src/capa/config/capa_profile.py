"""Shared CAPA profile metadata.

Single source of truth for:

* the required-channel groups the CAPA pyrolysis profile expects (used
  by Layer 3 validation and by the Setup editor's required-mapping
  panel);
* a helper that walks a config's channels and reports which CAPA group
  is currently mapped to which channel (used by the Overview pane and
  by the CAPA Profile section's status chips).

Pure data + one pure function — no Qt, no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

# Required CAPA pyrolysis groups → acceptable :class:`ChannelKind` values
# (StrEnum lower-case form). Layer 3 errors when a required group isn't
# mapped; the section's chip row flips red until it is.
CAPA_REQUIRED_GROUPS: dict[str, tuple[str, ...]] = {
    "heater_setpoint": ("setpoint",),
    "heater_pv": ("process_var",),
    "sample_temperature": ("tc", "thermocouple", "process_var"),
    "purge_gas_flow": ("mfc_flow",),
    "mass": ("mass",),
}

# Optional groups — surfaced for completeness but never block Apply.
CAPA_OPTIONAL_GROUPS: dict[str, tuple[str, ...]] = {
    "reactive_gas_flow": ("mfc_flow",),
}


def current_capa_mappings(channels: Iterable[object]) -> dict[str, list[str]]:
    """Walk raw channel dicts; return ``{group_name: [channel_name, ...]}``.

    Multi-channel groups (a sample_temperature TC array) appear with
    every matched channel; single-channel groups appear with a length-1
    list. Groups with no mapping aren't included in the return — callers
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


__all__ = [
    "CAPA_OPTIONAL_GROUPS",
    "CAPA_REQUIRED_GROUPS",
    "current_capa_mappings",
]
