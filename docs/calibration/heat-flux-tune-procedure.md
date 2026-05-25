# Heat-flux tune procedure — the algorithm

**Audience:** anyone tuning predicate thresholds, debugging an unstable tune, or reading a stalled session in the bundle audit trail. The page that comes alongside this one is [Procedure: heat-flux tune](../procedures/builtin-heat-flux-tune.md), which covers the operator/API surface — config fields, the dock, the tick payload. This page is the *algorithm*: the convergence rule, the predicate, the secant step, and the rationale for the empirical constants.

**Scope:** how `capa.builtin.heat_flux_tune` converges; *why* the tunables sit where they do; the failure modes the procedure explicitly catches.

---

## The supervisory framing

CAPA's scientific control parameter is the radiant **heat flux** at the specimen surface (kW/m²). The heater itself is driven by a Watlow PM3 PID that closes on the average of three embedded thermocouples — i.e. the controller already running on the rig is a temperature loop. Heater temperature is a proxy; flux is the truth.

This procedure runs a **slow supervisory outer loop** on top of the Watlow's temperature PID. It does not replace the Watlow loop, it does not stream rapid setpoints, and it does not implement a flux-PID. The structure is deliberately simple:

1. Command a heater setpoint.
2. Wait for *measured* steady state.
3. Read the windowed mean flux.
4. Compute the error against target; apply one damped secant step.
5. Repeat until the error sits in tolerance for two consecutive windows plus a verification soak.

The Schmidt-Boelter gauge under the heater is **removed before the specimen run**. The tune produces a setpoint↔flux mapping; the experiment-time controller stays temperature-only. The tune is a calibration receipt, not a control law.

The state machine lives in [src/capa/experiment/procedures/builtin/heat_flux_tune/controller.py](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/heat_flux_tune/controller.py). All pure-math primitives (rolling window, Hampel rejection, the predicate, secant, runaway detector) live in the sibling [signals.py](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/heat_flux_tune/signals.py) so they can be unit-tested without async, a databus, or a clock.

---

## Required hardware and channels

Three channels must be mapped on the active hardware profile:

| Channel role | Default name | What it is |
|---|---|---|
| Heat-flux gauge | `heat_flux_gauge` | Calibrated kW/m² reading — typically a Schmidt-Boelter gauge wired through an NI-DAQ AI channel with the V→kW/m² curve already applied. |
| Heater setpoint | `heater.setpoint` | Commanded °C — the write target. |
| Heater PV | `heater.pv` | Live process-variable °C reading from the Watlow's TC average. |

Preflight refuses to arm if any of the three is missing from the registry (see [`HeatFluxTune.preflight`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/heat_flux_tune/controller.py)). The channel names are configurable — an operator who rebinds `flux_channel = "flux_b"` gets the rebinding everywhere in the procedure for free, including the [recording-plan filter](#what-the-bundle-records).

The procedure also refuses `t_set_max_c > 1000 °C` at preflight — that is the documented rig-survival limit, not a soft policy.

---

## The convergence rule

```
                                ┌──────────────────┐
                  start  ──────►│ issue setpoint   │
                  (per target)  │ clear windows    │
                                └────────┬─────────┘
                                         │
                                         ▼
                                ┌──────────────────┐
                          ┌────►│   _wait_steady   │  ── timeout ──┐
                          │     └────────┬─────────┘               │
                          │              │ predicate fires         │
                          │              ▼                         │
                          │       take measurement                 │
                          │              │                         │
                          │       err = target − mean              │
                          │              │                         │
                  out of  │    ┌─────────┴──────────┐               │
                  tol     │    │ |err| ≤ tolerance  │ no            │
                  ◄───────┘    │ for ≥ 2 windows ?  │───────────────┤
                               └─────────┬──────────┘               │
                                         │ yes                      ▼
                                         ▼                  damped secant
                                ┌──────────────────┐         step + clamp
                                │ _verification_   │                │
                                │  soak (t_verify_s)│                │
                                └────────┬─────────┘                │
                          predicate      │ predicate       runaway? │
                          breaks ◄───────┤ holds                    │
                                         ▼                          │
                                  accept point                      │
                                  algorithm_converged               │
                                                                    │
                                  iter cap or wall-clock ◄──────────┘
                                  → warn_proceeded
```

A target is accepted when **two consecutive in-tolerance windows** are followed by a **verification soak** of `t_verify_s` (default 300 s) during which the predicate continues to hold. The two-window rule plus the soak is intentionally redundant — a single fluke window passing tolerance does not accept; the soak then guards against an immediate predicate break that the iteration loop wouldn't otherwise catch.

Each accepted `(target, setpoint, mean-flux)` triple is appended to the on-disk artifact **after every target**, not at session end. A session that aborts on target 3 of 4 still leaves targets 1 and 2 on disk as a usable artifact — see [Tune artifacts](tune-artifacts.md#partial-save-discipline).

### Three ways a target gets accepted

The `accept_reason` field on every [`HeatFluxTunePoint`](tune-artifacts.md#heatfluxtunepoint) records which branch fired:

| `accept_reason` | Branch | What it means for downstream readers |
|---|---|---|
| `algorithm_converged` | Two in-tol windows + verify soak | The standard happy path. Use freely. |
| `operator_override` | Dock "Accept Current" button | Operator decided the field was good enough. The predicate may not have held; the verify soak was skipped. |
| `warn_proceeded` | Iteration cap or wall-clock exhausted | The procedure could not converge in the budget. The last reading was recorded but `accepted=False` — interpolation helpers and "Apply latest tune" suggestions filter these out. |

The `accepted` boolean and `accept_reason` are not redundant: `accept_reason="warn_proceeded"` always pairs with `accepted=False`, while the other two pair with `accepted=True`.

---

## The steady-state predicate

[`SteadyStatePredicate`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/heat_flux_tune/signals.py) is a three-condition gate with a hold-time confirmation. All three conditions must hold *continuously* for `t_stable_s` (default 90 s); any one failing resets the dwell clock to zero.

| Condition | Inequality | What it catches |
|---|---|---|
| PV in band | `|mean(heater.pv) − setpoint| ≤ delta_t_band_c` | Heater is tracking. Default band is 0.3 °C. |
| Flux variance low | `std(flux) ≤ sigma_max` | Gauge field is quiet. |
| Flux slope flat | `|d(flux)/dt| ≤ slope_max_kw_per_min` | Field is not drifting monotonically. |

A fourth precondition gates the whole thing: the **rolling window must be warm** — at least `t_window_s` worth of samples accumulated. Until then `last_reason = "window-not-full"` and the dwell clock never starts.

### Why the PV gate compares means, not instantaneous samples

The Watlow PID tracks its setpoint very tightly *on average* but its instantaneous TC readings carry a few hundred mK of thermal and wire noise. At setpoints ≥ ~600 °C those instantaneous blips repeatedly cross a tight band and reset an instantaneous-sample timer even though the heater is steady by every other measure. Comparing the **windowed mean** smooths the blips out while a genuine tracking offset still moves the mean enough to trip the gate.

This is why `delta_t_band_c` is comfortable at 0.3 °C — it is a check on the mean, not on each raw sample.

### Why a 180 s rolling window

`t_window_s` defaults to 180 s, which feels conservative until you watch the Watlow's closed-loop limit cycle. On this rig at high setpoints the cycle period is ~45 s. A rolling window that fits only one or two cycles aliases the cycle phase into its slope estimate, and the slope-flat gate fails ~55% of the time even when the long-term mean is dead on target.

180 s straddles 3–4 full cycles and averages the phase out. The least-squares slope estimator's 1-σ error from gauge noise drops to ~0.007 kW/m²/min at this window — about 20× headroom under the 0.15 kW/m²/min default cap, so the slope gate accepts genuine steadiness and rejects ≥ ~0.2 kW/m²/min drift cleanly.

Trade-off: the minimum useful iteration time is now bounded by the window-warm wait (~180 s). The `n_iter_max` and `t_total_max_s` defaults are sized to match — see [Timing budgets](#timing-budgets).

### Why the σ floor

The variance cap is `max(sigma_flux_floor_kw_m2, sigma_flux_max_fraction × target)`. The floor (default 0.05 kW/m²) protects low-flux targets from chasing a cap below the gauge's intrinsic noise: the rig's Schmidt-Boelter head delivers σ ≈ 0.03 kW/m² on a steady field, so a 0.05 floor leaves ~1.7× headroom.

The fractional cap (default 0.5%) scales the cap upward at high targets without rewriting the config. At a 50 kW/m² target the cap is `max(0.05, 0.25) = 0.25` kW/m², matching the default tolerance and leaving ~2× headroom over the ~0.13 kW/m² gauge noise observed during steady 50 kW/m² operation.

### Hampel-filtered statistics

The mean, std, and slope all run over the **Hampel-filtered subset** of the current window (see [`hampel_mask`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/heat_flux_tune/signals.py)). Any sample more than `hampel_k` MADs from the window median is dropped from the statistics; the consistency factor `1.4826` makes `k=3` correspond to roughly 3σ for Gaussian noise.

The filtered samples stay in the window itself — they just don't pollute the statistics. A transient gauge glitch doesn't shorten the effective dwell once the next genuine sample lands.

### Predicate strictness relaxation

For all but the first iteration the predicate caps are loosened based on **distance from target**. The [`predicate_strictness`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/heat_flux_tune/setpoint.py) helper returns a multiplier `k ∈ [1.0, relax_factor]` applied as:

```
slope_max_effective = slope_max_kw_per_min * k
t_stable_effective  = t_stable_s / k
```

So when the prior iteration's error is ≥ 30% of target, the slope cap is `relax_factor`× looser and the dwell `relax_factor`× shorter — the procedure accepts a noisier reading faster because the *next* secant step is going to be a big move anyway, and waiting for a tight gauge field at this distance from target is wasted wall-clock. The multiplier decays linearly to 1.0 (full strictness) as `|err| → 2 × tolerance`.

The **verify soak is never relaxed.** That predicate is constructed from the config defaults directly, so the final acceptance gate always runs at full strictness even if every earlier iteration was loosened.

`relax_factor = 1.0` disables the feature entirely — useful when debugging a tune that seems to be accepting too easily.

---

## The correction step

The secant step is computed by [`secant_step`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/heat_flux_tune/signals.py):

```
ΔT_raw    = err / (dF/dT)
ΔT_damped = damping * ΔT_raw
ΔT_final  = clamp(ΔT_damped, ±delta_t_step_max_c)
```

Default damping is 0.7; default step clamp is 25 °C; default Jacobian default is 1 kW/m²/°C.

The function refuses to amplify a non-positive or numerically tiny `dF/dT` — `secant_step` returns 0.0 in that case rather than dividing by zero or moving the heater the wrong way. Wrong-sign Jacobians are caught one iteration later by the [runaway detector](#runaway-detector).

### Picking dF/dT each iteration

The Jacobian is picked by [`_estimate_df_dt`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/heat_flux_tune/controller.py) in priority order:

1. **`secant`** — once two in-session points exist for the current target, use the secant slope across the last two. This is the most accurate signal because it was measured on this rig in this session.
2. **`prior`** — the prior artifact's [`local_df_dt`](tune-artifacts.md#interpolation-helpers) around the current target, when available and positive.
3. **`sigma_t4`** — conservative default of 1 kW/m²/°C. Used only on iteration 1 of the first-ever tune on a rig.

The `df_dt_source` lands in every `heat_flux_tune.iteration` event and in every live tick — a tune that doesn't converge can be diagnosed by reading which Jacobian source the procedure trusted at each iteration.

### Window clearing at iteration boundaries

The flux and PV rolling windows are **cleared** at the top of every iteration (see [`RollingWindow.clear`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/heat_flux_tune/signals.py)). The setpoint just moved (or, for iteration 1, is about to); keeping pre-step data would dominate the std and slope statistics for the next `window_s` seconds and poison the predicate. The warmup wait is part of each iteration's settle budget, which is why `t_settle_max_s` defaults to 1200 s — leaving ~4× headroom over the ~270 s minimum useful settle (window + `t_stable_s`).

---

## Initial setpoint heuristics

[`choose_initial_setpoint`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/heat_flux_tune/setpoint.py) decides where to start. The choice is logged into the `heat_flux_tune.started` event and the result is clamped to `[t_safe_c, t_set_max_c]` regardless of which source wins.

| `initial_guess` | Source | When right |
|---|---|---|
| `lookup` (default) | Linearly interpolate against the most recent on-disk artifact; refuses to extrapolate. | The normal day-to-day case where yesterday's tune is in `configs/calibrations/flux/`. |
| `operator` | `operator_initial_setpoint_c` from config. | An operator who knows a good starting setpoint (rig characterised by hand). |
| `sigma_t4` | σT⁴ blackbody approximation anchored at 50 kW/m² ≈ 650 °C. | A truly cold start on a new rig — no artifact, no operator guess. |

The σT⁴ fallback ([`sigma_t4_setpoint_c`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/heat_flux_tune/setpoint.py)) is empirical, not physical. It solves `F = k(T_h⁴ − T_∞⁴)` for `T_h` with `k` fixed by the 650 °C / 50 kW/m² anchor on the CAPA cone. The result is a *guess* — iteration 1 is going to be wrong by tens of °C, the runaway detector might fire, and the procedure will hill-climb. The `n_iter_max = 14` default is sized so a cold-start session has 4–5 iterations of hill-climbing before convergence still completes in the budget.

A `lookup` source that finds an artifact but cannot bracket the target falls through to `operator`, then to `sigma_t4`. The artifact never silently extrapolates.

---

## Failure modes the procedure catches

Five conditions raise `HeatFluxTuneError` and trigger the abort path. The error is caught inside `run()` so the engine sees a clean return after cooldown — a `HeatFluxTuneError` does not crash the run.

### External stop

`ctx.external_stop` fires from the operator's Stop button, the Run-tab Abort, or an upstream safety system. Caught at the top of every poll loop; raises immediately. The accumulated artifact is preserved and the heater cools to `t_safe_c`.

### Wall-clock exhaustion

`t_total_max_s` (default 8100 s = 2.25 h) is the entire session budget. Sized for `n_iter_max=14` × ~580 s/iteration average at the 180 s window default. Single-target sessions almost always finish in 5–7 iterations; the headroom is for the multi-target sweep case.

When exhausted, the current iteration is interrupted and the artifact lands with whatever was accepted up to that point.

### Settle timeout

`t_settle_max_s` (default 1200 s) is the per-iteration cap on time spent in `_wait_steady`. When elapsed without the predicate firing, the procedure **warns and proceeds** with the noisier reading — it does *not* abort the target. The resulting iteration's measurement is marked `timed_out=True` in the audit event and the iteration count advances. This is a deliberate choice: a tune that gets within tolerance of the target despite a noisy field is more useful than no data at all.

If every iteration times out, the iteration cap is the backstop — see [`warn_proceeded`](#three-ways-a-target-gets-accepted).

### Gauge silence

If no fresh flux sample arrives for `gauge_silence_max_s` (default 30 s), the procedure aborts the target. Catches wiring failures, adapter crashes, and dead gauge channels mid-run. The check is `last_flux_sample_ns` vs. `t_mono_ns()` — purely passive on the sample stream.

### Gauge sanity check (pre-loop)

Before the iteration loop starts, the procedure waits up to 5 s for one flux sample and verifies:

- The value is finite (`isfinite`). NaN or inf → wiring/driver fault.
- The value is below `f_gauge_sanity_max_kw_m2` (default 150 kW/m²). The gauge's design full-scale is ~100 kW/m²; readings above 150 indicate a calibration off by 10× or a runaway gauge.

This is **not** a "must be cold" check. Starting the tune with the heater already at an intermediate setpoint is the supported workflow on this rig. The sanity check catches sensor failure modes, not heater state.

### Runaway detector

[`RunawayDetector`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/heat_flux_tune/signals.py) counts iterations where `sign(err)` and `sign(ΔT_last)` disagree. When the count reaches `runaway_sign_disagreement_count` (default 3), the procedure aborts with a `HeatFluxTuneError`.

This catches:

- A sign-flipped prior artifact (linearly interpolated setpoint sits the wrong side of target).
- A wiring fault where the gauge reads positive but the heater is moving the wrong direction.
- A user who edited an artifact by hand and inverted a column.

Iterations with `err == 0` or `ΔT_last == 0` reset the counter — converged steps and already-clamped steps are not runaway candidates and shouldn't trip the detector.

---

## Hold-at-completion

`hold_at_completion = True` leaves the heater **at the converged setpoint** instead of cooling to `t_safe_c` after a successful tune. Four gates all required ([`_should_hold`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/heat_flux_tune/controller.py)):

1. Operator opted in via config.
2. The loop ran to completion of the **final** target. Every abort path (external_stop, wall-clock, `HeatFluxTuneError`) leaves `completed_all_targets=False` and falls through to cooldown.
3. At least one accepted point exists.
4. The last accepted point's `accept_reason` is **not** `warn_proceeded`. The artifact filters non-accepted points, so holding at a `warn_proceeded` setpoint would leave the operator with a hot heater whose value the rest of the system refuses to surface via "Apply latest tune" — a broken handoff. The procedure cools instead and asks for a re-tune.

A `hold_at_completion=True` config with more than one target is rejected at config-validation time. Holding at the final (typically highest) target of a calibration sweep is almost never the intent — the operator collecting (25, 50, 75, 100) kW/m² data ends with the heater at ~700 °C and almost always wants the default cool-down. Single-target sessions can hold; multi-target sessions cannot.

When the procedure holds, it emits a `heat_flux_tune.holding` event with the held setpoint, target, measured flux, and accept reason. It also publishes one final tick with `phase="holding"` so the dock latches into the HOLDING display before the run leaves the RUNNING state.

---

## Timing budgets

The default budgets are interrelated — changing one usually means changing others. The relationships:

| Constant | Default | Sized against |
|---|---|---|
| `t_window_s` | 180 s | Watlow limit-cycle period × 3–4 |
| `t_stable_s` | 90 s | Half the window — long enough that a brief gauge dropout doesn't fire the predicate |
| `t_settle_max_s` | 1200 s | ~4× the minimum useful settle (`t_window_s + t_stable_s = 270 s`) |
| `t_verify_s` | 300 s | Long enough to catch a slow predicate break after the two in-tol windows |
| `t_total_max_s` | 8100 s | `n_iter_max × ~580 s/iteration` for the multi-target sweep case |
| `n_iter_max` | 14 | Cold-start session: 4–5 hill-climb iterations + 5–7 converge iterations + headroom |

A tune that converges from a good prior typically finishes a single target in 5–7 iterations. A cold-start session that has to discover the rig from σT⁴ may use 10–13. If you find yourself reliably hitting the iteration cap on warm starts, the `damping` or `delta_t_step_max_c` defaults are probably wrong for the rig's heater-time-constant; tune those before raising `n_iter_max`.

---

## What the bundle records

The procedure narrows the recording plan to **only the three required channels** and **suppresses every camera** (`plan_capture` in [controller.py](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/heat_flux_tune/controller.py)). The resulting bundle is a calibration receipt, not a science run — no PMMA video, no purge MFC, no sample TCs.

### Event taxonomy

| Event kind | When emitted | Carries |
|---|---|---|
| `heat_flux_tune.started` | Once, at `run()` entry | `targets_kw_m2`, `t_set_max_c`, `initial_guess` |
| `heat_flux_tune.command.issued` | Every setpoint write | channel, device, value, `authorization_id` |
| `heat_flux_tune.iteration` | Once per iteration | iteration number, target, setpoint old→new, mean / std / slope flux, error, `dF_dT_used` + `dF_dT_source`, dwell, `decision` |
| `heat_flux_tune.operator_command` | Every dock button press | command kind (`pause` / `resume` / `accept_current`), operator metadata |
| `heat_flux_tune.target_accepted` | Per target | target, setpoint, measured flux, `accept_reason` |
| `heat_flux_tune.holding` | Once if hold-at-completion fires | held setpoint, target, measured flux, accept reason, `gauge_calibration_ref` |
| `heat_flux_tune.aborted` | On `HeatFluxTuneError` or wall-clock exhaustion | `reason` |
| `heat_flux_tune.completed` | Once, in `finally` | `accepted_points`, `targets_kw_m2`, `held` |

The `decision` field on `iteration` events takes one of: `step`, `converged_window`, `operator_override`, `abort:runaway`. Reading the sequence of `decision` values is the fastest way to retrace a stalled tune from the bundle.

### Tick payload

The live-numerics dock subscribes to a separate stream of `ProcedureTick` payloads emitted once per poll cycle (~2 Hz at the default `poll_interval_s = 0.5`). The schema is documented inline on the dock side — see [Procedure: heat-flux tune](../procedures/builtin-heat-flux-tune.md#tick-payload-schema).

---

## Operator commands mid-tune

Three commands arrive via `ctx.operator_commands` and mutate the procedure's state without restarting the iteration loop:

- **`pause`** — sets `_paused = True`. The settle/verify loops sleep without re-evaluating the predicate, the rolling windows keep filling, the heater stays at its current commanded setpoint, and the elapsed-time clock for the settle budget freezes. Paused time does not count against `t_settle_max_s`.
- **`resume`** — clears `_paused`. The predicate resets (its dwell clock restarts from zero) so the procedure doesn't accept a stale "holding" state.
- **`accept_current`** — one-shot flag. On the next poll, the loop short-circuits with the rolling-window statistics as-is, marks the measurement `operator_accepted=True`, and the resulting tune point gets `accept_reason="operator_override"`. The verify soak is skipped because the operator has explicitly opted out of the algorithmic convergence rule.

Unknown command kinds are logged and dropped — the procedure is forward-compatible with new `OperatorCommandKind` literals.

---

## See also

- [Procedure: heat-flux tune](../procedures/builtin-heat-flux-tune.md) — config-field reference, dock walk-through, tick schema.
- [Tune artifacts](tune-artifacts.md) — the on-disk format of what this procedure writes.
- [Tuning workflow](tuning-workflow.md) — end-to-end operator flow: arm → watch → save.
- [CAPA profile fields](../configuration/capa-profile.md) — `HeaterProgram.flux_calibration_ref` is where a tune artifact id eventually gets cited.
