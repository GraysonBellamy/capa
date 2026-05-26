---
description: capa's two calibration subsystems — CalibrationSet channel curves (raw to engineering units) versus HeatFluxTuneArtifact (heater setpoint to delivered kW/m²).
---

# Calibration overview

**Audience:** anyone whose runs depend on calibrated values — operators, analysts, contributors writing adapter code.
**Scope:** the two distinct calibration subsystems capa ships, why they are separate, and which pages to read for each.

---

## Two unrelated things called "calibration"

capa has **two** subsystems whose names both contain "calibration." They share a directory tree and an entry in the project glossary; they share nothing else. Conflating them is the single biggest source of confusion in this area of the codebase.

| | Channel calibration | Tune artifact |
|---|---|---|
| What it transforms | Raw adapter sample → engineering-unit channel sample | Heater commanded setpoint → expected delivered heat flux |
| What that *looks like* | "0–10 V from the NI-DAQ becomes 0–1500 °C" | "726.97 °C → 49.90 kW/m² on this rig today" |
| Lives on disk as | `configs/calibrations/<name>.toml` | `configs/calibrations/flux/<id>.toml` |
| Python model | [`CalibrationSet`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/channels/calibration.py) of [`Calibration`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/channels/calibration.py) curves | [`HeatFluxTuneArtifact`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/calibration/tune_artifact.py) of [`HeatFluxTunePoint`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/calibration/tune_artifact.py) rows |
| Created by | Operator authoring; lab calibration procedure against a reference instrument | The [`capa.builtin.heat_flux_tune`](../procedures/builtin-heat-flux-tune.md) procedure |
| Bundle state today | `calibration.json` records the selected set's `name` and `revision`; full resolved-curve snapshots are planned | Tune-session config and audit events are in the bundle; the artifact TOML is currently an operational file under `persist_dir` |
| Code lives in | [`src/capa/channels/calibration.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/channels/calibration.py) | [`src/capa/calibration/tune_artifact.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/calibration/tune_artifact.py) |
| Reference page | [Calibration sets](calibration-sets.md) | [Tune artifacts](tune-artifacts.md) |

The shipped [`src/capa/calibration/__init__.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/calibration/__init__.py) is explicit about the split:

> This package holds artifact models that record the outcome of a calibration / tune *procedure* — distinct from the channel-level Calibration family, which models raw-acquisition to engineering-unit transforms attached to a ChannelSpec.

A heater-setpoint↔delivered-flux mapping is not a channel calibration because there is no channel whose raw unit is "commanded °C" and whose derived unit is "kW/m²". The two are different shapes of fact about the rig.

## Which page do I want?

- **"How does a raw NI-DAQ voltage become a temperature in the bundle?"** → [Calibration sets](calibration-sets.md).
- **"How does a target heat flux become a heater setpoint?"** → [Tune artifacts](tune-artifacts.md) (the data) + [Heat-flux tune procedure — the algorithm](heat-flux-tune-procedure.md) (the discovery loop).
- **"I need to re-calibrate after swapping a thermocouple."** → Channel side. Re-run the lab procedure against a reference instrument; build a new `CalibrationSet`.
- **"I need to re-tune after replacing the heater coil."** → Tune side. Run `capa.builtin.heat_flux_tune` to produce a fresh artifact.
- **"I'm authoring a daily tune."** → [Tuning workflow](tuning-workflow.md).

---

## The channel-calibration pipeline

A device adapter emits a *raw* sample carrying a value in the device's wire unit (volts, counts, raw flow registers). A `ChannelSpec` attaches a `Calibration` curve that transforms the raw value into the channel's declared engineering unit. The resulting `ChannelSample` lands on the data bus already calibrated.

```
adapter sample (raw, wire unit)
       │
       │  Calibration.evaluate(raw)
       ▼
channel sample (value, output_unit)
       │
       │  bundle writer
       ▼
parquet long-format channel-sample table
```

Every `Calibration` declares an `input_unit` and an `output_unit`. The dimensional algebra is checked at construction — e.g. an `Identity` transform requires `input_unit` and `output_unit` to be dimensionally compatible, and a calibration whose declared output unit doesn't match the channel's declared engineering unit is a Layer-3 validation error.

Every `Calibration` also carries an [`UncertaintySpec`](calibration-sets.md#uncertainty) — *or* an explicit `None`, declaring "this curve has no measured uncertainty." capa refuses to let an uncertainty be silently zero. The reasoning: an analyst five years from now should not have to wonder whether "no uncertainty in the bundle" meant "we measured it as zero" or "we forgot." The forcing function is to make the absence explicit.

### Versioned and recorded

A `CalibrationSet` is read from a file like `configs/calibrations/thermocouples_2026Q2.toml` and applied to channels at run-arm. Today, the bundle records the selected set's `name` and `revision` in `calibration.json`; it does not yet embed the full resolved per-channel curve payload.

The practical discipline is to bump the set `revision` whenever curves change and keep the source calibration TOML under version control or archived with the run. The full immutable curve snapshot is planned, but should not be assumed when reading current bundles.

---

## The tune-artifact pipeline

Channel calibration is what makes raw samples *interpretable*. Tune artifacts are what make the rig's heater *useful* — they document the rig-and-day-specific setpoint that delivers each target flux.

```
heat-flux tune procedure (algorithm)
       │
       │  per accepted target
       ▼
HeatFluxTunePoint  (target_kw_m2, heater_setpoint_c, measured_flux, ...)
       │
       │  appended to in-progress artifact, saved after every target
       ▼
configs/calibrations/flux/<id>.toml + latest.toml
       │
       │  next tune session reads this as the initial-setpoint prior
       │  CAPA profile UI offers to populate HeaterProgram.heater_setpoint_c
       ▼
referenced from experiment YAML as HeaterProgram.flux_calibration_ref
```

The artifact is rig-specific (`rig`, `heater_device`, `flux_channel`, `gauge_calibration_ref`, `geometry`) so a downstream consumer can refuse to apply an artifact made on a different rig. The setpoints don't transfer between rigs — they describe one specific rig's heater↔gauge geometry on one specific day.

### Operational artifact today

The tune artifact is currently persisted as an operational copy under
`configs/calibrations/flux/` (or the configured `persist_dir`). The tune
run's bundle still records the procedure config and audit events, but it
does not yet embed a tune-artifact TOML sidecar. For long-term study
archives, keep the operational artifact with the bundle or cite its id in
the subsequent specimen run's `flux_calibration_ref`.

---

## When to re-calibrate (channel side)

| Trigger | What to do |
|---|---|
| Drift suspected — channel readings disagree with a reference instrument | Run the lab calibration procedure against a reference; build a new `CalibrationSet`. |
| Hardware swap — thermocouple, gauge, MFC replaced | New cal curve required; the old one no longer applies to the wire. |
| Field shift — gauge moved, fixture geometry changed | The relationship between wire signal and engineering value changed; recalibrate. |
| Periodic review — lab policy 6 / 12 month cycle | Whatever the SOP demands. |

Channel calibration is a *lab discipline* — the curves are produced offline, reviewed, and committed to `configs/calibrations/`. There is no shipped capa procedure for the channel-cal lab workflow; it is operator-driven.

## When to re-tune (heat-flux side)

| Trigger | What to do |
|---|---|
| Daily / per-session | The recommended cadence — heater coil emissivity drifts, gauge wiring changes, ambient temperature differs. A fresh `capa_flux_<today>.toml` keeps the artifact fresh and the `capa.flux_calibration_freshness` preflight green. |
| Heater coil replaced | Coil age and emissivity reset; old artifact's setpoints are stale by tens of °C in some regions. |
| Gauge replaced or its `gauge_calibration_ref` changed | The wire-side calibration that the artifact's flux numbers depend on changed. Old artifact's setpoints no longer correspond to the same delivered flux. |
| Geometry changed — gauge plane moved, holder swapped | The flux-vs-setpoint curve depends on the gauge's position relative to the heater. |

The tune procedure is a *capa procedure* — armed from the Run tab like any other. See [Tuning workflow](tuning-workflow.md) for the operator path.

---

## See also

- [Calibration sets](calibration-sets.md) — channel-cal file format, transform kinds, uncertainty spec, diff workflow.
- [Tune artifacts](tune-artifacts.md) — tune-side file format, schema, partial-save discipline, interpolation helpers.
- [Heat-flux tune procedure — the algorithm](heat-flux-tune-procedure.md) — convergence rule + predicate that produces tune artifacts.
- [Procedure: heat-flux tune](../procedures/builtin-heat-flux-tune.md) — operator/API view of the same procedure.
- [Tuning workflow](tuning-workflow.md) — end-to-end: arm → watch → save.
- [Calibrations on disk](../configuration/calibrations.md) — how the channel-cal subsystem fits into the configuration story.
