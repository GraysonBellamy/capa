"""Canonical TOML/YAML writers.

Both writers normalise field order so ``load → save (no edits)`` produces
deterministic output across machines and Python versions. The orderings
match the orderings hand-written fixtures already use. The in-repo
fixtures under ``configs/`` are normalised up front so the round-trip
test is byte-identical from the start.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import tomli_w
from ruamel.yaml import YAML

# ---------------------------------------------------------------------------
# Canonical key orderings.
# ---------------------------------------------------------------------------

EXPERIMENT_KEY_ORDER: tuple[str, ...] = (
    "hardware",
    "method",
    "procedure",
    "domain_profile",
    "calibration_set",
    "operator",
    "sample",
    "storage",
    "safety",
    "runtime",
    "tags",
    "custom",
)
"""Top-level key order inside an experiment YAML/TOML."""

HARDWARE_KEY_ORDER: tuple[str, ...] = (
    "name",
    "devices",
    "cameras",
    "channels",
)
"""Top-level key order inside a hardware TOML."""

DEVICE_KEY_ORDER: tuple[str, ...] = (
    "name",
    "adapter",
    "resource_id",
    "on_failure",
    "params",
)

CAMERA_KEY_ORDER: tuple[str, ...] = (
    "name",
    "adapter",
    "kind",
    "model_hint",
    "serial",
    "output_root",
    "on_failure",
    "estimated_bps",
    "params",
)

CHANNEL_KEY_ORDER: tuple[str, ...] = (
    "name",
    "kind",
    "unit",
    "derived_unit",
    "plot_group",
    "sample_rate_hz",
    "keep_raw",
    "source",
    "calibration",
    "alarms",
    "metadata",
)


def _reorder(
    mapping: Mapping[str, Any],
    order: tuple[str, ...],
) -> dict[str, Any]:
    """Return a new dict with ``order`` keys first (when present), trailing
    keys appended in insertion order.

    Keys absent from ``mapping`` are skipped; keys present in ``mapping``
    but missing from ``order`` are appended in their original order so
    plugin-contributed top-level keys survive a round trip.
    """
    out: dict[str, Any] = {}
    for key in order:
        if key in mapping:
            out[key] = mapping[key]
    for key, value in mapping.items():
        if key not in out:
            out[key] = value
    return out


def _canonicalise_device(device: Mapping[str, Any]) -> dict[str, Any]:
    return _reorder(device, DEVICE_KEY_ORDER)


def _canonicalise_camera(camera: Mapping[str, Any]) -> dict[str, Any]:
    return _reorder(camera, CAMERA_KEY_ORDER)


def _canonicalise_channel(channel: Mapping[str, Any]) -> dict[str, Any]:
    return _reorder(channel, CHANNEL_KEY_ORDER)


def canonicalise_hardware_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Reshape a hardware-profile dict (raw, pre-validation) into canonical order."""
    out = _reorder(payload, HARDWARE_KEY_ORDER)
    if "devices" in out and isinstance(out["devices"], list):
        out["devices"] = [_canonicalise_device(d) for d in out["devices"]]
    if "cameras" in out and isinstance(out["cameras"], list):
        out["cameras"] = [_canonicalise_camera(c) for c in out["cameras"]]
    if "channels" in out and isinstance(out["channels"], list):
        out["channels"] = [_canonicalise_channel(c) for c in out["channels"]]
    return out


def canonicalise_experiment_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Reshape an experiment dict into canonical order.

    ``hardware`` and ``method`` may be string refs (external) or nested
    mappings (inline). When inline, the nested hardware payload is
    canonicalised in place; method bodies are left as-is (Method has its
    own canonical writer in ``capa.experiment.method``).
    """
    out = _reorder(payload, EXPERIMENT_KEY_ORDER)
    hardware = out.get("hardware")
    if isinstance(hardware, Mapping):
        out["hardware"] = canonicalise_hardware_payload(hardware)
    return out


# ---------------------------------------------------------------------------
# Writers.
# ---------------------------------------------------------------------------


def _yaml_writer() -> YAML:
    """Configured ruamel.yaml writer.

    Block style throughout for diffability; insertion-order respected.
    ``sort_base_mapping_type_on_output`` is the explicit knob that disables
    SafeDumper's default alphabetical sort — ``_reorder`` already produces
    dicts in canonical order, so a re-sort here would defeat the whole
    canonical-ordering effort.
    """
    yaml = YAML(typ="safe", pure=True)
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.sort_base_mapping_type_on_output = False  # type: ignore[assignment]
    return yaml


def write_yaml_canonical(payload: Mapping[str, Any], path: Path) -> None:
    """Write ``payload`` to ``path`` as canonically-ordered YAML."""
    canonical = canonicalise_experiment_payload(payload)
    yaml = _yaml_writer()
    with open(path, "w", encoding="utf-8", newline="\n") as fp:
        yaml.dump(canonical, fp)


def write_yaml_string(payload: Mapping[str, Any]) -> str:
    """Render canonical YAML to a string (for atomic-save staging)."""
    canonical = canonicalise_experiment_payload(payload)
    yaml = _yaml_writer()
    buf = io.StringIO()
    yaml.dump(canonical, buf)
    return buf.getvalue()


def write_toml_experiment(payload: Mapping[str, Any], path: Path) -> None:
    """Write an experiment dict as canonically-ordered TOML."""
    canonical = canonicalise_experiment_payload(payload)
    with open(path, "wb") as fp:
        tomli_w.dump(canonical, fp)


def write_toml_hardware(payload: Mapping[str, Any], path: Path) -> None:
    """Write a hardware-profile dict as canonically-ordered TOML."""
    canonical = canonicalise_hardware_payload(payload)
    with open(path, "wb") as fp:
        tomli_w.dump(canonical, fp)


def write_toml_string_experiment(payload: Mapping[str, Any]) -> bytes:
    canonical = canonicalise_experiment_payload(payload)
    return tomli_w.dumps(canonical).encode("utf-8")


def write_toml_string_hardware(payload: Mapping[str, Any]) -> bytes:
    canonical = canonicalise_hardware_payload(payload)
    return tomli_w.dumps(canonical).encode("utf-8")


__all__ = [
    "CAMERA_KEY_ORDER",
    "CHANNEL_KEY_ORDER",
    "DEVICE_KEY_ORDER",
    "EXPERIMENT_KEY_ORDER",
    "HARDWARE_KEY_ORDER",
    "canonicalise_experiment_payload",
    "canonicalise_hardware_payload",
    "write_toml_experiment",
    "write_toml_hardware",
    "write_toml_string_experiment",
    "write_toml_string_hardware",
    "write_yaml_canonical",
    "write_yaml_string",
]
