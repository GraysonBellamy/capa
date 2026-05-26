---
description: capa device_records/*.parquet — native adapter records for Alicat, Watlow, Sartorius, and NI-DAQ in wide_row, long_row, single_value_row, and block shapes.
---

# Device records (parquet)

**Audience:** analysts who need the raw native adapter records (not the binding-projected channel samples); plugin authors deciding what shape a new adapter should emit.
**Scope:** the `device_records/<adapter>.parquet` files — the four record shapes, per-adapter schemas, the schema-drift contract, and the link to `scalars.parquet`.

The [channel-binding system](../configuration/channel-bindings.md) in `hardware.toml` decides which adapter-emitted fields get projected into [`scalars.parquet`](parquet-channel-samples.md) as normalized channel samples. That projection is intentional — it's what gives every plot, alarm, and downstream analyzer a single canonical long table to read from. But it's also lossy by design: any library-native field the binding did *not* promote is gone from `scalars.parquet`. The `device_records/` directory is the unprocessed safety net. Every row an adapter emitted, in its native shape, lands here in one parquet file per adapter family. A future analyst can re-derive a channel the original binding never knew to promote without having to re-run the rig.

---

## The four record shapes

Every [`SourceRecord`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/records.py) carries a `shape` tag drawn from the closed set [`RecordShape`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/records.py). The tag mirrors the library's natural row/block layout:

| Shape | When | Layout |
|---|---|---|
| `wide_row` | Most multi-channel adapters (Alicat, NI-DAQ polled, NI-DAQ block metadata) | One row per emission, columns = readings (`Mass_Flow`, `Abs_Press`, …) |
| `long_row` | Watlow (one parameter at a time over Modbus) | Rows of `(device, parameter, instance, value)` |
| `single_value_row` | Sartorius balance | One value per record — the mass reading |
| `block` | Reserved for kHz NI-DAQ acquisition via TDMS sidecar | Metadata record; bulk samples live in a sidecar (not currently emitted in tree) |

The shape tag for each adapter family is mirrored into the bundle manifest's `data_shape.device_records[].layout` field by [`finalize.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/finalize.py), so a reader can decide how to interpret each file without opening it first. The known mapping is defined in `_KNOWN_LAYOUTS` and lives in lockstep with the adapter source.

---

## The `device_records/` directory

Each adapter family writes exactly one parquet file under `device_records/`, named after the adapter id ([`DEVICE_RECORDS_DIRNAME`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/device_records_sink.py) = `"device_records"`):

```
runs/<run_id>/device_records/
├── alicat.parquet           # wide_row
├── watlow.parquet           # long_row
├── sartorius.parquet        # single_value_row
├── nidaq_polled.parquet     # wide_row
└── nidaq_block.parquet      # wide_row block-metadata records (when block mode is used)
```

Per-family multiplexing: if two Alicats are configured in the same run, **both** contribute rows to the same `alicat.parquet`. The `device` column (Alicat-adapter-assigned name, matching `ChannelSpec.source.device`) disambiguates them. This is the [`DeviceRecordsSink`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/device_records_sink.py) routing one `_PerFamilyWriter` per `record.adapter`, regardless of how many physical devices that family covers.

---

## Per-adapter shape

### Alicat — `wide_row`

Each `alicatlib.Sample` is one `DataFrame` row per poll. The emit site is in [`alicat.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/alicat.py): the adapter calls `alicatlib.sample_to_row(sample)` (the library's own canonical serializer) so the row schema is identical to what an offline `alicatlib` recorder would produce. That parity guarantee is what lets sim and real bundles flow through the same downstream tooling without branching.

Columns (firmware-dependent measurement fields; the canonical `t_mono_ns`, `t_utc`, and `record_id` headers are prepended by the sink):

```
record_id, t_mono_ns, t_utc,
device, unit_id,
Mass_Flow, Abs_Press, Mass_Flow_Setpt, Mix_Gas, status,
requested_at, received_at, midpoint_at, monotonic_ns, latency_s
```

Field names use `alicatlib`'s underscored canonical form (`Mass_Flow`, not `"Mass Flow"`). Which measurement columns appear depends on the device's firmware — a pressure controller and a mass-flow controller will produce different column sets, but each is stable for the life of a run.

### Watlow — `long_row`

Watlow speaks Modbus, reading one parameter at a time. A single tick of the worker therefore produces several records, each carrying a single `(parameter, instance)` reading. The adapter ([`watlow.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/watlow.py)) emits one `SourceRecord` per parameter via `watlowlib.sample_to_row(sample)`:

```
record_id, t_mono_ns, t_utc,
device, address, protocol,
parameter, parameter_id, instance, value, unit,
requested_at, received_at, latency_s
```

The `parameter` column holds the readable parameter name (`process_value`, `setpoint`, …); `instance` is the loop index (1-based) for multi-loop controllers. The metadata field also carries `tick_first` so the conductor can collapse a parameter fanout back into a single observation per acquisition tick when reporting the operator-facing poll rate.

### Sartorius — `single_value_row`

The balance reports a single mass reading per poll. The adapter ([`sartorius.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sartorius.py)) emits one record per reading through `sartoriuslib.sample_to_row`:

```
record_id, t_mono_ns, t_utc,
device,
value, unit, sign, stable, overload, underload, decimals, sequence,
requested_at, received_at, latency_s
```

(Column set inferred from the `sartoriuslib.Reading` fields constructed in `sartorius_sim.py`; the real adapter uses the same library serializer, but the exact final column set is whatever `sartoriuslib.sinks.sample_to_row` produces.) The `stable`, `overload`, and `underload` boolean flags are what the safety layer reads to gate ignition-time procedure steps; they are also surfaced on the derived channel sample's `status` (`"ok"` / `"settling"` / `"overload"`).

### NI-DAQ polled — `wide_row`

Polled mode (`timing is None` or `timing.mode == "on_demand"`) drives `nidaqlib.streaming.record_polled` and emits one wide row per read via `nidaqlib.reading_to_row` — see [`nidaq.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/nidaq.py) around line 704:

```
record_id, t_mono_ns, t_utc,
device, task,
<channel_1>, <channel_2>, …, <channel_N>,
requested_at, received_at, latency_s
```

One column per declared NI channel. Suitable for the common 3–60 Hz scalar TC / analog-in case.

---

## NI-DAQ block mode and the `block` shape

This is the corner that surprises people, so it gets its own section.

**The `block` shape exists in the type system but is not emitted by any in-tree adapter today.** The [`RecordShape`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/records.py) literal includes `"block"`, the `DeviceRecordsSink` knows how to count and skip those records (`skipped_blocks` tracks them, the bulk-sample landing path was deferred to a TDMS sidecar that hasn't been implemented yet), and the manifest's `_KNOWN_LAYOUTS` reserves the `"block"` layout tag for `nidaq_block`. But what the **NI-DAQ block-mode adapter actually emits** is `shape="wide_row"` — one *metadata* record per `DaqBlock` (see [`nidaq.py:_record_for_block`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/nidaq.py) around line 755):

```
record_id, t_mono_ns, t_utc,
block_index, first_sample_index, samples_per_channel,
sample_rate_hz, channels, task_started_at,
read_started_at, read_finished_at, elapsed_s
```

The per-sample measurements are **not** written into `device_records/nidaq_block.parquet`. Instead, each block is unrolled into `samples_per_channel × bound_channels` `ChannelSample`s and routed through `scalars.parquet`, with per-sample timestamps reconstructed as `task_started_at + (first_sample_index + k) / sample_rate_hz`. The `device_records/nidaq_block.parquet` file is metadata-only — useful for joining a block id back to its source-side timing, but not where the kHz analog values live.

**Note for analysts.** If you want NI-DAQ hardware-clocked samples, read `scalars.parquet` filtered to the bound channels. The `device_records/nidaq_block.parquet` rows tell you *which block a sample came from*, not *what the values were*. Despite this on-disk reality, the manifest's `data_shape.device_records` entry for `nidaq_block` still publishes `layout: "block"` — a forward-compatible label, not a current-state description.

The fan-out is gated at adapter open by `max_samples_per_block_unroll` (default 10 000). Configurations that would exceed it fail to open with a pointer to the (still-deferred) rectangular sidecar path.

---

## Schema-drift contract

Within a single run, an adapter's emitted column set MUST be stable. The sink's [`_PerFamilyWriter`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/device_records_sink.py) infers the schema from the first batch of rows it flushes and then **locks** it. Every subsequent row is cast against the locked schema; rows that introduce columns the locked schema doesn't know about raise [`SchemaDriftError`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/device_records_sink.py), and the run will fail (the conductor surfaces this as a crash). The same error fires if the per-adapter `shape` tag changes mid-run — e.g. a Watlow `long_row` adapter that suddenly tries to emit a `wide_row`.

The rationale is simple: parquet rewrite at finalize needs a stable schema across the full row stream. There is no "schema-per-row-group" escape hatch — a single locked schema covers every row in the file. Strict failure beats a corrupted device-records file that downstream tooling can't open.

**Practical implication for plugin authors.** Emit a stable column set from the very first record onwards. If a measurement is sometimes absent (e.g. a gas-mix column that's only present when the controller is in mix mode), emit it as `None` rather than omitting the key. The schema inference at first flush treats unseen keys exactly like seen-as-`None` keys (both become nullable columns), so explicit nulls round-trip cleanly. Late-arriving keys do not.

---

## Linking records back to channel samples

Every [`ChannelSample`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/records.py) carries a `source_record_id` back-pointer to the `SourceRecord.record_id` that produced it (and a `source_field` naming the specific library-native field the binding projected). That's the join key for round-tripping from `scalars.parquet` back into `device_records/<adapter>.parquet`. Record ids are constructed per-adapter, per-device, per-sequence (`make_record_id(adapter_id, device_name, seq)`) and are stable for the life of the run.

A typical polars round-trip — "find the raw Alicat row behind this temperature sample":

```python
import polars as pl

samples = pl.read_parquet("runs/<run_id>/scalars.parquet")
alicat  = pl.read_parquet("runs/<run_id>/device_records/alicat.parquet")

joined = (
    samples.filter(pl.col("channel") == "mfc_n2_flow")
           .join(alicat, left_on="source_record_id", right_on="record_id", how="left")
)
```

For Watlow's `long_row` shape, the same join works — a single channel sample joins back to one Watlow row, identified by the `(parameter, instance)` columns alongside the bound value.

---

## When to read records vs. channel samples

| Reach for `scalars.parquet` when… | Reach for `device_records/<adapter>.parquet` when… |
|---|---|
| The binding promotes the field you care about | The binding didn't promote a field you now want |
| You want canonical units (`degC`, `sccm`, `Pa`) | You want raw pre-calibration values in their library-native units |
| You want cross-adapter alignment on a single timeline | You're auditing exactly what an adapter actually emitted |
| You're plotting, alarming, or running downstream analysis | You're debugging a binding or chasing a calibration mismatch |

`scalars.parquet` is the right answer for 90% of analysis. `device_records/` is the right answer when `scalars.parquet` doesn't carry the column you need — and that's the *whole reason* the records files exist.

---

## Implementation notes for contributors

A few details about the sink that aren't obvious from the public API:

- **One adapter family, one parquet file.** Multiple physical devices of the same family multiplex via the `device` column. The sink keys on `SourceRecord.adapter` (the adapter-id string), not on `device` — so two Alicats produce one file, but Alicat plus Watlow produce two files.
- **Type inference locks after the first flush per family.** The sink learns the schema from the first buffered batch and rejects drift thereafter. Columns whose only observed value is `None` are typed as nullable string — the safest fallback that round-trips both subsequent text and numerics (the latter via stringification rather than silent truncation).
- **Header columns are added by the sink, not the adapter.** `record_id`, `t_mono_ns`, and `t_utc` are prepended by [`_record_to_row`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/device_records_sink.py) and shadow any same-named keys in `record.row`. The library-native column with the same name (if any) is preserved alongside under its library name, but the canonical join column is the sink's.
- **`block`-shape records are skipped, not erroneous.** The sink counts them in `skipped_blocks` per adapter and writes nothing to disk — the TDMS sidecar landing path is deferred work. Today no in-tree adapter emits `shape="block"`, so this skip path is effectively dormant.
- **Block-record invariants are enforced by [`SourceRecord`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/records.py).** A `block` record must have empty `row` and a non-None `block_ref`; a non-`block` record must have `block_ref=None`. The pydantic `model_validator` rejects the violation at construction time, so a malformed record never reaches the sink.
- **Two-stage finalize.** While the run is live, the sink writes Arrow IPC streams to `<adapter>.in-flight.arrows`. At finalize, those are rewritten to `<adapter>.parquet` with large row groups, sorted by `t_mono_ns`, zstd-compressed (see [Integrity and sealing](integrity-and-sealing.md) for the rewrite-then-seal protocol).

---

*See also:* [Channel samples (parquet)](parquet-channel-samples.md) · [Channel bindings](../configuration/channel-bindings.md) · [Devices overview](../devices/overview.md) · [Reading a bundle](reading-bundles.md) · [Integrity and sealing](integrity-and-sealing.md)
