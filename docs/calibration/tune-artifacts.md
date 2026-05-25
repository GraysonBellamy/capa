# Tune artifacts

**Audience:** calibration authors, downstream tools, anyone parsing `configs/calibrations/flux/*.toml`.
**Scope:** the [`HeatFluxTuneArtifact`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/calibration/tune_artifact.py) schema, the file/pointer protocol under `configs/calibrations/flux/`, the partial-save discipline, the two interpolation helpers, and what the current runtime does and does not persist.

A tune artifact is **not** a channel calibration. It records a heater-setpoint↔delivered-flux mapping for one rig on one day; it does not transform raw samples into engineering units. The orthogonal channel-level calibration system is documented under [Calibration sets](calibration-sets.md) — the two subsystems share a directory tree but no other implementation. See [Calibration overview](overview.md) for the split.

---

## On-disk layout

```
configs/calibrations/
    sim_default.toml                  ← channel-cal (different subsystem)
    flux/
        capa_flux_2026-05-17.toml     ← one artifact per save
        capa_flux_2026-05-18.toml
        capa_flux_2026-05-18.toml.bak-2026-05-19   ← see "pointer redirect"
        capa_flux_2026-05-24.toml
        latest.toml                   ← one-line pointer to the latest id
```

One file per save under `configs/calibrations/flux/`, plus a `latest.toml` pointer. The id format is `<artifact_id_prefix>_<YYYY-MM-DD>` (default prefix `capa_flux`) — one artifact per day per rig is the intended cadence.

### `latest.toml`

```toml
id = "capa_flux_2026-05-24"
updated_at = 2026-05-24 18:14:50.870291+00:00
```

Two keys, both present, both required. Consumers that want "the most recent artifact" read this pointer rather than scanning the directory — the directory contains backups and partial saves that should not be silently picked up.

### Never-silent-overwrite

[`save_artifact`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/calibration/tune_artifact.py) refuses to overwrite an existing `<id>.toml`. If yesterday's tune is still on disk and today's session somehow picked the same id, the save raises `TuneArtifactError` — the operator deserves to see the conflict rather than have yesterday's tune silently clobbered.

This is also why the artifact id derives from the **calendar date** rather than the run id: two tune sessions on the same day with the same `artifact_id_prefix` is the case the never-overwrite rule is protecting against. Either delete the morning's artifact deliberately or set a different `artifact_id_prefix` for the afternoon's session.

### Pointer redirect and dated backups

When `latest.toml` is repointed at a new file, the *previous* target file is **copied** to `<previous-id>.toml.bak-<YYYY-MM-DD>` first. The backup uses today's date, not the artifact's accepted date. The original `<previous-id>.toml` stays in place — the `.bak-…` copy is an audit ledger of "this was the latest on this date," not a relocation.

This matters when a tune session crashes mid-target and writes partial saves with the same id across iterations. Within a single session, the previous-id check sees the same id as the new file, so no same-session backup is taken — the save unlinks-and-rewrites in place (see [Partial-save discipline](#partial-save-discipline) below).

---

## Schema

The artifact is a single TOML file validated by [`HeatFluxTuneArtifact`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/calibration/tune_artifact.py). The schema is frozen and `extra="forbid"` — unknown keys fail validation.

### `HeatFluxTuneArtifact`

| Field | Type | Notes |
|---|---|---|
| `id` | str | `<prefix>_<YYYY-MM-DD>`. Filename without `.toml`. |
| `rig` | str | Hardware-profile name. A downstream consumer can refuse to apply an artifact created on a different rig. |
| `heater_device` | str | Device id from the hardware profile. |
| `heater_setpoint_channel` | str | Channel the artifact's setpoints were written to. |
| `heater_pv_channel` | str | Channel the PV gate read. |
| `flux_channel` | str | Channel the measured flux came from. |
| `gauge_calibration_ref` | str \| missing | Link to the gauge's V→kW/m² calibration cert. **Bookkeeping signal** — if this changes between two artifacts, their setpoints no longer correspond to the same delivered flux. |
| `geometry` | str | Free-text gauge-placement description (e.g. `"40 mm below heater, centerline"`). |
| `accepted_at` | datetime (UTC) | When the artifact was finalized. Preserved as a native TOML datetime — round-trips through `from_toml`. |
| `operator_id` | str \| missing | Operator who ran the tune. |
| `procedure_id` | str | `"capa.builtin.heat_flux_tune"` for now. |
| `procedure_version` | str | PEP 440 version of the procedure plugin. |
| `capa_git_sha` | str \| missing | capa source commit. Populated when the procedure was built from a tagged install. |
| `points` | array of `HeatFluxTunePoint` | Ordered by acceptance, which is the same as the operator-supplied `targets_kw_m2` order. |

Optional fields (`gauge_calibration_ref`, `operator_id`, `capa_git_sha`) are emitted as **missing keys** when None, not as empty strings — TOML has no null literal, and the serializer's `_strip_none` discipline keeps the file readable.

### `HeatFluxTunePoint`

One row per accepted target.

| Field | Unit | Notes |
|---|---|---|
| `target_flux_kw_m2` | kW/m² | Operator-supplied target. Must be > 0. |
| `heater_setpoint_c` | °C | Setpoint that delivered the target. |
| `measured_flux_mean_kw_m2` | kW/m² | Windowed mean flux at acceptance. |
| `measured_flux_std_kw_m2` | kW/m² | Windowed std. ≥ 0. |
| `measured_flux_slope_kw_m2_per_min` | kW/m²/min | Windowed slope. Signed. |
| `heater_pv_mean_c` | °C | Windowed mean heater PV. |
| `soak_s` | s | Total dwell from the iteration's setpoint command to acceptance. |
| `accepted` | bool | `True` for `algorithm_converged` / `operator_override`; `False` for `warn_proceeded`. |
| `accept_reason` | `algorithm_converged` \| `operator_override` \| `warn_proceeded` | See below. |

### Example

```toml
id = "capa_flux_2026-05-24"
rig = "capa_real_full"
heater_device = "heater"
heater_setpoint_channel = "heater.setpoint"
heater_pv_channel = "heater.pv"
flux_channel = "heat_flux_gauge"
geometry = "40 mm below heater, centerline"
accepted_at = 2026-05-24 18:14:50.867469+00:00
procedure_id = "capa.builtin.heat_flux_tune"
procedure_version = "0.1.0"

[[points]]
target_flux_kw_m2 = 50.0
heater_setpoint_c = 726.970337753898
measured_flux_mean_kw_m2 = 49.904800492603634
measured_flux_std_kw_m2 = 0.17718712722569072
measured_flux_slope_kw_m2_per_min = 0.09137477944618944
heater_pv_mean_c = 726.9612397907554
soak_s = 1754.5979678
accepted = true
accept_reason = "algorithm_converged"
```

---

## The three `accept_reason` values

Every point records *why* it was accepted. Downstream readers MUST treat these as distinct.

| `accept_reason` | `accepted` | Meaning |
|---|---|---|
| `algorithm_converged` | `True` | The convergence rule (two consecutive in-tolerance windows + verification soak) was satisfied. The standard happy path. Use freely. |
| `operator_override` | `True` | The operator pressed "Accept Current" on the dock. The predicate may not have held; the verify soak was skipped. The point reflects the operator's judgment that the field was good enough; downstream consumers can use it but should record the override in any derived report. |
| `warn_proceeded` | `False` | The iteration cap was exhausted (`n_iter_max`) or the wall-clock budget ran out (`t_total_max_s`). The last reading was preserved so earlier targets in the same session still have a usable artifact, but **this specific target did not converge**. Interpolation helpers filter these out. |

The `accepted` boolean and `accept_reason` are not redundant — the `warn_proceeded` branch always pairs with `accepted=False`, while the other two pair with `accepted=True`. Reader code should check both: `accepted` for "trust this point at face value" and `accept_reason` for "explain why" in any derived UI or report.

---

## Partial-save discipline

The procedure saves the artifact **after every accepted target**, not just at session end. From [controller.py](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/heat_flux_tune/controller.py):

```
target 1 accepted → save artifact with [point 1]
target 2 accepted → save artifact with [point 1, point 2]
target 3 accepted → save artifact with [point 1, point 2, point 3]
                    (crashes here)
final state on disk: artifact with [point 1, point 2, point 3]
```

A session that loses power on target #3 of #4 still leaves a usable three-point artifact on disk. The id is the same across all partial saves within a session, so each subsequent save unlinks the existing file and rewrites it.

Because the id doesn't change within a session, the [pointer-redirect](#pointer-redirect-and-dated-backups) backup logic does **not** create a same-session `.bak-…` file. A new backup is taken only when `latest.toml` is redirected to a *different* id — the next day's tune, typically.

---

## Interpolation helpers

The artifact carries two pure-Python helpers that downstream code calls. Both filter out points where `accepted=False`.

### `setpoint_for_target(target_kw_m2) -> float | None`

Linearly interpolates `target_kw_m2` against the accepted points. Returns `None` when:

- The artifact has no accepted points.
- `target_kw_m2` falls outside the bracket of accepted targets (it would require extrapolation).

**Extrapolation is explicitly refused.** A flux↔setpoint curve outside the brackets is not characterised by this session; silently extrapolating would produce a plausible number that is wrong by an unknown amount. Returning `None` forces the caller to handle the missing-prior case explicitly.

Used by the heat-flux tune procedure with `initial_guess="lookup"` to pick the first heater setpoint for each target, and by the CAPA profile UI to pre-populate `HeaterProgram.heater_setpoint_c` from `HeaterProgram.target_heat_flux_kw_m2`.

### `local_df_dt(target_kw_m2) -> float | None`

Returns the local secant slope `d(flux)/d(setpoint)` (kW/m² per °C) around `target_kw_m2`, taken across the bracketing accepted points. Returns `None` when fewer than two points are accepted, or when the two endpoints share a setpoint (degenerate division).

The tune procedure uses this as the iteration-1 Jacobian prior — the `df_dt_source="prior"` branch in [`_estimate_df_dt`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/heat_flux_tune/controller.py). Once the procedure has two in-session points it switches to the secant across those instead.

---

## Where the artifact is persisted

The heat-flux tune procedure writes the operational copy under
`persist_dir` (default `configs/calibrations/flux/`). Future tune
sessions with `initial_guess="lookup"`, the CAPA profile UI, and
external audit tools read that directory.

Current behavior is intentionally simple:

- If `persist_dir` is set, each accepted point triggers a save to `<persist_dir>/<id>.toml` and updates `latest.toml`.
- If `persist_dir = null`, artifact-file persistence is skipped for that session.
- The run bundle records the tune session's config and audit events, but it does **not** currently include an automatic tune-artifact sidecar.

If you need a tune artifact to travel with an archived study today,
archive the operational `*.toml` alongside the bundle or cite its id in
the subsequent specimen run's `flux_calibration_ref`.

---

## Reading an artifact from Python

```python
from pathlib import Path
from capa.calibration.tune_artifact import load_artifact, load_latest

# Read a specific artifact by path.
art = load_artifact(Path("configs/calibrations/flux/capa_flux_2026-05-24.toml"))
print(art.rig, len(art.points))

# Or follow the latest pointer.
art = load_latest(Path("configs/calibrations/flux"))
if art is None:
    print("no tune on disk")
else:
    sp = art.setpoint_for_target(50.0)
    print(f"50 kW/m² → {sp:.2f} °C")
```

`load_latest` returns `None` for three legitimate "absent" cases:

- The directory does not exist.
- `latest.toml` is missing.
- `latest.toml`'s `id` pointer references a file that no longer exists (e.g. someone manually deleted it).

It raises `TuneArtifactError` only for *corrupt* states — a `latest.toml` that won't parse, an `id` field that isn't a non-empty string, an artifact file that fails schema validation. The distinction matters: absent is a normal first-run case; corrupt requires operator attention.

---

## Auditing tune history

The audit story today is *read the directory*: list `capa_flux_*.toml`, sort by date, eyeball the `points[]` blocks. Things that show up by inspection:

- **Drift in `heater_setpoint_c` for the same target.** A 50 kW/m² target moving from 720 °C to 740 °C over a few weeks suggests heater coil aging — worth checking emissivity and the gauge calibration cert.
- **A different `gauge_calibration_ref` between sessions.** Setpoints across artifacts are no longer directly comparable; the gauge's V→kW/m² curve changed under them.
- **A run of `accept_reason="warn_proceeded"` rows.** The predicate is failing to fire — usually a sign the rig's noise envelope has changed (cabinet door open, new gauge cable) and the predicate thresholds need tuning.

There is no shipped CLI for diffing artifacts. The TOML format and the `HeatFluxTuneArtifact` Pydantic model are stable enough that a notebook with `load_artifact` and a few `for` loops covers the day-to-day need.

---

## See also

- [Heat-flux tune procedure — the algorithm](heat-flux-tune-procedure.md) — what writes these artifacts.
- [Procedure: heat-flux tune](../procedures/builtin-heat-flux-tune.md) — config that controls what fields end up in the artifact (`gauge_calibration_ref`, `geometry`, `artifact_id_prefix`).
- [Calibration overview](overview.md) — how tune artifacts differ from channel calibration sets.
- [CAPA profile fields](../configuration/capa-profile.md#heaterprogram) — where `flux_calibration_ref` lives in profile metadata.
