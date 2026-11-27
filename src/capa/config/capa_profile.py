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
* the per-run fields (operator, specimen measurements) that every opened
  config starts without.
* the purge gas → purge MFC mirror. The profile's
  ``atmosphere.purge.species`` is where the operator names the purge gas;
  the purge MFC's ``device_settings`` gas is derived from it, so the gas
  is entered once. Layer 3 flags any drift between the two.

Pure data + pure functions — no Qt, no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import ValidationError

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
    "ambient_temperature": ("temperature", "tc", "analog_in"),
    "ambient_humidity": ("humidity", "analog_in"),
    "ambient_pressure": ("pressure", "analog_in"),
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

PER_RUN_SPECIMEN_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "material",
        "initial_mass_g",
        "mounted_mass_g",
        "thickness_mm",
        "diameter_mm",
        "specimen_holder_mass_g",
        "insulation_mass_g",
        "retainer_mass_g",
        "assembly_mass_g",
        "notes",
    }
)
"""Specimen fields entered fresh for every run: the specimen under test
(and as mounted), plus the holder, insulation and retainer masses weighed
with it and the whole loaded assembly. The rest of the specimen block (``form``, ``specimen_holder`` and
its dimensions, which describe the rig, and ``conditioning``, which a
series of specimens shares) carries over. See :func:`clear_per_run_fields`."""

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


def clear_per_run_fields(experiment_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``experiment_payload`` with its per-run values removed.

    The operator is emptied and, under the CAPA profile, the specimen
    loses :data:`PER_RUN_SPECIMEN_KEYS`; ``sample`` is re-mirrored from
    the cleared specimen. Opening a config or starting from a template
    goes through here, so a run never inherits the last run's operator
    or measurements.
    """
    exp = dict(experiment_payload)
    exp["operator"] = {}
    specimen = profile_specimen(exp)
    if specimen is None:
        return exp
    cleared = {k: v for k, v in specimen.items() if k not in PER_RUN_SPECIMEN_KEYS}
    profile = dict(exp["domain_profile"])
    profile["metadata"] = {**profile["metadata"], "specimen": cleared}
    exp["domain_profile"] = profile
    sample = exp.get("sample")
    exp["sample"] = sample_from_specimen(cleared, sample if isinstance(sample, Mapping) else None)
    return exp


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


# ---------------------------------------------------------------------------
# Purge gas → purge MFC mirror.
# ---------------------------------------------------------------------------


def _purge_species(experiment_payload: Mapping[str, Any]) -> Any:
    """``domain_profile.metadata.atmosphere.purge.species``, or ``None``."""
    node: Any = experiment_payload.get("domain_profile")
    for key in ("metadata", "atmosphere", "purge", "species"):
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def _purge_flow_device(hardware_payload: Mapping[str, Any]) -> str | None:
    """The device behind the channel mapped to ``purge_gas_flow``."""
    channels = hardware_payload.get("channels")
    if not isinstance(channels, Iterable) or isinstance(channels, str | bytes):
        return None
    for channel in channels:
        if not isinstance(channel, Mapping):
            continue
        metadata = channel.get("metadata")
        if not isinstance(metadata, Mapping) or metadata.get("capa_group") != "purge_gas_flow":
            continue
        source = channel.get("source")
        device = source.get("device") if isinstance(source, Mapping) else None
        return device if isinstance(device, str) and device else None
    return None


def _gas_setting_model(hardware_payload: Mapping[str, Any], device: str) -> Any:
    """The settings model of ``device``'s adapter when it has a ``gas``
    setting (an Alicat MFC), else ``None``."""
    from capa.devices.registry import require_descriptor  # noqa: PLC0415

    devices = hardware_payload.get("devices")
    if not isinstance(devices, Iterable) or isinstance(devices, str | bytes):
        return None
    adapter_id = next(
        (
            row.get("adapter")
            for row in devices
            if isinstance(row, Mapping) and row.get("name") == device
        ),
        None,
    )
    if not isinstance(adapter_id, str):
        return None
    try:
        settings = require_descriptor(adapter_id).settings
    except KeyError:
        return None
    if settings is None or "gas" not in settings.model.model_fields:
        return None
    return settings.model


def mirrored_purge_gas(
    experiment_payload: Mapping[str, Any],
    hardware_payload: Mapping[str, Any],
) -> tuple[str, str] | None:
    """``(device, gas)`` when the purge MFC's gas follows the CAPA profile.

    That is when the experiment declares the CAPA profile, a channel is
    mapped to ``purge_gas_flow``, its device has a ``gas`` setting, and
    the profile's ``atmosphere.purge.species`` is a gas that setting
    knows. ``gas`` is the species as the setting names it (``"nitrogen"``
    → ``"N2"``). ``None`` otherwise — notably for a species the device
    doesn't know as one gas (a mixture such as ``"5% O2/N2"``), whose MFC
    gas is then set in Device settings.
    """
    if not is_capa_profile(experiment_payload):
        return None
    species = _purge_species(experiment_payload)
    if not isinstance(species, str) or not species.strip():
        return None
    device = _purge_flow_device(hardware_payload)
    if device is None:
        return None
    model = _gas_setting_model(hardware_payload, device)
    if model is None:
        return None
    try:
        gas = model.model_validate({"gas": species.strip()}).gas
    except ValidationError:
        return None
    return (device, gas) if isinstance(gas, str) else None


def device_settings_with_purge_gas(
    experiment_payload: Mapping[str, Any],
    hardware_payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    """The experiment's ``device_settings`` with the purge MFC's gas set
    from the CAPA profile (:func:`mirrored_purge_gas`).

    ``None`` when the gas doesn't follow the profile, so the caller leaves
    ``device_settings`` as it is. Other devices and the MFC's other
    settings are kept.
    """
    mirrored = mirrored_purge_gas(experiment_payload, hardware_payload)
    if mirrored is None:
        return None
    device, gas = mirrored
    declared = experiment_payload.get("device_settings")
    out = {
        name: dict(entry) if isinstance(entry, Mapping) else entry
        for name, entry in (declared.items() if isinstance(declared, Mapping) else ())
    }
    entry = out.get(device)
    out[device] = {**(entry if isinstance(entry, Mapping) else {}), "gas": gas}
    return out


def purge_gas_mismatch(
    experiment_payload: Mapping[str, Any],
    hardware_payload: Mapping[str, Any],
) -> tuple[str, str, str | None] | None:
    """``(device, profile gas, declared gas)`` when the purge MFC's declared
    ``device_settings`` gas isn't the one :func:`mirrored_purge_gas` gives.

    Gases compare as the device's setting names them, so ``"nitrogen"``
    matches ``"N2"``; an unset gas is a mismatch. ``None`` when they agree,
    when the gas doesn't follow the profile, or when the declared entry
    or gas is itself invalid — device_settings validation reports those.
    """
    mirrored = mirrored_purge_gas(experiment_payload, hardware_payload)
    if mirrored is None:
        return None
    device, gas = mirrored
    declared = experiment_payload.get("device_settings")
    entry = declared.get(device) if isinstance(declared, Mapping) else None
    if entry is None:
        return device, gas, None
    if not isinstance(entry, Mapping):
        return None
    actual = entry.get("gas")
    if actual is None:
        return device, gas, None
    model = _gas_setting_model(hardware_payload, device)
    try:
        actual_gas = model.model_validate({"gas": actual}).gas
    except ValidationError:
        return None
    return None if actual_gas == gas else (device, gas, actual)


__all__ = [
    "CAPA_OPTIONAL_GROUPS",
    "CAPA_PROFILE_ID",
    "CAPA_REQUIRED_GROUPS",
    "PER_RUN_SPECIMEN_KEYS",
    "SPECIMEN_SAMPLE_FIELDS",
    "clear_per_run_fields",
    "current_capa_mappings",
    "device_settings_with_purge_gas",
    "is_capa_profile",
    "mirrored_purge_gas",
    "profile_model_fields",
    "profile_specimen",
    "purge_gas_mismatch",
    "sample_from_specimen",
    "sample_specimen_mismatches",
]
