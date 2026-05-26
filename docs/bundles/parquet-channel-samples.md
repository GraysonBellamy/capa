---
description: capa scalars.parquet schema — long-format channel samples with t_mono_ns, value, raw_value, unit, status; the canonical post-binding table for analysis.
---

# Channel samples (parquet)

**Audience:** analysts reading channel samples in polars, pyarrow, or pandas; anyone writing a binding-aware downstream tool.
**Scope:** the `scalars.parquet` schema — every column, its dtype, units, the time-base contract, and the round-trip rules for non-float values.

`scalars.parquet` is the long-format channel-sample table: one row per `(channel, t_mono_ns)`. Every adapter's emissions are projected through their declared [`ChannelBinding`](../configuration/channel-bindings.md) and land in this single file. **This is the file post-binding analysis should hit.** The per-adapter files under [`device_records/`](parquet-device-records.md) are the unprocessed safety net — useful when a binding misfires or you need the library-native field that didn't get promoted, but for normal "what was the specimen temperature 12 seconds after ignition" queries, you want `scalars.parquet`.

---

## Schema at a glance

The schema is locked at module load in [`channel_samples_sink.py:_arrow_schema()`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/channel_samples_sink.py):

```python
pa.schema([
    pa.field("t_mono_ns",        pa.int64(),                              nullable=False),
    pa.field("t_mono_s",         pa.float64(),                            nullable=False),
    pa.field("channel",          pa.dictionary(pa.int32(), pa.string()), nullable=False),
    pa.field("value",            pa.float64(),                            nullable=False),
    pa.field("value_kind",       pa.dictionary(pa.int32(), pa.string()), nullable=False),
    pa.field("raw_value",        pa.float64(),                            nullable=True),
    pa.field("raw_text",         pa.string(),                             nullable=True),
    pa.field("raw_kind",         pa.dictionary(pa.int32(), pa.string()), nullable=True),
    pa.field("unit",             pa.dictionary(pa.int32(), pa.string()), nullable=False),
    pa.field("uncertainty",      pa.float64(),                            nullable=True),
    pa.field("status",           pa.dictionary(pa.int32(), pa.string()), nullable=False),
    pa.field("source_record_id", pa.string(),                             nullable=True),
    pa.field("source_field",     pa.string(),                             nullable=True),
])
```

Thirteen columns, dictionary-encoded where cardinality is low, no nested types. The file is sorted by `t_mono_ns` ascending and written with zstd compression at level 6 in 262,144-row row groups (see [`finalize.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/finalize.py)).

---

## Column-by-column reference

| Column | Type | Nullable | Notes |
|---|---|---|---|
| `t_mono_ns` | `int64` | no | Canonical persisted time base. Nanoseconds since `RunClock.started_mono_ns` — the run-relative monotonic frame. Primary sort key on disk. |
| `t_mono_s` | `float64` | no | Convenience derived value (`t_mono_ns / 1e9`). Redundant with `t_mono_ns` but cheap and human-readable for plot axes. |
| `channel` | `dictionary<int32, string>` | no | Channel name as declared in `ChannelSpec.name` (e.g. `tc_specimen`, `mfc_n2`). Dictionary-encoded — see [Dictionary encoding](#dictionary-encoding). |
| `value` | `float64` | no | Calibrated, dimensioned value in `unit`. Always populated — bool channels store `0.0`/`1.0`, int channels store the integer cast to float. See [round-tripping](#round-tripping-non-float-values). |
| `value_kind` | `dictionary<int32, string>` | no | One of `"float"`, `"int"`, `"bool"`. The type tag needed to round-trip non-float channels out of the float64 `value` column. |
| `raw_value` | `float64` | yes | Pre-calibration numeric value when `ChannelSpec.keep_raw` is set and the raw value is numeric. Null when raw is text or not retained. |
| `raw_text` | `string` | yes | Pre-calibration text value when the source field is a string (status words, mode tags). Null for numeric or absent raws. |
| `raw_kind` | `dictionary<int32, string>` | yes | One of `"float"`, `"int"`, `"bool"`, `"str"`, or null when no raw was retained. Mirrors `value_kind` for the raw column. |
| `unit` | `dictionary<int32, string>` | no | Canonicalized output unit (e.g. `"degC"`, `"sccm"`, `"Pa"`). Matches `ChannelSpec.derived_unit` when set, otherwise `ChannelSpec.unit`. |
| `uncertainty` | `float64` | yes | Absolute uncertainty in `unit`. Populated only when the channel's calibration declares an `UncertaintySpec`. |
| `status` | `dictionary<int32, string>` | no | Health flag. `"ok"` is the success path; adapters also emit `"underrange"`, `"overload"`, `"sensor_fail"`, `"comm_error"`. The set is open-ended — sinks store it verbatim, downstream filters should treat unknown values as "non-ok". |
| `source_record_id` | `string` | yes | Back-pointer to the `SourceRecord.record_id` this sample was derived from. Join key into [`device_records/<adapter>.parquet`](parquet-device-records.md). |
| `source_field` | `string` | yes | Which field of the source record was projected. Library-native name — Alicat `Mass_Flow`, Watlow `process_value`, NI-DAQ `ai0`. |

Notably **not present:**

- **No `t_utc` column.** Wall-clock recovery is a post-hoc operation: `t_utc = manifest.started_utc + Timedelta(t_mono_ns, "ns")`. See [The two time bases](#the-two-time-bases).
- **No `metadata` column.** The Pydantic [`ChannelSample`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/records.py) model carries a `metadata: dict[str, Any]` field for in-memory routing; it is **not** persisted to the parquet. If a binding needs to carry context to the bundle, route it through `source_field` or emit a device event.

---

## The two time bases

`t_mono_ns` is the canonical time base. It comes from `time.monotonic_ns()` on the conductor host, offset so that `t_mono_ns == 0` corresponds to `RunClock.started_mono_ns` (the monotonic instant the run armed). It is **immune to wall-clock corrections** — NTP step events, manual clock changes, and DST transitions cannot perturb it. Two samples 100 ms apart on a wall clock with a mid-interval NTP correction are still exactly 100 ms apart in `t_mono_ns`.

`t_mono_s` is the same value in seconds as a float64. It's a convenience for plotting and human inspection. The float64 mantissa loses sub-microsecond precision past about 100 seconds of run time, so **never** use `t_mono_s` as a join key — use `t_mono_ns`.

Wall-clock recovery, when you need it for log correlation:

```python
import polars as pl
from datetime import datetime, timezone

manifest_started_utc = datetime.fromisoformat(manifest["started_utc"])  # from manifest.json
df = df.with_columns(
    t_utc=manifest_started_utc + pl.duration(nanoseconds=pl.col("t_mono_ns"))
)
```

`manifest.started_utc` is the wall-clock anchor; `t_mono_ns` is the run-relative offset. Both live in [`manifest.json`](manifest-and-schema.md). Do not use `t_utc` for alignment math across files in the bundle — every other parquet/sqlite file in the bundle uses `t_mono_ns` for exactly the immunity-to-NTP reason described above. Events, video frames, and device records all align on `t_mono_ns`; `t_utc` is for "what time of day did this happen" only.

---

## Round-tripping non-float values

`value` is always `float64`, even for bool and int channels. The `value_kind` column carries the original type so analysts can recover it:

| Original | `value` | `value_kind` | `raw_text` |
|---|---|---|---|
| `42.7` (float) | `42.7` | `"float"` | null |
| `7` (int) | `7.0` | `"int"` | null |
| `True` (bool) | `1.0` | `"bool"` | null |
| `"running"` (text raw) | `NaN` | `"float"` | (raw_text holds the string when `keep_raw`) |

Recovery in polars:

```python
import polars as pl

bool_view = (
    df.filter(pl.col("value_kind") == "bool")
      .with_columns(pl.col("value").cast(pl.Boolean).alias("bool_value"))
)
```

**Why a single-typed value column instead of a union or struct?** The dominant query path on this file is "all rows for one channel, plotted against time" — that path benefits massively from a rectangular, scan-friendly `value` column. Bool and int channels are a small minority (limit-switch states, mode codes); paying one extra dictionary-encoded column for them keeps the hot path simple. Analysts who never touch a bool channel can ignore `value_kind` entirely.

For text-valued raw fields (Alicat gas-name strings, Watlow alarm-state words), `raw_text` carries the original string and `raw_kind = "str"`. The numeric `value` is whatever the binding chose to project — typically `NaN`, sometimes a coded integer.

---

## Dictionary encoding

Five columns are dictionary-encoded: `channel`, `value_kind`, `raw_kind`, `unit`, `status`. The pattern is consistent — each is a low-cardinality string column riding alongside a high row count. A 1-hour run at 60 Hz across 30 channels produces ~6.5 M rows; storing `"tc_specimen"` as a 4-byte dictionary index instead of a 12-byte string per row collapses tens of megabytes to a single dictionary entry.

Practical implications:

- **polars** reads these as `pl.Categorical`. Filters like `pl.col("channel") == "tc_specimen"` are O(dict-size) on the dictionary, not O(rows).
- **pyarrow** exposes them as `pa.dictionary(pa.int32(), pa.string())`. Use `.dictionary_decode()` if you need the materialized string column (rarely necessary).
- **pandas** via `pyarrow` reads them as `CategoricalDtype`. Group-by and value-counts are fast; arbitrary string ops require `.astype(str)` first.

The dictionary indices are scoped per row group. Cross-row-group queries reconstruct the dictionary transparently.

---

## How rows get here

The path from device to row:

1. **An adapter emits a [`SourceRecord`](parquet-device-records.md).** One of four shapes — scalar, vector, block, or event. The record carries library-native field names (e.g. Alicat returns a row with `Mass_Flow`, `Pressure`, `Setpoint`).
2. **The configured [`ChannelBinding`](../configuration/channel-bindings.md) projects record fields into `ChannelSample` instances.** A single source record can produce zero, one, or many channel samples — one per bound field. The binding handles calibration, unit conversion, and uncertainty.
3. **The conductor routes the samples to the `ChannelSamplesSink`.** The sink stages rows in per-column lists and flushes to the in-flight Arrow IPC stream (`scalars.in-flight.arrows`) every `INFLIGHT_FLUSH_ROWS` samples. Each flush is fsynced — power loss leaves the last completed batch on disk.
4. **At finalize, [`_rewrite_inflight_to_parquet()`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/finalize.py) rewrites the stream to `scalars.parquet`.** The rewrite sorts the whole table by `t_mono_ns` ascending, then writes parquet with `compression=zstd`, `compression_level=6`, `row_group_size=262_144`, and `data_page_version="2.0"`. The in-flight file is removed once the rewrite verifies.

If the in-flight stream is torn before its first flush boundary (i.e. crash before any complete batch was written), the rewrite returns `False` and the bundle finalizes without a `scalars.parquet`. See [integrity and sealing](integrity-and-sealing.md) for what that looks like in the manifest.

---

## Reading recipe — minimum viable

```python
import polars as pl

df = pl.read_parquet("runs/<run_id>/scalars.parquet")

tc_t = (
    df.filter(pl.col("channel") == "tc_specimen")
      .select(["t_mono_s", "value"])
      .sort("t_mono_s")
)
```

That's the load-and-extract-one-channel recipe. For wider/pivoted queries (multiple channels side by side, resampling to a common time grid, joining with events or video frames), see [Reading a bundle](reading-bundles.md).

---

## Implementation notes for contributors

- **Schema is locked at [`channel_samples_sink.py:_arrow_schema()`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/channel_samples_sink.py).** Adding, removing, renaming, or retyping a column requires a `bundle_schema_version` bump and a migration entry — see [Bundle versioning](bundle-versioning.md).
- **The schema is constructed once at module import** and assigned to `CHANNEL_SAMPLES_SCHEMA`. Sink instances do not own their own schema; they reference the module-level one. Tests that need to monkey-patch should patch the module attribute, not pass a custom schema.
- **`value` is `float64` even for bool channels.** Do not "optimize" this to a union or sparse-column representation without coordination — every downstream reader assumes the rectangular `value` column. See [round-tripping](#round-tripping-non-float-values) for the design rationale.
- **The sink writes Arrow IPC during the run, not Parquet.** The streaming format is `scalars.in-flight.arrows` — a record-batch-per-flush Arrow IPC stream that survives power loss at batch boundaries. The Parquet form only materializes at finalize. This is the v1 → v2 schema-bump landmark (see [bundle versioning](bundle-versioning.md)); pre-v2 bundles wrote Parquet incrementally and had worse crash semantics.
- **Sort-by-`t_mono_ns` happens in the rewrite, not at write time.** In-flight order is insertion order, which is *roughly* sorted but not guaranteed monotonic across concurrent producers (two adapters flushing samples with overlapping timestamps can interleave). Any code reading the in-flight file directly (e.g. live-viewing tools) must not assume monotonic time.
- **`metadata` on the in-memory [`ChannelSample`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/records.py) is not persisted.** It exists for in-process routing only. If a binding needs to attach context that survives to the bundle, it must do so through `source_field`, a custom `status` value, or a separate device event.

---

*See also:* [Device records (parquet)](parquet-device-records.md) · [Reading a bundle](reading-bundles.md) · [Manifest and schema](manifest-and-schema.md) · [Bundle versioning](bundle-versioning.md) · [Channel bindings](../configuration/channel-bindings.md)
