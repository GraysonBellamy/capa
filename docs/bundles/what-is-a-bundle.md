# What's in a bundle

**Audience:** anyone who has just produced a sealed run.
**Scope:** the bundle directory layout, file-by-file. Each file gets a
one-paragraph description and a link to the deeper schema doc when
one exists.

---

## The directory

Every run produces one directory under the configured runs root
(default `runs/`):

```
runs/<run_id>/
├── manifest.json              # the bundle's index card (every reader starts here)
├── manifest.sha256            # sealed-bundle integrity hash
├── scalars.parquet            # normalized per-channel samples (long format)
├── device_records/
│   ├── alicat.parquet         # library-native rows, one file per adapter family
│   ├── watlow.parquet
│   ├── sartorius.parquet
│   └── nidaq_polled.parquet
├── events.sqlite              # DeviceEvent + procedure / safety events
├── status.sqlite              # periodic DeviceSnapshot health pings
├── run.log                    # structlog JSON-lines, tee'd from the engine logger
├── video/
│   ├── webcam0.mkv
│   ├── webcam0.frames.parquet # frame-index sidecar (frame # ↔ mono ns)
│   ├── flir_ir.csq
│   └── flir_ir.csq.meta.json  # IR-only sidecar
├── config.toml                # frozen ExperimentConfig (canonical TOML form)
├── method.toml                # frozen Method (only when one was loaded)
├── profiles/
│   └── capa_pyrolysis.toml    # frozen domain-profile metadata
├── equipment.toml             # what was actually opened (firmware, serial #s)
├── calibration.json           # CalibrationSet reference snapshot
└── env/
    ├── uv.lock                # exact Python dep tree at run-start
    └── packages.json          # installed distribution metadata
```

`<run_id>` is typically `YYYYMMDD-HHMMSS-<short>` but the exact
format is up to the caller of `RunBundleWriter`; the catalog reads
the manifest, not the directory name.

## File-by-file

### `manifest.json`

The single source of truth. Every reader, from the CLI catalog to a
downstream notebook, starts here. The
[`BundleManifest`](manifest-and-schema.md) Pydantic model is the
schema; this is where you find run identity, status, timings,
data-shape pointers, plugin lockfile snapshot, integrity verdict,
recording plan, and the per-camera summary.

### `manifest.sha256`

`sha256sum`-compatible file. Written during finalize as the last step
of sealing the bundle. The presence of this file is the
operator-facing signal that the bundle is durable and safe to copy.
See [Integrity and sealing](integrity-and-sealing.md).

### `scalars.parquet`

Long-format Parquet — one row per (channel, time) — covering every
recorded `ChannelSample`. This is the file plots, downstream
analyses, and most notebooks read. Columns: `channel`, `t_mono_ns`,
`t_mono_s`, `value`, `value_kind`, `raw_value`, `raw_text`, `raw_kind`,
`unit`, `status`, `uncertainty`, `source_record_id`, `source_field`.
Schema reference: [Channel samples parquet](parquet-channel-samples.md).

### `device_records/<adapter>.parquet`

The library-native row stream, **preserved without reshaping**. One
file per adapter family — `alicat.parquet` has Alicat-shaped wide
rows; `watlow.parquet` has `(device, parameter, instance)` long rows;
`sartorius.parquet` has single-value balance rows; `nidaq_polled.parquet`
has wide `(channel ↦ value)` rows.

Why both `scalars.parquet` and `device_records/`? The channel binding
in your hardware TOML decides what gets surfaced as a channel; the
device-records file is the safety net for re-analysis when a future
researcher needs a field the binding did not promote. Schema reference:
[Device records parquet](parquet-device-records.md).

NI-DAQ hardware-clocked block records do *not* land here — see
[Block records](parquet-device-records.md) for the sidecar path.

### `events.sqlite`

Append-only SQLite database holding the run's event log. Three
producers write here:

- adapters (`DeviceEvent`: connect, disconnect, comm_error, alarm
  latches);
- the procedure (`free_run.started`, `recipe.step_complete`,
  operator prompts, abort reasons);
- runtime safety paths (saturation-deadline trips, authorization-gate
  rejections; alarm-band events are reserved for the future safety monitor).

The schema is documented in [Events SQLite](events-sqlite.md).

### `status.sqlite`

Append-only SQLite database holding periodic
[`DeviceSnapshot`](../devices/overview.md#what-an-adapter-emits) rows
— firmware version, connection state, bus diagnostics, the
tri-state `ok / degraded / down` health pill. Separate from
`events.sqlite` so a 1-Hz status stream cannot drown the
operator-relevant event view.

### `run.log`

JSON-lines structlog output, tee'd from the engine logger.
`engine.bind(run_id=…)` runs at run-start so every emitted line
carries the run id and any contextvars in scope. Useful for
post-mortems when an event row points at "see run.log" but the
context is too verbose for the event table.

### `video/`

Per-camera recordings. Visible-spectrum cameras write `<name>.mkv`
via PyAV (Matroska tolerates a truncated tail better than MP4); FLIR IR
cameras write `<name>.csq` via the SDK. Every
camera additionally writes `<name>.frames.parquet`, a frame-index
sidecar mapping frame number to monotonic ns offset against
`manifest.started_mono_ns_anchor` — without it, a downstream
analyst cannot align frames to channel samples to better than the
container's container-level timestamp resolution. IR cameras
additionally write `<name>.csq.meta.json` with calibration / device
metadata. See [Video](video.md).

When a camera's
[`CameraSpec.output_root`](../configuration/hardware-toml.md#cameras)
points outside the bundle, only the `.frames.parquet` sidecar lands
here; the container lives at the external path, and the manifest's
[`CameraEntry.output_path_external`](manifest-and-schema.md#cameras)
records the external container location.

### `config.toml`

Canonical TOML of the resolved `ExperimentConfig` — every default
expanded, every external file inlined. This is the "what did we
actually run?" answer. Snapshotted at `open()` time; immutable
thereafter.

### `method.toml`

Present only when the procedure loaded a method. Broken out from
`config.toml` so a `diff method.toml` between two runs is small
and readable.

### `profiles/<short_id>.toml`

Present only when `domain_profile:` was set. Verbatim mirror of the
profile's `metadata` block plus `id` and `standard_refs`. Same
diff-friendliness rationale as `method.toml`.

### `equipment.toml`

A stub at run-start (device name + adapter id only); fleshed out
as adapters report firmware versions, serial numbers, and reachable
addresses. Distinct from `config.toml`: `config.toml` records *what
we asked for*, `equipment.toml` records *what answered*.

### `calibration.json`

Reference snapshot of the active calibration set — name and
revision. The resolved per-channel curves snapshot will land here
once the calibration runtime is wired in; until then the reference
is enough to re-locate the source on disk.

### `env/`

Exact Python dependency tree at run-start. Two files:

- `uv.lock` — verbatim copy of the lockfile found at run start.
  Hashed into `BundleManifest.lockfile.sha256`.
- `packages.json` — installed distribution metadata (name, version,
  dist-info hash) gathered by `gather_provenance`.

This snapshot is the answer to "what code wrote this bundle?" when
the package version alone is ambiguous.

## In-flight vs sealed

While the run is live, several files exist in **in-flight** form
rather than their final layout:

- `*.in-flight.arrows` — Arrow IPC stream sidecars for every
  Parquet-bound sink (channel samples, device records, frames). They
  are durable but optimised for append, not for read.
- `events.sqlite` and `status.sqlite` — already in their final
  format but with the WAL still active.
- `manifest.json` — written at `open()` with `bundle_status="open"`
  and updated to `"sealed"` at finalize.

[Finalize](integrity-and-sealing.md) is a pure-function rewrite that
turns `*.in-flight.arrows` into final Parquet with large row groups,
hashes every artifact, writes `manifest.sha256`, and updates
`bundle_status` to `"sealed"`. A run that crashes before finalize
runs leaves the bundle in `open` state — recoverable by `capa
finalize RUN_ID`. Recovery preserves the scientific `run_status`
where possible, maps live/crashed manifests to `run_status="crashed"`,
and ends with `bundle_status="sealed"` or `"verification_failed"`
depending on what the integrity walk found.

The five `bundle_status` values, in order:

| Status | Means |
|---|---|
| `open` | Run live; files may still be mid-write. |
| `finalizing` | Sinks closed; two-stage rewrite in progress. |
| `finalized_unverified` | Data readable; integrity hashes pending. |
| `sealed` | `manifest.sha256` written. Safe to copy and archive. |
| `verification_failed` | Enough finalized to inspect, but integrity failed. |

## Bundle outcomes

A separate axis, `run_status`, records how the run *ended*:

| `run_status` | Means |
|---|---|
| `running` | Acquisition active (in-flight only). |
| `completed` | Procedure returned normally. |
| `aborted` | Operator (or safety) stopped before completion. |
| `crashed` | Unhandled exception from the procedure, drain task, or pool. |

Plus the special runtime outcome `crashed_but_sealed` when the [saturation
deadline](../glossary.md#saturation-deadline) tripped — the conductor
disarmed every worker, ran safe-shutdown, and sealed the bundle anyway
so the run is not lost.

`run_status` and `bundle_status` are deliberately independent: an
aborted run still seals cleanly, a crashed run can still seal cleanly
after recovery. The legal combinations are enforced by
[`is_legal_finalize_combination`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/manifest.py).

## See also

- [Manifest and schema](manifest-and-schema.md) — every key in
  `manifest.json`.
- [Reading a bundle](reading-bundles.md) — recipes for polars,
  sqlite3, and video extraction.
- [Integrity and sealing](integrity-and-sealing.md) — sealing protocol
  and outcome states.
- [Bundle versioning](bundle-versioning.md) — schema-bump policy and
  migration registry.
