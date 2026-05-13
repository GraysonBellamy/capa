# Performance & architecture plan — outstanding work

**Status:** Trimmed 2026-05-12 to the items still open after the per-resource
worker migration plan landed.

The original review of the single-loop pipeline (adapter producers →
`BoundedQueue` → single fan-out → `WriterThread` + `DataBus`) covered nine
findings and nine work items. Most have been resolved or superseded:

- **Completed (landed 2026-05-10), within the old single-loop model:**
  - P0-1 — fan-out latency telemetry
  - P0-2 — rate-aware producer queue capacity + deadline policy
  - P0-3 — DataBus subscriber index
  - P0-4 — require `ABORT_RUN` for must-not-drop DataBus subscribers
- **Superseded by [`per-resource-worker-migration.md`](per-resource-worker-migration.md):**
  - P1-3 (BoundedQueue event reuse) — the migration removes
    `BoundedQueue` from the cross-thread emission path entirely
    (migration §11 item 14). The remaining `BoundedQueue` usage is
    loop-local inside `DataBus` and is not on the hot path.
  - P2-2 (multi-camera encode isolation) — the migration gives each
    camera its own per-resource worker thread, which is exactly the
    "dedicated thread per camera" approach P2-2 recommended.

The three items below remain real work. The migration plan explicitly
calls them out as out-of-scope (migration §12, scope note on kHz
aggregate rates): they become tractable *because of* the migration but
are a separate change set. Concrete file/line references throughout are
against `src/capa/...` and will need to be re-mapped to the
post-migration module layout (`src/capa/runtime/...`) when this work
proceeds.

---

## P1-1 · Internal hot-path record type (only if telemetry justifies it)

### Problem
`ChannelSample(...)` validates 11 fields through Pydantic v2 on every
construction. Adapters call it once per channel per tick. At sustained
high rates (NI block unrolled, multi-channel polled adapters) this
*could* be a meaningful CPython cost.

**However, the "skip validation" intuition does not survive measurement.**
A local microbenchmark on the dev machine:

```
ChannelSample(__init__):       2.35 μs/call
ChannelSample.model_construct: 17.52 μs/call   # ~7.5× SLOWER
```

Pydantic v2's `__init__` goes through the Rust validator core;
`model_construct` is a pure-Python fallback that walks each field. The
original draft of this section proposed routing the hot path through
`model_construct` — that would have **regressed** construction cost. Do
not do that.

### Approach
Treat this as a candidate, not a defect. **Gate the work on telemetry
evidence** from the existing fan-out / drain telemetry: only proceed if
writer-thread CPU or drain lag correlates with sample construction rate.

If the evidence lands, the right shape is a non-Pydantic internal
hot-path type, not a Pydantic fast path:

1. Define a slotted dataclass `_ChannelSampleRecord` (or use `attrs` with
   `slots=True, frozen=True`) carrying the same field set. No validators,
   no `extra="forbid"` machinery, no `model_validator`.
2. Adapters emit `_ChannelSampleRecord` through the worker outbound
   bridge and on to the writer. Pydantic `ChannelSample` is materialized
   only at boundaries that actually need validation: the public
   `DataBus.subscribe` surface, the procedure API, the manifest
   emitters.
3. Mirror the same split for `SourceRecord` if its
   `_check_shape_consistency` validator shows up in profiles.
   `DeviceEvent` / `DeviceSnapshot` are off-hot-path; leave them alone.
4. Sinks (`ChannelSamplesSink`, etc.) read fields by attribute access, so
   they don't care whether the input is the Pydantic or the dataclass
   variant — a `typing.Protocol` captures the read interface.

Microbenchmark first. If a slotted dataclass is not faster than
`ChannelSample.__init__` on this rig, skip P1-1 entirely; the bottleneck
isn't here.

### Files (only if work proceeds)
- [records.py](src/capa/devices/records.py) — add the internal record type.
- [_helpers.py:build_channel_sample](src/capa/devices/_helpers.py#L60) —
  return the internal type.
- Sinks/databus — protocol-typed reads.
- Boundary adapters (databus public API, procedure helpers) — convert
  to Pydantic at the boundary.

### Acceptance
- A microbenchmark (`tests/unit/test_emission_construct_perf.py`) shows
  ≥ 3× speedup for the internal record vs. `ChannelSample.__init__` on
  the dev machine, **before** any adapter code is converted. If the
  speedup isn't there, abandon the change.
- After conversion, an end-to-end sim at 4000 samples/s shows lower
  process CPU (`time.process_time` delta) vs. the Pydantic baseline.
- All existing tests pass.

### Risk
- Two emission types (internal vs. public) is a footgun — make the
  protocol the contract and never expose the internal type across module
  boundaries.
- Pydantic's `extra="forbid"` and shape validators are real safety nets.
  Losing them on the hot path means a buggy adapter can corrupt the
  bundle. Mitigation: a CI build runs the same adapters with an
  `assertion`-mode wrapper that constructs the Pydantic form alongside
  the internal form and diffs them.

### Effort
Two to three days, but only after telemetry justifies it. Otherwise
zero — defer indefinitely.

---

## P1-2 · Batch submission to the writer thread

### Problem
Each emission costs:
1. `queue.Queue.put_nowait` (lock acquire/release).
2. Drain-loop wake-up + `match` dispatch in the writer thread.
3. 13 individual `list.append` calls in `ChannelSamplesSink._Buffer`.
4. When the buffer hits `flush_rows=1024`, 13 `pa.array(...)` calls each
   walking the Python lists.

Single emissions are fine; the cost at 1000/s is the dominant Python-side
load. Batching can amortize all four costs.

**Independent of P1-1.** This change is about queue/list-churn cost, not
per-object construction cost; the wins are real even if the records stay
Pydantic. Sequence: do this *before* P1-1, and only revisit P1-1 if
telemetry still shows construction cost as the bottleneck after batching.

### Approach
1. Introduce a new `WriterItem` variant: `SampleBatch(samples: tuple[ChannelSample, ...])`.
2. The Conductor's per-worker drain task detects "burst" emissions from
   a single worker and coalesces them up to a small bound (e.g. 64).
   Specifically:
   - When the worker's outbound bridge has `depth > 8`, the drain task
     reads up to 64 items in a tight loop (non-blocking) before
     submitting them as a single `SampleBatch` to the writer thread.
   - The ConductorDataBus still gets one-at-a-time publishes (per-sample
     subscribers expect per-sample arrival).
3. `WriterThread._dispatch` learns the `SampleBatch` case and calls a new
   `RunBundleWriter.record_sample_batch(samples)` that appends all rows
   into the `_Buffer` lists in one go.
4. NIDAQ block-mode unroll skips the per-sample yield entirely and submits
   a batch directly — biggest win lives here.

### Files (post-migration mapping)
- [writer_thread.py](src/capa/storage/writer_thread.py) — new `SampleBatch` variant.
- [bundle.py](src/capa/storage/bundle.py) — `record_sample_batch`.
- [channel_samples_sink.py:write](src/capa/storage/channel_samples_sink.py#L216) — new `write_many(samples)`.
- `src/capa/runtime/conductor.py` (post-migration) — coalesce logic in
  the per-worker drain task.
- [nidaq.py:_stream_block_mode](src/capa/devices/nidaq.py#L572+) — emit batches.

### Acceptance
- A NIDAQ block-mode sim producing 500 Hz × 8 channels = 4000 samples/s
  shows ≥ 3× reduction in writer-thread CPU (measure via `time.process_time()`
  delta around the run).
- `manifest.json.queue_health.writer.bundle.write_p50_s` drops
  proportionally.
- All existing tests pass; per-sample arrival ordering on the databus is
  preserved.

### Risk
- Coalescing changes the timing distribution of writer submissions —
  bigger, less frequent. The fsync-per-flush durability story is unchanged
  (each batched submit still produces at most one flush).
- The coalesce trigger (`depth > 8`) is heuristic; needs tuning. Pick a
  conservative default; expose via env var for the rig.

### Effort
Two to three days.

---

## P2-1 · NI block-mode rectangular sidecar

### Problem
[nidaq.py:811-859](src/capa/devices/nidaq.py#L811-L859) unrolls each
`DaqBlock` into per-`(channel, sample)` `ChannelSample` instances.

Note: the current code *already* skips unbound channels — `bindings` at
[nidaq.py:830-837](src/capa/devices/nidaq.py#L830-L837) is built from
`self._channels`, and the per-sample loop at
[:846-847](src/capa/devices/nidaq.py#L846-L847) drops physical channels
that aren't bound. So the savings here are **not** "stop unrolling
unbound channels" — they're already not unrolled. The real costs that
remain:

1. Per-`(bound-channel, sample)` `ChannelSample` construction and
   fan-out. At 1 kHz × 8 bound channels = 8000 objects/s through the
   worker outbound bridge and into the normalized scalars sink.
2. The rectangular block itself is never persisted — only the
   library-native `SourceRecord` and the unrolled per-sample stream
   land in the bundle. Operators who want the raw waveform have nothing
   to read.

The guardrail `max_samples_per_block_unroll=10_000`
([nidaq.py:192-201](src/capa/devices/nidaq.py#L192-L201)) prevents
catastrophic misuse but still allows configurations that put the
pipeline at risk. The right answer is to **not unroll per-sample** at
all: keep the block rectangular all the way to disk, and derive
per-sample emissions only for the consumers that actually need them
(safety bands, live UI). Even one bound channel at 1 kHz is 1000 objects
per second of unnecessary churn through the drain if the procedure only
reads the channel at 10 Hz.

### Approach
1. Define a new in-bundle artifact:
   `device_records/<task_name>.blocks.in-flight.arrows` — an Arrow IPC
   stream with one record batch per `DaqBlock`. Schema:
   ```
   t_mono_ns_start   int64 (block start)
   sample_rate_hz    float64
   samples_per_ch    int32
   channels          list<dict<int32, string>>   (channel names)
   data              list<list<float64>>          (channels × samples)
   ```
   Or, to stay flat and queryable, a column-per-channel wide schema
   with `t_mono_ns` as a separate `int64` column reconstructed
   per-sample at write time (still cheaper than `ChannelSample` objects
   because no Python instance per sample).
2. Define `BlockEmission(SourceRecord)` — a new `DeviceEmission` variant
   that carries the rectangular numpy array directly, no per-sample
   Python objects.
3. NI block-mode emits **one** `BlockEmission` per `DaqBlock`. Per-sample
   `ChannelSample` derivation becomes opt-in per channel: a binding
   declares `derive_samples: Literal["all", "decimated", "none"]` (or
   similar), where `none` (default for raw waveform capture) means the
   block is persisted but no per-sample objects are ever constructed,
   `decimated` emits at a UI/safety cadence (e.g. 10 Hz from a 1 kHz
   block), and `all` matches today's behavior for backward compatibility.
   This is the actual win: a 1 kHz × 8-bound-channel run drops from 8000
   `ChannelSample` objects/s to ~80/s under `decimated`.
4. New `BlockSink` in `capa/storage/` handles the block artifact. The
   finalize stage rewrites it to `device_records/<task>.blocks.parquet`
   with the same large-row-group / sort-by-`t_mono_ns_start` treatment.
5. `SourceRecord` already has a `block_ref` field
   ([records.py:75](src/capa/devices/records.py#L75)) and a `shape="block"`
   variant — the scaffolding is already in records.py; this plan fills it
   in.
6. Update the guardrail: `max_samples_per_block_unroll` gates only the
   `derive_samples="all"` path. `decimated` and `none` are unconditionally
   allowed; raw block writes are unbounded.

### Files
- [records.py](src/capa/devices/records.py) — `BlockEmission` variant.
- [nidaq.py:_stream_block_mode](src/capa/devices/nidaq.py#L572+) — emit
  blocks, derive bound-channel samples only.
- New `src/capa/storage/block_sink.py`.
- [bundle.py](src/capa/storage/bundle.py) — route `BlockEmission` to the
  block sink.
- [writer_thread.py](src/capa/storage/writer_thread.py) — `_dispatch`
  case for `BlockEmission`.
- [finalize.py](src/capa/storage/finalize.py) — rewrite block in-flight
  to parquet.

### Acceptance
- A 1 kHz × 8-channel sim block run completes without tripping
  `ABORT_RUN`, writes `<task>.blocks.parquet`, and the per-sample drain
  cost is < 10% of the equivalent unrolled run (measure via writer
  metrics).
- The bundle reader can reconstruct per-sample views from the block file
  on demand. (Helper utility in `capa.storage.bundle.read_block_samples`
  or similar.)
- All existing NI tests pass; a new `tests/unit/test_nidaq_block_mode.py`
  asserts the new artifact shape and that bound channels still appear in
  `scalars.parquet` under `derive_samples="all"`.

### Risk
- This is the largest piece of work in the plan. Schema decision (long
  vs wide block shape) needs a deliberate call. Recommend wide
  (column-per-channel) because it's the natural NI shape and queryable
  in DuckDB without an unnest.
- Procedure / safety code that reads bound channels stays unchanged;
  unbound-channel consumers must learn to read the block file.

### Effort
Five to seven days including reader helpers, finalize integration, and
end-to-end test.

---

## Suggested order

1. **P1-2** (batch writer submission) — telemetry-gated; do this if
   `writer.bundle.write_p50_s` or drain lag rises with sample rate.
   Independent of P1-1.
2. **P2-1** (NI block rectangular sidecar) — the largest piece; do this
   only when there's a confirmed plan to run NI block at kHz, or when an
   operator needs the raw waveform persisted.
3. **P1-1** (internal hot-path record) — only if, after P1-2 lands,
   telemetry still shows construction cost as the dominant Python load.
   The naive Pydantic-fast-path approach does not work
   (`model_construct` is slower than `__init__` on this Pydantic v2
   build); proceed only if the slotted-dataclass microbenchmark
   demonstrates a real speedup first.

# Out of scope

- Replacing the asyncio/anyio event loop. AsyncIO is the right tool for
  the I/O-concurrency shape here; no plan changes this.
- Adopting general multiprocessing for the data path. The GIL hurts only
  in well-defined hotspots. Each is addressed in-process above (or by
  the per-resource worker migration). Multi-process is reserved for
  future `SubprocessWorker` fault isolation, not throughput.
- Trio backend. AnyIO already abstracts; if a future need arises,
  switching backends is a one-line config change.
