---
description: Reading a sealed capa bundle in polars, pyarrow, pandas, or sqlite — using started_mono_ns_anchor to join scalars.parquet, events.sqlite, and frame indices.
---

# Reading a bundle

**Audience:** analysts opening a sealed bundle in polars, pyarrow, pandas, or the sqlite CLI; downstream tool authors.
**Scope:** runnable recipes for reading every artifact in a bundle and cross-referencing across them via the monotonic time anchor.

Always start with `manifest.json`. A sealed bundle is self-describing: the manifest tells you what files exist, what their layouts are, which monotonic time anchor every other artifact is referenced against, which cameras recorded (and which were intentionally skipped), and the post-finalize integrity verdict. Every recipe on this page assumes you have already opened the manifest and held onto two values from it — `started_utc` and `started_mono_ns_anchor`. Without those, the timestamps in `scalars.parquet`, `events.sqlite`, and the frame-index sidecars are meaningless integers.

---

## The golden rule: `manifest.started_mono_ns_anchor`

Every `t_mono_ns` column in the bundle (channel samples, device records, events, status snapshots, video frames) is a single shared monotonic clock — `RunClock.t_mono_ns()` — captured at run-open. They can be joined directly without any per-artifact offset. To translate any `t_mono_ns` value back to wall clock:

```python
from datetime import timedelta

def mono_to_wall(t_mono_ns: int, manifest) -> "datetime":
    delta_s = (t_mono_ns - manifest.started_mono_ns_anchor) / 1e9
    return manifest.started_utc + timedelta(seconds=delta_s)
```

The one exception is the video container itself (`.mkv` / `.csq`). Frame timestamps stored inside the container start at zero relative to `CameraEntry.started_mono_ns_offset`, which is the `RunClock` value captured at `start_recording`. The accompanying `<camera>.frames.parquet` sidecar already converts those back into bundle-relative `t_mono_ns`, so for any analysis that touches the frame index you can stay in the bundle's single monotonic timeline. Only when you extract a frame out of the container directly do you need the per-camera offset (see [extracting a video frame](#extracting-a-video-frame-at-a-given-time)).

---

## Reading `manifest.json`

The capa way (recommended — applies registered schema migrations and validates the result):

```python
from capa.storage.manifest import BundleManifest

manifest = BundleManifest.read("runs/<run_id>/manifest.json")
print(manifest.run_id, manifest.bundle_status, manifest.integrity.status)
anchor_ns = manifest.started_mono_ns_anchor
```

The no-capa-dependency way (for downstream tools that ship without `capa` as a runtime dep):

```python
import json

with open("runs/<run_id>/manifest.json") as f:
    m = json.load(f)
print(m["run_id"], m["bundle_status"])
anchor_ns = m["started_mono_ns_anchor"]
```

Bypassing `BundleManifest.read()` means bypassing the registered migrations in [`capa.storage.schema`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/schema.py) — fine for bundles whose `bundle_schema_version` matches the version your tool was written against, but read [Bundle versioning](bundle-versioning.md) before depending on it for older bundles.

---

## Reading `scalars.parquet` with polars

This is the main analyst recipe. `scalars.parquet` is the post-finalize rewrite of every channel sample emitted during the run, sorted by `t_mono_ns`, in the normalized long layout (one row per (channel, sample) pair):

```python
import polars as pl

df = pl.read_parquet("runs/<run_id>/scalars.parquet")
# All channels present in this bundle:
df.select("channel").unique().sort("channel")
# One channel as a time series:
tc = df.filter(pl.col("channel") == "heater.pv").select(["t_mono_s", "value"])
# Joinable wall-clock column (anchor_ns from the manifest):
df = df.with_columns(
    (anchor_ns + pl.col("t_mono_ns")).alias("t_wall_ns"),  # if you prefer ns
)
```

Note that `value` is always `float64` even for boolean and integer channels — the round-trip discriminator lives in the `value_kind` column (`"float" | "int" | "bool"`). For non-float channels you cast back yourself:

```python
flags = df.filter(pl.col("value_kind") == "bool").with_columns(
    pl.col("value").cast(pl.Boolean).alias("flag")
)
```

See [Channel samples (parquet)](parquet-channel-samples.md) for the full column reference (`raw_value` / `raw_text` / `raw_kind` round-trip rules, `status` enum values, `source_record_id` back-pointer).

Not every bundle has a `scalars.parquet`. A bundle written with `recording_policy.channel_mode = "only"` and an empty `recorded_channels` list (or a free-run with no configured channels) will omit the file entirely; `manifest.data_shape.channel_samples` will be `null`. Guard accordingly:

```python
if manifest.data_shape.channel_samples is not None:
    df = pl.read_parquet("runs/<run_id>/" + manifest.data_shape.channel_samples.path)
```

---

## Reading `device_records/<adapter>.parquet`

Per-adapter native records are the raw frames each device library produced, captured before capa's channel-extraction stage. The columns vary by adapter — Watlow is `long_row`, Alicat is `wide_row`, Sartorius is `single_value_row`, NI-DAQ is `wide_row` — so always inspect the schema before assuming column names:

```python
import polars as pl

alicat = pl.read_parquet("runs/<run_id>/device_records/alicat.parquet")
alicat.schema  # see native columns (Mass_Flow, Pressure, Temperature, ...)
```

The manifest's `data_shape.device_records` block tells you which files exist and which layout each uses, so you can iterate without guessing:

```python
for entry in manifest.data_shape.device_records:
    print(entry.adapter, entry.path, entry.layout)
    table = pl.read_parquet(f"runs/<run_id>/{entry.path}")
    print(table.columns)
```

See [Device records (parquet)](parquet-device-records.md) for the per-adapter native schemas and which `record_id` / `t_mono_ns` / `t_utc` columns are always present.

---

## Reading `events.sqlite` with sqlite3

`events.sqlite` is a single `events` table with columns `id`, `t_mono_ns`, `t_utc`, `kind`, `severity`, `source`, `message`, `metadata_json`. Three common shapes:

**Full timeline** — every event in order:

```python
import sqlite3

conn = sqlite3.connect("runs/<run_id>/events.sqlite")
rows = conn.execute(
    "SELECT t_mono_ns, kind, severity, source, message FROM events ORDER BY t_mono_ns"
).fetchall()
for t, kind, sev, src, msg in rows[:20]:
    print(f"{t:>18}  {sev:7s}  {src:30s}  {kind:30s}  {msg}")
```

**Filter by kind prefix** — every method/procedure event, for example:

```python
rows = conn.execute(
    "SELECT t_mono_ns, kind, message FROM events "
    "WHERE kind LIKE 'free_run.%' OR kind LIKE 'method.%' "
    "ORDER BY t_mono_ns"
).fetchall()
```

**Load into polars for analysis** — useful when correlating event counts against samples:

```python
import json
import polars as pl

events = pl.read_database("SELECT * FROM events ORDER BY t_mono_ns", connection=conn)
# metadata_json is TEXT — parse per-row when you need it:
first = events.row(0, named=True)
meta = json.loads(first["metadata_json"]) if first["metadata_json"] else {}
```

The `source` column is free-form text — adapters write `"<adapter>:<device>"` (e.g. `"watlow:heater"`), the procedure layer writes `"procedure:<id>"`, and the safety/operator layers write `"safety"` and `"operator"` respectively. Filter on it when you need just one subsystem's events. Severities are constrained to `"info" | "warning" | "error"`.

See [Events and status (sqlite)](events-sqlite.md) for the full event taxonomy.

---

## Reading `status.sqlite`

`status.sqlite` is a single `status` table with columns `id`, `adapter`, `device`, `t_mono_ns`, `t_utc`, `health`, `fields_json`. Each row is one device-snapshot tick — low-rate periodic health (Watlow alarm bits, Alicat valve drive, balance stable flag, comm latency). The producer applies drop-oldest semantics, so successive rows can skip large gaps when a device is healthy and quiet:

```python
import sqlite3

conn = sqlite3.connect("runs/<run_id>/status.sqlite")
# Was the Alicat healthy 120 s after run start?
cutoff_ns = anchor_ns + 120_000_000_000
row = conn.execute(
    "SELECT t_mono_ns, health, fields_json FROM status "
    "WHERE adapter = 'alicat' AND t_mono_ns <= ? "
    "ORDER BY t_mono_ns DESC LIMIT 1",
    (cutoff_ns,),
).fetchone()
print(row)
```

`health` is a short adapter-defined string (`"ok"`, `"degraded"`, `"failed"`, …); `fields_json` is the free-form per-snapshot detail dict — JSON-encoded for storage, decode per-row with `json.loads`. The `(adapter, device, t_mono_ns)` index covers all the queries above.

---

## Cross-referencing events, samples, and frames

This is the recipe that pulls the rest together. Because every artifact shares `manifest.started_mono_ns_anchor`, you can join across them in either direction with raw integer comparison — no rounding, no clock-drift correction, no time-zone gotchas. Two worked examples.

**What was the heater setpoint when the operator pressed abort?**

```python
import polars as pl
import sqlite3

events = sqlite3.connect("runs/<run_id>/events.sqlite")
# 1. Find the run-end event (free-run procedure example).
abort_t = events.execute(
    "SELECT t_mono_ns FROM events WHERE kind = 'free_run.ended' LIMIT 1"
).fetchone()[0]

# 2. Most-recent setpoint sample at or before that time.
samples = pl.read_parquet("runs/<run_id>/scalars.parquet")
setpoint = (
    samples
    .filter((pl.col("channel") == "heater.setpoint") & (pl.col("t_mono_ns") <= abort_t))
    .sort("t_mono_ns")
    .tail(1)
)
print(setpoint)
```

**What did the camera see during the abort?**

```python
import polars as pl

# Find the closest frame index entry to abort_t.
frames = pl.read_parquet("runs/<run_id>/video/webcam0.frames.parquet")
closest = (
    frames
    .with_columns((pl.col("t_mono_ns") - abort_t).abs().alias("delta"))
    .sort("delta")
    .head(1)
)
frame_idx = closest["frame_idx"][0]
frame_t_mono_ns = closest["t_mono_ns"][0]
print(f"closest frame: idx={frame_idx}  t_mono_ns={frame_t_mono_ns}")
# Extract that frame from the .mkv container — see the next section.
```

The same join shape works for any pairing — a `method.segment_started` event against the `tc_specimen` channel to measure thermocouple lag, a `safety.threshold_tripped` event against the IR camera's frame index to pull the moment of ignition, a balance-stable transition in `status.sqlite` against the corresponding mass channel sample. There is no "preferred" anchor; `t_mono_ns` is the same clock everywhere.

---

## Extracting a video frame at a given time

Visible-camera containers are `.mkv` (matroska); IR-camera containers are `.csq` (FLIR proprietary). The frame-index sidecar gives you the bundle-relative `t_mono_ns`. To translate into container-relative seconds — the value `ffmpeg -ss` wants — subtract the camera's `started_mono_ns_offset` from `manifest.cameras[*]`:

```python
import polars as pl

frames = pl.read_parquet("runs/<run_id>/video/webcam0.frames.parquet")
frame_t_mono_ns = int(frames["t_mono_ns"][1000])  # pick whichever frame you want

camera = next(c for c in manifest.cameras if c.name == "webcam0")
container_offset_s = (frame_t_mono_ns - camera.started_mono_ns_offset) / 1e9
print(f"seek ffmpeg to -ss {container_offset_s:.6f}")
```

Then extract a single frame:

```bash
ffmpeg -ss <container_offset_s> -i runs/<run_id>/video/webcam0.mkv -frames:v 1 frame.png
```

For IR `.csq` containers, the same offset arithmetic applies, but extraction itself requires a FLIR-side tool (Researcher Studio, `csq_split`) or the `flir_csq_python` ecosystem — there is no general-purpose ffmpeg decoder for `.csq`. See [Video](video.md) for the IR extraction story and the `.csq.meta.json` sidecar that frames-parquet pairs with.

---

## Verifying integrity

After copying a bundle off the rig (or before publishing one), re-verify the sha256 manifest. With capa installed:

```python
from capa.storage.integrity import verify

result = verify("runs/<run_id>")
print(result.status)  # 'ok' | 'mismatch' | 'partial'
for m in result.mismatches:
    print(f"  {m.kind:18s} {m.path}  expected={m.expected}  actual={m.actual}")
```

Without capa, the on-disk `manifest.sha256` follows the standard `sha256sum` line format so the GNU coreutils CLI verifies it directly:

```bash
cd runs/<run_id>
sha256sum -c manifest.sha256
```

Either path catches bit-rot, partial copies, and post-hoc tampering. See [Integrity and sealing](integrity-and-sealing.md) for the difference between `ok`, `mismatch` (some file's content changed), and `partial` (a file the manifest references is missing, or an extra file is present).

---

## Inspecting the environment snapshot

The `env/` directory captures the exact Python world the bundle was written against:

```python
import json

with open("runs/<run_id>/env/packages.json") as f:
    pkgs = json.load(f)
capa_pkg = next(p for p in pkgs if p["name"] == "capa")
print(capa_pkg["version"])
```

The bundle also includes `env/uv.lock` (a snapshot of the project lockfile at run-open), and `manifest.json` records the hash in `lockfile.sha256` — so a downstream tool can detect if the lockfile in the bundle was swapped after sealing without going through `verify()`.

---

## No `BundleReader` helper yet

Today there is no single `BundleReader` class that wraps all of the above into one ergonomic surface. Readers compose the primitives in this page directly — `BundleManifest.read()` for the index, `polars.read_parquet` for the columnar artifacts, `sqlite3.connect` for the SQLite artifacts, `capa.storage.integrity.verify` for the seal check. The reason there is no helper today is that the read patterns are still being shaken out by downstream tools (the catalog, the post-run report renderer, ad-hoc analyst notebooks); fixing a `BundleReader` API too early would freeze a surface before we know what shape it needs.

If you find yourself building a wrapper in your own tool — caching the manifest, exposing typed accessors for the per-artifact frames, threading the time anchor through — the team would like to know. There is a case for promoting it into `capa.storage` once the read patterns stabilise.

---

## See also

- [What's in a bundle](what-is-a-bundle.md) — directory tour
- [Manifest and schema](manifest-and-schema.md) — every key in `manifest.json`
- [Channel samples (parquet)](parquet-channel-samples.md) — `scalars.parquet` columns and the `value_kind` round-trip
- [Device records (parquet)](parquet-device-records.md) — per-adapter native shapes
- [Events and status (sqlite)](events-sqlite.md) — event taxonomy and severities
- [Video](video.md) — frames sidecar and IR `.csq` extraction
- [Integrity and sealing](integrity-and-sealing.md) — the `verify()` helper
- [Bundle versioning](bundle-versioning.md) — schema migrations applied by `BundleManifest.read()`
