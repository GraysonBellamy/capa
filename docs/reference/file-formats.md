# File formats

**Audience:** tool authors.
**Scope:** every on-disk format capa reads or writes, with a pointer
to its schema documentation.

---

## Configuration files

| Format | Extension(s) | Where it appears | Schema doc |
|---|---|---|---|
| YAML | `.yaml`, `.yml` | Experiment recipes (`configs/experiments/*.yaml`). The recommended outer format. | [Experiment YAML](../configuration/experiment-yaml.md) |
| TOML | `.toml` | Experiment recipes (alternative); hardware profiles; methods; calibration sets; profile defaults; the optional `plugins.lock`. | [Configuration overview](../configuration/overview.md) |
| TOML | `*.method.toml` | Method files. | [Method TOML](../configuration/method-toml.md) |
| TOML | `configs/hardware/*.toml` | Hardware profiles. | [Hardware TOML](../configuration/hardware-toml.md) |
| TOML | `configs/calibrations/<family>/*.toml` | Calibration sets. | [Calibrations on disk](../configuration/calibrations.md) |
| TOML | `plugins.lock` | Trusted-plugin manifest. | [Plugin lockfile](../extending/plugin-lockfile.md) |

YAML parsing uses `ruamel.yaml`; TOML uses `tomllib` (read) and
`tomli_w` (write). Canonical-form writers live in
[`capa.config.canonical`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/config/canonical.py).

## Bundle artifacts

Every artifact below lives inside a sealed bundle directory. The
top-level directory tour is in [What's in a
bundle](../bundles/what-is-a-bundle.md).

| Format | Filename(s) | Contents | Schema doc |
|---|---|---|---|
| JSON | `manifest.json` | The bundle index. The single source of truth. | [Manifest and schema](../bundles/manifest-and-schema.md) |
| Plain text | `manifest.sha256` | `sha256sum`-compatible integrity hash file. Sealed-bundle signal. | [Integrity and sealing](../bundles/integrity-and-sealing.md) |
| Parquet | `scalars.parquet` | Normalized per-channel samples (long format). | [Channel samples parquet](../bundles/parquet-channel-samples.md) |
| Parquet | `device_records/<adapter>.parquet` | Library-native rows, one file per adapter family. | [Device records parquet](../bundles/parquet-device-records.md) |
| SQLite | `events.sqlite` | Procedure / safety / device-event log. | [Events SQLite](../bundles/events-sqlite.md) |
| SQLite | `status.sqlite` | Periodic per-device health snapshots. | [Events SQLite](../bundles/events-sqlite.md) |
| JSON lines | `run.log` | Structlog output tee'd from the engine logger. One JSON object per line. | [What's in a bundle](../bundles/what-is-a-bundle.md#runlog) |
| Video | `video/<name>.mkv` | Visible-camera container (PyAV / Matroska, libx264 by default). | [Video](../bundles/video.md) |
| Video | `video/<name>.csq` | FLIR IR raw container. | [Video](../bundles/video.md) |
| Parquet | `video/<name>.frames.parquet` | Frame-index sidecar (frame # ↔ monotonic ns). One per camera. | [Video](../bundles/video.md) |
| JSON | `video/<name>.csq.meta.json` | IR-only metadata sidecar (palette, radiometric kit, calibration). | [Video](../bundles/video.md) |
| TOML | `config.toml` | Frozen `ExperimentConfig` (canonical TOML form). | [Experiment YAML](../configuration/experiment-yaml.md) |
| TOML | `method.toml` | Frozen `Method` (present only when one was loaded). | [Method TOML](../configuration/method-toml.md) |
| TOML | `profiles/<short_id>.toml` | Frozen domain-profile metadata. | [CAPA profile fields](../configuration/capa-profile.md) |
| TOML | `equipment.toml` | What was actually opened (firmware, serial #s). | [What's in a bundle](../bundles/what-is-a-bundle.md#equipmenttoml) |
| JSON | `calibration.json` | CalibrationSet reference snapshot. | [Calibrations on disk](../configuration/calibrations.md) |
| TOML | `env/uv.lock` | Verbatim copy of the lockfile at run-start. Hash recorded in `manifest.json` under `lockfile.sha256`. | [What's in a bundle](../bundles/what-is-a-bundle.md#env) |
| JSON | `env/packages.json` | Installed distribution metadata gathered by `gather_provenance`. | [What's in a bundle](../bundles/what-is-a-bundle.md#env) |

## In-flight (transient) artifacts

These exist *during* a run and are rewritten by [finalize](../bundles/integrity-and-sealing.md):

| Format | Filename pattern | Becomes | Notes |
|---|---|---|---|
| Arrow IPC | `scalars.in-flight.arrows` | `scalars.parquet` | Append-optimised channel-sample stream. |
| Arrow IPC | `device_records/<adapter>.in-flight.arrows` | `device_records/<adapter>.parquet` | One per family. |
| Arrow IPC | `video/<name>.in-flight.arrows` | `video/<name>.frames.parquet` | Frame-index sidecar (the container is written directly to its final path). |

Arrow IPC has no compression-level knob, so the in-flight codec is
just a name (e.g. `"zstd"`). Final Parquet codecs may carry a level
(e.g. `"zstd:6"`).

## Calibration tune artifacts

Heat-flux tune procedure emits *tune artifact* TOML files outside the
bundle directory, under `configs/calibrations/flux/`:

| Format | Filename pattern | Contents | Schema doc |
|---|---|---|---|
| TOML | `capa_flux_YYYY-MM-DD.toml` | One tune session's per-target flux/setpoint pairs plus fit metadata. | [Tune artifacts](../calibration/tune-artifacts.md) |
| TOML | `latest.toml` | Symlink-equivalent pointer to the most recent artifact. | [Tune artifacts](../calibration/tune-artifacts.md) |

Subsequent runs read these via `calibration_set: name = flux` (with an
optional `revision:` pin).

## Format choice rationale

A short note on why each format ended up where it did:

- **YAML** for experiment recipes — nested metadata (specimen,
  atmosphere, operator) reads better than TOML.
- **TOML** for hardware, method, and calibration files — heavy use of
  array-of-tables (`[[devices]]`, `[[channels]]`, `[[steps]]`) reads
  better in TOML than YAML lists of mappings.
- **JSON** for `manifest.json`, `equipment.toml`'s eventual sibling
  blocks, and the camera sidecars — machine-targeted, no comments
  needed, every language has a fast parser.
- **Parquet** for sample / record streams — columnar, compressed, and
  every analyst's preferred input. Row groups are large
  (`parquet_final_row_group_rows = 262144` by default) for scan
  efficiency.
- **SQLite** for events and status — transactional appends, queryable
  in place, robust to mid-run crashes.
- **Arrow IPC** for in-flight — append cost matters more than read
  cost during acquisition.
- **`sha256sum`-compatible** for `manifest.sha256` — readable by
  every OS's command-line verifier without bespoke tooling.

## See also

- [What's in a bundle](../bundles/what-is-a-bundle.md) — directory
  tour.
- [Configuration overview](../configuration/overview.md) — how config
  files compose.
- [Bundle versioning](../bundles/bundle-versioning.md) — schema-bump
  policy across formats.
