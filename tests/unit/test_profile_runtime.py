"""Tests for the profile preflight runtime — registry categories and
``adapters_started``-aware silent-channel handling.

Static checks read only config; dynamic
checks observe live samples and must run after ``adapter.start()``. The
silent-channel branches in dynamic checks branch on ``adapters_started``
so a quiet stream is a warning before the engine has opened producers but
a blocking error once they should be running.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from capa.experiment.profiles.runtime import (
    ProfilePreflightContext,
    filter_by_category,
    get_category,
    register,
    registered_ids,
    resolve_preflight_check_ids,
)


def _ctx(adapters_started: bool) -> ProfilePreflightContext:
    """Minimal context for direct check invocation. The mocks satisfy the
    type slots; the dynamic checks under test only touch ``profile_metadata``,
    ``config.domain_profile.id``, ``config.hardware.channels``, and
    ``databus``."""
    config = MagicMock()
    config.domain_profile.id = "capa.profiles.capa_pyrolysis"
    config.hardware.channels = ()  # no channels declared

    databus = MagicMock()
    databus.subscribe.return_value = iter(())  # empty async iter stub

    return ProfilePreflightContext(
        config=config,
        instruments=MagicMock(),
        databus=databus,
        profile_metadata={},
        adapters_started=adapters_started,
    )


def test_register_default_category_is_static() -> None:
    @register("test.runtime.default_category")
    async def _check(ctx: ProfilePreflightContext) -> None:
        return None

    try:
        assert get_category("test.runtime.default_category") == "static"
    finally:
        # Clean up to avoid leaking into other tests.
        from capa.experiment.profiles.runtime import _REGISTRY

        _REGISTRY.pop("test.runtime.default_category", None)


def test_register_dynamic_category_is_recorded() -> None:
    @register("test.runtime.dynamic_marker", category="dynamic")
    async def _check(ctx: ProfilePreflightContext) -> None:
        return None

    try:
        assert get_category("test.runtime.dynamic_marker") == "dynamic"
    finally:
        from capa.experiment.profiles.runtime import _REGISTRY

        _REGISTRY.pop("test.runtime.dynamic_marker", None)


def test_filter_by_category_partitions_known_ids() -> None:
    # The CAPA profile registers both static and dynamic checks; the
    # filter should partition them cleanly.
    all_ids = tuple(i for i in registered_ids() if i.startswith("capa."))
    static_ids = filter_by_category(all_ids, "static")
    dynamic_ids = filter_by_category(all_ids, "dynamic")

    # Disjoint and complete (under the assumption that every registered id
    # has exactly one category).
    assert set(static_ids).isdisjoint(set(dynamic_ids))
    assert set(static_ids) | set(dynamic_ids) == set(all_ids)

    # The three checks that read live samples must be classified dynamic.
    assert "capa.heater_pv_in_safe_range" in dynamic_ids
    assert "capa.purge_flow_established" in dynamic_ids
    assert "capa.balance_stability" in dynamic_ids
    # Pure config/filesystem checks must be classified static.
    assert "capa.required_channel_mappings" in static_ids
    assert "capa.atmosphere_consistency" in static_ids
    assert "capa.leak_test_recency" in static_ids
    assert "capa.disk_projection" in static_ids


def test_resolve_preflight_check_ids_capa_pyrolysis() -> None:
    """The CAPA pyrolysis profile's preflight check ids are resolvable
    via the public helper. Used by the conductor's profile-preflight
    hook to learn which checks the active profile declares without
    importing the profile module at top-level."""
    ids = resolve_preflight_check_ids("capa.profiles.capa_pyrolysis")
    assert "capa.required_channel_mappings" in ids
    assert "capa.heater_pv_in_safe_range" in ids
    assert "capa.purge_flow_established" in ids


def test_resolve_preflight_check_ids_unknown_profile_returns_empty() -> None:
    """An unknown / future profile id returns ``()`` rather than raising
    — mirrors :func:`_resolve_required_groups`'s contract so the
    conductor treats absence as 'no profile-level checks' rather than
    refusing the run."""
    assert resolve_preflight_check_ids("unknown.profile.id") == ()
    assert resolve_preflight_check_ids("") == ()


@pytest.mark.anyio
async def test_purge_silent_warns_before_adapters_started() -> None:
    """Pre-adapter-start, a silent purge-flow channel is a warning, not
    blocking. Mirrors the legacy behavior so plugin-registered checks that
    reuse ``ProfilePreflightContext`` keep working."""
    from capa.experiment.profiles.runtime import _purge_flow_established

    ctx = _ctx(adapters_started=False)
    ctx.profile_metadata = {"atmosphere": {"purge": {"target_flow_sccm": 100.0}}}

    problem = await _purge_flow_established(ctx)
    assert problem is not None
    assert problem.code == "capa.purge_silent"
    assert problem.severity == "warning"
    assert problem.blocking is False


@pytest.mark.anyio
async def test_purge_zero_target_is_explicit_optout() -> None:
    """target_flow_sccm == 0 declares a no-flow run; the check should
    no-op even when adapters are started and no samples arrive."""
    from capa.experiment.profiles.runtime import _purge_flow_established

    ctx = _ctx(adapters_started=True)
    ctx.profile_metadata = {"atmosphere": {"purge": {"target_flow_sccm": 0.0}}}

    problem = await _purge_flow_established(ctx)
    assert problem is None


@pytest.mark.anyio
async def test_purge_silent_blocks_after_adapters_started() -> None:
    """Post-adapter-start, a silent purge-flow channel is a blocking
    error — the device should be streaming and isn't."""
    from capa.experiment.profiles.runtime import _purge_flow_established

    ctx = _ctx(adapters_started=True)
    ctx.profile_metadata = {"atmosphere": {"purge": {"target_flow_sccm": 100.0}}}

    problem = await _purge_flow_established(ctx)
    assert problem is not None
    assert problem.code == "capa.purge_silent"
    assert problem.severity == "error"
    assert problem.blocking is True


@pytest.mark.anyio
async def test_heater_pv_silent_blocks_after_adapters_started() -> None:
    from capa.experiment.profiles.runtime import _heater_pv_safe

    ctx = _ctx(adapters_started=True)

    problem = await _heater_pv_safe(ctx)
    assert problem is not None
    assert problem.code == "capa.heater_pv_silent"
    assert problem.severity == "error"
    assert problem.blocking is True


# ---------------------------------------------------------------------------
# heater_pv hot-start policy
# ---------------------------------------------------------------------------


async def _fake_sample(value_c: float) -> object:
    """Build a ``ChannelSample``-shaped object with the given value.

    Returned by the monkey-patched ``_sample_one`` so the check
    progresses past the silent-channel branch to evaluate the limit
    arithmetic itself."""
    from capa.devices.records import ChannelSample

    return ChannelSample(
        channel="heater.pv",
        t_mono_ns=0,
        t_mono_s=0.0,
        value=value_c,
        unit="degC",
    )


@pytest.mark.anyio
async def test_heater_pv_typical_hot_workflow_passes_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hold-mode workflow arms against a 500-700 °C held heater and
    must pass the default ceiling. At a 1000 °C rig-survival limit, a
    typical CAPA workflow PV (650 °C) is well under the gate — no
    refusal, no hot-start permit fired (setpoint < default)."""
    from capa.experiment.profiles import runtime as runtime_mod

    ctx = _ctx(adapters_started=True)
    ctx.profile_metadata = {"program": {"heater_setpoint_c": 600.0}}

    monkeypatch.setattr(runtime_mod, "_sample_one", lambda *a, **kw: _fake_sample(650.0))
    # No refusal, no info-Problem fired.
    assert await runtime_mod._heater_pv_safe(ctx) is None


@pytest.mark.anyio
async def test_heater_pv_above_rig_survival_ceiling_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PV above the rig-survival ceiling (1000 °C default) refuses.

    Catches the genuine "something is on fire" cases — sensor runaway,
    miswired channel, mechanical failure — that should refuse any arm
    regardless of intended setpoint."""
    from capa.experiment.profiles import runtime as runtime_mod

    ctx = _ctx(adapters_started=True)
    ctx.profile_metadata = {"program": {"heater_setpoint_c": 600.0}}

    monkeypatch.setattr(runtime_mod, "_sample_one", lambda *a, **kw: _fake_sample(1050.0))
    problem = await runtime_mod._heater_pv_safe(ctx)
    assert problem is not None
    assert problem.code == "capa.heater_pv_too_hot"
    assert problem.metadata["limit_c"] == 1000.0
    assert problem.blocking is True


@pytest.mark.anyio
async def test_heater_pv_hot_start_raises_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exotic method declaring ``heater_setpoint_c > 1000`` engages
    the hot-start permit — limit auto-raises to ``setpoint + margin``.

    Rare in practice (the default ceiling already accommodates the full
    CAPA operating range), but the policy still works as a safety net
    if a future high-temp method needs it."""
    from capa.experiment.profiles import runtime as runtime_mod

    ctx = _ctx(adapters_started=True)
    ctx.profile_metadata = {"program": {"heater_setpoint_c": 1100.0}}

    monkeypatch.setattr(runtime_mod, "_sample_one", lambda *a, **kw: _fake_sample(1130.0))
    problem = await runtime_mod._heater_pv_safe(ctx)
    assert problem is not None
    # Hot-start permit surfaces as a non-blocking info Problem rather
    # than passing silently — operator sees the relaxation in the
    # preflight output.
    assert problem.code == "capa.heater_pv_hot_start_permitted"
    assert problem.severity == "info"
    assert problem.blocking is False
    assert problem.metadata["limit_c"] == 1150.0


@pytest.mark.anyio
async def test_heater_pv_hot_start_still_refuses_above_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hot-start permit raises the ceiling but doesn't disable it.

    A 1200 °C PV against a method with ``heater_setpoint_c=1100`` (permit
    ceiling 1150 °C) still refuses — the policy gives headroom, not a
    blank check."""
    from capa.experiment.profiles import runtime as runtime_mod

    ctx = _ctx(adapters_started=True)
    ctx.profile_metadata = {"program": {"heater_setpoint_c": 1100.0}}

    monkeypatch.setattr(runtime_mod, "_sample_one", lambda *a, **kw: _fake_sample(1200.0))
    problem = await runtime_mod._heater_pv_safe(ctx)
    assert problem is not None
    assert problem.code == "capa.heater_pv_too_hot"
    assert problem.metadata["limit_c"] == 1150.0
    assert problem.blocking is True


@pytest.mark.anyio
async def test_heater_pv_explicit_override_wins_over_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``_safe_arm.max_heater_pv_c`` override beats both the
    default ceiling and the hot-start permit.

    Used for diagnostic / new-rig commissioning workflows that want a
    *tighter* gate than the default (e.g. requiring cold start for an
    instrument calibration sweep)."""
    from capa.experiment.profiles import runtime as runtime_mod

    ctx = _ctx(adapters_started=True)
    ctx.profile_metadata = {
        "program": {"heater_setpoint_c": 600.0},
        "_safe_arm": {"max_heater_pv_c": 100.0},  # explicit cold-start gate
    }

    monkeypatch.setattr(runtime_mod, "_sample_one", lambda *a, **kw: _fake_sample(150.0))
    problem = await runtime_mod._heater_pv_safe(ctx)
    assert problem is not None
    assert problem.code == "capa.heater_pv_too_hot"
    assert problem.metadata["limit_c"] == 100.0
    assert problem.blocking is True


@pytest.mark.anyio
async def test_heater_pv_no_program_uses_default_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile_metadata without a ``program`` block falls back to 1000 °C."""
    from capa.experiment.profiles import runtime as runtime_mod

    ctx = _ctx(adapters_started=True)
    ctx.profile_metadata = {}

    # Typical operating PV — passes silently.
    monkeypatch.setattr(runtime_mod, "_sample_one", lambda *a, **kw: _fake_sample(650.0))
    assert await runtime_mod._heater_pv_safe(ctx) is None

    # Above rig-survival ceiling — refuses.
    monkeypatch.setattr(runtime_mod, "_sample_one", lambda *a, **kw: _fake_sample(1050.0))
    problem = await runtime_mod._heater_pv_safe(ctx)
    assert problem is not None
    assert problem.code == "capa.heater_pv_too_hot"
    assert problem.metadata["limit_c"] == 1000.0


@pytest.mark.anyio
async def test_heater_pv_accepts_legacy_heater_program_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hot-start lookup accepts the legacy ``heater_program`` alias.

    The CAPA pyrolysis model field is ``program`` (matches the YAML
    config keys today), but earlier test fixtures and some plugin
    code use ``heater_program``. Both shapes feed the policy so a
    fixture/profile shipping with either key works."""
    from capa.experiment.profiles import runtime as runtime_mod

    ctx = _ctx(adapters_started=True)
    ctx.profile_metadata = {"heater_program": {"heater_setpoint_c": 1100.0}}

    monkeypatch.setattr(runtime_mod, "_sample_one", lambda *a, **kw: _fake_sample(1130.0))
    problem = await runtime_mod._heater_pv_safe(ctx)
    assert problem is not None
    assert problem.code == "capa.heater_pv_hot_start_permitted"
    assert problem.metadata["limit_c"] == 1150.0


# ---------------------------------------------------------------------------
# flux_calibration_freshness preflight gate
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_flux_calibration_freshness_no_target_passes() -> None:
    """A run that doesn't declare a flux target has nothing to verify."""
    from capa.experiment.profiles.runtime import _flux_calibration_freshness

    ctx = _ctx(adapters_started=False)
    ctx.profile_metadata = {"heater_program": {}}

    assert await _flux_calibration_freshness(ctx) is None


@pytest.mark.anyio
async def test_flux_calibration_freshness_target_but_empty_ref_warns() -> None:
    """Target declared, no ref → non-blocking warning prompting a tune."""
    from capa.experiment.profiles.runtime import _flux_calibration_freshness

    ctx = _ctx(adapters_started=False)
    ctx.profile_metadata = {
        "heater_program": {"target_heat_flux_kw_m2": 50.0, "flux_calibration_ref": ""}
    }

    problem = await _flux_calibration_freshness(ctx)
    assert problem is not None
    assert problem.code == "capa.flux_calibration_missing"
    assert problem.severity == "warning"
    assert problem.blocking is False


@pytest.mark.anyio
async def test_flux_calibration_freshness_freeform_ref_passes() -> None:
    """A ref that doesn't resolve to an on-disk artifact is treated as
    operator free-form (lab notebook entry) — pass."""
    from capa.experiment.profiles.runtime import _flux_calibration_freshness

    ctx = _ctx(adapters_started=False)
    ctx.profile_metadata = {
        "heater_program": {
            "target_heat_flux_kw_m2": 50.0,
            "flux_calibration_ref": "lab notebook 2026-05-17 p.43",
        },
        "_flux_calibration_dir": "configs/calibrations/flux",
    }

    assert await _flux_calibration_freshness(ctx) is None


@pytest.mark.anyio
async def test_flux_calibration_freshness_fresh_artifact_passes(tmp_path: Path) -> None:
    """An on-disk artifact within the recency window passes."""
    from datetime import UTC, datetime, timedelta

    from capa.calibration.tune_artifact import (
        HeatFluxTuneArtifact,
        HeatFluxTunePoint,
        save_artifact,
    )
    from capa.experiment.profiles.runtime import _flux_calibration_freshness

    artifact = HeatFluxTuneArtifact(
        id="capa_flux_fresh",
        rig="test_rig",
        heater_device="heater",
        heater_setpoint_channel="heater.setpoint",
        heater_pv_channel="heater.pv",
        flux_channel="heat_flux_gauge",
        geometry="40 mm below heater",
        accepted_at=datetime.now(UTC) - timedelta(days=1),
        procedure_id="capa.builtin.heat_flux_tune",
        procedure_version="0.1.0",
        points=(
            HeatFluxTunePoint(
                target_flux_kw_m2=50.0,
                heater_setpoint_c=650.0,
                measured_flux_mean_kw_m2=50.1,
                measured_flux_std_kw_m2=0.02,
                measured_flux_slope_kw_m2_per_min=0.005,
                heater_pv_mean_c=649.8,
                soak_s=400.0,
                accepted=True,
                accept_reason="algorithm_converged",
            ),
        ),
    )
    save_artifact(artifact, tmp_path)

    ctx = _ctx(adapters_started=False)
    ctx.profile_metadata = {
        "heater_program": {
            "target_heat_flux_kw_m2": 50.0,
            "flux_calibration_ref": "capa_flux_fresh",
        },
        "_flux_calibration_dir": str(tmp_path),
    }

    assert await _flux_calibration_freshness(ctx) is None


@pytest.mark.anyio
async def test_flux_calibration_freshness_stale_artifact_warns(tmp_path: Path) -> None:
    """An on-disk artifact older than the recency window warns (non-blocking)."""
    from datetime import UTC, datetime, timedelta

    from capa.calibration.tune_artifact import (
        HeatFluxTuneArtifact,
        HeatFluxTunePoint,
        save_artifact,
    )
    from capa.experiment.profiles.runtime import _flux_calibration_freshness

    artifact = HeatFluxTuneArtifact(
        id="capa_flux_stale",
        rig="test_rig",
        heater_device="heater",
        heater_setpoint_channel="heater.setpoint",
        heater_pv_channel="heater.pv",
        flux_channel="heat_flux_gauge",
        geometry="40 mm below heater",
        accepted_at=datetime.now(UTC) - timedelta(days=21),
        procedure_id="capa.builtin.heat_flux_tune",
        procedure_version="0.1.0",
        points=(
            HeatFluxTunePoint(
                target_flux_kw_m2=50.0,
                heater_setpoint_c=650.0,
                measured_flux_mean_kw_m2=50.1,
                measured_flux_std_kw_m2=0.02,
                measured_flux_slope_kw_m2_per_min=0.005,
                heater_pv_mean_c=649.8,
                soak_s=400.0,
                accepted=True,
                accept_reason="algorithm_converged",
            ),
        ),
    )
    save_artifact(artifact, tmp_path)

    ctx = _ctx(adapters_started=False)
    ctx.profile_metadata = {
        "heater_program": {
            "target_heat_flux_kw_m2": 50.0,
            "flux_calibration_ref": "capa_flux_stale",
        },
        "_flux_calibration_dir": str(tmp_path),
        "_flux_calibration_window_days": 7,
    }

    problem = await _flux_calibration_freshness(ctx)
    assert problem is not None
    assert problem.code == "capa.flux_calibration_stale"
    assert problem.severity == "warning"
    assert problem.blocking is False
    assert problem.metadata["age_days"] >= 14
