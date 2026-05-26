---
description: capa engine sink contract — ChannelSamplesSink, DeviceRecordsSink, FramesSink, EventsSink — patterns for InfluxDB, Postgres, S3, or LIMS run data.
---

# Custom sinks

**Audience:** integrators wanting to wire capa to a destination beyond the on-disk bundle (InfluxDB, Postgres, S3, a live LIMS push).
**Scope:** the engine's internal sink contract, why it is not yet a plugin surface, and the patterns available today.

---

## Status: roadmap + contract reference

Sinks are **engine-internal** today. They are not discoverable through an entry-point group; there is no `capa.sinks` plugin kind, no `plugins.lock` entry, no `capa plugins list` line. The shipped sinks — `ChannelSamplesSink`, `DeviceRecordsSink`, `FramesSink`, `EventsSink`, `StatusSink`, and `LogSink` — live inside `src/capa/storage/` and are owned by the `WriterThread` / `RunBundleWriter` path.

This page exists to:

1. Document the *contract* the writer thread expects, so external code (a post-run uploader, a downstream pipeline) can mimic it.
2. Capture the runtime constraints (backpressure, batching, schema drift) that any future pluggable sink will inherit.
3. Be honest about the gap: this is not yet a plugin tutorial.

If you have a concrete use case for an additional live destination, treat this page as design notes — and consider whether a *post-run* pipeline (read the bundle from disk, push to your destination) is sufficient. For most "send the data to system X" needs, the answer is yes. The bundle is well-shaped for downstream consumption — see [Reading bundles](../bundles/reading-bundles.md).

---

## The shipped sinks

| Sink | Source | Owns |
|---|---|---|
| `ChannelSamplesSink` | [`src/capa/storage/channel_samples_sink.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/channel_samples_sink.py) | Long-format `scalars.parquet` |
| `DeviceRecordsSink` | [`src/capa/storage/device_records_sink.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/device_records_sink.py) | Per-family `device_records/<family>.parquet` |
| `FramesSink` | [`src/capa/storage/video_sink.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/video_sink.py) | `video/<name>.frames.parquet`; camera adapters own the `.mkv` / `.csq` containers |
| `EventsSink` | [`src/capa/storage/events_sink.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/events_sink.py) | `events.sqlite` |

These are deliberately specialized. The contract documented below is what they have in common — not a formal Protocol that all four implement, but a pattern.

---

## The internal sink contract (descriptive)

A sink, in the current engine, is an object the `WriterThread` calls into when it has data to land. The pattern:

```
WorkerEmission (worker thread)
      │
      │ ThreadBridge (bounded, BLOCK policy)
      ▼
Conductor._drain_worker (conductor thread)
      │
      │ dispatch by emission type
      ▼
WriterThread.<method>(emission)         e.g. write_event, record_frame
      │
      │ owns file/Arrow handles
      ▼
Sink.append(...)                        appends to in-flight buffer
      │
      │ at INFLIGHT_FLUSH_ROWS or on close
      ▼
Sink.flush()                            atomic batch to disk
```

The methods on a sink that the writer thread relies on:

- An `append`-style method (`append_sample`, `append_record`, `record_frame`) that takes a typed emission and adds it to an in-flight buffer.
- A `flush` method, called automatically when the buffer reaches `INFLIGHT_FLUSH_ROWS` rows or when the run ends.
- A `close` / `finalize` method that converts the in-flight format to the bundle's final on-disk format and releases handles.

The sinks own their own file handles; the writer thread does not. This is deliberate — it lets each sink choose its on-disk format (Arrow IPC then Parquet, raw `.csq`, SQLite) without leaking format details into the engine.

### Backpressure: BLOCK at the worker-conductor bridge

The bounded inter-thread bridge between worker and conductor uses `BridgePolicy.BLOCK` ([`bridge.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/bridge.py)). A slow sink causes the writer thread to fall behind, which causes the conductor's inbound queue to fill, which back-propagates to the worker, which blocks on its `put`. The worker blocking is what the [saturation deadline](../safety/saturation-and-deadlines.md) monitor watches for; sustained block aborts the run.

This is the engineering rationale for not yet allowing arbitrary plugin sinks: a plugin sink that does long-running network I/O would trip the saturation deadline routinely. A future plugin sink kind will need either (a) its own off-engine worker thread, decoupled from the writer thread by an additional bridge, or (b) an explicit "best-effort, drop on backpressure" mode.

### Conductor → UI uses DROP_OLDEST

The conductor-to-UI bridge uses `BridgePolicy.DROP_OLDEST`. The reasoning: the UI is a subscriber to live data and falling behind it is preferable to blocking the engine on UI lag. The writer side never sees this bridge — it is downstream of the conductor's split into "writer path" and "UI path."

This split is also why the writer is not pluggable today and the UI is. Pluggable UI subscribers exist (the `DataBus`); pluggable writers do not.

### Batching: `INFLIGHT_FLUSH_ROWS`

The default flush threshold is `DEFAULT_FLUSH_ROWS_BULK` (from [`storage/_ipc.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/_ipc.py)). Sinks expose this as a module-level constant so tests can monkey-patch it down — synthetic tests would otherwise emit too few rows to trip a flush.

A custom sink would need to pick its own batching policy. The shipped sinks all use "row-count threshold" because Arrow IPC streams have a natural row-batch granularity; a time-based policy (`flush every N seconds`) would also work but would complicate crash recovery — the in-flight Arrow file would have to be timestamped per batch.

### Schema drift: lock on first flush

`DeviceRecordsSink` locks column types at first flush ([`device_records_sink.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/device_records_sink.py)). A row that contradicts the locked schema raises `SchemaDriftError`. This catches devices that switch payload shapes mid-run (firmware reconfiguration, library bug) and surfaces the inconsistency loudly rather than corrupting the file.

A custom sink would need to make an analogous choice: either lock the schema and refuse drift, or accept and translate. The shipped pattern is "lock at first flush, refuse drift" — that is the safer default for scientific data.

---

## Patterns that work today

Until a plugin sink kind exists, three patterns cover almost every "wire capa to system X" need:

### 1. Read the bundle after the run finishes

For analytics, archival, and any destination that does not need live data, read the sealed bundle. Parquet is fast and standard; SQLite is portable; the bundle's `manifest.json` is enough to drive a generic uploader. See [Reading bundles](../bundles/reading-bundles.md) for recipes.

This is the right answer for ~90% of "I want to push this to InfluxDB" requests. The lag — runs are tens of minutes; uploads are seconds afterward — is almost always acceptable.

### 2. Subscribe to the `DataBus` from a UI-side process

The engine's `DataBus` is the publish/subscribe surface the UI uses. An external subscriber can attach to the same bus and receive every `ChannelSample` in real time, with no involvement from the writer thread or the saturation deadline. This is appropriate for **live** consumption — dashboards, alerting — but not for archival, because subscribers can fall behind and lose data (the bus uses `DROP_OLDEST` on slow subscribers).

This is not a fully documented plugin path either — it requires reaching into engine internals — but it is the closest thing to a "live custom subscriber" capa has.

### 3. Stream the in-flight Arrow IPC files

The in-flight `scalars.in-flight.arrows` and `device_records/<family>.in-flight.arrows` files are written incrementally during the run. An external process can tail these and push rows to its own destination while the run is in progress. Crash recovery in the engine handles partial in-flight files; an external reader has to do the same.

This is the closest thing to "live write-through" that does not require engine changes. It is not officially supported, but the file format is stable enough that the pattern works.

---

## What a plugin sink kind would look like

For reference, a plausible shape if and when `capa.sinks` becomes a plugin kind:

```python
class Sink(Protocol):
    id: ClassVar[str]
    name: ClassVar[str]
    version: ClassVar[str]
    config_model: ClassVar[type[BaseModel]]
    accepts: ClassVar[frozenset[EmissionKind]]   # which emission types to receive
    backpressure: ClassVar[Literal["block", "drop_oldest", "drop_newest"]]

    async def open(self, ctx: SinkContext) -> None: ...
    async def append(self, emission: WorkerEmission) -> None: ...
    async def flush(self) -> None: ...
    async def close(self) -> None: ...
```

The shape is not committed and is liable to change before any release. The point of listing it: a future sink plugin will look broadly like a procedure plugin — Pydantic config model, entry-point registration, contract check at load time — with the added wrinkle of declaring its backpressure policy upfront so the engine can isolate slow plugins from the saturation deadline.

If you have a concrete need that is not covered by the three patterns above, that is useful design input — the shape of the gap informs what the eventual plugin surface needs to look like. Open an issue describing the destination, the latency tolerance, and the failure mode you want; it informs the contract more concretely than this page can.

*See also:* [Bundle write path](../architecture/bundle-write-path.md), [Reading bundles](../bundles/reading-bundles.md), [Saturation and deadlines](../safety/saturation-and-deadlines.md), [Threading model](../architecture/threading-model.md).
