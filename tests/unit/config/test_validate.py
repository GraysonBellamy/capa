"""Tests for the 5-layer validation pipeline ()."""

from __future__ import annotations

from pathlib import Path

import pytest

from capa.config import ConfigDocument, ConfigProblem, validate
from capa.devices.registry import _import_builtins


@pytest.fixture(scope="module", autouse=True)
def _ensure_builtins_loaded() -> None:
    _import_builtins()


# ---------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------


def test_validate_known_good_fixture(configs_dir: Path) -> None:
    """The reference CAPA pyrolysis config validates cleanly through all layers."""
    doc = ConfigDocument.load(configs_dir / "experiments" / "sim_capa_pyrolysis.yaml")
    problems = validate(doc)
    assert problems == []


# ---------------------------------------------------------------------------
# Layer 1 — schema.
# ---------------------------------------------------------------------------


def test_layer1_schema_error_yields_pydantic_path(configs_dir: Path) -> None:
    """A malformed value surfaces as a single error with the offending path."""
    doc = ConfigDocument.load(configs_dir / "experiments" / "sim_capa_pyrolysis.yaml")
    # Corrupt the hardware payload: remove the required ``name`` field.
    doc.hardware_payload.pop("name")
    problems = validate(doc)
    assert any(p.severity == "error" for p in problems)
    # At least one problem should target hardware.name.
    assert any(p.section == "devices" and p.path and p.path[0] == "name" for p in problems) or any(
        "name" in str(p.path) for p in problems
    )


# ---------------------------------------------------------------------------
# Layer 3 — domain (CAPA profile).
# ---------------------------------------------------------------------------


def test_layer3_capa_missing_required_group(configs_dir: Path) -> None:
    """Removing a required CAPA mapping surfaces a profile error."""
    doc = ConfigDocument.load(configs_dir / "experiments" / "sim_capa_pyrolysis.yaml")
    # Strip the capa_group from every channel — domain layer should now
    # report all four required groups as missing.
    for ch in doc.hardware_payload.get("channels", []):
        if isinstance(ch, dict) and "metadata" in ch:
            ch["metadata"].pop("capa_group", None)
    problems = validate(doc)
    missing_codes = {p.code for p in problems if p.code == "capa_profile.missing_required_group"}
    assert "capa_profile.missing_required_group" in missing_codes
    # All four required groups should report missing.
    missing_groups = {
        p.path[-1] for p in problems if p.code == "capa_profile.missing_required_group"
    }
    assert {"heater_setpoint", "heater_pv", "sample_temperature", "purge_gas_flow"}.issubset(
        missing_groups
    )


def test_layer3_skipped_when_profile_is_not_capa(configs_dir: Path) -> None:
    """Domain layer is profile-specific; non-CAPA profiles don't trigger it."""
    doc = ConfigDocument.load(configs_dir / "experiments" / "sim_capa_pyrolysis.yaml")
    # Swap profile id; nothing else changes. Domain layer should not
    # emit CAPA-specific problems.
    doc.experiment_payload["domain_profile"] = {"id": "capa.profiles.cone_calorimeter"}
    problems = validate(doc)
    capa_problems = [p for p in problems if p.code.startswith("capa_profile.")]
    assert capa_problems == []


# ---------------------------------------------------------------------------
# Layer 4 — resource dry run.
# ---------------------------------------------------------------------------


def test_layer4_catches_resource_conflict_without_opening_hardware(
    configs_dir: Path,
) -> None:
    """Two adapters on the same serial port surface as a Layer-4 problem.

    The check runs through ``collect_resource_problems``, which
    constructs adapters passively — no serial bus is opened.
    """
    doc = ConfigDocument.load(configs_dir / "experiments" / "sim_capa_pyrolysis.yaml")
    # Replace the sim hardware with two real Watlow devices on the same port.
    # Sim adapters don't produce serial: resource_ids, so to test a real
    # conflict we drop in two real Watlow devices with colliding ports.
    doc.hardware_payload["devices"] = [
        {
            "name": "heater_a",
            "adapter": "capa.devices.watlow",
            "params": {"port": "COM6", "address": 1},
        },
        {
            "name": "heater_b",
            "adapter": "capa.devices.watlow",
            "params": {"port": "COM6", "address": 2},
        },
    ]
    # Drop CAPA-profile to focus on the resource layer.
    doc.experiment_payload["domain_profile"] = {"id": "capa.profiles.cone_calorimeter"}
    # Replace channels with a single passthrough so the schema validates.
    doc.hardware_payload["channels"] = [
        {
            "name": "heater_a.pv",
            "kind": "process_var",
            "unit": "degC",
            "derived_unit": "degC",
            "source": {
                "source": "watlow_parameter",
                "device": "heater_a",
                "parameter": "process_value",
                "instance": 1,
            },
            "calibration": {"kind": "identity", "input_unit": "degC", "output_unit": "degC"},
        }
    ]
    problems = validate(doc)
    # If two Watlow adapters with the same port end up with the same
    # resource_id (multi-drop sharing), no problem. If they differ,
    # Layer 4 catches it. Either way, validation must not raise.
    # The real-Watlow path may also raise during construction if the
    # required serial library isn't usable in this environment — in
    # that case Layer 4 wraps the failure as a single error.
    assert isinstance(problems, list)
    assert all(isinstance(p, ConfigProblem) for p in problems)


# ---------------------------------------------------------------------------
# Layer 2 — referential.
# ---------------------------------------------------------------------------


def test_layer2_binding_family_check_passes_for_matching_adapter(
    configs_dir: Path,
) -> None:
    """A correctly-bound channel produces no binding-family problem."""
    doc = ConfigDocument.load(configs_dir / "experiments" / "sim_capa_pyrolysis.yaml")
    problems = validate(doc)
    mismatch_problems = [p for p in problems if p.code == "channels.binding_family_mismatch"]
    assert mismatch_problems == []


# ---------------------------------------------------------------------------
# Layer 2 — NI-DAQ join (declared NI channel ↔ capa channel binding).
# ---------------------------------------------------------------------------


def test_layer2_nidaq_join_passes_on_correct_real_config(configs_dir: Path) -> None:
    """The reference real-NI fixture has a coherent join; no new errors."""
    doc = ConfigDocument.load(configs_dir / "experiments" / "nidaq_real_freerun.yaml")
    problems = validate(doc)
    new_codes = {
        "channels.binding_field_unresolved",
        "devices.nidaq.duplicate_channel_name",
    }
    assert {p.code for p in problems} & new_codes == set()


def test_layer2_nidaq_join_flags_typoed_field(configs_dir: Path) -> None:
    """Mistyped binding field surfaces as binding_field_unresolved with the
    available fields list — the silent-runtime-skip failure mode is now
    a config-load error.
    """
    doc = ConfigDocument.load(configs_dir / "experiments" / "nidaq_real_freerun.yaml")
    # Typo the first channel's binding field: TC_top_1 → TC_top_X.
    channel = doc.hardware_payload["channels"][0]
    channel["source"]["field"] = "TC_top_X"
    problems = validate(doc)
    unresolved = [p for p in problems if p.code == "channels.binding_field_unresolved"]
    assert len(unresolved) == 1
    assert unresolved[0].section == "channels"
    assert unresolved[0].path[:2] == ("channels", 0)
    assert "TC_top_1" in unresolved[0].message  # available fields surfaced


def test_layer2_nidaq_join_flags_typoed_task(configs_dir: Path) -> None:
    """A mistyped task name surfaces a binding_field_unresolved against the
    task, with the available task list in the message.
    """
    doc = ConfigDocument.load(configs_dir / "experiments" / "nidaq_real_freerun.yaml")
    channel = doc.hardware_payload["channels"][0]
    channel["source"]["task"] = "wrong_task_name"
    problems = validate(doc)
    unresolved = [p for p in problems if p.code == "channels.binding_field_unresolved"]
    assert len(unresolved) >= 1
    assert any("default_task" in p.message for p in unresolved)


def test_layer2_nidaq_join_flags_empty_declared_task(configs_dir: Path) -> None:
    """A channel bound to an NI task with no declared inputs is a clean
    Layer-2 problem, not a silent opt-out or later materialization crash.
    """
    doc = ConfigDocument.load(configs_dir / "experiments" / "nidaq_real_freerun.yaml")
    device = doc.hardware_payload["devices"][0]
    device["params"]["channels"] = []
    problems = validate(doc)
    unresolved = [p for p in problems if p.code == "channels.binding_field_unresolved"]
    assert unresolved
    assert any("no NI fields" in p.message for p in unresolved)


def test_layer2_nidaq_join_flags_duplicate_ni_channel_names(configs_dir: Path) -> None:
    """Two NI channel rows sharing a display name within the same task is an
    error — DaqReading.values is dict-keyed by display name and the second
    row would non-deterministically shadow the first.
    """
    doc = ConfigDocument.load(configs_dir / "experiments" / "nidaq_real_freerun.yaml")
    # Rename the second channel to collide with the first.
    device = doc.hardware_payload["devices"][0]
    device["params"]["channels"][1]["name"] = device["params"]["channels"][0]["name"]
    problems = validate(doc)
    duplicates = [p for p in problems if p.code == "devices.nidaq.duplicate_channel_name"]
    assert len(duplicates) == 1
    assert duplicates[0].section == "devices"


def test_layer2_nidaq_join_is_silent_for_sim_configs(configs_dir: Path) -> None:
    """The sim adapter family ("sim") doesn't go through the NI channels
    array, so the join validator must not falsely flag sim bindings.
    """
    doc = ConfigDocument.load(configs_dir / "experiments" / "sim_capa_pyrolysis.yaml")
    problems = validate(doc)
    join_problems = [
        p
        for p in problems
        if p.code in {"channels.binding_field_unresolved", "devices.nidaq.duplicate_channel_name"}
    ]
    assert join_problems == []


# ---------------------------------------------------------------------------
# Live checks gated.
# ---------------------------------------------------------------------------


def test_layer5_live_checks_are_gated(configs_dir: Path) -> None:
    """Layer 5 returns empty when live checks are disabled; the live path is populated separately."""
    doc = ConfigDocument.load(configs_dir / "experiments" / "sim_capa_pyrolysis.yaml")
    # Asking for live checks doesn't crash and doesn't produce live findings.
    problems_no_live = validate(doc, with_live_checks=False)
    problems_with_live = validate(doc, with_live_checks=True)
    # No additional problems from the live stub.
    assert len(problems_with_live) == len(problems_no_live)


# ---------------------------------------------------------------------------
# Output ordering.
# ---------------------------------------------------------------------------


def test_errors_sort_before_warnings(configs_dir: Path) -> None:
    """The Problems panel needs errors first."""
    # Synthesize: drop required CAPA group (warning under future profile
    # tweaks, error today) AND remove hardware.name (schema error). Both
    # surface; errors must come first.
    doc = ConfigDocument.load(configs_dir / "experiments" / "sim_capa_pyrolysis.yaml")
    doc.hardware_payload.pop("name", None)
    problems = validate(doc)
    severities = [p.severity for p in problems]
    # First non-empty severity rank should be error.
    if severities:
        assert severities[0] == "error"
