---
description: Field reference for `capa.profiles.capa_pyrolysis` domain profile — specimen, HeaterProgram, Atmosphere, DownstreamAnalyzer, preflight checks for pyrolysis.
---

# CAPA profile fields

**Audience:** CAPA pyrolysis operators authoring an experiment YAML; anyone parsing `profiles/capa_pyrolysis.toml` from a bundle.
**Scope:** every field of the `capa.profiles.capa_pyrolysis` domain profile, what it captures, and why each field is in the schema. This is the project's default domain profile.

A *domain profile* layers scientific metadata + preflight checks on top of the generic experiment recipe. It does not drive the run — that's the [procedure](../procedures/what-is-a-procedure.md)'s job. The profile contributes:

- **specimen fields** — id, material, mass, form, holder geometry
- **method fields** — heater program (target heat flux + heater setpoint), atmosphere composition, optional secondary gas
- **required channel groups** — heater pair, sample TC, mass, purge MFC
- **preflight checks** — heater PV safe range, purge flow established, leak-test recency, balance stability

The profile sits at `experiment.domain_profile.id = "capa.profiles.capa_pyrolysis"` in the experiment YAML. When set, the Setup tab's CAPA Profile section appears with all the fields below.

CAPA is a **controlled-atmosphere cone-calorimeter-class instrument**: a specimen sits in a holder on a load cell under a radiant heater, swept by a purge gas to control atmosphere chemistry. The scientific parameter is the radiant heat flux at the specimen surface (kW/m²). Most runs are a single setpoint hold; dynamic programs (ramps) are the minority.

CAPA is **not a TGA** — there is no crucible, no carrier gas in the TGA sense, and the controller closes on heat flux at the specimen rather than a heating rate. The terminology is documented in the [glossary](../glossary.md).

---

## Schema overview

The full metadata block is [`CapaPyrolysisMetadata`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/profiles/capa_pyrolysis.py):

```yaml
domain_profile:
  id: capa.profiles.capa_pyrolysis
  metadata:
    specimen: { ... }      # CapaSpecimen
    program: { ... }       # HeaterProgram
    atmosphere: { ... }    # Atmosphere (purge + optional reactive)
    analyzer: { ... }      # DownstreamAnalyzer (optional)
    sop_revision: "..."    # optional
```

Every sub-model is frozen and `extra="forbid"` — typos in field names fail validation, they do not silently default. The Pydantic models live in [`capa_pyrolysis.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/profiles/capa_pyrolysis.py).

---

## Specimen (`CapaSpecimen`)

The physical sample under test.

| Field | Unit | Required | Notes |
|---|---|---|---|
| `id` | — | yes | Operator-assigned specimen id. Goes into `sample.id` and child-bundle templates for Batch runs. |
| `material` | — | yes | Free-text material name. The value an analyzer five years from now needs to know "what was this?" |
| `initial_mass_g` | g | yes | Mass on the load cell before heating begins. Must be > 0. |
| `form` | `disk` \| `other` | yes | ~99% of CAPA runs use a disk. `other` is the escape hatch for irregular solids, liquids, etc.; describe in `notes`. |
| `particle_size_um` | µm | no | Median particle size for powder/granulate runs. Leave unset for the typical solid disk. |
| `specimen_holder` | — | yes | Holder description (e.g. `"stainless steel cup"`). Holder geometry varies by run; depth and diameter change the exposed surface area. |
| `specimen_holder_diameter_mm` | mm | no | Outside / nominal diameter of the holder cup. |
| `specimen_holder_depth_mm` | mm | no | Internal cup depth. Together with diameter, captures the cavity geometry that affects exposed surface area. |
| `conditioning` | — | no | Pre-test conditioning (drying, desiccator, storage humidity). |
| `notes` | — | no | Free text. |

### Why these are captured

The specimen fields are not optional record-keeping. Five years later, an analyst reopening the bundle reconstructs the run from these values:

- **Mass** sets the integration baseline for the load cell's mass-loss trace.
- **Form + holder geometry** sets the exposed surface area, which is needed to convert mass-loss rate into a mass-loss flux.
- **Particle size + conditioning** explain transport-limited effects that the rate trace alone cannot account for.

Missing fields are a Layer-3 validation error and the engine refuses to arm.

---

## Heater program (`HeaterProgram`) { #heaterprogram }

**The operator's declared heater program.** This block records *intent*; the actual command sequence lives in the [`Method`](method-toml.md). The summary is captured here so a downstream analyzer can classify the run ("50 kW/m² hold, no ramp") without parsing the method graph.

| Field | Unit | Required | Notes |
|---|---|---|---|
| `target_heat_flux_kw_m2` | kW/m² | yes | **The scientific parameter for the run.** Operators choose this; the heater setpoint is derived from it via the flux↔setpoint calibration. |
| `heater_setpoint_c` | °C | yes | Heater temperature setpoint chosen to deliver the target flux. Comes from the day's tune artifact (or the most recent on-disk one). |
| `flux_calibration_ref` | — | no | Free-form pointer to the heat-flux↔heater-setpoint calibration used. Typically a [tune artifact id](../calibration/tune-artifacts.md) (`"capa_flux_2026-05-24"`) but can be a lab-notebook entry. |
| `ramp_rate_c_per_min` | °C/min | no | Optional ramp rate for dynamic programs. Leave unset for the common single-setpoint hold. |

### `flux_calibration_ref` is not an auto-apply

Setting `flux_calibration_ref = "capa_flux_2026-05-24"` does **not** automatically load that artifact's heater setpoint for the target flux. It records, for the bundle, *which* calibration the operator was relying on. The operator still types the matching `heater_setpoint_c` into the form (the Setup-tab UI offers to pre-populate it from a tune artifact via the [lookup workflow](../calibration/tuning-workflow.md)).

This is deliberate: the artifact is the *source of truth* for the calibration, but human-typed fields stay human-typed so an operator who knows yesterday's tune was suspect can override deliberately.

---

## Atmosphere (`Atmosphere`)

Controls the gas atmosphere the specimen sees during the run.

| Field | Type | Notes |
|---|---|---|
| `mode` | `inert` \| `oxidative` \| `reducing` \| `reactive_blend` | Coarse classification. Drives the `capa.atmosphere_consistency` preflight for modes that need a reactive-gas flow channel. |
| `purge` | `PurgeGas` | The inert/sweep gas spec. Always required. |
| `reactive` | `ReactiveGas` \| `None` | Optional secondary gas for partial-oxidation or doped-purge experiments. `None` for pure-inert runs. |
| `purge_duration_s` | s | How long the reactor is swept with purge gas before heating begins. Captured so downstream analysis knows the pre-run sweep duration. |
| `leak_check_at` | datetime | Most-recent leak / pressure-decay check timestamp. Read by the `capa.leak_test_recency` preflight (default 7-day window, non-blocking warning). |

### `PurgeGas`

| Field | Notes |
|---|---|
| `species` | Common name: `"N2"`, `"Ar"`, `"He"`, `"air"`, `"5% O2/N2"`. |
| `purity` | Grade / purity: `"UHP 5.0"`, `"99.999%"`, `"zero-grade air"`. |
| `supplier` | Optional. |
| `cylinder_lot` | Optional. |
| `target_flow_sccm` | Operator's intended setpoint. **The MFC channel is the actual source of truth.** Setting this to 0 opts out of the `capa.purge_flow_established` preflight. |

### `ReactiveGas`

Same shape as `PurgeGas` plus an optional `target_mole_fraction` (0–1) recording the operator's intended blend fraction. The actual blend depends on both MFCs and is what the channel data records — the mole fraction here is captured for intent.

---

## Downstream analyzer (`DownstreamAnalyzer`, optional)

CAPA pyrolysis is often paired with FTIR / GC / MS / NDIR for qualitative product identification. The analyzer is **not** a capa-controlled device — its data lives outside the bundle — but the pedigree fields are captured here so the run record cross-references the right external file.

| Field | Notes |
|---|---|
| `kind` | `ftir` \| `gc` \| `ms` \| `gc_ms` \| `ndir` \| `other` |
| `serial` | Optional analyzer serial. |
| `sampling_line_delay_s` | Transport delay from sample point to analyzer detector. Used to time-align analyzer output with capa channels. |
| `response_time_s` | Analyzer's 90% step response time — the instrument's measurement time constant, distinct from the sampling-line delay. |
| `external_file_ref` | Pointer to the analyzer's data file/dataset. Free-form path or URI; captured into the bundle so a later analyst can re-locate the correlated data. |

CAPA does **not** do oxygen-depletion calorimetry by default — the analyzer block is shaped for "qualitative product analysis," not "quantitative HRR." Use the `cone_calorimeter` profile for the HRR / O₂-depletion workflow.

---

## `sop_revision` (optional)

Lab SOP identifier (`"CAPA-SOP-2026-03"`, etc.). Free-form string captured into the bundle so the run can be cross-referenced against the procedure document the operator was working from.

---

## Required channel groups

The profile requires the following [channel groups](channel-bindings.md) to be mapped on the active hardware profile. Each channel in the hardware TOML declares its role via `metadata.capa_group`; preflight matches them against this list.

| Group | Accepted channel kinds | Min count | Why |
|---|---|---|---|
| `heater_setpoint` | `setpoint` | 1 | The write target. |
| `heater_pv` | `process_var` | 1 | The Watlow's live PV reading. |
| `sample_temperature` | `thermocouple` or `analog_in` | 1 | At least one TC inside or close to the sample. Multi-TC arrays appear in this group too. |
| `purge_gas_flow` | `mfc_flow` or `analog_in` | 1 | The inert/sweep gas MFC. |
| `mass` | `mass` | 1 | Load cell reading the specimen. |

Optional groups (warned, never block):

| Group | Accepted kinds | Why optional |
|---|---|---|
| `reactive_gas_flow` | `mfc_flow` or `analog_in` | Required only when atmosphere `mode` is `oxidative` or `reactive_blend`. The `capa.atmosphere_consistency` preflight catches the inconsistency. |

The required-mapping list is exposed to the Setup editor as a panel of status chips — red until the group is mapped, then green. Layer-3 validation errors when a required group has fewer than `min_count` members.

---

## Preflight checks

The profile contributes the following preflight checks, evaluated when the run is armed. Blocking checks abort arming; non-blocking land in the bundle as warnings.

| Id | Blocking? | What it checks |
|---|---|---|
| `capa.required_channel_mappings` | yes | Every required channel group has at least `min_count` members. |
| `capa.atmosphere_consistency` | yes | Declared atmosphere mode is consistent with declared channels: `oxidative` / `reactive_blend` modes must declare a `reactive_gas_flow`. |
| `capa.heater_pv_in_safe_range` | yes | Heater PV reading is within the rig-survival ceiling (< 1000 °C by default). Catches sensor runaway / miswired channel; **not** a cold-start gate. |
| `capa.purge_flow_established` | yes | Purge gas flow has been seen ≥ `target × 0.5` for ≥ 3 s. Skip by setting `purge.target_flow_sccm = 0`. |
| `capa.leak_test_recency` | no | `Atmosphere.leak_check_at` is within the lab-policy recency window (default 7 days). |
| `capa.flux_calibration_freshness` | no | When `target_heat_flux_kw_m2` is declared, `flux_calibration_ref` is set, and the on-disk tune artifact it points to is within the recency window (default 7 days). |
| `capa.balance_stability` | no | When a mass channel is declared, it reports stable for ≥ 5 s prior to arming. |
| `capa.disk_projection` | yes | Projected bundle size leaves ≥ 1.5× margin on the bundle volume. |

The blocking choices reflect a "fail loud at arm time" policy: anything that would silently produce a misleading bundle blocks; anything that's an operator-judgment call (stale leak check, stale calibration) warns.

---

## Compared to `cone_calorimeter`

The sibling [`cone_calorimeter`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/profiles/cone_calorimeter.py) profile targets ASTM E1354 / ISO 5660-style measurements. It is **not** wired in as a default; opt in via `domain_profile.id = "capa.profiles.cone_calorimeter"` when running an HRR measurement.

The high-level differences:

| | `capa_pyrolysis` (this page) | `cone_calorimeter` |
|---|---|---|
| Scientific output | Mass-loss + product identification | Heat release rate (HRR) by oxygen depletion |
| Required atmosphere channels | Purge MFC (inert) | Exhaust flow + O₂ analyzer |
| Specimen geometry fields | Disk-shaped, holder cup geometry | Thickness + exposed area + orientation |
| Standard reference | Lab SOPs (free-form) | ASTM E1354 / ISO 5660 (built-in standard refs) |
| Analyzer block | Optional FTIR / GC / MS (qualitative) | Required O₂ + CO + CO₂ analyzers (quantitative) |

A run can declare either profile against the same procedure (most often [Recipe runner](../procedures/builtin-recipe-runner.md)); the procedure does not change based on which profile is attached. The profile only changes *what metadata is required* and *what preflight checks run*.

---

## See also

- [What is a procedure](../procedures/what-is-a-procedure.md) — the three-axis split between procedure, method, and profile.
- [Channel bindings](channel-bindings.md) — how the `capa_group` metadata on each channel maps it to a profile's required group.
- [Heat-flux tune procedure](../calibration/heat-flux-tune-procedure.md) — produces the artifact that `flux_calibration_ref` cites.
- [Tune artifacts](../calibration/tune-artifacts.md) — the on-disk format of `flux_calibration_ref` targets.
- [Glossary](../glossary.md) — CAPA terminology.
