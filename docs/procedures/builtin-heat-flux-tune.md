# Procedure: heat-flux tune

**Audience:** operators arming a heat-flux tune; UI authors wiring the dock; anyone debugging a stalled session from the live tick payload or audit events.
**Scope:** the procedure surface — config fields, dock walk-through, operator commands, event taxonomy, and the live-numerics tick schema. The *algorithm* (convergence rule, predicate, secant step, why the constants sit where they do) is documented in [Heat-flux tune procedure — the algorithm](../calibration/heat-flux-tune-procedure.md); read that page if you need to tune predicate thresholds or interpret a runaway abort.

---

## Why this procedure exists

CAPA's scientific control parameter is the radiant **heat flux** at the specimen surface (kW/m²). The heater is driven by a Watlow PM3 PID closing on a temperature setpoint — temperature is a proxy for flux. This procedure runs a slow supervisory loop that discovers, for each target flux, the heater setpoint that delivers it on this rig, today.

The output is a **tune artifact** — a TOML file under `configs/calibrations/flux/` listing `(target_kw_m2, heater_setpoint_c, measured_flux)` triples. Subsequent specimen runs reference this artifact via `HeaterProgram.flux_calibration_ref` so the heater setpoint they commanded is traceable back to the day's measured flux.

The Schmidt-Boelter gauge is **removed before the specimen run**. The tune is a calibration receipt, not a control law.

---

## Quick reference

| | |
|---|---|
| Plugin id | `capa.builtin.heat_flux_tune` |
| Module | [src/capa/experiment/procedures/builtin/heat_flux_tune/](https://github.com/GraysonBellamy/capa/tree/main/src/capa/experiment/procedures/builtin/heat_flux_tune) |
| `uses_method` | `False` — the Method tab is hidden when this procedure is selected |
| Required channels | `heat_flux_gauge`, `heater.setpoint`, `heater.pv` (rebindable in config) |
| Bundle shape | One bundle + one `*.flux.toml` artifact in `configs/calibrations/flux/` |
| Cameras / extra channels | Suppressed by `plan_capture` — the bundle records only the three required channels |

---

## Picking config values

The `HeatFluxTuneConfig` schema lives in [config.py](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/heat_flux_tune/config.py) with `extra="forbid"` — typos in field names fail validation, they do not silently default.

The fields are grouped (by `capa_group` metadata on each Pydantic field) so the auto-generated Run-tab editor renders them in collapsible sections. The groups, in order:

### Top-level — targets and safety

| Field | Unit | Default | Notes |
|---|---|---|---|
| `targets_kw_m2` | kW/m² | — | One or more target fluxes. Typically ascending (`(25, 50, 75)`) so the heater warms monotonically across the session. Multiple targets become a calibration sweep; single target is the daily-tune case. |
| `t_set_max_c` | °C | 900 | Per-session hard ceiling on commanded heater setpoint. Preflight refuses anything above 1000 °C (the rig-survival limit). |
| `operator_id` | — | `None` | Recorded into the artifact for audit. |
| `tolerance_kw_m2` | kW/m² | `None` | Convergence band. Leave unset for the default `max(0.1, 0.005 × target)` per target. |

### Initial setpoint (`initial_setpoint`)

How to pick the first heater setpoint for each target. Detail in [Initial setpoint heuristics](../calibration/heat-flux-tune-procedure.md#initial-setpoint-heuristics).

| Field | Default | Notes |
|---|---|---|
| `initial_guess` | `lookup` | `lookup` interpolates the most recent on-disk artifact. `operator` uses `operator_initial_setpoint_c`. `sigma_t4` is the σT⁴ cold-start fallback. |
| `operator_initial_setpoint_c` | `None` | **Required** when `initial_guess = "operator"`; otherwise ignored. Config validation refuses the missing-pair case. |

### Channels (`channels`, advanced)

| Field | Default | Notes |
|---|---|---|
| `flux_channel` | `heat_flux_gauge` | Name of the calibrated heat-flux gauge channel in the registry. |
| `heater_setpoint_channel` | `heater.setpoint` | Channel to issue setpoint writes against. |
| `heater_pv_channel` | `heater.pv` | Channel of the heater process variable. |

The procedure reads these names — not literal strings — when filtering the recording plan, subscribing to channels, and issuing setpoint writes. Rebinding here is enough; the rest of the procedure follows.

### Predicate (`predicate`, advanced)

The three-condition steady-state gate. **Do not casually edit these.** Each value sits where it does for an empirical reason — see [the algorithm page](../calibration/heat-flux-tune-procedure.md#the-steady-state-predicate) before changing.

| Field | Unit | Default | What it controls |
|---|---|---|---|
| `t_stable_s` | s | 90 | How long every steady-state condition must hold continuously before a measurement window is taken. |
| `t_window_s` | s | 180 | Rolling-statistics window for mean / std / slope of flux. Sized to average across 3–4 Watlow limit cycles. |
| `delta_t_band_c` | °C | 0.3 | Heater PV deadband: `|mean(PV) − setpoint| ≤ this`. Compares windowed mean, not instantaneous samples. |
| `sigma_flux_floor_kw_m2` | kW/m² | 0.05 | Absolute floor on the variance cap. Protects low-flux targets from chasing a cap below gauge noise. |
| `sigma_flux_max_fraction` | — | 0.005 | Relative cap on rolling std-dev as a fraction of target. 0.005 = 0.5%. |
| `predicate_relax_factor` | — | 3.0 | Distance-based relaxation. Looser slope cap + shorter dwell when far from target; tightens to 1.0 as `|err| → 2 × tolerance`. Set to 1.0 to disable. |
| `slope_max_kw_per_min` | kW/m²/min | 0.15 | Maximum `|d(flux)/dt|`. ~20σ above the noise floor at the default window. |
| `hampel_k` | — | 3.0 | Outlier threshold in MADs over the rolling window. Hampel's classical recommendation. |

### Timing budgets (`timing`, advanced)

Interrelated — change one and the others usually need to follow. The relationships are documented in [the algorithm page](../calibration/heat-flux-tune-procedure.md#timing-budgets).

| Field | Unit | Default | What it bounds |
|---|---|---|---|
| `t_settle_max_s` | s | 1200 | Per-iteration cap on time in `_wait_steady`. On timeout, warn and proceed with the noisier reading — does **not** abort. |
| `t_verify_s` | s | 300 | Verification soak after the two in-tolerance windows. Predicate must keep holding for this dwell. |
| `t_total_max_s` | s | 8100 | Whole-session wall-clock budget. Exhausting this aborts the current target and seals what's been accepted. |
| `gauge_silence_max_s` | s | 30 | Abort if no fresh flux sample arrives within this window. Catches wiring/adapter failures mid-run. |
| `poll_interval_s` | s | 0.5 | Predicate-loop wake cadence. 2 Hz is plenty since channel sample rates are 1–10 Hz. |

### Correction step (`correction`, advanced)

Defaults to a damped secant with a ±25 °C clamp. The `n_iter_max` default of 14 is sized for the cold-start case; warm-start sessions typically converge in 5–7.

| Field | Unit | Default | Notes |
|---|---|---|---|
| `damping` | — | 0.7 | Damping on the secant step. 1.0 is undamped Newton; 0.7 prevents oscillation when the prior slope is stale. |
| `delta_t_step_max_c` | °C | 25 | Anti-runaway / human-comprehensible per-iteration ΔT clamp. |
| `n_iter_max` | — | 14 | Iteration cap per target. Exhausting it accepts the last reading with `accept_reason="warn_proceeded"`. |
| `runaway_sign_disagreement_count` | — | 3 | Abort if `sign(err)` and `sign(ΔT_last)` disagree for this many consecutive iterations. |

### Safety (`safety`, advanced)

| Field | Unit | Default | Notes |
|---|---|---|---|
| `t_safe_c` | °C | 25 | Cooldown setpoint on abort or completion. Must be below `t_set_max_c`; config validation enforces this. |
| `hold_at_completion` | bool | `False` | Leave heater at the converged setpoint after a successful single-target tune. Four gates must all pass — see [hold-at-completion](../calibration/heat-flux-tune-procedure.md#hold-at-completion). Refused at config validation for multi-target sessions. |
| `f_gauge_sanity_max_kw_m2` | kW/m² | 150 | Pre-loop gauge sanity ceiling. Catches calibration-off-by-10× or runaway gauge. **Not** a "must be cold" check — starting with the heater at an intermediate setpoint is supported. |

### Artifact metadata (`artifact`, advanced)

| Field | Default | Notes |
|---|---|---|
| `persist_dir` | `configs/calibrations/flux` | Directory to write the artifact into. Set to blank/`None` to skip on-disk persistence — the artifact still lands in the bundle. |
| `geometry` | `40 mm below heater, centerline` | Free-text gauge-placement description recorded in the artifact. |
| `gauge_calibration_ref` | `None` | Link to the gauge's V→kW/m² calibration cert. A later analyst can detect a calibration change by comparing this across artifacts. |
| `artifact_id_prefix` | `capa_flux` | Artifact id is `<prefix>_<YYYY-MM-DD>` — one artifact per day per rig. |

### Example config

```yaml
procedure:
  id: capa.builtin.heat_flux_tune
  config:
    targets_kw_m2: [25, 50, 75]
    t_set_max_c: 900
    operator_id: gbellamy
    gauge_calibration_ref: "schmidt_boelter_SN1234_cert_2026-04"
    # everything else takes defaults
```

A single-target daily tune is just:

```yaml
procedure:
  id: capa.builtin.heat_flux_tune
  config:
    targets_kw_m2: [50]
```

---

## The dock

The [`HeatFluxTuneDock`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/docks/heat_flux_tune.py) auto-shows when `RunController.active_procedure_id == capa.builtin.heat_flux_tune` and hides on run completion. It surfaces three operator commands and a live-numerics panel.

### The three operator commands

| Button | Procedure effect | Audit event |
|---|---|---|
| **Pause / Resume** | Sets `_paused` on the procedure. Settle/verify loops sleep without re-evaluating the predicate; rolling windows keep filling; heater stays at its current commanded setpoint; `t_settle_max_s` clock freezes. Resuming resets the predicate dwell. | `heat_flux_tune.operator_command` with `command="pause"` / `"resume"` |
| **Accept Current** | One-shot. Next poll, the loop short-circuits with the rolling-window statistics as-is. The verify soak is skipped. Resulting tune point gets `accept_reason="operator_override"`. | `heat_flux_tune.operator_command` with `command="accept_current"` |
| **Abort** | Sets `ctx.external_stop`. The procedure raises `HeatFluxTuneError` from the current poll loop, then the `_safe_cool` path drives the heater to `t_safe_c` before returning. | Standard run-abort audit chain |

Buttons are enabled only during `PREPARING`, `RUNNING`, and `DRAINING` UI states — outside of those the procedure is not consuming the operator-command stream, so a click would silently no-op.

### Live-numerics panel

The panel is driven by `ProcedureTick` payloads emitted once per poll cycle (~2 Hz at default). Numbers go visually stale (dimmed) when no tick arrives within `STALE_AFTER_MS` (1500 ms — two poll cycles) — catches a procedure that has stopped publishing without the dock needing to know why.

Layout, top to bottom:

| Row | Format | What it shows |
|---|---|---|
| Target | `Target N of M · 50.00 ±0.25 kW/m²` | Current target, tolerance band. |
| Iteration | `Iteration 3 of 14 · SP 685.40 °C` | Iteration count and current commanded setpoint. |
| Flux | `49.83 kW/m² · err −0.17 ✓` | Windowed mean flux + signed error. Green check when `|err| ≤ tolerance`; amber otherwise. |
| Std | `0.143 kW/m² · ≤ 0.250` | Rolling std vs. per-iteration σ cap. |
| Slope | `+0.041 kW/m²/min · ≤ 0.150` | Rolling slope vs. cap. |
| PV | `685.34 °C · SP 685.40 °C` | Latest heater PV + commanded setpoint. |
| Settle | `dwell 78 s · 245/1200 s` | Predicate dwell + elapsed-of-budget. |
| Window | `812 samples · span 175.4 s · age 0.2 s` | Rolling window depth, span, and last-sample age. |
| Phase | `settle · holding` | Current phase + predicate's last-reason string. Green when `phase="holding"`. |
| In-tol | `1 of 2 · runaway 0` | Two-in-a-row counter; amber `runaway N` when the runaway detector has accumulated disagreements. |
| dF/dT | `0.1842 · secant` | Jacobian used this iteration + source (`secant`, `prior`, or `sigma_t4`). |

The `predicate_last_reason` string takes one of: `window-not-full`, `missing-pv-or-setpoint`, `pv-out-of-band`, `flux-noisy`, `flux-drifting`, `holding`, `reset`. Reading this in real time is the fastest way to know why the tune isn't accepting.

---

## Tick payload schema

For anyone debugging the bridge directly, the `ProcedureTick.payload` shape this procedure publishes:

| Key | Type | Notes |
|---|---|---|
| `phase` | `"settle" \| "verify_soak" \| "paused" \| "holding"` | Loop phase. `paused` is set when `_paused=True` regardless of which loop is running. |
| `target_index` | int | 1-based index within the session. |
| `target_count` | int | Total targets. |
| `target_kw_m2` | float | Current target. |
| `tolerance_kw_m2` | float | Per-target tolerance. |
| `iteration` | int | 1-based iteration within the current target. `0` before the first measurement. |
| `iteration_max` | int | `n_iter_max`. |
| `commanded_setpoint_c` | float | Latest commanded heater setpoint. |
| `pv_latest_c` | float \| None | Most recent heater PV sample. |
| `pv_mean_c` | float \| None | Windowed mean PV (the band gate's actual input). |
| `mean_flux_kw_m2` | float | Hampel-filtered window mean. |
| `std_flux_kw_m2` | float | Hampel-filtered window std. |
| `slope_flux_kw_m2_per_min` | float | Hampel-filtered least-squares slope. |
| `window_full` | bool | `True` once the window has accumulated ≥ `t_window_s` of samples. |
| `flux_window_span_s` | float | Wall-time span of samples currently in the window. |
| `flux_samples_in_window` | int | Sample count. |
| `flux_last_sample_age_s` | float \| None | Seconds since the most recent flux sample. |
| `predicate_dwell_s` | float | How long the predicate has been holding (resets on any condition failing). |
| `predicate_last_reason` | str | See table above. |
| `elapsed_s` | float | Time in the current `_wait_steady` / `_verification_soak` call, paused time excluded. |
| `settle_budget_s` | float | `t_settle_max_s` during settle, `t_verify_s × 2` during verify. |
| `sigma_max_kw_m2` | float | The per-iteration σ cap (after distance-based relaxation). |
| `slope_max_kw_m2_per_min` | float | The per-iteration slope cap. |
| `paused` | bool | `_paused` flag. |
| `in_tol_windows` | int | Two-in-a-row counter. |
| `df_dt_used` | float | The dF/dT this iteration trusted. |
| `df_dt_source` | `"secant" \| "prior" \| "sigma_t4"` | Where the dF/dT came from. |
| `runaway_count` | int | Current runaway-disagreement count. |
| `error_kw_m2` | float \| None | `target − mean_flux`. `None` before the first iteration. |

The `holding` tick (emitted once when hold-at-completion fires) carries a narrower payload: `phase`, `target_index`, `target_count`, `target_kw_m2`, `commanded_setpoint_c`, `mean_flux_kw_m2`, `accept_reason`, `paused=False`.

---

## Audit event taxonomy

Every operator-visible state change writes a `bundle_writer.write_event` so the bundle records the complete session history.

| Event kind | When emitted | Metadata of interest |
|---|---|---|
| `heat_flux_tune.started` | Once, at `run()` entry | `targets_kw_m2`, `t_set_max_c`, `initial_guess` |
| `heat_flux_tune.command.issued` | Every setpoint write | `channel`, `device`, `value`, `accepted`, `authorization_id` |
| `heat_flux_tune.iteration` | Once per iteration | `iteration`, `target_kw_m2`, `setpoint_old_c`, `setpoint_new_c`, `mean_flux_kw_m2`, `std_flux_kw_m2`, `slope_kw_m2_per_min`, `error_kw_m2`, `dwell_s`, `dF_dT_used`, `dF_dT_source`, `decision`, `timed_out` |
| `heat_flux_tune.operator_command` | Every Pause/Resume/Accept | `command`, `operator_metadata` |
| `heat_flux_tune.target_accepted` | Per target | `target_kw_m2`, `setpoint_c`, `measured_flux_kw_m2`, `accept_reason` |
| `heat_flux_tune.holding` | When hold-at-completion fires | `held_setpoint_c`, `held_target_kw_m2`, `held_measured_flux_kw_m2`, `accept_reason`, `gauge_calibration_ref` |
| `heat_flux_tune.aborted` | On `HeatFluxTuneError` or wall-clock exhaustion | `reason` |
| `heat_flux_tune.completed` | Once, in the `finally` block | `accepted_points`, `targets_kw_m2`, `held` |

The `decision` field on `heat_flux_tune.iteration` events takes one of: `step`, `converged_window`, `operator_override`, `abort:runaway`. Reading the sequence of `decision` values from a bundle is the fastest way to retrace a stalled tune.

---

## Promoting an artifact

The artifact written by this procedure is **not** a channel calibration. It does not feed back into a `CalibrationSet`; it does not auto-edit channel transforms. It is a separate file under `configs/calibrations/flux/` (see [Tune artifacts](../calibration/tune-artifacts.md)) that downstream consumers reference by id.

Two consumers exist today:

1. **The next heat-flux tune session.** With `initial_guess=lookup` (the default), the procedure reads `latest.toml` and interpolates the previous artifact's accepted points to pick its starting setpoint.
2. **A specimen run that fills out `HeaterProgram.flux_calibration_ref`.** This is a free-form id pointer — the operator typing `capa_flux_2026-05-24` (or a notebook entry, or a calibration cert id) into the CAPA profile metadata. The pointer is recorded into the bundle for audit; capa does not auto-apply it. See [CAPA profile fields](../configuration/capa-profile.md#heaterprogram).

There is no "promote artifact to calibration set" action because the two subsystems aren't transforms of the same thing. [Calibration overview](../calibration/overview.md) lays out the split.

---

## See also

- [Heat-flux tune procedure — the algorithm](../calibration/heat-flux-tune-procedure.md) — convergence rule, predicate, secant step, all the *why* behind the constants above.
- [Tune artifacts](../calibration/tune-artifacts.md) — on-disk format of what this procedure writes.
- [Tuning workflow](../calibration/tuning-workflow.md) — operator flow: arm → watch → save.
- [What is a procedure](what-is-a-procedure.md) — the three-axis split (procedure / method / profile) and the `Procedure` Protocol.
