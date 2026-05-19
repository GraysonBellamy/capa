"""CAPA Profile section tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from capa.config import ConfigDocument
from capa.config.capa_profile import (
    CAPA_OPTIONAL_GROUPS,
    CAPA_REQUIRED_GROUPS,
    current_capa_mappings,
)
from capa.ui.tabs.setup_sections.capa_profile import CapaProfileSection
from capa.ui.tabs.setup_state import SetupDraft

REPO_ROOT = Path(__file__).resolve().parents[4]
SIM_CAPA_EXP = REPO_ROOT / "configs" / "experiments" / "sim_capa_pyrolysis.yaml"


# ---------------------------------------------------------------------------
# Shared helper.
# ---------------------------------------------------------------------------


def test_current_capa_mappings_extracts_groups() -> None:
    channels = [
        {"name": "heater.pv", "metadata": {"capa_group": "heater_pv"}},
        {"name": "TC_top_1", "metadata": {"capa_group": "sample_temperature"}},
        {"name": "TC_top_2", "metadata": {"capa_group": "sample_temperature"}},
        {"name": "noise", "metadata": {}},
    ]
    mappings = current_capa_mappings(channels)
    assert mappings == {
        "heater_pv": ["heater.pv"],
        "sample_temperature": ["TC_top_1", "TC_top_2"],
    }


def test_required_groups_present_for_pyrolysis() -> None:
    assert {
        "heater_setpoint",
        "heater_pv",
        "sample_temperature",
        "purge_gas_flow",
        "mass",
    } == set(CAPA_REQUIRED_GROUPS)
    assert "reactor_pressure" not in CAPA_OPTIONAL_GROUPS
    assert "mass" not in CAPA_OPTIONAL_GROUPS


# ---------------------------------------------------------------------------
# Section behaviour.
# ---------------------------------------------------------------------------


def _make_section(qtbot: Any) -> tuple[CapaProfileSection, SetupDraft]:
    document = ConfigDocument.load(SIM_CAPA_EXP)
    draft = SetupDraft(document=document)
    section = CapaProfileSection()
    qtbot.addWidget(section)
    section.set_draft(draft)
    return section, draft


def test_capa_profile_renders_mapping_rows(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    groups = [row.group for row in section._mapping_rows]
    # Required + optional groups present, required first.
    assert groups[: len(CAPA_REQUIRED_GROUPS)] == list(CAPA_REQUIRED_GROUPS)


def test_capa_profile_chip_states_reflect_existing_mappings(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    chips = {row.group: row.chip.text() for row in section._mapping_rows}
    # sim_capa.toml maps all required groups → ✓.
    for group in CAPA_REQUIRED_GROUPS:
        assert chips[group] == "✓", f"{group} should be ✓ in sim_capa.toml"


def test_capa_profile_payload_includes_channels_and_profile(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    payload = section.payload()
    assert "channels" in payload
    assert "domain_profile" in payload
    profile = payload["domain_profile"]
    assert isinstance(profile, dict)
    assert "metadata" in profile
    metadata = profile["metadata"]
    assert "specimen" in metadata
    assert "heater_program" in metadata
    assert "atmosphere" in metadata


def test_capa_profile_change_mapping_updates_channel_metadata(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    # Find the heater_pv mapping combo; change it from heater.pv to
    # heater.setpoint (silly choice for the kind, but the test verifies
    # only metadata routing — the validator handles kind sanity).
    for row in section._mapping_rows:
        if row.group != "heater_pv":
            continue
        # The combo has been populated by allowed kinds (process_var);
        # only heater.pv is a process_var. Let's verify the combo
        # contains only that channel + the (none) sentinel.
        items = [row.combo.itemData(i) for i in range(row.combo.count())]
        assert items == ["", "heater.pv"]
        # Clear the mapping.
        row.combo.setCurrentIndex(0)
        section._on_mapping_changed("heater_pv")
        break

    channels = section._compose_channels_with_mappings()
    # heater.pv channel should have lost its capa_group.
    target = next(c for c in channels if c["name"] == "heater.pv")
    assert "capa_group" not in (target.get("metadata") or {})


def test_capa_profile_specimen_pane_round_trips(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    section._specimen_form.set_values(
        {
            "id": "pmma_disk_S073-001",
            "material": "PMMA",
            "form": "disk",
            "mass_g": 25.0,
            "thickness_mm": 10.0,
            "specimen_holder": "ceramic_ring_25mm",
        }
    )
    payload = section.payload()
    metadata = payload["domain_profile"]["metadata"]
    assert metadata["specimen"]["id"] == "pmma_disk_S073-001"
    assert metadata["specimen"]["material"] == "PMMA"
    assert metadata["specimen"]["mass_g"] == 25.0


def test_capa_profile_compose_preserves_unmanaged_capa_group(qtbot: Any) -> None:
    """A channel mapped to a non-required, non-optional ``capa_group``
    (a plugin's custom group) should keep its metadata across compose."""
    section, draft = _make_section(qtbot)
    channels = list(draft.document.hardware_payload["channels"])
    channels.append(
        {
            "name": "exotic",
            "kind": "process_var",
            "unit": "Pa",
            "source": {"source": "watlow_parameter", "device": "heater", "parameter": "x"},
            "metadata": {"capa_group": "exotic_plugin_group"},
        }
    )
    draft.document.hardware_payload["channels"] = channels
    section.refresh()
    composed = section._compose_channels_with_mappings()
    exotic = next(c for c in composed if c["name"] == "exotic")
    assert exotic["metadata"]["capa_group"] == "exotic_plugin_group"


# ---------------------------------------------------------------------------
# Tune-artifact autofill (Phase 3 W7)
# ---------------------------------------------------------------------------


def _make_artifact(*, points: list[tuple[float, float]]):
    """Construct an artifact with the given (target, setpoint) pairs."""
    from datetime import UTC, datetime

    from capa.calibration.tune_artifact import (
        HeatFluxTuneArtifact,
        HeatFluxTunePoint,
    )

    return HeatFluxTuneArtifact(
        id="capa_flux_test",
        rig="sim_rig",
        heater_device="heater",
        heater_setpoint_channel="heater.setpoint",
        heater_pv_channel="heater.pv",
        flux_channel="heat_flux_gauge",
        geometry="40 mm below heater",
        accepted_at=datetime.now(UTC),
        procedure_id="capa.builtin.heat_flux_tune",
        procedure_version="0.1.0",
        points=tuple(
            HeatFluxTunePoint(
                target_flux_kw_m2=t,
                heater_setpoint_c=sp,
                measured_flux_mean_kw_m2=t,
                measured_flux_std_kw_m2=0.02,
                measured_flux_slope_kw_m2_per_min=0.005,
                heater_pv_mean_c=sp,
                soak_s=300.0,
                accepted=True,
                accept_reason="algorithm_converged",
            )
            for t, sp in points
        ),
    )


def test_apply_artifact_in_bracket_writes_setpoint_and_ref(qtbot: Any) -> None:
    """Applying an artifact that brackets the current target writes both
    ``heater_setpoint_c`` (linearly interpolated) and
    ``flux_calibration_ref`` (the artifact id) back into the form."""
    section, _ = _make_section(qtbot)
    section._heater_form.set_values({"target_heat_flux_kw_m2": 50.0})
    artifact = _make_artifact(points=[(25.0, 450.0), (75.0, 750.0)])

    section._apply_artifact(artifact)

    values = section._heater_form.values()
    assert values["heater_setpoint_c"] == 600.0  # interp at midpoint
    assert values["flux_calibration_ref"] == "capa_flux_test"
    assert "applied capa_flux_test" in section._tune_status_label.text()


def test_apply_artifact_out_of_bracket_leaves_form_alone(qtbot: Any, monkeypatch: Any) -> None:
    """Out-of-bracket targets show a warning and leave the form untouched."""
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    section, _ = _make_section(qtbot)
    section._heater_form.set_values(
        {"target_heat_flux_kw_m2": 100.0, "heater_setpoint_c": 600.0, "flux_calibration_ref": ""}
    )
    artifact = _make_artifact(points=[(25.0, 450.0), (75.0, 700.0)])

    section._apply_artifact(artifact)

    values = section._heater_form.values()
    # Setpoint left at its operator-supplied value.
    assert values["heater_setpoint_c"] == 600.0
    assert values["flux_calibration_ref"] == ""
    assert "does not bracket" in section._tune_status_label.text()


def test_apply_artifact_no_target_prompts_operator(qtbot: Any, monkeypatch: Any) -> None:
    """Applying with target ≤ 0 is a no-op with an informational toast."""
    from PySide6.QtWidgets import QMessageBox

    called: list[tuple[Any, ...]] = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: called.append(args))
    section, _ = _make_section(qtbot)
    section._heater_form.set_values({"target_heat_flux_kw_m2": 0.0})
    artifact = _make_artifact(points=[(25.0, 450.0), (75.0, 700.0)])

    section._apply_artifact(artifact)

    assert called, "expected an informational message when target is 0"


def test_clear_tune_ref_clears_ref_only(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    section._heater_form.set_values(
        {
            "target_heat_flux_kw_m2": 50.0,
            "heater_setpoint_c": 600.0,
            "flux_calibration_ref": "capa_flux_test",
        }
    )

    section._on_clear_tune_ref_clicked()

    values = section._heater_form.values()
    assert values["flux_calibration_ref"] == ""
    # Setpoint untouched.
    assert values["heater_setpoint_c"] == 600.0
