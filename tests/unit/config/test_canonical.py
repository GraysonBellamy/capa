"""Tests for canonical TOML/YAML writers (plan §5.13)."""

from __future__ import annotations

from capa.config.canonical import (
    EXPERIMENT_KEY_ORDER,
    HARDWARE_KEY_ORDER,
    canonicalise_experiment_payload,
    canonicalise_hardware_payload,
)


def test_experiment_top_level_keys_reordered() -> None:
    """Top-level keys move to canonical order; unknown keys append after."""
    shuffled = {
        "tags": ["sim"],
        "operator": {"id": "abr"},
        "hardware": {"name": "rig"},
        "procedure": {"id": "p"},
        "custom_extension_key": {"x": 1},  # plugin/unknown — must survive at end
    }
    canonical = canonicalise_experiment_payload(shuffled)
    keys = list(canonical.keys())
    # Known canonical-order keys come first in canonical order …
    known = [k for k in keys if k in EXPERIMENT_KEY_ORDER]
    assert known == ["hardware", "procedure", "operator", "tags"]
    # … and unknown keys are appended (without losing them).
    assert keys[-1] == "custom_extension_key"


def test_hardware_top_level_keys_reordered() -> None:
    shuffled = {
        "channels": [{"name": "c1", "kind": "tc"}],
        "name": "rig",
        "devices": [{"name": "d1", "adapter": "x"}],
    }
    canonical = canonicalise_hardware_payload(shuffled)
    assert list(canonical.keys()) == ["name", "devices", "channels"]
    # Unknown keys (none here) — confirm canonical order is exact.
    assert canonical["devices"][0]["name"] == "d1"


def test_channel_field_reordered() -> None:
    """Channel rows have their fields canonicalised too."""
    shuffled = {
        "name": "rig",
        "channels": [
            {
                "calibration": {"kind": "identity"},
                "metadata": {"capa_group": "x"},
                "source": {"source": "watlow_parameter", "device": "h"},
                "unit": "degC",
                "name": "ch",
                "kind": "tc",
            }
        ],
    }
    canonical = canonicalise_hardware_payload(shuffled)
    ch = canonical["channels"][0]
    keys = list(ch.keys())
    # Identity first, then unit, then source/calibration/metadata per CHANNEL_KEY_ORDER.
    assert keys.index("name") < keys.index("kind") < keys.index("unit")
    assert (
        keys.index("unit")
        < keys.index("source")
        < keys.index("calibration")
        < keys.index("metadata")
    )


def test_unknown_top_level_keys_preserved() -> None:
    """Plugin-contributed keys at the top level survive a canonical pass.

    Plan §5.13: canonical writers must not drop unknown keys, so that
    plugin-defined extensions don't get silently lost when the
    operator clicks Save.
    """
    payload = {"hardware": {"name": "x"}, "plugin_specific": {"a": 1}}
    out = canonicalise_experiment_payload(payload)
    assert "plugin_specific" in out
    assert out["plugin_specific"] == {"a": 1}


def test_hardware_orderings_constants() -> None:
    """Document the canonical orderings as constants the tests pin."""
    assert HARDWARE_KEY_ORDER == ("name", "devices", "cameras", "channels")
    assert EXPERIMENT_KEY_ORDER[0] == "hardware"
    assert "procedure" in EXPERIMENT_KEY_ORDER
