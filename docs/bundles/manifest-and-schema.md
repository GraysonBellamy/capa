# Manifest and schema

**Audience:** bundle readers and downstream tooling.
**Scope:** every key in `manifest.json`, the bundle schema version,
and the forward / backward compatibility contract.

---

## What the manifest is

`manifest.json` is the bundle's index card. Every reader — the CLI
catalog, a downstream notebook, the integrity verifier — starts here.
The schema is the
[`BundleManifest`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/manifest.py)
Pydantic model with `extra="forbid"` on the top level; any unexpected
key fails to validate. Sub-models that genuinely need extensibility
(`SampleBlock`, `custom`) opt in explicitly.

The writer is the only object that writes to this file. It is written
at `open()` time with `bundle_status="open"` and `run_status="running"`,
re-written at finalize with the final status pair plus integrity
block, and atomically (via `tmp + rename`) so partial writes are not
observable.

## Schema version

```json
"bundle_schema_version": 2
```

The current version is **`2`**. Bumps follow this contract:

- Every change is a numeric bump.
- Old bundles remain first-class — the
  [`migrate()`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/schema.py)
  registry maps `(v) → migrate → (v+1)` and chains as needed.
- A manifest with a recorded version newer than this capa rejects
  loudly (`BundleSchemaError`).
- v2 changed in-flight transit format from Parquet to Arrow IPC; final
  Parquet artifacts are unchanged.

See [Bundle versioning](bundle-versioning.md) for the bump policy and
the migration-writing checklist.

## Top-level keys

| Key | Type | Notes |
|---|---|---|
| `run_id` | str | Stable run identifier; same as the bundle directory name. |
| `bundle_schema_version` | int | Always present; routes through the migration chain on read. |
| `started_utc` | datetime | Wall-clock at `open()`. |
| `ended_utc` | datetime &#124; null | Wall-clock at finalize. Null while running. |
| `started_mono_ns_anchor` | int | `RunClock.started_mono_ns`. Anchor for every `t_mono_ns` column. |
| `run_status` | str | `running` / `completed` / `aborted` / `crashed`. See [Bundle outcomes](what-is-a-bundle.md#bundle-outcomes). |
| `bundle_status` | str | `open` / `finalizing` / `finalized_unverified` / `sealed` / `verification_failed`. |
| `exit_reason` | str &#124; null | Short string set by the finalize call (`"completed"`, `"operator_abort"`, …). |
| `operator` | object | See [Operator](#operator). |
| `sample` | object | See [Sample](#sample). |
| `procedure` | object | See [Procedure](#procedure). |
| `domain_profile` | object &#124; null | See [Domain profile](#domain-profile). |
| `tags` | array of str | Verbatim copy of `ExperimentConfig.tags`. |
| `capa` | object | See [Capa version provenance](#capa-version-provenance). |
| `python` | object | See [Python](#python). |
| `platform` | object | See [Platform](#platform). |
| `lockfile` | object | See [Lockfile](#lockfile). |
| `plugins` | array of object | One entry per trusted plugin. See [Plugins](#plugins). |
| `data_shape` | object | See [Data shape](#data-shape). |
| `queue_health` | object | See [Queue health](#queue-health). |
| `dropped_samples` | object | Per-channel drop counts. Populated at finalize. |
| `integrity` | object | See [Integrity](#integrity). |
| `cameras` | array of object | One row per declared camera. See [Cameras](#cameras). |
| `recording` | object &#124; null | See [Recording](#recording). |
| `custom` | object | Free-form bag. Procedures and profiles may stamp summary numbers here. |

## Sub-block reference

### Operator

```json
"operator": {
  "id": "gbellamy",
  "display_name": "Grayson Bellamy"
}
```

Mirrors [`ExperimentConfig.operator`](../configuration/experiment-yaml.md#operator-and-sample).

### Sample

```json
"sample": {
  "id": "PMMA-2026-05-24-A",
  "material": "PMMA",
  ...
}
```

`SampleBlock` has `extra="allow"` so the profile's specimen fields
(initial_mass_g, form, holder, conditioning) round-trip without
re-validating against today's `SampleInfo` schema.

### Procedure

```json
"procedure": {
  "id": "capa.builtin.recipe_runner",
  "version": "0.1.0"
}
```

Just the id + version that actually loaded. The procedure's `config`
block is part of the snapshotted `config.toml`, not the manifest.

### Domain profile

```json
"domain_profile": {
  "id": "capa.profiles.capa_pyrolysis",
  "standard_refs": ["ASTM E1354-25"]
}
```

Null when no profile was attached. The profile's `metadata` block
lives in `profiles/<short_id>.toml`, not in the manifest, so a reader
can decide whether to slurp the profile-specific blob.

### Capa version provenance

```json
"capa": {
  "version": "0.1.0.dev",
  "git_sha": "f965aab",
  "git_dirty": false,
  "build_time": "2026-05-24T12:00:00Z",
  "engine_version": "conductor-2026-05-23"
}
```

`engine_version` is the engine-task-group revision marker stamped at
run-start, so a post-mortem can tell which engine code produced this
bundle even when the package version is unchanged. Populated by
[`gather_provenance`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/provenance.py).

### Python

```json
"python": {
  "version": "3.13.0",
  "implementation": "CPython",
  "executable": "C:\\Users\\gbellamy\\.venv\\Scripts\\python.exe"
}
```

### Platform

```json
"platform": {
  "os": "Windows-11-10.0.26200",
  "machine": "AMD64",
  "node": "lab-pc-01"
}
```

### Lockfile

```json
"lockfile": {
  "path": "env/uv.lock",
  "sha256": "ab12…"
}
```

Both fields may be `null` when no lockfile was found at snapshot time
— the bundle records the absence honestly rather than pretending.

### Plugins

```json
"plugins": [
  {
    "id": "capa.builtin.recipe_runner",
    "version": "0.1.0",
    "package": "capa",
    "entry_point": "capa.experiment.procedures.builtin.recipe_runner:RecipeRunner",
    "distribution_hash": "ab12…"
  }
]
```

Verbatim mirror of the `plugins.lock` entries handed to the run. In
production mode those entries gate procedure discovery before the run
arms; in the manifest they are an archival trust-set snapshot, not proof
that every listed plugin was used by the procedure. See [Plugin
lockfile](../extending/plugin-lockfile.md).

### Data shape

```json
"data_shape": {
  "channel_samples": {
    "path": "scalars.parquet",
    "layout": "normalized_long"
  },
  "device_records": [
    {"adapter": "alicat", "path": "device_records/alicat.parquet", "layout": "wide_row"},
    {"adapter": "watlow", "path": "device_records/watlow.parquet", "layout": "long_row"},
    {"adapter": "sartorius", "path": "device_records/sartorius.parquet", "layout": "single_value_row"},
    {"adapter": "nidaq_polled", "path": "device_records/nidaq_polled.parquet", "layout": "wide_row"}
  ]
}
```

The layout tag mirrors
[`SourceRecord.shape`](../devices/overview.md#emission-shapes) —
`wide_row`, `long_row`, `single_value_row`, `block` — so a reader
knows what to expect before opening the file.

### Queue health

```json
"queue_health": {
  "producer": {
    "depth_p50": 12.0,
    "depth_p99": 98.0,
    "depth_max": 412.0,
    "lag_s_max": 0.31,
    "write_bytes_total": 12345678
  }
}
```

`extra="allow"` on the entry so collectors can add per-sink fields
without a schema bump. Populated at finalize from the metrics module.

### Integrity

```json
"integrity": {
  "status": "ok",
  "manifest_sha256_path": "manifest.sha256",
  "algorithm": "sha256"
}
```

`status` is one of `unknown` (pre-finalize), `ok` (every artifact
verified), `mismatch` (one or more failed), `partial` (some artifacts
unreachable). The hash file format is `sha256sum`-compatible. See
[Integrity and sealing](integrity-and-sealing.md).

### Cameras

```json
"cameras": [
  {
    "name": "webcam0",
    "adapter": "webcam",
    "kind": "visible",
    "model": "Logitech C920",
    "serial": "ABCD1234",
    "output_path": "video/webcam0.mkv",
    "output_path_external": null,
    "frames_path": "video/webcam0.frames.parquet",
    "meta_path": null,
    "frame_count": 18000,
    "started_mono_ns_offset": 12345678,
    "on_failure": "warn",
    "healthy": true,
    "error": null,
    "recorded": true,
    "suppressed_reason": null
  }
]
```

| Field | Notes |
|---|---|
| `name`, `adapter`, `kind`, `model`, `serial` | Identity. |
| `output_path` | Bundle-relative POSIX path. Always present, even when the container is external. |
| `output_path_external` | External container path when `CameraSpec.output_root` overrode the bundle directory; else null. |
| `frames_path` | Bundle-relative path to the frame-index sidecar (`<name>.frames.parquet`). |
| `meta_path` | Adapter-specific sidecar (e.g. `<name>.csq.meta.json` for FLIR IR). |
| `frame_count` | Final frame count after finalize. |
| `started_mono_ns_offset` | `RunClock.t_mono_ns()` at `start_recording`. Anchors frame numbers to the manifest's `started_mono_ns_anchor`. |
| `on_failure` | `warn` / `abort_run` / `safe_shutdown`. |
| `healthy`, `error` | Final health verdict + last error if any. |
| `recorded` | `False` when the resolved recording plan excluded this camera; the container file does not exist. |
| `suppressed_reason` | Why `recorded=False`. Currently the only value is `"recording_policy"`. |

### Recording

```json
"recording": {
  "policy": "procedure_default",
  "source": "procedure_default",
  "channel_mode": "all",
  "recorded_channels": [],
  "camera_mode": "all",
  "recorded_cameras": [],
  "native_device_records": "all"
}
```

Mirrors the conductor's `ResolvedRecordingPlan` 1:1 so a reader does
not need to import runtime code to interpret it. Written at arm-time
and immutable thereafter.

| Field | Notes |
|---|---|
| `policy` | Operator-facing enum: `procedure_default` or `record_all`. |
| `source` | How the plan was materialised: `procedure_default` (from `Procedure.plan_capture` or the full-rig fall-through) or `operator_override` (`record_all`). |
| `channel_mode` | `all` (every declared channel) or `only` (the explicit list in `recorded_channels`). |
| `recorded_channels` | When `channel_mode = "only"`, the list. Empty otherwise. |
| `camera_mode` | `all` or `none`. |
| `recorded_cameras` | When `camera_mode = "all"` is suppressed for specific cameras, the included list. |
| `native_device_records` | Currently always `"all"`. Filtering deferred to a future schema bump. |

`null` only on bundles written before this field existed; pydantic
defaults backfill on load. Production bundles always populate.

### Custom

```json
"custom": {
  "notes": "third disk from box B",
  "batch": {"id": "abcdef01", "iteration": 3, "of": 5}
}
```

Free-form. Sinks never write here; procedures and profiles may stamp
run-summary numbers at finalize.

## Status legality

`run_status` and `bundle_status` are deliberately independent — an
aborted run can still seal cleanly, a crashed run can still seal
cleanly after recovery. The
[`is_legal_finalize_combination`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/manifest.py)
helper refuses the one combination that does not make sense: the
bundle cannot transition past `open` while the run is still `running`.

| `run_status` | Legal `bundle_status` values |
|---|---|
| `running` | `open` only |
| `completed` | any |
| `aborted` | any |
| `crashed` | any |

## Reading a manifest

```python
from capa.storage.manifest import BundleManifest

manifest = BundleManifest.read("runs/2026-05-24-pmma/manifest.json")
print(manifest.run_id, manifest.bundle_status)
for cam in manifest.cameras:
    print(cam.name, cam.frame_count, cam.output_path)
```

`BundleManifest.read()` applies any registered schema migrations
*before* validating, so a v1 bundle is upgraded in memory to v2 (when
v1→v2 is registered) before Pydantic sees it. The on-disk file is not
modified.

Atomic writes via `BundleManifest.write()` use `<path>.tmp` then
`rename` so a reader never sees a half-written file.

## See also

- [What's in a bundle](what-is-a-bundle.md) — the directory tour.
- [Bundle versioning](bundle-versioning.md) — schema-bump policy.
- [Integrity and sealing](integrity-and-sealing.md) — the finalize
  protocol and `manifest.sha256`.
- [Reading a bundle](reading-bundles.md) — recipes for polars,
  sqlite3, and video extraction.
