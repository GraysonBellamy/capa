---
description: Open-air transfer calibration of a second heat-flux gauge against the Medtherm reference — setup, pilot, the 12-level R D R calibration run, analysis and uncertainty budget.
---

# Heat-flux gauge transfer calibration

**Audience:** whoever calibrates a new heat-flux gauge against the rig's reference, and whoever analyses the bundles afterwards.
**Scope:** open-air transfer calibration, 5–70 kW/m², of a gauge on ai8 (HFG2) against the Medtherm Schmidt-Boelter reference on ai14, under the CAPA heater at 40 mm: setup, the pilot, the calibration run, the analysis, the uncertainty budget, and where the timing numbers come from. [Windowed radiometer with purge](#windowed-radiometer-with-purge) covers the variant for a purged, windowed radiometer on ai8 (where HFG2 was), with the purge set by the Alicat.

HFG2 is rated 10 BTU/(ft²·s), 113.6 kW/m², with 150 % overrange, so the top level sits at 62 % of its range. The calibration covers 5–70 kW/m² only. If HFG2 will be used above that, add levels now: a fit extrapolated past its top level is not a calibration.

The reference carries a manufacturer calibration (3 %, k = 2). HFG2 inherits it by comparison. Both gauges sit side by side under the heater, and the heater slides between two stops so that each gauge in turn sits on the heater centreline. Reading each gauge at the same spot, rather than both at once, keeps the field's non-uniformity out of the ratio. Reading the reference before and after HFG2 removes the heater's drift.

---

## Files

| File | What it is |
|---|---|
| [configs/hardware/hfg_transfer_cal.toml](https://github.com/GraysonBellamy/capa/blob/main/configs/hardware/hfg_transfer_cal.toml) | Heater, NI 9214 (reference on ai14, HFG2 on ai8 at ±78 mV, HFG2 body TC on ai4, type K), ambient sensor. No chamber devices. |
| [configs/experiments/hfg_transfer_pilot.yaml](https://github.com/GraysonBellamy/capa/blob/main/configs/experiments/hfg_transfer_pilot.yaml) | Pilot run, about 75 min. |
| [configs/experiments/hfg_transfer_cal.yaml](https://github.com/GraysonBellamy/capa/blob/main/configs/experiments/hfg_transfer_cal.yaml) | Calibration run, about 3.5 h. |
| `configs/methods/hfg_transfer_{pilot,cal}.method.toml` | Generated from the script below; don't edit by hand. |
| [scripts/make_hfg_transfer_methods.py](https://github.com/GraysonBellamy/capa/blob/main/scripts/make_hfg_transfer_methods.py) | Writes both methods from the setpoint table. |
| [scripts/analyze_hfg_transfer.py](https://github.com/GraysonBellamy/capa/blob/main/scripts/analyze_hfg_transfer.py) | The analysis below, for either gauge. |
| [configs/hardware/rad_transfer_cal.toml](https://github.com/GraysonBellamy/capa/blob/main/configs/hardware/rad_transfer_cal.toml) | Radiometer variant: reference on ai14, radiometer on ai8 at ±78 mV, Alicat purge MFC (COM7, 19200 baud), ambient sensor. |
| [configs/experiments/rad_transfer_cal.yaml](https://github.com/GraysonBellamy/capa/blob/main/configs/experiments/rad_transfer_cal.yaml) | Radiometer calibration run with the purge sweep, about 6 h. |
| [configs/experiments/rad_fan_test.yaml](https://github.com/GraysonBellamy/capa/blob/main/configs/experiments/rad_fan_test.yaml), [scripts/make_rad_fan_test_method.py](https://github.com/GraysonBellamy/capa/blob/main/scripts/make_rad_fan_test_method.py), [scripts/analyze_fan_test.py](https://github.com/GraysonBellamy/capa/blob/main/scripts/analyze_fan_test.py) | Fan test: heater over the reference at 378 °C, fan switched on and off at prompts; separates convection on the reference (a step within seconds) from a change in how the heater radiates (a drift over minutes). About 22 min. |
| [scripts/make_rad_transfer_methods.py](https://github.com/GraysonBellamy/capa/blob/main/scripts/make_rad_transfer_methods.py) | Writes `configs/methods/rad_transfer_cal.method.toml` (and `--from-level N` resumes) from the same setpoint table. |

Both experiments run [`capa.builtin.recipe_runner`](../procedures/builtin-recipe-runner.md) with operator [prompts](../procedures/method-steps-reference.md#prompt): a dialog with the instruction and a **Continue** button. The prompts have no timeout, so an unanswered prompt waits with the heater holding its setpoint; Stop ends the run.

---

## Setup

- **Gauges.** Side by side, both water-cooled from one loop at a steady flow, with the water above the dew point. The ambient sensor gives air temperature and RH; HFG2's body TC should sit at the water temperature. Take the zero only once the body temperature is flat (under 0.02 °C/min): the zero includes the face's heat exchange with the room, so it moves when the water temperature does. On 2026-10-17 the supply drifted 1.3 °C over the first 35 min of the session and both zeros moved with it. Mount both gauges the same way, flush in the same board or holder: the cooled face also picks up convection from the hot air under the heater, and that depends on how the gauge sits.
- **Face height.** Measure the height difference between the two sensing faces and record it. Height matters far more than lateral position: on the centreline the field is at a lateral extremum, so a small sideways error costs little, while the vertical gradient is first-order.
- **Slide stops.** Fixed stops at A (reference centred) and B (HFG2 centred). The stops are the lateral positioning; placing the heater by eye is not repeatable enough. The heater can't slide clear of the gauges, so the only zero is taken at the start with the heater cold (PV within 2 °C of the room) over A. A heater 8 °C above the room still puts about 0.04 kW/m² on the reference, 0.8 % of the 5 kW/m² level.
- **Air.** Hood or room ventilation in its run state and left alone, no drafts across the gauges, doors closed.
- **Polarity.** HFG2 must read positive under flux. Its channel uses the 9214's full ±78 mV range, so reversed leads show as a negative signal instead of clipping.

---

## Phase 0: pilot

Optional. The timing below comes from 50 kW/m² data recorded inside the chamber (see [Where the numbers come from](#where-the-numbers-come-from)). The pilot checks it in open air at both ends of the range before the calibration run. Skipping it is reasonable: the calibration run's residuals and repeats expose the same problems, at the cost of rerunning levels.

| Segment | Heater | Purpose |
|---|---|---|
| Zero Z1, 2 min | stop A, cold | Offsets of both gauges |
| ~5 kW/m², 30 min | stop A | Open-air flux at the lowest setpoint; fluctuations at the low end |
| ~70 kW/m², 20 min | stop A | Open-air flux at the highest setpoint; fluctuations at the high end |
| One swap, 2 × 2 min | B, then A | First ratio; post-move transient on HFG2 |

What the pilot decides:

1. **Setpoints.** The estimates come from tunes at the same 40 mm standoff, but in the chamber. If the open-air fluxes differ from the targets by more than about 10 %, refit the setpoint-to-flux curve to the two open-air points, update `SETPOINTS_C` in the script and rerun it. Levels only need to land near their targets, because the reference measures what is actually delivered.
2. **Window length.** Replay R D R on the two stationary records, as in the analysis below. If 60 s windows still give about 0.2 % rms or less at both ends, keep them; otherwise lengthen `WINDOW_S`.
3. **Discard after a move.** The signal should be flat within 10 s of a move. If it isn't, lengthen the discard and `WINDOW_S` by the same amount.

---

## Phase 1: calibration run

Twelve levels:

| Level | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Target, kW/m² | 5 | 10 | 15 | 20 | 30 | 40 | 50 | 60 | 70 | 50 | 30 | 10 |
| Direction | up | up | up | up | up | up | up | up | up | down | down | down |
| Setpoint, °C (estimate) | 268 | 378 | 454 | 512 | 602 | 671 | 728 | 776 | 819 | 728 | 602 | 378 |

The setpoints fit F = k(T⁴ − Ta⁴) + h(T − Ta) to the in-chamber tunes at the same 40 mm standoff (20.1 kW/m² at 513 °C; 49.9–50.7 kW/m² at 727–732 °C); the pilot confirms or replaces them. The levels are denser at the low end, where zero offsets and any non-linearity show first. The three repeats on the way down check hysteresis, and separate drift over the session from dependence on flux.

Each level:

1. Command the setpoint, then wait until the PV first comes within 1 °C of it: up to 20 min going up, up to 45 min going down, where cooling is passive.
2. Settle with the heater at stop A: 15 min at level 1, 10 min after that. The data call for 5 min (see below); the rest is margin.
3. **R1:** 70 s on the reference.
4. Prompt, slide to stop B, Continue. **D:** 70 s on HFG2.
5. Prompt, slide to stop A, Continue. **R2:** 70 s on the reference.

Click Continue only once the heater is against the stop. Each window step carries a tag such as `hfgcal level=03 q=15 dir=up win=R1` in its `method.step.entered` and `method.step.exited` events, so the analysis finds the windows from the bundle alone.

**During the run, watch `hfg2.body_temperature`.** More than about 5 °C above the water and rising means a cooling problem: stop the run. CAPA's safety rules are not enforced yet, so this is the operator's job.

**A level is spoiled** by a bump, a slide while a window is recording, or a `method.wait.timeout` warning. Note it in the sample's `spoiled_levels` and rerun that level.

---

## Phase 2: analysis

1. **Windows.** For each R1, D and R2 step, keep the last 60 s; the first 10 s follow a move.
2. **Zero.** Average each gauge's voltage over Z1, the cold opening zero, and subtract it. With no flux, the reference has read −0.034 kW/m² (2026-10-07) and −0.13 kW/m² (2026-10-17) through the certificate fit. Subtracting a measured zero rather than relying on the certificate intercept is worth 0.7–2.6 % at 5 kW/m². There is no closing zero (the heater can't slide clear), so the zero is taken as constant through the run; the intercept in step 5 is the check on that.
3. **Reference flux during D.** q_ref = 12 867.29 kW/m²/V × (V_ref − V_ref,0), averaged over R1 and over R2, then interpolated linearly to the mid-time of D.
4. **Remove the heater oscillation.** At low setpoints the PM3's limit cycle is far larger than at 50 kW/m²: ±4–6 % within a 70 s window at 4–15 kW/m² on 2026-10-17, against ±0.5–1 % above 30. The bracketed estimate can't average that out (levels 1 and 12 were off by +1.2 % and −1.9 %). Both gauges record all the time, though, and the off-centre gauge sees the same oscillation at the same instant. The per-sample ratio V_HFG2/V_ref with the reference centred (ρ_A) and with HFG2 centred (ρ_B) gives S_ratio = S_ref·√(ρ_A·ρ_B), with the oscillation divided out. It assumes the field is equally strong at the two off-centre positions, so it carries a constant bias F (1.0101 on 2026-10-17: the field was 2 % stronger on one side). F is the mean S_ratio/S_bracketed over the levels at or above ~20 kW/m², where the bracketed estimate is reliable. Each level's calibration point is then S_ratio/F.
5. **Fit.** S over all levels, weighted by 1/q²: a line through the origin gives the sensitivity S (µV per kW/m²). Also fit with an intercept and with a quadratic term. Keep the line through the origin and carry any significant curvature as a linearity term in the uncertainty, since the transfer can't say which gauge it belongs to.

[scripts/analyze_hfg_transfer.py](https://github.com/GraysonBellamy/capa/blob/main/scripts/analyze_hfg_transfer.py) does steps 1–5 and the checks below. Pass it every bundle of the calibration, including the runs of a resumed session.
6. **Acceptance.** Rerun any level whose residual exceeds 0.6 %; normal scatter topped out near 0.57 %. The down-going repeats should agree with the up-going ones at the same level within about 0.5 %. A systematic gap is hysteresis, for example heat soak in a gauge or holder, and goes into the budget.
7. **Output.** A `linear_two_point` calibration block for HFG2's channel: `ref_low_raw = 0`, `ref_low_value = 0`, `ref_high_raw = 0.01`, `ref_high_value = 0.01 / S` with S in V per kW/m², plus the uncertainty below and `fit_metadata` naming the reference, the bundle ids and the date.

---

## Uncertainty budget (k = 1)

| Term | Size | Source |
|---|---|---|
| Reference calibration | 1.5 % | Certificate: 3 % at k = 2 |
| Transfer procedure | about 0.05 % | 0.17 % rms per level over 12 levels; use the fit's own standard error |
| Face-height mismatch | Δz × (dq/dz)/q | Δz measured at setup; the gradient isn't measured, so keep Δz small and state it |
| Zero offsets | in the fit | Matters only at the lowest levels; a significant fitted intercept means the zero drifted after Z1 |
| Hysteresis | half the up/down gap | The down-going repeats |
| Mounting and convection difference | not quantified | Minimised by identical mounting; state it in the report |

Combined, about 1.5–1.6 % at k = 1, or 3.0–3.2 % at k = 2: the result is limited by the reference, not by the transfer. It holds for a cone-heater source in open air. The two gauges see the same spectrum and view, so those cancel here, but they would not for a very different source.

---

## Windowed radiometer with purge

The same rig and levels calibrate a purged, windowed radiometer: first, on 2026-10-18, the Medtherm 24P-10SB-24-21062, SN 222091, which has a ZnSe window and a 150° view. Its factory calibration is 16.56 kW/m² per mV, with water at 23.7 °C and 6.5 mL/s, dated 2021-05-26; the certificate gives no source temperature. The radiometer will measure this heater. The method also measures how its reading depends on the N2 purge, which the Alicat sets through `purge.setpoint` (sccm; 200 is 0.2 L/min). It found none over 0.1–0.4 L/min and no transient after a slide, so the repeat on 2026-10-22, the calibration of record, ran the shorter `--single` method from `scripts/make_rad_transfer_methods.py`: one cold zero, the purge held at 0.2 L/min, and plain R D R with 70 s windows, as for HFG2 (about 3 h).

**What changes from HFG2**

- **No body thermocouple.** The only check that the zero has settled is both gauge signals lying flat on the plot. During the run, check the cooling water by eye at each prompt.
- **Zeros at each purge rate.** Cold, heater over stop A: purge off, then 200, 100 and 400 sccm, each 5 min settle + 120 s (60 s until 2026-10-22, too short; see "Zero" below). Every window is zero-corrected with the zero at its own purge rate.
- **Purge off during setpoint changes.** At each level the purge goes to 0 before the setpoint command and back to 200 sccm once the PV reaches the setpoint, so the window settles together with the heater (10 min, 15 at level 1). On 2026-10-17 the reach waits took 1–5 min.
- **Four holds on the radiometer.** After R1 and the slide to stop B: D1 at 200, D2 at 100, D3 at 400, D4 at 200 sccm, each a 150 s settle followed by a 70 s window. The settle covers the window warming when the heater centres on it, and each purge change. Then the slide back and R2. About 6 h in all.
- **Reference plane.** Both gauges' top faces at 40 mm below the heater. The radiometer's window sits a little below its top face, and its sensor further down (neither measured), so the calibration includes that recess. It holds for this mounting and standoff.

**Analysis**

```
uv run python scripts/analyze_hfg_transfer.py --dut-channel radiometer.voltage --dut-label RAD [--zero-run ZERO_DIR] RUN_DIR [RUN_DIR ...]
```

The calibration is at the R1/R2 purge rate (200 sccm), from D1 and D4 averaged, with the same ratio estimator and F as for HFG2. The 100 and 400 sccm holds are each read against the off-centre reference at the same heater position, so the heater oscillation and the field both divide out. Their ratio to the 200 sccm holds, interpolated to the same time, is the purge effect. The script prints it per level and per rate (mean, scatter, slope with flux), together with three checks:

- drift inside each kept window, which would mean the window hadn't settled;
- the radiometer's off-centre ratio in R2 against R1, a transient after the slide back;
- the measured flow against the tagged rate.

A synthetic bundle built from the method's own timeline (known S, field asymmetry, purge effect, per-purge zeros) comes back exact.

**How to read the result**

- **Total against radiative.** The reference reads total flux on a cold face: radiation plus convection. The radiometer, behind its window, sees only radiation, so against the reference it reads low by the convective share. The fan test bounds that share: switching a fan on or off didn't step the radiometer/reference ratio within 10 s (under ±0.15 %, the noise at 378 °C), so in open air at 40 mm convection is at most about 0.15 % of the reference's reading at 10 kW/m².
- **Seating.** The radiometer's reading depends strongly on how it sits under the heater. On 2026-10-18 it read about 7 % less with the heater centred over it than 39.65 mm to the side, which was taken for an angular response. Re-seated on 2026-10-22, it read 13.5 % more centred than 36.449 mm to the side (the reference: +7 %), saw 16 % less of the heater at stop A although it was 3.2 mm closer, and its centred sensitivity came out 2.5 % higher. The 10-18 seating was off-centre or tilted, and that calibration is superseded. Check the seating before a run: with the heater at stop A, the radiometer should read about 58 µV per kW/m² of the reference's reading at 36.449 mm (57.6 at 4 kW/m², rising to 58.5 at 70) (the 10-18 seating gave 69.3 at 39.65 mm). Seat it the same way in service.
- **How the heater radiates.** The fan test (with the 10-18 seating) moved the radiometer/reference ratio 0.5–1.1 % over 30–120 s as the heater's thermal state changed (its output rose about 2 %), and back with the fan off. Use the radiometer with the heater at steady state and no draft across it.
- **Window spectrum.** ZnSe transmission falls off beyond about 15–20 µm. At ~270 °C roughly 7–14 % of a blackbody's emission lies out there, against 1–2 % at ~820 °C. A sharp cut-off would make the sensitivity drift with level by several percent. The 10-22 run, with the settled zero from the next morning (see "Zero"), is flat within 0.15 % rms from 15 to 70 kW/m² and reads 0.9–1.6 % low at 9.4 kW/m² and 2.5 % low at 4.2. The settled zero removed only 0.2–0.6 points of that deficit, so it is not a zero error. Its cause is open: the window, or the heater's radiation at its coolest setpoints (the up and down readings at 9.4 kW/m² differ by 0.7 %). The calibrated range stops at 14.8 kW/m²; below it, expect readings 1–2.5 % low. If the window rolls off at all, it does so gradually.
- **Zero.** Start the purge and let both gauges sit cold for at least 5 min before the zero; the methods now settle 5 min after each purge change before a zero window. On 2026-10-22 the zero taken 60 s after the purge came on wandered by 5–8 µV for minutes, 1–3 % of the signal at 4–10 kW/m². A cold zero the next morning (`2026-10-23_135855_RAD_222091_cal2_zero`) sat flat from the start, purge change included: radiometer +1.01 µV and reference −0.83 µV, against +0.69 µV for the radiometer on 10-18. The zero repeats to about 0.3 µV over days, so the 10-22 calibration uses that bundle's zeros (`--zero-run`). A zero bundle stopped inside its settle window still works: the analysis then averages `settleZ1`.

**Alicat notes** (MC-500SCCM-D, SN 125238, firmware 5v12)

- Its setpoint source must be serial (front panel).
- It talks at 19200 baud, and its frame fields are `Mass`, `SetPoint`, `Pressure` and `Temperature`, not `Mass_Flow` / `Mass_Flow_Setpt`.
- It takes the legacy setpoint as `AS200.0`, with no space. `AS 200.0`, what alicatlib 0.3.1 sends, sets the setpoint to 0, so it needs alicatlib with the fix on branch `fix-legacy-setpoint-space`.

---

## Where the numbers come from

The procedure was replayed on the steady flux records of 2026-10-07 and 2026-10-08, about 50 kW/m² inside the chamber. One gauge under one heater has a true ratio of exactly 1, so any departure is procedure error.

- The PM3 limit-cycles in bursts, with a period that wanders between 43 and 57 s. The error of a window mean is largest for windows of about 20 s (0.3–0.4 %).
- R D R with 15–30 s windows: 0.36–0.46 % rms per level, up to 2.6 %. With 60 s windows: 0.17 % rms, 0.34 % at the 95th percentile, 0.57 % worst. Longer windows barely improve on this. Three swaps of 90 s reach 0.07 % but take 12.5 min a level, which the reference's 1.5 % doesn't justify.
- A discard of anything from 10 to 30 s after a move made no measurable difference.
- After a cold heat-up the flux overshoots by 9–14 % and decays with a time constant of 62–116 s; even 12–20 °C setpoint steps left 3–5 % transients. Going up, the leftover overshoot pushes every level the same way, so it doesn't average out in the fit. At 5 min after the PV reaches setpoint the bias is below 0.02 % even for the slowest transient; the run settles 10 min for margin.
- Flux leads the heater PV by 9–10 s at 0.83–0.88 %/°C, twice the steady-state 0.4 %/°C. A PV-based correction cuts short-window error 2–3×. The procedure doesn't depend on it, but the analysis can apply it as a cross-check.

These are in-chamber data at one flux. The pilot confirms them in open air at both ends of the range. The 2026-10-17 calibration skipped the pilot and found what it would have caught: at 268–454 °C the oscillation is about ten times larger relative to the signal than at 728 °C. The ratio estimator in the analysis (step 4) recovers those levels, so the procedure stands, but a pilot is the way to see it before the run.

---

## Calibrations done

| Gauge | Date | Sensitivity | U (k=2) | Range, kW/m² | Record |
|---|---|---|---|---|---|
| 175681 (Medtherm 8-2-10-4-0-36-20680K) | 2026-10-17 | 68.259 µV per kW/m² (14.650 kW/m² per mV) | 3.1 % | 4.2–68.8 | [configs/calibrations/hfg_175681_2026-10-17.toml](https://github.com/GraysonBellamy/capa/blob/main/configs/calibrations/hfg_175681_2026-10-17.toml) |
| 222091 (Medtherm 24P-10SB-24-21062, ZnSe window, N2 purge) | 2026-10-22 | 65.759 µV per kW/m² (15.207 kW/m² per mV), centred, top face 40 mm, purge 0.2 L/min (0.1–0.4 indistinguishable) | 3.3 % | 14.8–69.5 | [configs/calibrations/rad_222091_2026-10-22.toml](https://github.com/GraysonBellamy/capa/blob/main/configs/calibrations/rad_222091_2026-10-22.toml) |

Each record's header carries the reference chain, the uncertainty budget, the conditions and the command that reproduces it from the bundles. The bundles themselves are under `runs/`, which git ignores: keep a copy of each calibration's run folders somewhere backed up.

---

## See also

- [Calibration overview](overview.md)
- [Heat-flux tune procedure](heat-flux-tune-procedure.md): the heater's setpoint-to-flux tune, which this page's setpoint estimates come from.
- [Method step reference](../procedures/method-steps-reference.md): `prompt`, `wait` and `acquire` semantics.
