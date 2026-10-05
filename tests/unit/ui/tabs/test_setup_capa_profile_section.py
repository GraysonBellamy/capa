"""CAPA Profile section tests."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from capa.config import ConfigDocument
from capa.config.capa_profile import (
    CAPA_OPTIONAL_GROUPS,
    CAPA_REQUIRED_GROUPS,
    current_capa_mappings,
)
from capa.experiment.profiles.capa_pyrolysis import CapaPyrolysisMetadata
from capa.ui.tabs.setup_sections.capa_profile import CapaProfileSection
from capa.ui.tabs.setup_state import SetupDraft

REPO_ROOT = Path(__file__).resolve().parents[4]
SIM_CAPA_EXP = REPO_ROOT / "configs" / "experiments" / "sim_capa_pyrolysis.yaml"
CAPA_EXPERIMENTS = sorted(
    p
    for p in (REPO_ROOT / "configs" / "experiments").glob("*.yaml")
    if "capa.profiles.capa_pyrolysis" in p.read_text(encoding="utf-8")
)


# ---------------------------------------------------------------------------
# Shared helper.
# ---------------------------------------------------------------------------


def test_current_capa_mappings_extracts_groups() -> None:
    channels = [
        {"name": "heater.pv", "metadata": {"capa_group": "heater_pv"}},
        {"name": "purge.flow", "metadata": {"capa_group": "purge_gas_flow"}},
        {"name": "purge.flow_b", "metadata": {"capa_group": "purge_gas_flow"}},
        {"name": "noise", "metadata": {}},
    ]
    mappings = current_capa_mappings(channels)
    assert mappings == {
        "heater_pv": ["heater.pv"],
        "purge_gas_flow": ["purge.flow", "purge.flow_b"],
    }


def test_required_groups_present_for_pyrolysis() -> None:
    assert {
        "heater_setpoint",
        "heater_pv",
        "purge_gas_flow",
        "mass",
    } == set(CAPA_REQUIRED_GROUPS)
    assert "reactor_pressure" not in CAPA_OPTIONAL_GROUPS
    assert "mass" not in CAPA_OPTIONAL_GROUPS
    assert "sample_temperature" not in CAPA_OPTIONAL_GROUPS


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


def _metadata(payload: dict[str, object] | None) -> dict[str, Any]:
    """``domain_profile.metadata`` out of a section payload."""
    assert payload is not None
    profile = payload["domain_profile"]
    assert isinstance(profile, dict)
    metadata = profile["metadata"]
    assert isinstance(metadata, dict)
    return metadata


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


def test_capa_profile_payload_includes_channels_profile_and_sample(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    payload = section.payload()
    assert payload is not None
    assert set(payload) == {"channels", "domain_profile", "sample"}
    profile = payload["domain_profile"]
    assert isinstance(profile, dict)
    assert profile["id"] == "capa.profiles.capa_pyrolysis"
    # What the panes write is exactly what the profile validates.
    CapaPyrolysisMetadata.model_validate(profile["metadata"])


@pytest.mark.parametrize("path", CAPA_EXPERIMENTS, ids=lambda p: p.name)
def test_capa_profile_round_trips_shipped_configs(qtbot: Any, path: Path) -> None:
    """Loading a shipped CAPA config and composing the payload without
    edits reproduces its profile and sample blocks unchanged."""
    document = ConfigDocument.load(path)
    original_profile = copy.deepcopy(document.experiment_payload["domain_profile"])
    original_sample = copy.deepcopy(document.experiment_payload["sample"])
    section = CapaProfileSection()
    qtbot.addWidget(section)
    section.set_draft(SetupDraft(document=document))

    payload = section.payload()
    assert payload is not None
    assert payload["domain_profile"] == original_profile
    assert payload["sample"] == original_sample


def test_capa_profile_specimen_edit_rewrites_sample(qtbot: Any) -> None:
    """The Specimen pane is the one place the specimen is described; the
    payload's ``sample`` block follows it and keeps ``extra``."""
    section, draft = _make_section(qtbot)
    draft.document.experiment_payload["sample"]["extra"] = {"lot": "B7"}
    form = section._pane_forms["specimen"]
    form.set_values(
        {
            "id": "PMMA-S073-001",
            "material": "PMMA (cast)",
            "initial_mass_g": 4.82,
            "thickness_mm": 6.0,
            "notes": "edge chipped",
        }
    )

    payload = section.payload()
    assert payload is not None
    assert payload["sample"] == {
        "id": "PMMA-S073-001",
        "material": "PMMA (cast)",
        "mass_g": 4.82,
        "thickness_mm": 6.0,
        "notes": "edge chipped",
        "extra": {"lot": "B7"},
    }


def test_capa_profile_unset_required_number_is_omitted(qtbot: Any) -> None:
    """A required number with no value stays out of the payload, so
    validation reports it missing instead of accepting a placeholder."""
    section, draft = _make_section(qtbot)
    del draft.document.experiment_payload["domain_profile"]["metadata"]["atmosphere"]["purge"][
        "target_flow_slpm"
    ]
    section.refresh()

    payload = section.payload()
    assert payload is not None
    purge = _metadata(payload)["atmosphere"]["purge"]
    assert "target_flow_slpm" not in purge


def test_capa_profile_refresh_replaces_previous_draft_values(qtbot: Any) -> None:
    """Switching to a draft whose specimen omits optional fields clears
    the values the previous draft left in the form."""
    section, draft = _make_section(qtbot)
    assert section._pane_forms["specimen"].values()["conditioning"]
    specimen = draft.document.experiment_payload["domain_profile"]["metadata"]["specimen"]
    del specimen["conditioning"]
    section.refresh()

    assert section._pane_forms["specimen"].values()["conditioning"] is None


def test_capa_profile_preserves_preflight_knobs(qtbot: Any) -> None:
    """``_``-prefixed preflight knobs aren't pane fields but survive an edit."""
    section, draft = _make_section(qtbot)
    metadata = draft.document.experiment_payload["domain_profile"]["metadata"]
    metadata["_safe_arm"] = {"max_heater_pv_c": 400.0}
    section.refresh()

    payload = section.payload()
    assert payload is not None
    assert _metadata(payload)["_safe_arm"] == {"max_heater_pv_c": 400.0}


def test_capa_profile_without_profile_offers_add(qtbot: Any) -> None:
    section, draft = _make_section(qtbot)
    del draft.document.experiment_payload["domain_profile"]
    draft.document.experiment_payload["sample"] = {
        "id": "S-9",
        "material": "PS",
        "mass_g": 3.5,
    }
    section.refresh()

    assert section._editor.isHidden()
    assert not section._add_profile_btn.isHidden()
    assert section.payload() is None

    with qtbot.waitSignal(section.valuesChanged):
        section._add_profile_btn.click()

    payload = section.payload()
    assert payload is not None
    specimen = _metadata(payload)["specimen"]
    assert specimen["id"] == "S-9"
    assert specimen["material"] == "PS"
    assert specimen["initial_mass_g"] == 3.5
    # The heater program starts unset rather than at placeholder numbers.
    assert _metadata(payload)["program"] == {}


def test_capa_profile_leaves_other_profiles_alone(qtbot: Any) -> None:
    section, draft = _make_section(qtbot)
    draft.document.experiment_payload["domain_profile"] = {
        "id": "capa.profiles.cone_calorimeter",
        "metadata": {},
    }
    section.refresh()

    assert section._editor.isHidden()
    assert section._add_profile_btn.isHidden()
    assert section.payload() is None


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
    section._pane_forms["specimen"].set_values(
        {
            "id": "pmma_disk_S073-001",
            "material": "PMMA",
            "form": "disk",
            "initial_mass_g": 25.0,
            "thickness_mm": 10.0,
            "specimen_holder": "ceramic_ring_25mm",
        }
    )
    payload = section.payload()
    assert payload is not None
    specimen = _metadata(payload)["specimen"]
    assert specimen["id"] == "pmma_disk_S073-001"
    assert specimen["material"] == "PMMA"
    assert specimen["initial_mass_g"] == 25.0
    assert specimen["thickness_mm"] == 10.0


def test_capa_profile_gas_sampling_round_trips(qtbot: Any) -> None:
    """A declared gas-sampling block loads into the form and comes back out
    unchanged; without one, the payload leaves the key out."""
    section, draft = _make_section(qtbot)
    assert "gas_sampling" not in _metadata(section.payload())

    gas_sampling = {
        "probe_location": "exhaust duct, centerline",
        "probe_height_mm": 450.0,
        "sample_flow_slpm": 1.0,
        "line_length_m": 3.0,
        "conditioning": "particulate filter, chiller",
        "transport_delay_s": 12.5,
    }
    draft.document.experiment_payload["domain_profile"]["metadata"]["gas_sampling"] = dict(
        gas_sampling
    )
    section.refresh()

    metadata = _metadata(section.payload())
    assert metadata["gas_sampling"] == gas_sampling
    CapaPyrolysisMetadata.model_validate(metadata)


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
# Tune-artifact autofill
# ---------------------------------------------------------------------------


def _make_artifact(*, points: list[tuple[float, float]]) -> Any:
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
        {"target_heat_flux_kw_m2": 100.0, "heater_setpoint_c": 600.0, "flux_calibration_ref": None}
    )
    artifact = _make_artifact(points=[(25.0, 450.0), (75.0, 700.0)])

    section._apply_artifact(artifact)

    values = section._heater_form.values()
    # Setpoint left at its operator-supplied value.
    assert values["heater_setpoint_c"] == 600.0
    assert values["flux_calibration_ref"] is None
    assert "does not bracket" in section._tune_status_label.text()


def test_apply_artifact_no_target_prompts_operator(qtbot: Any, monkeypatch: Any) -> None:
    """Applying with no target set is a no-op with an informational toast."""
    from PySide6.QtWidgets import QMessageBox

    called: list[tuple[Any, ...]] = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: called.append(args))
    section, _ = _make_section(qtbot)
    section._heater_form.set_values({"target_heat_flux_kw_m2": None})
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
    assert values["flux_calibration_ref"] is None
    # Setpoint untouched.
    assert values["heater_setpoint_c"] == 600.0


# ---------------------------------------------------------------------------
# Post-tune apply prompt (hold mode)
# ---------------------------------------------------------------------------


def _holding_tick(target_kw_m2: float = 25.0, setpoint_c: float = 520.0) -> Any:
    """Build a ``ProcedureTick`` carrying the hold-mode phase payload.

    Construction mirrors what
    :meth:`HeatFluxTune._emit_holding_tick` publishes on the run's
    final tick — the section's subscriber walks ``payload["phase"]``
    and the held SP/target pair off this shape."""
    from capa.experiment.procedures.builtin.heat_flux_tune.config import (
        PROCEDURE_ID as HEAT_FLUX_TUNE_PROCEDURE_ID,
    )
    from capa.runtime.emissions import ProcedureTick

    return ProcedureTick(
        procedure_id=HEAT_FLUX_TUNE_PROCEDURE_ID,
        t_mono_ns=0,
        payload={
            "phase": "holding",
            "target_kw_m2": target_kw_m2,
            "commanded_setpoint_c": setpoint_c,
            "mean_flux_kw_m2": target_kw_m2,
            "accept_reason": "in_tolerance",
        },
    )


def test_holding_tick_applies_setpoint_to_heater_form(qtbot: Any, monkeypatch: Any) -> None:
    """On a ``phase="holding"`` tick the section writes the held SP /
    target pair into the heater-program form. The prompt is suppressed
    in tests by monkey-patching ``_prompt_apply_hold`` to call the
    apply path directly — the dialog itself is non-modal and hard to
    drive headlessly, but the apply logic is the contract worth
    pinning."""
    section, _ = _make_section(qtbot)
    monkeypatch.setattr(
        section,
        "_prompt_apply_hold",
        section._apply_held_values,
    )

    section._on_procedure_tick(_holding_tick(target_kw_m2=25.0, setpoint_c=520.0))

    values = section._heater_form.values()
    assert values["heater_setpoint_c"] == 520.0
    assert values["target_heat_flux_kw_m2"] == 25.0


def test_holding_tick_latches_to_fire_once_per_run(qtbot: Any, monkeypatch: Any) -> None:
    """Repeat ``phase="holding"`` ticks (the procedure may emit more
    than one, or the UI may resubscribe mid-hold) must not re-pop the
    prompt within the same hold state."""
    section, _ = _make_section(qtbot)
    call_count = {"n": 0}
    monkeypatch.setattr(
        section,
        "_prompt_apply_hold",
        lambda *a, **kw: call_count.__setitem__("n", call_count["n"] + 1),
    )

    section._on_procedure_tick(_holding_tick())
    section._on_procedure_tick(_holding_tick())
    section._on_procedure_tick(_holding_tick())
    assert call_count["n"] == 1


def test_holding_latch_resets_on_non_holding_tick(qtbot: Any, monkeypatch: Any) -> None:
    """A non-holding tick (e.g. the next run's ``phase="settle"``)
    resets the latch so the next hold gets a fresh dialog. Without
    this, only the first tune of a session would prompt."""
    from capa.experiment.procedures.builtin.heat_flux_tune.config import (
        PROCEDURE_ID as HEAT_FLUX_TUNE_PROCEDURE_ID,
    )
    from capa.runtime.emissions import ProcedureTick

    section, _ = _make_section(qtbot)
    call_count = {"n": 0}
    monkeypatch.setattr(
        section,
        "_prompt_apply_hold",
        lambda *a, **kw: call_count.__setitem__("n", call_count["n"] + 1),
    )

    section._on_procedure_tick(_holding_tick())
    assert call_count["n"] == 1

    # Simulate a new run starting — phase="settle" reaches the
    # subscriber before the next hold tick lands.
    section._on_procedure_tick(
        ProcedureTick(
            procedure_id=HEAT_FLUX_TUNE_PROCEDURE_ID,
            t_mono_ns=1,
            payload={"phase": "settle"},
        )
    )
    section._on_procedure_tick(_holding_tick())
    assert call_count["n"] == 2


def test_holding_tick_ignored_for_foreign_procedure_id(qtbot: Any, monkeypatch: Any) -> None:
    """A tick from a different procedure (a future TGA-style procedure,
    say) must not trigger the heat-flux-tune apply prompt — payload
    shapes are procedure-specific and the section only consumes its own."""
    from capa.runtime.emissions import ProcedureTick

    section, _ = _make_section(qtbot)
    called = False

    def _fail(*_a: Any, **_kw: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(section, "_prompt_apply_hold", _fail)

    section._on_procedure_tick(
        ProcedureTick(
            procedure_id="capa.builtin.free_run",
            t_mono_ns=0,
            payload={"phase": "holding", "target_kw_m2": 25.0, "commanded_setpoint_c": 520.0},
        )
    )
    assert called is False
