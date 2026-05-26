---
description: Reviewing a sealed capa bundle — finding runs_root, opening manifest.json, events.sqlite, parquet samples, bridging to runs.sqlite catalog or external tools.
---

# Reviewing a run

**Audience:** anyone reading the bundle after the fact — the operator
checking yesterday's run, an analyst pulling data into a notebook, a
collaborator who wasn't at the rig.
**Scope:** how to find a sealed bundle, what you can see without leaving
capa, and the external tools you'll reach for once you do.

This is the bridge between the runtime (what produced the bundle) and
the bundle docs (what the bundle contains). For per-file schema, see
[what's in a bundle](../bundles/what-is-a-bundle.md) and
[reading bundles](../bundles/reading-bundles.md).

---

## Finding the bundle

### Right after the run

When the [Run tab](the-run-tab.md) badge moves to `Sealed`, the status
bar's `bundle:` pill shows the full path of the bundle that was just
written. The run-identity line on the Run tab itself reads
`run: <run_id>  (<run_status>)`.

### Help → Open logs folder

The simplest way to open the runs directory in your OS file browser:
**Help → Open logs folder**. Every bundle lives under
`runs/<run_id>/` (or wherever `runs_root` is configured — see
[storage / workspace layout](../configuration/workspace-layout.md)).

### Recent configs vs. recent bundles

The welcome screen and the Setup tab's **Open ▾ → Recent** submenu list
recently-opened **configs**, not bundles. There is no in-app
recent-bundles list today — bundles live on disk, indexed by their
`<run_id>` directory name (e.g. `runs/2026-05-24T143012Z_A12/`).

If you want a queryable index of runs across days, the runs root holds
a SQLite catalog (`runs.sqlite`) that the `capa catalog`
CLI subcommand reads — see [capa catalog](../cli/capa-catalog.md).

---

## What you can review inside capa

The app does not include a sealed-bundle browser. The Run tab shows the
last run's state badge (`Sealed` / `Failed`) and its `run_id`, and the
status bar's `bundle:` pill points at the path — but to read the data,
open the bundle externally.

This is deliberate. The bundle format is designed so any tool that can
read Parquet, SQLite, and a JSON manifest is a first-class reader; the
GUI shouldn't try to be the best one of those.

---

## What's in there

A sealed bundle directory:

```
runs/<run_id>/
├── manifest.json              ← the index card; every reader starts here
├── manifest.sha256            ← sealed-bundle integrity hash
├── scalars.parquet            ← normalized per-channel samples (long format)
├── device_records/            ← per-device native shapes
│   ├── watlow.parquet
│   ├── alicat.parquet
│   └── …
├── events.sqlite              ← structured event log (procedure, method, adapter, safety)
├── status.sqlite              ← periodic device health snapshots
├── video/
│   ├── webcam0.mkv            ← visible-light camera (PyAV / Matroska)
│   ├── webcam0.frames.parquet ← per-frame monoclock sidecar
│   ├── flir_ir.csq            ← FLIR IR (native radiometric container)
│   ├── flir_ir.frames.parquet ← per-frame monoclock sidecar
│   └── flir_ir.csq.meta.json  ← IR-only metadata sidecar
├── run.log                    ← structlog JSON-lines from the engine
└── env/                       ← uv.lock + packages.json for reproducibility
```

See [what's in a bundle](../bundles/what-is-a-bundle.md) for the
file-by-file tour.

---

## The manifest is the single source of truth

Open `manifest.json` first. Three fields together tell you whether to
trust the data:

| Field | What to look for |
|---|---|
| `run_status` | `completed` (normal end), `aborted` (operator stopped), `crashed` (recovered from abnormal termination). |
| `bundle_status` | `sealed` means safe to read and archive. Anything else, read with care. |
| `integrity.status` | `ok` means the seal hash matched on the last verification. |

The combination `aborted` + `sealed` + `ok` is the **expected** outcome
when you click Stop or Emergency — it means the operator interrupted the
run cleanly and the recorded data through the abort point is fully
trustworthy. See [aborting safely](aborting-safely.md#what-ends-up-in-the-bundle)
for the full status combination matrix.

The manifest also captures every piece of provenance the run needs to
be reproducible years later: capa version + git sha, Python version,
the full experiment / hardware / method snapshot, the calibration-set
reference, the operator id, the equipment manifest with adapter handles. See
[manifest and schema](../bundles/manifest-and-schema.md).

---

## Reading the data outside capa

The bundle reader has no runtime dependency — an analyst pulling data
into a notebook does **not** need NI-DAQ drivers, anything else the rig
PC needs. Just Python (or any language with Parquet / SQLite / MKV
readers). FLIR `.csq` files are the one exception: reading IR pixels
requires the FLIR SDK or Researcher tooling.

### `scalars.parquet`

Long format, one row per `(channel, t_mono_ns)` sample. Open with
polars, pandas, or pyarrow:

```python
import polars as pl
df = pl.read_parquet("runs/2026-05-24T143012Z_A12/scalars.parquet")
```

See [parquet channel samples](../bundles/parquet-channel-samples.md)
for the column reference, units, and time bases.

### Per-device records

`device_records/*.parquet` files preserve each device's native
emission shape — useful when you need raw Watlow registers or the full
Alicat multi-column row. See
[parquet device records](../bundles/parquet-device-records.md).

### `events.sqlite`

Structured event log. Every procedure milestone, every method step
entry/exit, every adapter command, every safety event. Open in any
SQLite browser (DBeaver, `sqlite3` CLI, DB Browser for SQLite):

```sql
SELECT t_mono_ns, kind, message, metadata_json FROM events
WHERE kind LIKE 'method.%' OR kind LIKE 'free_run.%'
ORDER BY t_mono_ns;
```

See [events sqlite](../bundles/events-sqlite.md) for the full event
taxonomy and column reference. The sibling `status.sqlite` holds the
periodic device-health snapshots — separate so the 1 Hz heartbeat
doesn't drown the event view.

### Video

Visible-camera files (`video/<name>.mkv`) are Matroska with libx264 by
default — play with VLC, ffmpeg, or any standard tool. FLIR IR files
(`video/<name>.csq`) are the **native radiometric container**; they
preserve per-frame temperature data and require the FLIR SDK or
Researcher IR tools to read pixels. Capa does not transcode them.

Every camera additionally writes a `<name>.frames.parquet` sidecar
mapping `frame_idx → t_mono_ns`, so you can align video frames to
channel samples at ms granularity. See [video](../bundles/video.md).

### Integrity verification

`manifest.sha256` is the integrity hash written at seal. To verify a
bundle copied off the rig:

```sh
uv run capa catalog verify 2026-05-24T143012Z_A12
```

If the copy lives outside the default runs root, pass
`--runs-root /path/to/runs`. From inside the bundle directory, standard
checksum tools can also read `manifest.sha256` directly.

See [integrity and sealing](../bundles/integrity-and-sealing.md) for
the protocol.

---

## When a run didn't seal cleanly

| Manifest reads | What to do |
|---|---|
| `bundle_status: sealed`, `integrity.status: ok` | Use the bundle normally. |
| `bundle_status: verification_failed`, `integrity.status: mismatch` or `partial` | The data is probably intact but the hash chain doesn't match — treat as suspect, investigate disk health. |
| `bundle_status: finalizing` | The process died mid-rewrite. Launch capa; finalize-on-startup will pick this up and re-seal. |
| `bundle_status: open` | The owning process is still alive, or it died without writing any final state. See [crash recovery](../troubleshooting/crash-recovery.md). |

---

*See also:* [What's in a bundle](../bundles/what-is-a-bundle.md),
[Manifest and schema](../bundles/manifest-and-schema.md),
[Reading bundles](../bundles/reading-bundles.md) for hands-on recipes,
[Integrity and sealing](../bundles/integrity-and-sealing.md),
[Aborting safely](aborting-safely.md) for the source of the status
fields you're reading.
