"""Config model + procedure metadata for :class:`HeatFluxTune`.

Holds only the pydantic shape, the procedure-id constants, the
initial-guess-source literal, and the package's domain-specific error
type. No async, no I/O, no calibration imports — anything heavier lives
in sibling modules so this file stays cheap to import and trivial to
review when the schema changes.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from capa.core.errors import CapaError

PROCEDURE_ID = "capa.builtin.heat_flux_tune"
PROCEDURE_NAME = "Heat-Flux Tune"
PROCEDURE_VERSION = "0.1.0"


class HeatFluxTuneError(CapaError):
    """Raised when the tuner cannot make progress.

    Distinct from
    :class:`~capa.experiment.procedures.base.ProcedureError` (preflight
    refusal) so the engine can classify an in-flight tune failure as a
    procedure crash rather than a misconfiguration. Caught by
    :meth:`HeatFluxTune.run` itself for the abort-and-cool path; the
    engine sees a clean return after the cooldown.
    """


InitialGuessSource = Literal["lookup", "operator", "sigma_t4"]
"""How to pick the first heater setpoint per target.

* ``lookup``: most-recent on-disk artifact (default).
* ``operator``: ``operator_initial_setpoint_c`` from the config.
* ``sigma_t4``: σT⁴ fallback for a genuine cold start.
"""


class HeatFluxTuneConfig(BaseModel):
    """``config.procedure.config`` shape for :class:`HeatFluxTune`.

    Defaults are intentionally generous on time and tight on safety —
    the procedure runs once a day and an extra five minutes of dwell is
    cheap compared to an undercharacterised flux number going into the
    bundle. Per-rig tuning of the predicate thresholds is expected once
    a few tune sessions have been compared against operator judgement.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    targets_kw_m2: tuple[float, ...] = Field(
        min_length=1,
        json_schema_extra={
            "capa_unit": "kW/m²",
            "capa_help": (
                "One or more target radiant heat fluxes to converge. Typically "
                "supplied ascending (25, 50, 75) so the heater warms monotonically "
                "across the session."
            ),
            "capa_group_open": True,
        },
    )

    t_set_max_c: float = Field(
        default=900.0,
        gt=0,
        json_schema_extra={
            "capa_unit": "°C",
            "capa_help": (
                "Per-session hard ceiling on commanded heater setpoint. 900 °C is "
                "the typical CAPA upper end; 1000 °C is the absolute rig-survival "
                "limit — preflight refuses anything higher."
            ),
            "capa_group_open": True,
        },
    )

    operator_id: str | None = Field(
        default=None,
        json_schema_extra={
            "capa_help": "Operator running the tune; recorded into the artifact.",
            "capa_group_open": True,
        },
    )

    tolerance_kw_m2: float | None = Field(
        default=None,
        json_schema_extra={
            "capa_unit": "kW/m²",
            "capa_help": (
                "Absolute tolerance for convergence. Leave unset to use the default "
                "``max(0.1, 0.005 × target)`` per target — relative-or-floor."
            ),
            "capa_group_open": True,
        },
    )

    initial_guess: InitialGuessSource = Field(
        default="lookup",
        json_schema_extra={
            "capa_help": (
                "How to pick the first heater setpoint. ``lookup`` interpolates the "
                "most recent on-disk artifact; ``operator`` uses the supplied "
                "value; ``sigma_t4`` is the cold-start σT⁴ fallback."
            ),
            "capa_group": "initial_setpoint",
            "capa_group_subtitle": "First-iteration heater setpoint",
        },
    )
    operator_initial_setpoint_c: float | None = Field(
        default=None,
        json_schema_extra={
            "capa_unit": "°C",
            "capa_help": (
                "Required when ``initial_guess = operator``. Operator-supplied "
                "known-safe starting setpoint."
            ),
            "capa_group": "initial_setpoint",
        },
    )

    flux_channel: str = Field(
        default="heat_flux_gauge",
        json_schema_extra={
            "capa_help": "Channel name of the calibrated heat-flux gauge reading.",
            "capa_group": "channels",
            "capa_group_subtitle": "Bound channels (advanced)",
        },
    )
    heater_setpoint_channel: str = Field(
        default="heater.setpoint",
        json_schema_extra={
            "capa_help": "Channel name to issue heater setpoint writes against.",
            "capa_group": "channels",
        },
    )
    heater_pv_channel: str = Field(
        default="heater.pv",
        json_schema_extra={
            "capa_help": "Channel name of the heater process variable (live PV).",
            "capa_group": "channels",
        },
    )

    t_stable_s: float = Field(
        default=90.0,
        gt=0,
        json_schema_extra={
            "capa_unit": "s",
            "capa_help": (
                "How long every steady-state condition (PV in band, flux variance "
                "low, flux slope flat) must hold continuously before a measurement "
                "window is taken."
            ),
            "capa_group": "predicate",
            "capa_group_subtitle": "Steady-state predicate (advanced)",
        },
    )
    t_window_s: float = Field(
        default=180.0,
        gt=0,
        json_schema_extra={
            "capa_unit": "s",
            "capa_help": (
                "Rolling-statistics window for mean / std / slope of flux. At the "
                "rig's 5 Hz sample rate this gives ~900 samples per window. Sized "
                "wide so that a Watlow closed-loop limit cycle (observed period "
                "~45 s on this rig at high setpoints) gets averaged across 3-4 "
                "full cycles — without that, the rolling slope aliases the cycle "
                "phase and the slope gate fails ~55 % of the time even when the "
                "long-term mean is on target. The least-squares slope estimator's "
                "1-σ error from gauge noise drops to ~0.007 kW/m²/min at this "
                "window, leaving ~20× headroom over the strict slope cap. Trade-"
                "off: minimum iteration time is now bounded by the window-warm "
                "wait (~180 s) so n_iter_max and t_total_max_s have been raised "
                "to match."
            ),
            "capa_group": "predicate",
        },
    )
    delta_t_band_c: float = Field(
        default=0.3,
        gt=0,
        json_schema_extra={
            "capa_unit": "°C",
            "capa_help": (
                "Heater PV deadband: ``|PV − setpoint| ≤ this`` is required for the "
                "predicate to hold."
            ),
            "capa_group": "predicate",
        },
    )
    sigma_flux_floor_kw_m2: float = Field(
        default=0.05,
        gt=0,
        json_schema_extra={
            "capa_unit": "kW/m²",
            "capa_help": (
                "Absolute floor on the rolling std-dev cap. The effective cap is "
                "``max(sigma_flux_floor_kw_m2, sigma_flux_max_fraction × target)`` — the "
                "floor keeps low-flux targets from chasing a cap below the gauge's "
                "intrinsic noise. Empirically the rig's S-B head delivers σ ~0.03 kW/m² "
                "on a steady field, so a 0.05 floor leaves ~1.7× headroom."
            ),
            "capa_group": "predicate",
        },
    )
    sigma_flux_max_fraction: float = Field(
        default=0.005,
        gt=0,
        json_schema_extra={
            "capa_help": (
                "Relative cap on rolling std-dev, as a fraction of the target flux. "
                "0.005 = 0.5% — at a 50 kW/m² target this allows ±0.25 kW/m² rolling σ, "
                "matching the default tolerance and leaving ~2× headroom over the "
                "~0.13 kW/m² gauge noise observed during otherwise-steady operation "
                "at 50 kW/m². Used together with ``sigma_flux_floor_kw_m2`` "
                "(whichever is larger wins per-target). Tighten below 0.003 only "
                "when a quieter gauge has been characterised on this rig."
            ),
            "capa_group": "predicate",
        },
    )
    predicate_relax_factor: float = Field(
        default=3.0,
        ge=1.0,
        json_schema_extra={
            "capa_help": (
                "Distance-based relaxation of the slope-flat and dwell-time "
                "predicate gates. When the previous iteration was far from target "
                "(|err| ≥ 30 % of target), ``slope_max_kw_per_min`` is multiplied "
                "by this factor and ``t_stable_s`` is divided by it — so a noisy / "
                "still-drifting field is accepted faster so the next big secant "
                "step can be taken. Decays linearly to 1.0 (full strictness) by "
                "the time |err| reaches 2× ``tolerance_kw_m2``. The verify soak is "
                "unaffected. Set to 1.0 to disable."
            ),
            "capa_group": "predicate",
        },
    )
    slope_max_kw_per_min: float = Field(
        default=0.15,
        gt=0,
        json_schema_extra={
            "capa_unit": "kW/m²/min",
            "capa_help": (
                "Maximum |d(flux)/dt|. Catches slow monotonic drift that the "
                "variance check would miss. With ~0.1 kW/m² gauge noise over a "
                "180-s window (5 Hz × 180 s = 900 samples), the slope estimator's "
                "1-σ error is ~0.007 kW/m²/min, so 0.15 sits at ~20σ above the "
                "noise floor — false-positives are vanishingly rare while real "
                "drift > ~0.2 kW/m²/min still fails the gate."
            ),
            "capa_group": "predicate",
        },
    )
    hampel_k: float = Field(
        default=3.0,
        gt=0,
        json_schema_extra={
            "capa_help": (
                "Outlier threshold in MADs over the rolling window. Hampel's "
                "classical recommendation is 3.0."
            ),
            "capa_group": "predicate",
        },
    )

    t_settle_max_s: float = Field(
        default=1200.0,
        gt=0,
        json_schema_extra={
            "capa_unit": "s",
            "capa_help": (
                "Hard cap on settle time per iteration. After this elapses the "
                "procedure warns and proceeds with the noisier reading. Sized "
                "for the wider t_window_s = 180 s default — the predicate cannot "
                "fire until the window warms, so the minimum useful settle is "
                "~270 s (window + t_stable_s). 1200 s leaves ~4× headroom for a "
                "slow-tracking iteration without prematurely accepting noise."
            ),
            "capa_group": "timing",
            "capa_group_subtitle": "Timing budgets (advanced)",
        },
    )
    t_verify_s: float = Field(
        default=300.0,
        gt=0,
        json_schema_extra={
            "capa_unit": "s",
            "capa_help": (
                "Verification soak after the in-tolerance window pair. The "
                "predicate must keep holding for this dwell before acceptance."
            ),
            "capa_group": "timing",
        },
    )
    t_total_max_s: float = Field(
        default=8100.0,
        gt=0,
        json_schema_extra={
            "capa_unit": "s",
            "capa_help": (
                "Total wall-clock budget for the whole session. Sized for "
                "n_iter_max=14 × ~580 s/iteration average at the wider t_window_s "
                "default. Single-target sessions almost always finish in 5-7 "
                "iterations; the headroom is for the multi-target sweep case."
            ),
            "capa_group": "timing",
        },
    )
    gauge_silence_max_s: float = Field(
        default=30.0,
        gt=0,
        json_schema_extra={
            "capa_unit": "s",
            "capa_help": (
                "Abort if no fresh flux sample arrives within this window. Catches "
                "wiring or adapter failures mid-run."
            ),
            "capa_group": "timing",
        },
    )
    poll_interval_s: float = Field(
        default=0.5,
        gt=0,
        json_schema_extra={
            "capa_unit": "s",
            "capa_help": (
                "How often the predicate loop wakes to re-evaluate. 0.5 s is plenty "
                "since channel sample rates are typically 1–10 Hz."
            ),
            "capa_group": "timing",
        },
    )

    damping: float = Field(
        default=0.7,
        gt=0,
        le=1.0,
        json_schema_extra={
            "capa_help": (
                "Damping on the secant step. ``1.0`` is undamped Newton; ``0.7`` "
                "default prevents oscillation when the prior slope is stale."
            ),
            "capa_group": "correction",
            "capa_group_subtitle": "Correction step (advanced)",
        },
    )
    delta_t_step_max_c: float = Field(
        default=25.0,
        gt=0,
        json_schema_extra={
            "capa_unit": "°C",
            "capa_help": "Anti-runaway / human-comprehensible per-iteration ΔT clamp.",
            "capa_group": "correction",
        },
    )
    n_iter_max: int = Field(
        default=14,
        ge=1,
        json_schema_extra={
            "capa_help": (
                "Maximum iterations per target. Exhausting the cap accepts the last "
                "reading with ``warn_proceeded``. 14 covers a cold-start session "
                "where iters 1-4 hill-climb from a bad σT⁴ prior; a session with a "
                "good prior (lookup or operator initial setpoint) typically "
                "converges in 5-7."
            ),
            "capa_group": "correction",
        },
    )
    runaway_sign_disagreement_count: int = Field(
        default=3,
        ge=2,
        json_schema_extra={
            "capa_help": (
                "Abort if ``sign(err)`` and ``sign(ΔT_last)`` disagree for this "
                "many consecutive iterations (sign-flipped prior / wiring fault)."
            ),
            "capa_group": "correction",
        },
    )

    t_safe_c: float = Field(
        default=25.0,
        ge=0,
        json_schema_extra={
            "capa_unit": "°C",
            "capa_help": (
                "Cooldown setpoint on abort or completion. Must be below ``t_set_max_c``."
            ),
            "capa_group": "safety",
            "capa_group_subtitle": "Safety limits (advanced)",
        },
    )
    hold_at_completion: bool = Field(
        default=False,
        json_schema_extra={
            "capa_help": (
                "When True and the tune completes successfully, leave the "
                "heater holding at the converged setpoint instead of cooling "
                "to t_safe_c. Aborts and errors always cool. Use the dock's "
                "Cool Down button or the heater card's 'Cool to safe' "
                "action to drive to safe when finished. Requires a single "
                "target — multi-target calibration sweeps cool unconditionally."
            ),
            "capa_group": "safety",
        },
    )
    f_gauge_sanity_max_kw_m2: float = Field(
        default=150.0,
        gt=0,
        json_schema_extra={
            "capa_unit": "kW/m²",
            "capa_help": (
                "Gauge-alive sanity ceiling, checked once before the iteration "
                "loop starts. A reading above this indicates a wiring fault, "
                "calibration off by an order of magnitude, or a runaway gauge "
                "(real fluxes above the gauge's design full-scale of ~100 kW/m² "
                "shouldn't occur in normal operation). This is NOT a \"must be "
                'cold" check — starting the tune with the heater already at '
                "an intermediate setpoint is the supported workflow."
            ),
            "capa_group": "safety",
        },
    )

    persist_dir: str | None = Field(
        default="configs/calibrations/flux",
        json_schema_extra={
            "capa_help": (
                "Directory to write the tune artifact into. Set to blank/None to "
                "skip on-disk persistence (the artifact still lands in the bundle)."
            ),
            "capa_group": "artifact",
            "capa_group_subtitle": "Artifact metadata (advanced)",
        },
    )
    geometry: str = Field(
        default="40 mm below heater, centerline",
        json_schema_extra={
            "capa_help": "Description of the gauge placement, recorded in the artifact.",
            "capa_group": "artifact",
        },
    )
    gauge_calibration_ref: str | None = Field(
        default=None,
        json_schema_extra={
            "capa_help": (
                "Link to the gauge's V→kW/m² calibration cert. Recorded into the "
                "artifact so a later analyst can detect a calibration change."
            ),
            "capa_group": "artifact",
        },
    )
    artifact_id_prefix: str = Field(
        default="capa_flux",
        json_schema_extra={
            "capa_help": (
                "Artifact id prefix. The full id is "
                "``<prefix>_<YYYY-MM-DD>`` — one artifact per day per rig."
            ),
            "capa_group": "artifact",
        },
    )

    @model_validator(mode="after")
    def _check(self) -> HeatFluxTuneConfig:
        if self.t_safe_c >= self.t_set_max_c:
            raise ValueError(
                f"t_safe_c ({self.t_safe_c} °C) must be below t_set_max_c "
                f"({self.t_set_max_c} °C) — cooldown must not exceed the "
                f"session ceiling."
            )
        if self.initial_guess == "operator" and self.operator_initial_setpoint_c is None:
            raise ValueError("initial_guess='operator' requires operator_initial_setpoint_c")
        if any(t <= 0 for t in self.targets_kw_m2):
            raise ValueError("every target_kw_m2 must be positive")
        if self.hold_at_completion and len(self.targets_kw_m2) > 1:
            # Holding at the last (typically highest) target of a
            # calibration-curve session is almost never the intent —
            # the operator collecting (25, 50, 75, 100) kW/m² data
            # ends with the heater at ~700 °C and probably wants the
            # default cool-down. Refuse at config-validation time so
            # the UI shows a clean error before the run starts.
            raise ValueError(
                "hold_at_completion=True requires a single target; "
                f"multi-target session (n={len(self.targets_kw_m2)}) would "
                "hold at the highest target. Run single-target sessions "
                "with hold, or leave hold off for calibration sweeps."
            )
        return self
