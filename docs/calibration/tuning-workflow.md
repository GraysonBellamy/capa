# Tuning workflow

**Audience:** operators running a heat-flux tune; calibration authors arming the procedure for the first time.
**Scope:** the end-to-end operator path — pick a target, arm the procedure, watch convergence on the dock, save the artifact, cite it in a future specimen run.

This is the *workflow* page. The procedure's config surface lives in [Procedure: heat-flux tune](../procedures/builtin-heat-flux-tune.md); the convergence algorithm in [Heat-flux tune procedure — the algorithm](heat-flux-tune-procedure.md); the artifact schema in [Tune artifacts](tune-artifacts.md). This page assumes you've read [Calibration overview](overview.md) and know which subsystem you're touching (the tune side, not the channel-cal side).

---

## When to arm a tune

The recommended cadence is **daily, per session**. Heater coil emissivity drifts, gauge wiring changes between sessions, ambient temperature differs — a fresh artifact keeps the `capa.flux_calibration_freshness` preflight green and gives the next specimen run a calibrated starting setpoint.

Mandatory re-tune triggers:

| Trigger | Why |
|---|---|
| Heater coil replaced | Coil age and emissivity reset; old artifact's setpoints are stale by tens of °C in some regions. |
| Heat-flux gauge replaced | The wire-side calibration changed; old setpoints no longer correspond to the same delivered flux. |
| Gauge geometry changed (gauge moved, holder swapped, fixture redesigned) | The flux-vs-setpoint curve depends on the gauge's position relative to the heater. |
| New rig profile | A fresh hardware profile has no on-disk artifact at all. |
| The freshness preflight warning fires | The lab-policy recency window (default 7 days) has elapsed. |

---

## Procedure choices

Today there is one tune procedure: `capa.builtin.heat_flux_tune`. The plan accommodates future variants (e.g. a per-band emissivity tune, a holder-geometry sweep) but none ship yet.

Choose `initial_guess` based on what you have on hand:

| Source | Use when |
|---|---|
| `lookup` (default) | A recent artifact is on disk under `configs/calibrations/flux/`. The most common case — yesterday's tune is the prior. |
| `operator` | You're characterising a new rig by hand and know a safe starting setpoint. Requires `operator_initial_setpoint_c`. |
| `sigma_t4` | Truly cold start, no artifact, no operator knowledge. The σT⁴ fallback is anchored at 50 kW/m² ≈ 650 °C and will be wrong by tens of °C — the procedure hill-climbs in iterations 1–4. |

Defaults are tuned for the daily-tune workflow: 1 target (`[50]`), `lookup` priors, 900 °C ceiling. For a calibration sweep, change `targets_kw_m2` to the ascending list of points you want, e.g. `[25, 50, 75, 100]`.

---

## Preflight checklist

Before arming, confirm:

1. **The three required channels are mapped** on the active hardware profile:
   - `heat_flux_gauge` (or whatever `flux_channel` you set)
   - `heater.setpoint` (or whatever `heater_setpoint_channel`)
   - `heater.pv` (or whatever `heater_pv_channel`)

   Missing channels are caught by procedure preflight with `code="heat_flux_tune.missing_channel"`.

2. **The Schmidt-Boelter gauge is physically installed** at the operator-declared `geometry` position. The procedure's gauge-sanity check catches NaN/garbage but **does not** know whether the gauge is in the right place — that's an operator judgment call.

3. **`t_set_max_c ≤ 1000 °C`.** Preflight refuses higher; even if it didn't, that's the documented rig-survival limit.

4. **Cooling water + safety interlocks are live.** The tune runs the heater for tens of minutes at hundreds of °C — same physical hazards as a specimen run.

5. **No specimen is on the load cell.** Run the tune with the load cell unloaded (or with a known thermal mass) but never with the day's actual specimen — the heat-up profile would consume the sample before the real run.

---

## Arming from the GUI

1. Open the Setup tab.
2. Select procedure: **Heat-Flux Tune**. The Method tab disables itself (`uses_method=False`).
3. Edit the procedure config in the auto-generated form. The form groups fields by `capa_group` — *Top-level* (targets + safety) is always expanded; *advanced* groups (predicate, timing, correction, safety, artifact) default to collapsed. For a daily tune, *Top-level* is usually all you touch.
4. Set `operator_id` and `gauge_calibration_ref` — both land in the artifact and are read at audit time. Skipping `gauge_calibration_ref` makes it harder to detect a future calibration change.
5. Click Arm. Standard preflight runs; missing channels or invalid configs surface before the run starts.
6. Hit Run.

The [`HeatFluxTuneDock`](../procedures/builtin-heat-flux-tune.md#the-dock) auto-shows when the run state transitions to RUNNING.

---

## Arming headless

```bash
uv run capa run --headless tune.yaml
```

with `tune.yaml`:

```yaml
experiment:
  hardware: configs/hardware/capa_real_full.toml
  domain_profile:
    id: capa.profiles.capa_pyrolysis  # optional but recommended
  sample:
    id: tune_2026-05-24
    notes: "Daily tune, all-new gauge cable"

procedure:
  id: capa.builtin.heat_flux_tune
  config:
    targets_kw_m2: [50]
    operator_id: gbellamy
    gauge_calibration_ref: schmidt_boelter_SN1234_cert_2026-04
```

The dock is not active in headless mode (no UI), so the three operator commands (Pause / Resume / Accept Current) are unavailable. The procedure runs to completion or until the wall-clock budget exhausts. Audit events + the on-disk artifact land normally.

---

## Watching convergence on the dock

The live-numerics panel updates at ~2 Hz. The two fields that tell you the most:

- **Phase + last reason.** `phase` shows the loop you're in (`settle`, `verify_soak`, `paused`, `holding`). The last-reason column shows the predicate's current state: `holding`, `pv-out-of-band`, `flux-noisy`, `flux-drifting`, `window-not-full`, `missing-pv-or-setpoint`. A predicate stuck on `flux-noisy` after the window is warm means the variance cap is tight for the current rig conditions — see [Troubleshooting](#troubleshooting) below.
- **Flux + err.** Green check when `|err| ≤ tolerance`, amber otherwise. Two consecutive green ticks + the verify soak → acceptance. A flux value that oscillates around target without ever staying green for `t_stable_s` (default 90 s) usually means a noisy gauge field — the iteration eventually settles or times out into `warn_proceeded`.

The In-tol row counts `windows of 2` — the two-in-a-row acceptance counter. When it reads `2 of 2`, the procedure is in the verify soak.

The dF/dT row's source field (`secant`, `prior`, or `sigma_t4`) is the fastest way to see whether the procedure is using a good prior or hill-climbing. `secant` from iteration 2 onward is the happy state.

---

## When to abort an unstable tune

Hit **Abort** if any of the following fire:

| Symptom | Likely cause | Next step |
|---|---|---|
| `predicate_last_reason = "flux-noisy"` persistently after the window warms | Gauge cable, EMI source nearby, or cabinet door open | Check physical setup; if persistent, widen `sigma_flux_floor_kw_m2` or `sigma_flux_max_fraction` and re-run |
| `predicate_last_reason = "flux-drifting"` with a real ramp visible in the slope row | Heater not yet at steady state; needs more dwell | Usually self-corrects — the predicate-relaxation logic gives the procedure a few iterations to get close |
| `runaway_count` reaching 2 (one short of the trip) | Wrong-sign Jacobian, sign-flipped prior, gauge wired backwards | Abort, check `gauge_calibration_ref` and the gauge wiring before retrying |
| Flux reading abruptly drops to ~0 or pegs at the gauge full-scale | Gauge cable disconnected, water-cooling line clogged | **Abort immediately** — the procedure's gauge-silence check will fire in 30 s but a clogged cooling line damages the gauge |
| Heater PV diverges from setpoint by > 10 °C with no Watlow error | Watlow controller fault | Abort; investigate via Watlow's own front panel |

The procedure's own safety nets (gauge silence, gauge sanity, runaway detector, wall-clock budget) catch most of these eventually. Hitting Abort is the operator-friendly path when you see it sooner than the procedure does.

After an Abort, the procedure's `finally` block drives the heater to `t_safe_c` (default 25 °C). Any accepted targets up to the abort point are preserved in the on-disk artifact.

---

## Where the artifact lands

One artifact file is written when `persist_dir` is configured:

| Path | Purpose |
|---|---|
| `configs/calibrations/flux/<id>.toml` (default `persist_dir`) | Operational copy. Read by the next tune session's `lookup` prior and by the CAPA profile UI. |

The id is `<artifact_id_prefix>_<YYYY-MM-DD>` (default prefix `capa_flux`, so `capa_flux_2026-05-24.toml`). `latest.toml` is repointed at the new id; the previous target is copied to `<previous-id>.toml.bak-<today>` first. If `persist_dir` is null, artifact-file persistence is skipped. The tune run's bundle records config and audit events, but it does not currently include an automatic artifact sidecar. Details in [Tune artifacts](tune-artifacts.md#on-disk-layout).

---

## Citing the artifact in a specimen run

After the tune, the *next* specimen run's CAPA profile metadata cites the artifact by id:

```yaml
domain_profile:
  id: capa.profiles.capa_pyrolysis
  metadata:
    program:
      target_heat_flux_kw_m2: 50.0
      heater_setpoint_c: 727.0           # operator types this from the artifact
      flux_calibration_ref: capa_flux_2026-05-24
```

Two facts worth being explicit about:

1. **The reference is free-form.** capa does not parse `flux_calibration_ref` as a file path or auto-load the artifact at run-arm. It's metadata for the bundle.
2. **`heater_setpoint_c` is operator-typed**, not auto-derived. The Setup-tab UI offers to *populate* it from the latest artifact (using `setpoint_for_target`), but the operator confirms the value before arming. This is deliberate — an operator who knows the morning's tune was suspect can override.

There is no automatic "promote artifact to calibration set" action because the two subsystems are unrelated. See [Calibration overview](overview.md) for the framing.

---

## Troubleshooting

### "The tune kept timing out into warn_proceeded"

Symptoms: every iteration's `dwell_s` is close to `t_settle_max_s`; `last_reason` stays on `flux-noisy` or `flux-drifting`.

Likely causes, in order of frequency:

1. **The gauge field is genuinely noisier than the cap.** Check that the cabinet door is closed, no fans pointing at the rig, no cycling HVAC. The default `sigma_flux_max_fraction = 0.5%` and `sigma_flux_floor_kw_m2 = 0.05` are tight on this rig; a noisier rig may need looser values.
2. **A new gauge cable was just installed without retuning the predicate.** New cabling can change the noise envelope. Run a deliberate look at `std_flux` for ~10 minutes at a known steady setpoint (free run, manual heater command) to characterise the noise before re-tuning.
3. **`t_window_s` is too small for the heater's current limit-cycle period.** Defaults to 180 s, sized for the rig's observed ~45 s cycle period. If the rig is closing on a longer cycle (different setpoint, different gain), widen the window.

### "Iteration 1 wildly overshoots the target"

Symptoms: `sigma_t4` source in iteration 1, then a runaway warning by iteration 3.

The σT⁴ fallback is anchored at one point — it can be off by tens of °C for very high or very low targets. The procedure's `predicate_relax_factor` is meant to absorb this (loose predicate when far from target → next iteration's secant correction is large but informed). If the runaway detector trips before convergence, the most reliable fix is to set `initial_guess = "operator"` and supply an `operator_initial_setpoint_c` close to the expected value.

A historical artifact (even from a different rig, even from months ago) is usually a better prior than σT⁴. If one exists, set `initial_guess = "lookup"` and point `persist_dir` at the artifact's directory.

### "The tune converged but the held setpoint looks wrong"

Symptoms: artifact accepts at a setpoint visibly different from what you'd expect from the historical record.

Read the artifact: does `gauge_calibration_ref` match yesterday's? If not, the gauge's V→kW/m² curve changed under the tune — yesterday's and today's setpoints are not directly comparable.

If `gauge_calibration_ref` matches and the setpoint is still off by tens of °C, suspect physical changes: the gauge moved (geometry no longer the recorded `40 mm below heater, centerline`), the holder was swapped, a different specimen disk was previously left in the field.

### "I aborted partway through a multi-target session — do I have to start over?"

No. Any targets accepted before the abort are in the on-disk artifact. The next session reads it as the `lookup` prior; if you only need to re-do the failed targets, edit the session's `targets_kw_m2` to that list. The partial-save behavior is documented in [Tune artifacts](tune-artifacts.md#partial-save-discipline).

---

## See also

- [Procedure: heat-flux tune](../procedures/builtin-heat-flux-tune.md) — config reference, dock walk-through, tick schema.
- [Heat-flux tune procedure — the algorithm](heat-flux-tune-procedure.md) — convergence rule, predicate, secant step, all the "why" behind the constants.
- [Tune artifacts](tune-artifacts.md) — what gets written, where, and how to read it.
- [CAPA profile fields](../configuration/capa-profile.md#heaterprogram) — where the artifact id eventually lands in a specimen run's metadata.
- [Calibration overview](overview.md) — why the heat-flux tune subsystem is distinct from channel calibration.
