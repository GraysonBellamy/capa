"""Tests for :mod:`capa.experiment.procedures.builtin.heat_flux_tune`.

Focused on the pure-function building blocks (Hampel mask, rolling
statistics, steady-state predicate, secant step, runaway detector,
initial-guess chooser). The async control loop is exercised via a sim
integration test (separate module).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from capa.calibration.tune_artifact import HeatFluxTuneArtifact, HeatFluxTunePoint
from capa.experiment.procedures.builtin.heat_flux_tune.config import (
    PROCEDURE_ID,
    PROCEDURE_VERSION,
    HeatFluxTuneConfig,
)
from capa.experiment.procedures.builtin.heat_flux_tune.controller import HeatFluxTune
from capa.experiment.procedures.builtin.heat_flux_tune.setpoint import (
    choose_initial_setpoint,
    default_tolerance_kw_m2,
    predicate_strictness,
    sigma_t4_setpoint_c,
)
from capa.experiment.procedures.builtin.heat_flux_tune.signals import (
    RollingWindow,
    RunawayDetector,
    SteadyStatePredicate,
    hampel_mask,
    linear_slope_per_min,
    secant_step,
)

# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_config_requires_at_least_one_target() -> None:
    with pytest.raises(Exception):
        HeatFluxTuneConfig.model_validate({"targets_kw_m2": [], "t_set_max_c": 900.0})


def test_config_rejects_safe_above_ceiling() -> None:
    with pytest.raises(ValueError, match="t_safe_c"):
        HeatFluxTuneConfig.model_validate(
            {"targets_kw_m2": [50.0], "t_set_max_c": 100.0, "t_safe_c": 200.0}
        )


def test_config_rejects_nonpositive_target() -> None:
    with pytest.raises(ValueError, match="target_kw_m2"):
        HeatFluxTuneConfig.model_validate({"targets_kw_m2": [50.0, 0.0], "t_set_max_c": 900.0})


def test_config_operator_initial_requires_value() -> None:
    with pytest.raises(ValueError, match="operator_initial_setpoint_c"):
        HeatFluxTuneConfig.model_validate(
            {
                "targets_kw_m2": [50.0],
                "t_set_max_c": 900.0,
                "initial_guess": "operator",
            }
        )


def test_config_defaults_are_sensible() -> None:
    cfg = HeatFluxTuneConfig.model_validate({"targets_kw_m2": [50.0], "t_set_max_c": 900.0})
    assert cfg.t_settle_max_s == 1200.0
    assert cfg.tolerance_kw_m2 is None  # use default_tolerance_kw_m2
    assert cfg.initial_guess == "lookup"
    assert cfg.persist_dir == "configs/calibrations/flux"
    assert cfg.hold_at_completion is False  # opt-in per session


def test_config_hold_default_false_keeps_legacy_cool() -> None:
    """Default cool-on-completion behavior is unchanged for any config
    that doesn't opt in. Regression for the most common path."""
    cfg = HeatFluxTuneConfig.model_validate({"targets_kw_m2": [50.0], "t_set_max_c": 900.0})
    assert cfg.hold_at_completion is False


def test_config_hold_multi_target_rejected() -> None:
    """Holding at the highest target of a calibration-curve session is
    almost never the intent. The validator refuses at parse time so
    the operator sees the error before the session runs."""
    with pytest.raises(ValueError, match="hold_at_completion"):
        HeatFluxTuneConfig.model_validate(
            {
                "targets_kw_m2": [25.0, 50.0, 75.0],
                "t_set_max_c": 900.0,
                "hold_at_completion": True,
            }
        )


def test_config_hold_single_target_allowed() -> None:
    """The dominant CAPA workflow (single target per session) accepts hold."""
    cfg = HeatFluxTuneConfig.model_validate(
        {
            "targets_kw_m2": [50.0],
            "t_set_max_c": 900.0,
            "hold_at_completion": True,
        }
    )
    assert cfg.hold_at_completion is True


# ---------------------------------------------------------------------------
# Hold mode — `_should_hold` gate
# ---------------------------------------------------------------------------


def _hold_proc(hold: bool = True) -> HeatFluxTune:
    """Build a HeatFluxTune procedure with a single 50 kW/m² target."""
    return HeatFluxTune.from_config(
        {
            "targets_kw_m2": [50.0],
            "t_set_max_c": 900.0,
            "hold_at_completion": hold,
        }
    )


def _make_point(*, accepted: bool, accept_reason: str = "algorithm_converged") -> HeatFluxTunePoint:
    return HeatFluxTunePoint(
        target_flux_kw_m2=50.0,
        heater_setpoint_c=520.0,
        measured_flux_mean_kw_m2=50.05,
        measured_flux_std_kw_m2=0.05,
        measured_flux_slope_kw_m2_per_min=0.0,
        heater_pv_mean_c=519.8,
        soak_s=300.0,
        accepted=accepted,
        accept_reason=accept_reason,
    )


def test_should_hold_requires_completed_all_targets() -> None:
    """A run that aborted mid-session (completed_all_targets=False) must
    cool, even if hold_at_completion=True. Guards the doc's named bug:
    the `break` paths must not leak past the hold gate."""
    proc = _hold_proc(hold=True)
    proc._accepted_points.append(_make_point(accepted=True))
    assert proc._should_hold(completed_all_targets=False) is False


def test_should_hold_requires_opt_in() -> None:
    """Without ``hold_at_completion=True`` the gate refuses — default
    cool behavior is preserved."""
    proc = _hold_proc(hold=False)
    proc._accepted_points.append(_make_point(accepted=True))
    assert proc._should_hold(completed_all_targets=True) is False


def test_should_hold_requires_non_empty_points() -> None:
    """Defensive: no point appended → nothing to hold at."""
    proc = _hold_proc(hold=True)
    assert proc._should_hold(completed_all_targets=True) is False


def test_should_hold_refuses_warn_proceeded_last_point() -> None:
    """Holding at a ``warn_proceeded`` SP would leave the operator with
    a hot heater whose tuned value the artifact's setpoint_for_target
    refuses to re-surface (it filters non-accepted points). Cool
    instead and let them re-tune — the cost is one warm-up, the
    alternative is a broken handoff."""
    proc = _hold_proc(hold=True)
    proc._accepted_points.append(_make_point(accepted=False, accept_reason="warn_proceeded"))
    assert proc._should_hold(completed_all_targets=True) is False


def test_should_hold_accepts_clean_completion() -> None:
    """Happy path: opt-in, completed, point accepted → hold."""
    proc = _hold_proc(hold=True)
    proc._accepted_points.append(_make_point(accepted=True))
    assert proc._should_hold(completed_all_targets=True) is True


def test_should_hold_accepts_operator_override_point() -> None:
    """An operator-accepted point is still ``accepted=True``; holding
    on it is intentional behavior, not a corner case."""
    proc = _hold_proc(hold=True)
    proc._accepted_points.append(_make_point(accepted=True, accept_reason="operator_override"))
    assert proc._should_hold(completed_all_targets=True) is True


# ---------------------------------------------------------------------------
# Hold mode — holding tick payload
# ---------------------------------------------------------------------------


def test_emit_holding_tick_publishes_phase_holding(qtbot_unused: object = None) -> None:
    """The final tick on a hold path carries ``phase="holding"`` with
    the held SP / target / measured-flux / accept-reason. The dock's
    update_from_tick path is what makes it visible — pinning the
    payload contract guards against silent dock breakage on later
    payload-shape changes."""
    from unittest.mock import MagicMock

    proc = _hold_proc(hold=True)
    proc._accepted_points.append(_make_point(accepted=True, accept_reason="algorithm_converged"))
    sink = MagicMock()
    ctx = MagicMock()
    ctx.ui_sink = sink
    ctx.clock.t_mono_ns.return_value = 12345

    proc._emit_holding_tick(ctx)

    assert sink.publish.call_count == 1
    tick = sink.publish.call_args[0][0]
    assert tick.payload["phase"] == "holding"
    assert tick.payload["commanded_setpoint_c"] == 520.0
    assert tick.payload["target_kw_m2"] == 50.0
    assert tick.payload["mean_flux_kw_m2"] == 50.05
    assert tick.payload["accept_reason"] == "algorithm_converged"


def test_emit_holding_tick_silent_with_no_sink() -> None:
    """Headless / test paths without a UI sink keep working — the
    helper degrades to a no-op rather than raising."""
    from unittest.mock import MagicMock

    proc = _hold_proc(hold=True)
    proc._accepted_points.append(_make_point(accepted=True))
    ctx = MagicMock()
    ctx.ui_sink = None
    ctx.clock.t_mono_ns.return_value = 0

    # Returns None — assertion is "did not raise".
    proc._emit_holding_tick(ctx)


# ---------------------------------------------------------------------------
# hampel_mask
# ---------------------------------------------------------------------------


def test_hampel_mask_passes_clean_signal() -> None:
    # Tight cluster representative of post-Hampel S-B gauge noise
    # (~0.01 kW/m² 1-sigma). With k=3 and a Gaussian-consistent
    # threshold of 3 * 1.4826 * MAD, every sample stays in.
    vals = [10.0, 10.01, 9.99, 10.02, 9.98, 10.01, 10.0, 9.99, 10.0, 10.01, 9.99]
    mask = hampel_mask(vals)
    assert all(mask), f"clean signal should not be filtered: {list(zip(vals, mask, strict=True))}"


def test_hampel_mask_rejects_isolated_spike() -> None:
    vals = [10.0, 10.01, 9.99, 10.02, 9.98, 50.0, 10.0, 9.99, 10.01, 10.0, 10.0]
    mask = hampel_mask(vals)
    assert mask[5] is False, "the 50.0 spike must be rejected"
    assert mask.count(True) == len(vals) - 1


def test_hampel_mask_rejects_outlier_in_constant_signal() -> None:
    """MAD-zero edge case: 29 identical samples + 1 huge spike."""
    vals = [10.0] * 29 + [999.0]
    mask = hampel_mask(vals)
    assert mask[-1] is False
    assert mask.count(True) == 29


def test_hampel_mask_rejects_non_finite() -> None:
    vals = [10.0, 10.1, float("nan"), 10.05, float("inf")]
    mask = hampel_mask(vals)
    assert mask[2] is False
    assert mask[4] is False


def test_hampel_mask_short_window_keeps_finite_only() -> None:
    assert hampel_mask([1.0, 2.0]) == [True, True]
    assert hampel_mask([1.0, float("nan")]) == [True, False]


def test_hampel_mask_zero_mad_keeps_everything() -> None:
    # All identical -> MAD = 0; the filter must not reject anything
    mask = hampel_mask([5.0] * 10)
    assert all(mask)


# ---------------------------------------------------------------------------
# linear_slope_per_min
# ---------------------------------------------------------------------------


def test_linear_slope_constant_signal_is_zero() -> None:
    samples = [(float(t), 10.0) for t in range(0, 30, 1)]
    assert linear_slope_per_min(samples) == pytest.approx(0.0, abs=1e-9)


def test_linear_slope_one_per_minute() -> None:
    # value rises 1 unit per 60 s -> slope_per_min == 1.0
    samples = [(float(t), t / 60.0) for t in range(0, 120, 5)]
    assert linear_slope_per_min(samples) == pytest.approx(1.0, rel=1e-9)


def test_linear_slope_empty_or_single() -> None:
    assert linear_slope_per_min([]) == 0.0
    assert linear_slope_per_min([(0.0, 5.0)]) == 0.0


def test_linear_slope_duplicate_timestamps_is_zero() -> None:
    assert linear_slope_per_min([(1.0, 2.0), (1.0, 5.0)]) == 0.0


# ---------------------------------------------------------------------------
# RollingWindow
# ---------------------------------------------------------------------------


def test_rolling_window_evicts_old_samples() -> None:
    w = RollingWindow(window_s=10.0)
    for t in range(0, 20):
        w.push(float(t), 5.0)
    # newest is t=19; cutoff is 19 - 10 = 9; samples strictly older
    # than 9 are dropped, leaving t in [9, 19] -> 11 samples.
    assert w.count() == 11
    assert w.span_s() == pytest.approx(10.0)


def test_rolling_window_is_warm_handles_sample_period_boundary() -> None:
    # At a realistic rate (5 Hz -> 0.2 s period) the rolling window's
    # span peaks at window_s - period (eviction removes the sample at
    # the cutoff so the oldest retained sample sits one period inside).
    # A naive ``span >= window_s`` check therefore never fires; is_warm
    # uses the in-window average period as the slop.
    w = RollingWindow(window_s=60.0)
    t = 0.0
    while t <= 120.0:
        w.push(t, 50.0)
        t += 0.2
    assert w.span_s() < 60.0  # span tops out at 59.8 s
    assert w.is_warm(60.0) is True


def test_rolling_window_is_warm_false_during_warmup() -> None:
    w = RollingWindow(window_s=60.0)
    for t in (0.0, 0.2, 0.4, 0.6, 0.8):
        w.push(t, 50.0)
    # 5 samples spanning 0.8 s + ~0.2 s of slop is still far below 60.
    assert w.is_warm(60.0) is False


def test_rolling_window_is_warm_false_with_under_two_samples() -> None:
    w = RollingWindow(window_s=60.0)
    assert w.is_warm(60.0) is False
    w.push(0.0, 50.0)
    assert w.is_warm(60.0) is False


def test_rolling_window_clear_drops_all_samples() -> None:
    w = RollingWindow(window_s=60.0)
    for t in range(0, 30):
        w.push(float(t), 50.0)
    assert w.count() == 30
    w.clear()
    assert w.count() == 0
    assert w.span_s() == 0.0
    assert w.is_warm(60.0) is False
    # Window stays usable post-clear — push refills it as usual.
    w.push(100.0, 49.0)
    assert w.count() == 1


def test_rolling_window_mean_std_slope_on_clean_signal() -> None:
    w = RollingWindow(window_s=30.0)
    for t in range(0, 31):
        w.push(float(t), 50.0)
    assert w.mean() == pytest.approx(50.0)
    assert w.std() == pytest.approx(0.0)
    assert w.slope_per_min() == pytest.approx(0.0)


def test_rolling_window_slope_with_drift() -> None:
    w = RollingWindow(window_s=60.0)
    for t in range(0, 61):
        # 1 kW/m² per 60 s == 1 kW/m² per min
        w.push(float(t), 0.01 * t)
    slope = w.slope_per_min()
    # rise is 0.6 over 60 s == 0.6 per min
    assert slope == pytest.approx(0.6, rel=1e-9)


def test_rolling_window_rejects_non_finite_at_boundary() -> None:
    w = RollingWindow(window_s=10.0)
    w.push(0.0, 5.0)
    w.push(1.0, float("nan"))
    w.push(2.0, float("inf"))
    w.push(3.0, 5.5)
    assert w.count() == 2


def test_rolling_window_spike_does_not_corrupt_mean() -> None:
    w = RollingWindow(window_s=30.0, hampel_k=3.0)
    for t in range(0, 30):
        w.push(float(t), 10.0)
    w.push(30.0, 999.0)  # huge outlier
    # Hampel must reject 999; mean should be ~10.0
    assert w.mean() == pytest.approx(10.0, abs=0.5)


# ---------------------------------------------------------------------------
# SteadyStatePredicate
# ---------------------------------------------------------------------------


def test_predicate_fires_when_all_conditions_hold_for_t_stable() -> None:
    pred = SteadyStatePredicate(
        delta_t_band_c=1.0,
        sigma_flux_max=0.1,
        slope_max_kw_per_min=0.02,
        t_stable_s=10.0,
    )
    for now in [0.0, 1.0, 5.0]:
        pred.evaluate(
            now_s=now,
            pv_mean_c=500.0,
            setpoint_c=500.0,
            flux_std_kw_m2=0.05,
            flux_slope_kw_per_min=0.0,
            window_full=True,
        )
    assert pred.fired(5.0) is False  # 5 s < 10 s hold
    pred.evaluate(
        now_s=11.0,
        pv_mean_c=500.0,
        setpoint_c=500.0,
        flux_std_kw_m2=0.05,
        flux_slope_kw_per_min=0.0,
        window_full=True,
    )
    assert pred.fired(11.0) is True  # 11 - 0 == 11 >= 10


def test_predicate_resets_on_pv_band_violation() -> None:
    pred = SteadyStatePredicate(
        delta_t_band_c=1.0,
        sigma_flux_max=0.1,
        slope_max_kw_per_min=0.02,
        t_stable_s=10.0,
    )
    pred.evaluate(
        now_s=0.0,
        pv_mean_c=500.0,
        setpoint_c=500.0,
        flux_std_kw_m2=0.05,
        flux_slope_kw_per_min=0.0,
        window_full=True,
    )
    pred.evaluate(
        now_s=5.0,
        pv_mean_c=510.0,
        setpoint_c=500.0,  # 10 °C off!
        flux_std_kw_m2=0.05,
        flux_slope_kw_per_min=0.0,
        window_full=True,
    )
    assert pred.last_reason == "pv-out-of-band"
    assert pred.fired(20.0) is False


def test_predicate_resets_on_slow_drift() -> None:
    """The slope gate catches monotonic drift that variance-alone misses."""
    pred = SteadyStatePredicate(
        delta_t_band_c=1.0,
        sigma_flux_max=0.1,
        slope_max_kw_per_min=0.02,
        t_stable_s=5.0,
    )
    pred.evaluate(
        now_s=0.0,
        pv_mean_c=500.0,
        setpoint_c=500.0,
        flux_std_kw_m2=0.05,
        flux_slope_kw_per_min=0.1,  # drifting at 0.1 per min > 0.02 cap
        window_full=True,
    )
    assert pred.last_reason == "flux-drifting"
    assert pred.fired(10.0) is False


def test_predicate_does_not_fire_until_window_full() -> None:
    pred = SteadyStatePredicate(
        delta_t_band_c=1.0,
        sigma_flux_max=0.1,
        slope_max_kw_per_min=0.02,
        t_stable_s=1.0,
    )
    pred.evaluate(
        now_s=0.0,
        pv_mean_c=500.0,
        setpoint_c=500.0,
        flux_std_kw_m2=0.05,
        flux_slope_kw_per_min=0.0,
        window_full=False,
    )
    assert pred.last_reason == "window-not-full"
    assert pred.fired(100.0) is False


# ---------------------------------------------------------------------------
# secant_step
# ---------------------------------------------------------------------------


def test_secant_step_simple_case() -> None:
    # err = 5 kW/m², dF/dT = 0.2 → raw ΔT = 25; damping 1.0 → 25; max 50 → 25
    out = secant_step(err_kw_m2=5.0, df_dt_kw_m2_per_c=0.2, damping=1.0, delta_t_step_max_c=50.0)
    assert out == pytest.approx(25.0)


def test_secant_step_damping_reduces_magnitude() -> None:
    out = secant_step(err_kw_m2=5.0, df_dt_kw_m2_per_c=0.2, damping=0.7, delta_t_step_max_c=50.0)
    assert out == pytest.approx(17.5)


def test_secant_step_clamps_to_step_max() -> None:
    out = secant_step(err_kw_m2=100.0, df_dt_kw_m2_per_c=0.2, damping=1.0, delta_t_step_max_c=25.0)
    assert out == 25.0
    out_neg = secant_step(
        err_kw_m2=-100.0, df_dt_kw_m2_per_c=0.2, damping=1.0, delta_t_step_max_c=25.0
    )
    assert out_neg == -25.0


def test_secant_step_zero_when_slope_invalid() -> None:
    assert (
        secant_step(err_kw_m2=10.0, df_dt_kw_m2_per_c=0.0, damping=0.7, delta_t_step_max_c=25.0)
        == 0.0
    )
    assert (
        secant_step(err_kw_m2=10.0, df_dt_kw_m2_per_c=-0.5, damping=0.7, delta_t_step_max_c=25.0)
        == 0.0
    )
    assert (
        secant_step(
            err_kw_m2=10.0,
            df_dt_kw_m2_per_c=float("nan"),
            damping=0.7,
            delta_t_step_max_c=25.0,
        )
        == 0.0
    )


# ---------------------------------------------------------------------------
# RunawayDetector
# ---------------------------------------------------------------------------


def test_runaway_detector_trips_after_threshold_disagreements() -> None:
    rd = RunawayDetector(trip_threshold=3)
    # err > 0 means flux too low → ΔT should be > 0. Wrong-sign ΔT = -5.
    rd.record(err_kw_m2=5.0, delta_t_c=-5.0)
    rd.record(err_kw_m2=5.0, delta_t_c=-5.0)
    assert rd.tripped() is False
    rd.record(err_kw_m2=5.0, delta_t_c=-5.0)
    assert rd.tripped() is True
    assert rd.count == 3


def test_runaway_detector_resets_on_correct_direction() -> None:
    rd = RunawayDetector(trip_threshold=3)
    rd.record(err_kw_m2=5.0, delta_t_c=-5.0)
    rd.record(err_kw_m2=5.0, delta_t_c=-5.0)
    rd.record(err_kw_m2=5.0, delta_t_c=5.0)  # correct direction → reset
    assert rd.count == 0
    assert rd.tripped() is False


def test_runaway_detector_ignores_zero_steps() -> None:
    rd = RunawayDetector(trip_threshold=2)
    rd.record(err_kw_m2=5.0, delta_t_c=0.0)
    rd.record(err_kw_m2=5.0, delta_t_c=0.0)
    rd.record(err_kw_m2=0.0, delta_t_c=5.0)
    assert rd.tripped() is False


# ---------------------------------------------------------------------------
# choose_initial_setpoint
# ---------------------------------------------------------------------------


def _artifact_with_points() -> HeatFluxTuneArtifact:
    pts = (
        HeatFluxTunePoint(
            target_flux_kw_m2=25.0,
            heater_setpoint_c=480.0,
            measured_flux_mean_kw_m2=25.0,
            measured_flux_std_kw_m2=0.02,
            measured_flux_slope_kw_m2_per_min=0.0,
            heater_pv_mean_c=480.0,
            soak_s=600.0,
            accepted=True,
            accept_reason="algorithm_converged",
        ),
        HeatFluxTunePoint(
            target_flux_kw_m2=75.0,
            heater_setpoint_c=740.0,
            measured_flux_mean_kw_m2=75.0,
            measured_flux_std_kw_m2=0.02,
            measured_flux_slope_kw_m2_per_min=0.0,
            heater_pv_mean_c=740.0,
            soak_s=600.0,
            accepted=True,
            accept_reason="algorithm_converged",
        ),
    )
    return HeatFluxTuneArtifact(
        id="x",
        rig="r",
        heater_device="d",
        heater_setpoint_channel="heater.setpoint",
        heater_pv_channel="heater.pv",
        flux_channel="heat_flux_gauge",
        geometry="g",
        accepted_at=datetime(2026, 5, 17, tzinfo=UTC),
        procedure_id="p",
        procedure_version="v",
        points=pts,
    )


def test_choose_initial_lookup_in_bracket() -> None:
    art = _artifact_with_points()
    sp, source = choose_initial_setpoint(
        target_kw_m2=50.0,
        source="lookup",
        operator_setpoint_c=None,
        prior_artifact=art,
        t_min_c=25.0,
        t_set_max_c=900.0,
    )
    # 50 is the midpoint of (25,480)-(75,740) → 610
    assert source == "lookup"
    assert sp == pytest.approx(610.0)


def test_choose_initial_lookup_outside_bracket_falls_to_sigma_t4() -> None:
    art = _artifact_with_points()
    sp, source = choose_initial_setpoint(
        target_kw_m2=10.0,  # below bracket
        source="lookup",
        operator_setpoint_c=None,
        prior_artifact=art,
        t_min_c=25.0,
        t_set_max_c=900.0,
    )
    assert source == "sigma_t4"
    assert sp > 25.0


def test_choose_initial_operator_overrides_when_set() -> None:
    sp, source = choose_initial_setpoint(
        target_kw_m2=50.0,
        source="operator",
        operator_setpoint_c=600.0,
        prior_artifact=None,
        t_min_c=25.0,
        t_set_max_c=900.0,
    )
    assert source == "operator"
    assert sp == 600.0


def test_choose_initial_clamps_to_ceiling() -> None:
    sp, source = choose_initial_setpoint(
        target_kw_m2=50.0,
        source="operator",
        operator_setpoint_c=9999.0,
        prior_artifact=None,
        t_min_c=25.0,
        t_set_max_c=900.0,
    )
    assert source == "operator"
    assert sp == 900.0


def test_choose_initial_cold_start_uses_sigma_t4() -> None:
    sp, source = choose_initial_setpoint(
        target_kw_m2=50.0,
        source="lookup",
        operator_setpoint_c=None,
        prior_artifact=None,
        t_min_c=25.0,
        t_set_max_c=900.0,
    )
    assert source == "sigma_t4"
    # at the anchor point sigma_t4 should give ~650 °C
    assert 600.0 < sp < 700.0


# ---------------------------------------------------------------------------
# default_tolerance + sigma_t4
# ---------------------------------------------------------------------------


def test_default_tolerance_floor_and_scaling() -> None:
    # max(0.1, 0.005 * target): floor wins at low targets, scaling at high.
    assert default_tolerance_kw_m2(10.0) == pytest.approx(0.1)
    assert default_tolerance_kw_m2(20.0) == pytest.approx(0.1)
    assert default_tolerance_kw_m2(50.0) == pytest.approx(0.25)
    assert default_tolerance_kw_m2(100.0) == pytest.approx(0.5)


def test_sigma_t4_monotonic_in_target() -> None:
    a = sigma_t4_setpoint_c(25.0)
    b = sigma_t4_setpoint_c(50.0)
    c = sigma_t4_setpoint_c(75.0)
    assert a < b < c
    # rough order-of-magnitude check against the anchor (50 kW/m² at 650 °C)
    assert math.isfinite(b)
    assert 600.0 < b < 700.0


# ---------------------------------------------------------------------------
# predicate_strictness — distance-based predicate relaxation
# ---------------------------------------------------------------------------


def test_predicate_strictness_far_returns_relax_factor() -> None:
    # |err| = 20 vs target 50 → 40% off, well past the 30% far threshold.
    k = predicate_strictness(
        err_kw_m2=-20.0,
        target_kw_m2=50.0,
        tolerance_kw_m2=1.0,
        relax_factor=3.0,
    )
    assert k == pytest.approx(3.0)


def test_predicate_strictness_near_returns_one() -> None:
    # |err| = 1.5 vs tolerance 1.0 → inside the 2·tol "near" band.
    k = predicate_strictness(
        err_kw_m2=1.5,
        target_kw_m2=50.0,
        tolerance_kw_m2=1.0,
        relax_factor=3.0,
    )
    assert k == pytest.approx(1.0)


def test_predicate_strictness_interpolates_between() -> None:
    # near = 2.0, far = 15.0 → at err = 8.5 we're exactly halfway.
    k = predicate_strictness(
        err_kw_m2=8.5,
        target_kw_m2=50.0,
        tolerance_kw_m2=1.0,
        relax_factor=3.0,
    )
    assert k == pytest.approx(2.0)


def test_predicate_strictness_relax_factor_one_disables() -> None:
    k = predicate_strictness(
        err_kw_m2=100.0,
        target_kw_m2=50.0,
        tolerance_kw_m2=1.0,
        relax_factor=1.0,
    )
    assert k == 1.0


def test_predicate_strictness_inf_err_is_fully_relaxed() -> None:
    # Iter 1 has no prior err; caller passes inf → relax_factor.
    k = predicate_strictness(
        err_kw_m2=float("inf"),
        target_kw_m2=50.0,
        tolerance_kw_m2=1.0,
        relax_factor=3.0,
    )
    assert k == pytest.approx(3.0)


def test_predicate_strictness_handles_tiny_target() -> None:
    # If target is so small that 0.3·target < 2·tol, the function should
    # still return a monotonic k without divide-by-zero; near < far must
    # be enforced internally.
    k_far = predicate_strictness(
        err_kw_m2=10.0,
        target_kw_m2=1.0,  # 0.3 * 1 = 0.3 < 2 * 0.5 = 1.0
        tolerance_kw_m2=0.5,
        relax_factor=3.0,
    )
    assert k_far == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Procedure-level class invariants (no async)
# ---------------------------------------------------------------------------


def test_from_config_validates_pydantic_payload() -> None:
    proc = HeatFluxTune.from_config({"targets_kw_m2": [25.0, 50.0], "t_set_max_c": 900.0})
    assert proc.cfg.targets_kw_m2 == (25.0, 50.0)
    assert proc.cfg.t_set_max_c == 900.0


def test_procedure_classvars_match_entry_point() -> None:
    assert HeatFluxTune.id == PROCEDURE_ID
    assert HeatFluxTune.version == PROCEDURE_VERSION
    assert HeatFluxTune.config_model is HeatFluxTuneConfig
    names = {req.name for req in HeatFluxTune.required_channels}
    assert names == {"heat_flux_gauge", "heater.setpoint", "heater.pv"}


def test_from_config_with_no_dict_uses_defaults_under_constructor() -> None:
    # `targets_kw_m2` is required → passing `{}` must fail Pydantic
    with pytest.raises(Exception):
        HeatFluxTune.from_config({})


def test_t_set_max_above_ceiling_fails_preflight_path_via_pydantic() -> None:
    # The preflight check is also a runtime check inside the procedure;
    # config-level Pydantic accepts any positive number, so the
    # > 1000 °C value lives until preflight surfaces the Problem.
    cfg = HeatFluxTuneConfig.model_validate({"targets_kw_m2": [50.0], "t_set_max_c": 1500.0})
    assert cfg.t_set_max_c == 1500.0  # not blocked at the model


# ---------------------------------------------------------------------------
# Operator-command handling
# ---------------------------------------------------------------------------


def test_handle_operator_command_pause_sets_paused() -> None:
    from unittest.mock import MagicMock

    from capa.experiment.procedures.base import OperatorCommand

    proc = HeatFluxTune.from_config({"targets_kw_m2": [50.0], "t_set_max_c": 900.0})
    ctx = MagicMock()
    ctx.clock.t_mono_ns.return_value = 0

    assert proc._paused is False
    proc._handle_operator_command(ctx, OperatorCommand(kind="pause"))
    assert proc._paused is True
    # And resume clears it
    proc._handle_operator_command(ctx, OperatorCommand(kind="resume"))
    assert proc._paused is False


def test_handle_operator_command_accept_current_sets_oneshot_flag() -> None:
    from unittest.mock import MagicMock

    from capa.experiment.procedures.base import OperatorCommand

    proc = HeatFluxTune.from_config({"targets_kw_m2": [50.0], "t_set_max_c": 900.0})
    ctx = MagicMock()
    ctx.clock.t_mono_ns.return_value = 0

    assert proc._accept_now is False
    proc._handle_operator_command(ctx, OperatorCommand(kind="accept_current"))
    assert proc._accept_now is True


def test_handle_operator_command_unknown_kind_logs_and_no_state_change() -> None:
    from unittest.mock import MagicMock

    from capa.experiment.procedures.base import OperatorCommand

    proc = HeatFluxTune.from_config({"targets_kw_m2": [50.0], "t_set_max_c": 900.0})
    ctx = MagicMock()
    ctx.clock.t_mono_ns.return_value = 0
    # Bypass the OperatorCommand kind literal check by constructing the
    # dataclass and overriding via object.__setattr__ (frozen dataclass).
    cmd = OperatorCommand(kind="pause")
    object.__setattr__(cmd, "kind", "something_made_up")

    proc._handle_operator_command(ctx, cmd)
    assert proc._paused is False
    assert proc._accept_now is False
    ctx.logger.warning.assert_called()


@pytest.mark.anyio
async def test_consume_operator_commands_translates_stream_to_state() -> None:
    """A live anyio memory stream drives ``_paused`` / ``_accept_now``."""
    from unittest.mock import MagicMock

    import anyio

    from capa.experiment.procedures.base import OperatorCommand

    proc = HeatFluxTune.from_config({"targets_kw_m2": [50.0], "t_set_max_c": 900.0})
    send_stream, recv_stream = anyio.create_memory_object_stream[OperatorCommand](max_buffer_size=4)
    ctx = MagicMock()
    ctx.operator_commands = recv_stream
    ctx.clock.t_mono_ns.return_value = 0

    async with anyio.create_task_group() as tg:
        tg.start_soon(proc._consume_operator_commands, ctx)

        await send_stream.send(OperatorCommand(kind="pause"))
        # Yield until the consumer has processed it.
        for _ in range(50):
            await anyio.sleep(0)
            if proc._paused:
                break
        assert proc._paused is True

        await send_stream.send(OperatorCommand(kind="accept_current"))
        for _ in range(50):
            await anyio.sleep(0)
            if proc._accept_now:
                break
        assert proc._accept_now is True

        await send_stream.aclose()


# ---------------------------------------------------------------------------
# ProcedureTick live numerics
# ---------------------------------------------------------------------------


def _build_state_with_samples() -> object:
    """Construct a ``_SessionState`` populated with enough samples that
    the rolling-window statistics are non-trivial. Returned typed as
    ``object`` to keep the private import inside the helper rather
    than in the test file's top-level namespace."""
    from capa.experiment.procedures.builtin.heat_flux_tune.controller import _SessionState
    from capa.experiment.procedures.builtin.heat_flux_tune.signals import RollingWindow

    state = _SessionState(
        flux_window=RollingWindow(window_s=10.0),
        pv_window=RollingWindow(window_s=10.0),
        target_index=2,
        target_count=3,
        target_kw_m2=50.0,
        tolerance_kw_m2=1.0,
        iteration=4,
        iteration_max=10,
        commanded_setpoint_c=712.0,
        in_tol_windows=1,
        sigma_max_kw_m2=0.5,
        slope_max_kw_m2_per_min=0.15,
        df_dt_used=0.073,
        df_dt_source="secant",
    )
    state.pv_latest = 710.0
    for t, v in [(0.0, 49.8), (1.0, 50.1), (2.0, 49.9), (3.0, 50.0), (4.0, 50.05)]:
        state.flux_window.push(t, v)
        state.pv_window.push(t, 710.0)
    state.iterations.append((712.0, 50.0))
    return state


def test_emit_tick_no_sink_is_silent_noop() -> None:
    """No UI attached (headless / test) — _emit_tick must not raise
    and must not try to construct a tick. The procedure must keep
    running on the headless path."""
    from unittest.mock import MagicMock

    proc = HeatFluxTune.from_config({"targets_kw_m2": [50.0], "t_set_max_c": 900.0})
    ctx = MagicMock()
    ctx.ui_sink = None
    ctx.clock.t_mono_ns.return_value = 0

    state = _build_state_with_samples()
    # Returns None — assertion is "did not raise".
    proc._emit_tick(
        ctx,
        state=state,  # type: ignore[arg-type]
        predicate=None,
        phase="settle",
        elapsed_s=0.0,
        settle_budget_s=1500.0,
    )


def test_emit_tick_payload_carries_full_state() -> None:
    """The payload published to the sink reflects state + predicate.

    Pins the schema the dock parses against — adding a new field is
    fine, removing one is a breaking change.
    """
    from unittest.mock import MagicMock

    from capa.experiment.procedures.builtin.heat_flux_tune.signals import SteadyStatePredicate

    proc = HeatFluxTune.from_config({"targets_kw_m2": [50.0], "t_set_max_c": 900.0})
    sink = MagicMock()
    ctx = MagicMock()
    ctx.ui_sink = sink
    ctx.clock.t_mono_ns.return_value = 12345

    predicate = SteadyStatePredicate(
        delta_t_band_c=1.5,
        sigma_flux_max=0.5,
        slope_max_kw_per_min=0.15,
        t_stable_s=90.0,
    )
    state = _build_state_with_samples()

    proc._emit_tick(
        ctx,
        state=state,  # type: ignore[arg-type]
        predicate=predicate,
        phase="settle",
        elapsed_s=12.0,
        settle_budget_s=1500.0,
    )

    sink.publish.assert_called_once()
    tick = sink.publish.call_args.args[0]
    assert tick.procedure_id == "capa.builtin.heat_flux_tune"
    assert tick.t_mono_ns == 12345
    payload = tick.payload
    # Phase, progress, and budget fields.
    assert payload["phase"] == "settle"
    assert payload["target_index"] == 2
    assert payload["target_count"] == 3
    assert payload["target_kw_m2"] == 50.0
    assert payload["tolerance_kw_m2"] == 1.0
    assert payload["iteration"] == 4
    assert payload["iteration_max"] == 10
    assert payload["commanded_setpoint_c"] == 712.0
    assert payload["settle_budget_s"] == 1500.0
    assert payload["elapsed_s"] == 12.0
    # Live statistics derived from the rolling window.
    assert payload["window_full"] is False  # 5 samples spanning 4 s
    assert payload["pv_latest_c"] == 710.0
    assert abs(payload["mean_flux_kw_m2"] - 49.97) < 0.05
    # Predicate state.
    assert payload["predicate_dwell_s"] == 0.0  # predicate not yet evaluated
    assert payload["predicate_last_reason"] == "no-samples-yet"
    # Predicate caps echoed from state.
    assert payload["sigma_max_kw_m2"] == 0.5
    assert payload["slope_max_kw_m2_per_min"] == 0.15
    # Two-in-a-row counter and dF/dT bookkeeping.
    assert payload["in_tol_windows"] == 1
    assert payload["df_dt_used"] == 0.073
    assert payload["df_dt_source"] == "secant"
    # Error against target — None before any iteration recorded; in
    # this fixture state.iterations has one entry, so a real number.
    assert payload["error_kw_m2"] is not None
    # Pause flag stays False unless the procedure is paused.
    assert payload["paused"] is False


def test_emit_tick_phase_paused_when_pause_flag_set() -> None:
    """Pause is surfaced in the tick payload regardless of the phase
    name the call site passes, so the dock can render a single
    'Paused' state across both wait_steady and verify_soak loops."""
    from unittest.mock import MagicMock

    proc = HeatFluxTune.from_config({"targets_kw_m2": [50.0], "t_set_max_c": 900.0})
    proc._paused = True
    sink = MagicMock()
    ctx = MagicMock()
    ctx.ui_sink = sink
    ctx.clock.t_mono_ns.return_value = 0

    state = _build_state_with_samples()
    proc._emit_tick(
        ctx,
        state=state,  # type: ignore[arg-type]
        predicate=None,
        phase="settle",
        elapsed_s=0.0,
        settle_budget_s=1500.0,
    )

    tick = sink.publish.call_args.args[0]
    assert tick.payload["phase"] == "paused"
    assert tick.payload["paused"] is True


def test_emit_tick_error_is_none_before_any_iteration() -> None:
    """First iteration of a target: ``state.iterations`` is empty, so
    no flux mean has yet been compared to the target. The dock relies
    on ``error_kw_m2 is None`` to suppress the err/✓ overlay."""
    from unittest.mock import MagicMock

    from capa.experiment.procedures.builtin.heat_flux_tune.controller import _SessionState
    from capa.experiment.procedures.builtin.heat_flux_tune.signals import RollingWindow

    proc = HeatFluxTune.from_config({"targets_kw_m2": [50.0], "t_set_max_c": 900.0})
    sink = MagicMock()
    ctx = MagicMock()
    ctx.ui_sink = sink
    ctx.clock.t_mono_ns.return_value = 0

    state = _SessionState(
        flux_window=RollingWindow(window_s=10.0),
        pv_window=RollingWindow(window_s=10.0),
        target_index=1,
        target_count=1,
        target_kw_m2=50.0,
        tolerance_kw_m2=1.0,
        iteration=1,
        iteration_max=10,
        commanded_setpoint_c=600.0,
    )
    state.flux_window.push(0.0, 49.0)
    proc._emit_tick(
        ctx,
        state=state,
        predicate=None,
        phase="settle",
        elapsed_s=0.0,
        settle_budget_s=1500.0,
    )
    tick = sink.publish.call_args.args[0]
    assert tick.payload["error_kw_m2"] is None
