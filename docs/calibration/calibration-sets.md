---
description: capa channel-calibration TOML format — CalibrationSet curves, linear_two_point and polynomial transforms, UncertaintySpec, diff workflow for Setup-tab drafts.
---

# Calibration sets

**Audience:** calibration authors writing `configs/calibrations/*.toml` files; operators applying a set in the Setup tab; analysts reading channel curves out of a bundle.
**Scope:** the on-disk TOML format of a `CalibrationSet`, every supported transform kind, the `UncertaintySpec` discipline, and the diff workflow used to apply a set to a draft.

This page covers **channel-level calibration** — the transform that turns a raw adapter sample into an engineering-unit channel sample. The orthogonal heat-flux *tune* artifact (which records a heater-setpoint↔delivered-flux mapping) is documented under [Tune artifacts](tune-artifacts.md). See [Calibration overview](overview.md) for why they are separate.

---

## On-disk layout

```
configs/calibrations/
    thermocouples_2026Q2.toml         ← a CalibrationSet (this page)
    flux/
        capa_flux_2026-05-24.toml     ← a HeatFluxTuneArtifact (different subsystem)
        latest.toml
```

One TOML file per set. Filenames are free-form; the `name` inside the file identifies the set. Conventional naming: `<channel-group>_<period>.toml` (e.g. `thermocouples_2026Q2.toml`).

---

## Schema

A set has three top-level fields plus a `[curves.*]` table per channel name. The model lives in [`src/capa/channels/calibration.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/channels/calibration.py):

```toml
name = "thermocouples_2026Q2"
revision = "3"

[curves.sample_tc_1]
kind = "linear_two_point"
input_unit = "V"
output_unit = "degC"
ref_low_raw = 0.0
ref_low_value = 0.0
ref_high_raw = 0.01
ref_high_value = 250.0

[curves.sample_tc_1.uncertainty]
kind = "absolute"
value = 0.5
coverage_factor = 2.0
method = "type-K reference + ice-point cross-check, k=2"

[curves.sample_tc_1.fit_metadata]
reference_instrument = "Fluke 1551 Stik"
reference_serial = "98765"
fitted_at = 2026-04-02T10:30:00+00:00
rms_residual = 0.18
notes = "two-point at ice and 250 °C oil bath"
```

| Field | Notes |
|---|---|
| `name` | The set's identifier. |
| `revision` | Free-form revision string. Bump when curves change. |
| `[curves.<channel-name>]` | One table per channel. The key is the channel name as it appears in the hardware profile. |

Every curve table carries:

| Field | Notes |
|---|---|
| `kind` | Discriminator — one of `identity`, `linear_two_point`, `polynomial`, `lookup`, `piecewise`, `custom_callable`. |
| `input_unit` | Wire unit. Validated against the unit registry. |
| `output_unit` | Engineering unit. Validated against the unit registry; must agree with the channel's declared engineering unit at apply time. |
| `uncertainty` | Optional sub-table. See [Uncertainty](#uncertainty) below. |
| `fit_metadata` | Optional sub-table. See [Fit metadata](#fit-metadata) below. |
| Per-kind fields | See each kind below. |

The dimensional algebra is checked at construction. An `Identity` whose units aren't dimensionally compatible, or a `LinearTwoPoint` whose `ref_high_raw == ref_low_raw`, fails validation.

---

## Transform kinds

### `identity`

```toml
[curves.heater_pv]
kind = "identity"
input_unit = "degC"
output_unit = "degC"
```

Pass-through. `evaluate(raw) → raw`. Required for channels whose adapter already delivers values in engineering units (Watlow temperature channels, MFC channels reporting calibrated sccm) — the data still needs a calibration entry so the bundle records "this channel was identity-calibrated" rather than "this channel is uncalibrated."

`input_unit` and `output_unit` must be **dimensionally compatible**. They don't need to be the literal same string (e.g. `degC` ↔ `K` could work in principle), but the runtime currently treats them as the same. Use the same unit on both sides unless you have a deliberate reason.

Identity is invertible — `invert(value) → value` — which matters on the setpoint write path so a derived-unit value round-trips through the identity calibration unchanged.

### `linear_two_point`

```toml
[curves.sample_tc_1]
kind = "linear_two_point"
input_unit = "V"
output_unit = "degC"
ref_low_raw = 0.0
ref_low_value = 0.0
ref_high_raw = 0.01
ref_high_value = 250.0
```

Two-point linear fit: `value = slope · raw + intercept`. Slope and intercept are computed at construction from the two reference pairs, so the manifest and the runtime path agree (the bundle serializes `ref_low_*` / `ref_high_*`, not the derived slope).

Also invertible — used on the setpoint write path to convert a user-facing (`output_unit`) value back into the wire-unit (`input_unit`) the device expects.

### `polynomial`

```toml
[curves.exhaust_temp]
kind = "polynomial"
input_unit = "V"
output_unit = "degC"
coefficients = [0.0, 24987.5, -0.4173]   # y = c0 + c1*raw + c2*raw²
```

Horner-evaluated `y = c0 + c1·raw + c2·raw² + …`. Coefficients are in **ascending** order. At least one coefficient is required (a zero-degree polynomial is a constant — degenerate but legal).

Polynomial uncertainty propagation is currently the same as the base `UncertaintySpec` (no Monte-Carlo per-evaluation propagation in the shipped code).

### `lookup`

```toml
[curves.k_type]
kind = "lookup"
input_unit = "V"
output_unit = "degC"
table = [
    [0.000, 0.0],
    [0.002, 50.0],
    [0.004, 100.0],
    [0.020, 500.0],
    [0.040, 1000.0],
]
```

Linear interpolation over a sorted `(raw, value)` table. **Out-of-range raws clamp to the nearest endpoint** — the variant declares this explicitly rather than silently extrapolating. Extrapolated calibrations are a recurring source of "the data looked plausible" bugs; the explicit clamp produces a wrong but obvious value (the table endpoint), and the bundle's audit trail makes the clamp visible.

Validation:
- The table must have ≥ 2 rows.
- `raw` values must be **strictly ascending** — sorting alone is not enough; duplicate raws fail validation.

### `piecewise`

```toml
[curves.broad_range_sensor]
kind = "piecewise"
input_unit = "V"
output_unit = "kPa"

[[curves.broad_range_sensor.segments]]
raw_min = 0.0
raw_max = 0.005
coefficients = [0.0, 200000.0]            # 0 → 1 kPa per millivolt

[[curves.broad_range_sensor.segments]]
raw_min = 0.005
raw_max = 0.020
coefficients = [-3000.0, 800000.0]        # different slope above 5 mV
```

Sequence of polynomial segments. Two **continuity invariants** are checked at construction:

1. Adjacent segments must share their boundary raw value (`prev.raw_max == next.raw_min`).
2. Adjacent segments must produce the same value at that boundary (`prev.evaluate(boundary) ≈ next.evaluate(boundary)` within `1e-9 × max(1, |value|)`).

Discontinuous piecewise fits silently produce step artifacts in plots; the construction-time check refuses them. If you legitimately want a discontinuity (a sensor whose response changes character at a known threshold), build two separate calibrations and switch channels rather than fighting the continuity check.

Out-of-range clamps at the endpoints like `lookup`.

### `custom_callable`

```toml
[curves.special_tc]
kind = "custom_callable"
input_unit = "V"
output_unit = "degC"
entry_point = "labkit.thermocouples:k_type_v3"
package = "labkit"
version = "2.4.1"
distribution_hash = "sha256:abcd..."
callable_id = "thermocouple.k_type_v3"
parameters = { ref_junction_c = 22.0 }
test_vectors = [[0.001, 25.0], [0.020, 491.5]]
```

Reference to an installed callable. A custom calibration must name an entry point, a package name + version, the distribution hash, a stable callable id, serialized parameters, input/output dimensions, **and** test vectors. Anonymous lambdas and unversioned scripts are a config error.

| Field | Notes |
|---|---|
| `entry_point` | `"package.module:callable"` — resolvable via `importlib.metadata`. The `:` is required; missing it fails validation. |
| `package` | Distribution name. Cross-checked against `plugins.lock`. |
| `version` | PEP 440. |
| `distribution_hash` | SHA-256 of the installed wheel/sdist, matched against the lockfile. Algorithm prefix (`sha256:` or `sha512:`) is enforced. |
| `callable_id` | Stable id within the package (e.g. `"thermocouple.k_type_v3"`). The package can refactor its module layout as long as the id stays the same. |
| `parameters` | Serialized parameters passed to the callable. Scalars only (`float`, `int`, `str`, `bool`). |
| `test_vectors` | `(raw, expected_value)` pairs. The callable must reproduce these at calibration-load time — used as a self-test. |

`evaluate()` raises if called without the plugin runtime resolved — `CustomCallable` validates its *schema* in isolation, but actually computing values requires the procedure plugin runtime. The callable metadata travels with the channel's curve, so the bundle's `config.toml` records it.

This is **the only calibration variant gated by the plugins lockfile.** Built-in variants (`identity`, `linear_two_point`, etc.) don't need lockfile admission because their algebra is part of capa itself.

---

## Uncertainty

Every calibration declares an `uncertainty` block — *or* an explicit absence. capa refuses to let an uncertainty be silently zero.

```toml
[curves.sample_tc_1.uncertainty]
kind = "absolute"           # or "relative"
value = 0.5                 # output units for "absolute", dimensionless fraction for "relative"
coverage_factor = 2.0       # k-factor: 1 = standard, 2 = ~95% expanded
method = "type-K + ice-point cross-check"
```

| Field | Notes |
|---|---|
| `kind` | `"absolute"` — `value ± uncertainty` in output units. `"relative"` — `value × (1 ± uncertainty)` as a dimensionless fraction (`0.01 = 1%`). |
| `value` | Magnitude. ≥ 0. |
| `coverage_factor` | k-factor. Default 1.0. > 0. Recorded so an analyst five years later quotes the right confidence interval without guessing. |
| `method` | Free-text description of how the uncertainty was estimated (residuals from a fit, manufacturer spec, …). Optional but strongly recommended. |

Calibrations without an `[uncertainty]` block load with `uncertainty = None`, which is the *explicit "unmeasured"* state. The bundle records the `None`, and downstream `evaluate_with_uncertainty()` returns `(value, None)`. An analyst can distinguish "we measured the uncertainty as zero" from "no uncertainty was characterised" — silent-zero is impossible.

For relative uncertainties, `absolute_for(value)` returns `relative_fraction × |value| × coverage_factor` — the dimensioned magnitude at a specific evaluation point.

---

## Fit metadata

The pedigree of a calibration produced by a *procedure* (a lab calibration run, not a hand-written file).

```toml
[curves.sample_tc_1.fit_metadata]
reference_instrument = "Fluke 1551 Stik"
reference_serial = "98765"
fitted_at = 2026-04-02T10:30:00+00:00
rms_residual = 0.18
source_procedure_id = "lab.tc_two_point_fit"
capa_git_sha = "abc123..."
notes = "two-point at ice and 250 °C oil bath"
```

| Field | Notes |
|---|---|
| `reference_instrument` | Required when this block is present. |
| `reference_serial` | Optional. |
| `fitted_at` | UTC datetime. Required. |
| `rms_residual` | Fit residual in output units. Optional. |
| `source_procedure_id` | Capa procedure id that produced this fit. Optional — hand-built calibrations don't have one. |
| `capa_git_sha` | capa source commit at fit time. Optional. |
| `notes` | Free-text. |

The whole block is optional. Hand-typed calibrations skip it entirely. The point of having it is that a curve's origin should be **recoverable without trusting human-written notes** — `reference_instrument`, `reference_serial`, `fitted_at` together name the artifact that should exist in the lab notebook.

---

## How a set reaches a run

An experiment doesn't reference a calibration set. Each channel carries its
own curve in the hardware profile (`[channels.calibration]`), and that is the
curve the run uses. A set moves curves onto channels in bulk: the Setup tab's
Calibration section applies a set to the draft's channels (below), and exports
the draft's current curves as a new set. Once applied and saved, the curves
are part of the hardware TOML.

---

## The Setup-tab apply-set diff dialog

The Setup tab's Calibration section has an "Apply set…" action that walks an operator through which curves *would* change before committing. The mechanics live in [`calibration_set_io.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/config/calibration_set_io.py).

Every (channel, set entry) pair lands in one of five classes:

| `DiffKind` | What it means | Pre-checked? |
|---|---|---|
| `override_identity` | Draft channel currently has an `identity` curve; the set provides a real curve. | **yes** — applying cannot make things worse, the channel had no meaningful calibration before. |
| `override_existing` | Draft channel has a non-`identity` curve; the set's curve differs. | **no** — this is destructive (operators have lost characterisation work this way). The operator must explicitly opt in. |
| `matches` | Draft channel's curve already equals the set's entry. | n/a (informational, no apply checkbox). |
| `set_only` | Set has a curve for a channel the draft doesn't define. | n/a — there is no channel to attach it to. |
| `channel_only` | Draft has a channel the set doesn't cover. | n/a — the set doesn't change this channel. |

The dialog renders all five classes so the operator sees the **full picture**, not just the rows that would change. The default-pre-checked policy (only `override_identity`) is deliberately conservative: a destructive overwrite of a real curve always requires a manual opt-in, every time.

---

## In the bundle

The bundle's `config.toml` holds the frozen hardware profile, so it records
every channel's curve as the run used it — `kind`, parameters, `uncertainty`
and `fit_metadata` — under `hardware.channels[*].calibration`. Which set the
curves came from is not recorded; give a curve `fit_metadata` when its origin
matters.

---

## See also

- [Calibration overview](overview.md) — the two-axis split between channel calibrations and tune artifacts.
- [Calibrations on disk](../configuration/calibrations.md) — same content, framed from the Configuration nav.
- [Channel bindings](../configuration/channel-bindings.md) — how channels are declared on the hardware profile; calibration sets apply to channel *names*.
- [Manifest and schema](../bundles/manifest-and-schema.md) — what the bundle manifest does and does not index today.
