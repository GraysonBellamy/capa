"""New Setup Wizard seed logic."""

from __future__ import annotations

from pathlib import Path

import pytest

from capa.config import ConfigDocument
from capa.ui.tabs.setup_wizard import _Spec, build_document

# ---------------------------------------------------------------------------
# Seed dispatch.
# ---------------------------------------------------------------------------


def test_blank_seed_produces_empty_hardware() -> None:
    spec = _Spec()
    spec.starting_point = "blank"
    spec.layout = "yaml_ext_toml"
    spec.method_choice = "none"
    spec.save_now = False
    doc = build_document(spec)
    assert isinstance(doc, ConfigDocument)
    assert doc.hardware_payload["devices"] == []
    assert doc.hardware_payload["channels"] == []
    assert doc.hardware_payload["cameras"] == []
    # Blank drafts always have no source paths.
    assert doc.experiment_path is None
    assert doc.hardware_path is None


def test_sim_capa_seed_clones_fixture_devices_and_channels() -> None:
    spec = _Spec()
    spec.starting_point = "sim_capa"
    doc = build_document(spec)
    # The wizard clones configs/experiments/sim_capa_pyrolysis.yaml.
    devices = doc.hardware_payload["devices"]
    assert isinstance(devices, list)
    adapters = {d["adapter"] for d in devices}
    assert "capa.devices.sim.watlow_sim" in adapters
    assert "capa.devices.sim.alicat_sim" in adapters
    assert "capa.devices.sim.sartorius_sim" in adapters
    # Channels reference the seeded devices.
    channels = doc.hardware_payload["channels"]
    assert isinstance(channels, list)
    by_name = {ch["name"]: ch for ch in channels}
    assert "heater.pv" in by_name
    assert "heater.setpoint" in by_name
    # capa_group metadata survives the clone so the CAPA mapping panel
    # shows ✓ on the new draft.
    assert by_name["heater.pv"]["metadata"]["capa_group"] == "heater_pv"


def test_wizard_clears_source_paths_so_save_as_is_required() -> None:
    """The wizard must not leave the cloned fixture's path on the doc
    — otherwise a stray Ctrl+S would overwrite the canonical fixture."""
    spec = _Spec()
    spec.starting_point = "sim_capa"
    doc = build_document(spec)
    assert doc.experiment_path is None
    assert doc.hardware_path is None


# ---------------------------------------------------------------------------
# Source-layout pass-through.
# ---------------------------------------------------------------------------


def test_external_layout_sets_hardware_mode_external() -> None:
    spec = _Spec()
    spec.starting_point = "sim_capa"
    spec.layout = "yaml_ext_toml"
    doc = build_document(spec)
    assert doc.hardware_mode == "external"


def test_inline_layout_sets_hardware_mode_inline() -> None:
    spec = _Spec()
    spec.starting_point = "sim_capa"
    spec.layout = "single_yaml"
    doc = build_document(spec)
    assert doc.hardware_mode == "inline"


# ---------------------------------------------------------------------------
# Method choice.
# ---------------------------------------------------------------------------


def test_method_none_leaves_method_unset() -> None:
    spec = _Spec()
    spec.starting_point = "sim_capa"
    spec.method_choice = "none"
    doc = build_document(spec)
    assert doc.method_mode == "none"
    assert doc.method_path is None


def test_method_attach_carries_path_through() -> None:
    spec = _Spec()
    spec.starting_point = "sim_capa"
    spec.method_choice = "attach"
    spec.method_path = Path("/tmp/example.method.toml")
    doc = build_document(spec)
    assert doc.method_mode == "external"
    assert doc.method_path == Path("/tmp/example.method.toml")
    assert doc.method_format == "toml"


# ---------------------------------------------------------------------------
# Save round-trip — the §9.1 acceptance path.
# ---------------------------------------------------------------------------


def test_save_now_writes_external_layout(tmp_path: Path) -> None:
    """Acceptance §9.1 — create a sim CAPA setup, save, reload."""
    from capa.ui.tabs.setup_wizard import _layout_for

    spec = _Spec()
    spec.starting_point = "sim_capa"
    spec.layout = "yaml_ext_toml"
    spec.method_choice = "none"
    spec.save_now = True
    spec.experiment_path = tmp_path / "new_sim.yaml"

    doc = build_document(spec)
    doc.save_as(_layout_for(spec, spec.experiment_path))

    # Both files exist; the YAML stays on disk and the sibling
    # hardware TOML lands next to it.
    assert spec.experiment_path.exists()
    hw_file = tmp_path / "new_sim_hardware.toml"
    assert hw_file.exists()

    # Reload and confirm the clone carries devices + channels.
    reloaded = ConfigDocument.load(spec.experiment_path)
    devices = reloaded.hardware_payload["devices"]
    assert {d["name"] for d in devices} == {"heater", "purge_mfc", "balance", "cdaq1"}
    channels = reloaded.hardware_payload["channels"]
    names = {ch["name"] for ch in channels}
    assert "heater.pv" in names
    assert "heater.setpoint" in names


def test_save_now_inline_layout_writes_one_file(tmp_path: Path) -> None:
    from capa.ui.tabs.setup_wizard import _layout_for

    spec = _Spec()
    spec.starting_point = "sim_capa"
    spec.layout = "single_yaml"
    spec.method_choice = "none"
    spec.save_now = True
    spec.experiment_path = tmp_path / "inline.yaml"

    doc = build_document(spec)
    doc.save_as(_layout_for(spec, spec.experiment_path))

    # Hardware inlined — only the experiment file should exist.
    assert spec.experiment_path.exists()
    assert not (tmp_path / "inline_hardware.toml").exists()

    reloaded = ConfigDocument.load(spec.experiment_path)
    assert reloaded.hardware_mode == "inline"
    assert "heater" in {d["name"] for d in reloaded.hardware_payload["devices"]}


# ---------------------------------------------------------------------------
# Wizard validation (§9.4 — saved drafts validate cleanly).
# ---------------------------------------------------------------------------


def test_sim_seed_passes_layer1_through_layer4(tmp_path: Path) -> None:
    """A wizard-produced sim draft must be clean against Layers 1-4
    (no method attached so we don't drag in method-target checks)."""
    from capa.config import validate
    from capa.ui.tabs.setup_wizard import _layout_for

    spec = _Spec()
    spec.starting_point = "sim_capa"
    spec.layout = "yaml_ext_toml"
    spec.method_choice = "none"
    spec.save_now = True
    spec.experiment_path = tmp_path / "validated.yaml"

    doc = build_document(spec)
    doc.save_as(_layout_for(spec, spec.experiment_path))

    reloaded = ConfigDocument.load(spec.experiment_path)
    problems = validate(reloaded, with_live_checks=False)
    errors = [p for p in problems if p.severity == "error"]
    if errors:
        msgs = [f"{p.code}: {p.message}" for p in errors]
        pytest.fail("seed should be error-free:\n" + "\n".join(msgs))
