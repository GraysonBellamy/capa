# Experiment YAML

**Audience:** config authors composing a full experiment.
**Scope:** every field of an experiment YAML — the top-level recipe
that ties hardware, method, calibration, procedure, and profile
together into one runnable run.

---

## Top-level shape

An experiment file declares one
[`ExperimentConfig`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/config.py).
Every section corresponds to one nested Pydantic model with
`extra="forbid"` — unknown keys are validation errors, not warnings.

The shipped CAPA simulator recipe is a useful reference:

--8<-- "_snippets/sim_capa_pyrolysis.yaml.md"

YAML is recommended because nested metadata reads better than TOML's
flatter table shape; the parser accepts either format and picks by
extension.

## Required vs optional sections

| Section | Required | Default |
|---|:-:|---|
| `hardware` | yes | — |
| `procedure` | yes | — |
| `calibration_set` | yes | — |
| `operator` | yes | — |
| `sample` | yes | — |
| `method` | no | `None` (procedure must accept method-less runs) |
| `domain_profile` | no | `None` (no scientific layer) |
| `storage` | no | `StoragePolicy()` defaults |
| `safety` | no | `SafetyPolicy()` defaults |
| `runtime` | no | `RuntimeConfig()` defaults |
| `run_options` | no | `RunOptions()` defaults |
| `tags` | no | `[]` |
| `custom` | no | `{}` |

## `hardware:` and `method:` — inline or file-ref

Both sections accept either a string path or an inline table.

```yaml
# external file (the common case)
hardware: ../hardware/sim_capa.toml
method:   ../methods/sim_capa_pyrolysis.method.toml
```

```yaml
# inline (for self-contained one-offs and tests)
hardware:
  name: minimal
  devices: []
  channels: []
```

Relative string-ref paths resolve against the experiment file's
directory. When a string-ref is used, the resulting
`ExperimentConfig` records the source path on
`hardware_source_path` / `method_source_path` (excluded from
serialisation but available to the UI) so a "save method" writes back
to the source file rather than inlining into the experiment.

Hardware fields and method step kinds are documented in [Hardware
TOML](hardware-toml.md) and [Method TOML](method-toml.md). Inline
shapes use the same models — just nested in YAML instead of TOML.

## `procedure:`

References a procedure plugin by id:

```yaml
procedure:
  id: capa.builtin.recipe_runner
  version: "0.1"
  config:
    auto_acknowledge_prompts: false
    notes: "PMMA replicate 3"
```

| Field | Required | Notes |
|---|:-:|---|
| `id` | yes | Plugin id. Matched against `plugins.lock`. |
| `version` | no | PEP 440 specifier. `None` = any installed version (pin in production). |
| `config` | no | Plugin-specific config blob. Validated by the procedure's `config_model` at load. |

The four built-in procedures and their `config` shapes are
documented in [What is a procedure](../procedures/what-is-a-procedure.md).
Custom procedures register the same way; see [Writing a
procedure](../extending/writing-a-procedure.md).

## `domain_profile:`

Optional scientific layer. When set, the profile contributes required
channel groups, additional metadata fields, and preflight checks.

```yaml
domain_profile:
  id: capa.profiles.capa_pyrolysis
  standard_refs: []
  metadata:
    specimen:
      id: PMMA-2026-05-24-A
      material: PMMA
      initial_mass_g: 5.0
      form: disk
      specimen_holder: "stainless steel cup"
      conditioning: "23 C / 50% RH for 48h"
    program:
      target_heat_flux_kw_m2: 50.0
      heater_setpoint_c: 600.0
    atmosphere:
      mode: inert
      purge_duration_s: 30.0
      purge:
        species: N2
        purity: "UHP 5.0"
        target_flow_sccm: 100.0
```

| Field | Required | Notes |
|---|:-:|---|
| `id` | yes | Profile plugin id. `capa.profiles.capa_pyrolysis` is the default. |
| `standard_refs` | no | Standard editions (`"ASTM E1354-25"`, `"ISO 5660-1:2015"`). Recorded into manifest. |
| `metadata` | no | Profile-specific metadata. Validated by the profile's `metadata_model`. |

The CAPA-pyrolysis profile's `metadata` schema (specimen, program,
atmosphere blocks) is documented in [CAPA profile
fields](capa-profile.md). Cone-calorimeter and any future profiles
have their own `metadata` shape — switch on `id`.

## `calibration_set:`

Pointer to a CalibrationSet on disk:

```yaml
calibration_set:
  name: default
  revision: "2026-05-24"
```

| Field | Required | Notes |
|---|:-:|---|
| `name` | yes | Logical set name; resolved against `configs/calibrations/`. |
| `revision` | no | Pin a specific revision. `None` = latest. |

The current bundle writer records the referenced set's `name` and
`revision` in `calibration.json`. Full resolved-curve snapshots are
planned, but are not wired into the storage path yet. See
[Calibrations on disk](calibrations.md).

## `operator:` and `sample:`

Per-run identity:

```yaml
operator:
  id: gbellamy
  display_name: Grayson Bellamy

sample:
  id: PMMA-2026-05-24-A
  material: PMMA
  thickness_mm: 6.0
  mass_g: 5.0
  notes: "third disk from box B"
```

`OperatorRef`:

| Field | Required | Notes |
|---|:-:|---|
| `id` | yes | Stable operator id. Carried into every device command's `issued_by`. |
| `display_name` | no | Friendly name for the UI. |

`SampleInfo`:

| Field | Required | Notes |
|---|:-:|---|
| `id` | yes | Specimen id. |
| `material` | no | Material name. |
| `thickness_mm` | no | Sample thickness, > 0. |
| `mass_g` | no | Mass at the start of the run, > 0. |
| `notes` | no | Free text. |
| `extra` | no | Free-form `dict[str, Any]` for fields the schema does not cover. |

When `domain_profile` is set, the profile's own metadata model
layers additional required specimen fields on top of these (initial
mass, form, holder, etc.) — see [CAPA profile fields](capa-profile.md).

## `storage:`

Storage policy fields. The operator rarely touches this; everything below
`bundle_root` is grouped under "Advanced" in the UI. Today, `bundle_root`
is used by profile disk-space preflight and the Setup editor. Runs launched
from the CLI or GUI are written under the launcher runs root
(`--runs-root`, `CAPA_RUNS_ROOT`, or `./runs`). The remaining fields are
schema/UI placeholders; the current writer and finalize code still use
code-level constants for flush and Parquet settings.

```yaml
storage:
  bundle_root: runs
  inflight_flush_seconds: 1.0
  parquet_final_row_group_rows: 262144
  inflight_compression: zstd
  parquet_final_compression: "zstd:6"
  enable_tdms_passthrough: false
  enable_rocrate: false
  producer_queue_abort_after_s: 5.0
```

| Field | Default | Notes |
|---|---|---|
| `bundle_root` | `"runs"` | Workspace/runs-root hint used by profile disk-space preflight and the Setup UI. Runtime bundle location comes from the launcher runs root today. |
| `inflight_flush_seconds` | `1.0` | Schema/UI placeholder. Current sinks flush Arrow IPC streams by row count. |
| `parquet_final_row_group_rows` | `262144` | Schema/UI placeholder. Finalize currently uses `FINAL_ROW_GROUP_ROWS = 262144`. |
| `inflight_compression` | `"zstd"` | Schema/UI placeholder. Current in-flight files are Arrow IPC streams written by `IpcStreamSink`. |
| `parquet_final_compression` | `"zstd:6"` | Schema/UI placeholder. Finalize currently uses `zstd` level 6. |
| `enable_tdms_passthrough` | `false` | Reserved. No TDMS sidecar is emitted by the current storage path. |
| `enable_rocrate` | `false` | Reserved. No RO-Crate JSON-LD wrapper is emitted by the current storage path. |
| `producer_queue_abort_after_s` | `5.0` | Reserved. Conductor saturation timing and bridge policies are code-level today. |

## `safety:`

Safety policy — declarative rules plus the abort-mode default.

```yaml
safety:
  default_abort: safe_shutdown
  rules:
    - id: heater_overtemp
      kind: max_temperature
      params:
        channel: heater.pv
        threshold_c: 950
      action: safe_shutdown
    - id: disk_low
      kind: disk_space_low
      params:
        free_gb_min: 5.0
      action: warn
```

Rule `kind` values: `max_temperature`, `max_ramp_rate`,
`missing_data_timeout`, `disk_space_low`, `writer_lag`,
`camera_recording_failure`, … See [Safety
principles](../safety/principles.md) for the full set.

Actions: `warn`, `pause_method`, `abort_run`, `safe_shutdown`.

`default_abort` selects what the UI's red button does by default —
`safe_shutdown` runs the cooldown step, `abort_run` is an immediate
cancel.

## `runtime:`

Operator-tunable runtime knobs. Only fields a real experiment might
reasonably want to adjust live here; internal timing constants
(adapter grace timers, [saturation
deadline](../glossary.md#saturation-deadline), bridge capacity factor)
remain code-level.

```yaml
runtime:
  shutdown_grace_s: 5.0
  loop_lag_warn_ms: 50.0
  ui_bridge_capacity: 4096
```

| Field | Default | Notes |
|---|---|---|
| `shutdown_grace_s` | `5.0` | Per-worker grace before hard-stop during disarm. |
| `loop_lag_warn_ms` | `50.0` | Threshold at which the per-loop heartbeat starts warning. Surfaced in the status-bar latency badge. |
| `ui_bridge_capacity` | `4096` | Capacity of the Conductor → UI bridge. `DROP_OLDEST` policy so the conductor never blocks on a slow UI subscriber. |

## `run_options:`

Per-run knobs that are *not* part of the saved recipe. The Run tab
mutates these on the operator's behalf before arm; the policy is
snapshotted into the bundle manifest as the audit trail.

```yaml
run_options:
  recording_policy:
    mode: procedure_default
```

| Mode | Means |
|---|---|
| `procedure_default` | Consult `Procedure.plan_capture`. Procedures that don't override get full-rig recording. |
| `record_all` | Ignore `plan_capture`; record every channel and camera the hardware profile declares. The Run-tab override checkbox sets this. |

## `tags:` and `custom:`

```yaml
tags: [sim, capa, pyrolysis, replicate-3]

custom:
  notebook_url: "https://example.org/notebooks/2026-05-24"
  test_protocol_revision: "2026-04"
```

`tags` is a free list, useful for catalog filtering.
`custom` is a free-form dict carried verbatim into the bundle manifest
as `manifest.json.custom` — procedures and profiles may also stamp
run-summary numbers here at finalize.

## Validation outcomes

The Setup validation pipeline has five layers:

1. **Schema** - Pydantic models and `extra="forbid"`.
2. **Referential** - channel bindings resolve, method targets name declared
   channels, adapter families match binding kinds, and camera/device names
   do not collide.
3. **Domain** - when the CAPA profile is set, required channel groups are
   present.
4. **Resource** - passive adapter/materialization dry run and resource
   conflict checks. This does not open hardware.
5. **Live** - optional discovery and handshake checks. This touches
   hardware and only runs from explicit "Check Hardware" flows such as
   `capa config validate --live`.

The top-level `capa validate CONFIG --strict` command is older and simpler:
it loads `ExperimentConfig` and optionally runs declared adapter handshakes.
Procedure-specific preflight happens at run-arm time, not during a plain
config load.

See [Validation and problems](validation-and-problems.md) for the
problem-to-field navigation surface in the UI and `capa validate`.

## What the bundle preserves

At `open()` time the bundle writer snapshots the resolved config to
`config.toml` (and `method.toml`, `profiles/<id>.toml` when present).
That snapshot is the durable answer to "what was this run?" — it
includes every default expanded, every external file inlined, and is
immutable thereafter.

## See also

- [Configuration overview](overview.md) — how the four config kinds
  compose.
- [Hardware TOML](hardware-toml.md) — the hardware-profile fields the
  inline form mirrors.
- [Method TOML](method-toml.md) — the step kinds.
- [CAPA profile fields](capa-profile.md) — the `domain_profile.metadata`
  shape for `capa.profiles.capa_pyrolysis`.
- [What is a procedure](../procedures/what-is-a-procedure.md) —
  procedure `config` shapes and decision tree.
