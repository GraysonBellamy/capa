# Performance & architecture plan

Drafted 2026-05-10 from the architectural review of the in-process pipeline:
adapter producers → `BoundedQueue` → single fan-out → `WriterThread` (durable)
+ `DataBus` (lossy/UI). Concrete file/line references throughout are against
`src/capa/...` at the time of writing.

## Scope

The review confirms the architecture is sound for the documented 3–60 Hz
envelope. The plan below addresses (a) blind spots in observability that
should land before anyone tries to push the rig harder, and (b) the specific
data-shape and pipeline changes needed to take the system past ~100 Hz × N
channels and past a single camera at 30 fps. Multiprocessing is **not**
adopted for the data path; it appears only as a deferred option for
multi-camera encoding.

---

## Confirmed findings

Each finding cites the source. Items 1–5 are observed defects or missing
instrumentation. Items 6–8 are scaling limits that will bite at well-defined
load thresholds. Item 9 is deferred.

| # | Finding | Evidence |
|---|---|---|
| 1 | Fan-out latency is not measured. `QueueMetrics.mark_enqueued`/`mark_dequeued` exist but are never called from the producer / fan-out path; only `observe_depth` is wired. | [engine.py:1145](src/capa/experiment/engine.py#L1145), [metrics.py:99-113](src/capa/core/metrics.py#L99-L113) |
| 2 | Producer queue is hard-coded capacity 256 with `policy=BLOCK` and **no deadline** — `BLOCK`'s `put()` branch ignores `abort_after_s` entirely. A stalled fan-out blocks every adapter indefinitely. | [engine.py:112](src/capa/experiment/engine.py#L112), [engine.py:1026-1030](src/capa/experiment/engine.py#L1026-L1030), [backpressure.py:131-138](src/capa/core/backpressure.py#L131-L138) |
| 3 | `DataBus.publish` walks every subscriber and runs each predicate per emission. No `(kind, channel)` index. | [databus.py:172-175](src/capa/core/databus.py#L172-L175) |
| 4 | **Latent risk**: a `BLOCK` `DataBus` subscriber would halt the fan-out, back-pressure the producer queue, and stall every adapter. No current subscriber uses `BLOCK` (UI uses `DROP_OLDEST`; `SafetyMonitor` is still a placeholder — see [statusbar.py:49](src/capa/ui/statusbar.py#L49), [engine.py:1186](src/capa/experiment/engine.py#L1186)), but nothing prevents one from being added. | [databus.py:175](src/capa/core/databus.py#L175), [engine.py:1218-1225](src/capa/experiment/engine.py#L1218-L1225) |
| 5 | `ChannelSample`, `SourceRecord`, `DeviceEvent`, `DeviceSnapshot` are Pydantic v2 `BaseModel` (`frozen=True`, `extra="forbid"`). Full validation runs on every construction (~2.4 μs/call locally for `ChannelSample`). **Not yet proven to be a bottleneck**; treat as a candidate, not a defect. | [records.py:34-220](src/capa/devices/records.py), [_helpers.py:87](src/capa/devices/_helpers.py#L87) |
| 6 | `WriterThread.submit` is one-item-at-a-time. Each item costs a `queue.Queue.put` (lock-acquired) and a `match` dispatch. The bulk-row sinks (`ChannelSamplesSink`) then `list.append` 13 columns per sample. | [writer_thread.py:285-296](src/capa/storage/writer_thread.py#L285-L296), [channel_samples_sink.py:216-235](src/capa/storage/channel_samples_sink.py#L216-L235) |
| 7 | NI block-mode unrolls hardware-clocked blocks into per-`(channel, sample)` `ChannelSample` objects. Guardrail `max_samples_per_block_unroll=10_000` exists; the "rectangular sidecar / TDMS escape" referenced in docstrings is **not yet implemented**. | [nidaq.py:192-201](src/capa/devices/nidaq.py#L192-L201), [nidaq.py:812+](src/capa/devices/nidaq.py#L812) |
| 8 | `BoundedQueue` reallocates `anyio.Event` instances on every full → not-full and empty → not-empty transition. At sustained high churn this is ~1 alloc per put/get pair. | [backpressure.py:95-106](src/capa/core/backpressure.py#L95-L106) |
| 9 | Camera encode is `anyio.to_thread.run_sync` per frame against the default anyio thread pool. Fine at 1–2 cameras × 30 fps; 3+ cameras or 60+ fps will contend for the pool and steal scheduler time from device adapters. Deferred — addressed in §P2. | [webcam.py:730](src/capa/devices/camera/webcam.py#L730) |

---

# Plans

Priority labels:

- **P0** — small, observable wins. Land before anyone runs the rig harder.
- **P1** — required to comfortably exceed ~100 Hz × N channels sustained.
- **P2** — required to run NI block mode at kHz or 3+ simultaneous cameras.

---

## P0-1 · Wire fan-out latency telemetry ✅ (landed 2026-05-10)

### Problem
We measure queue **depth** but not queue **lag**. Lag (the time an emission
spends between adapter enqueue and writer-thread submit) is the leading
indicator of fan-out becoming the bottleneck — depth can stay low while lag
grows if the fan-out is just barely keeping up. Without lag we can't tell
whether the writer thread, the databus, or a slow subscriber is at fault.

### Approach
1. The producer task calls `metrics.mark_enqueued(id(emission))` immediately
   before `await queue.put(emission)`.
2. The fan-out task calls `metrics.mark_dequeued(id(emission))` immediately
   after `await queue.get()`.
3. Add a new `WriterMetrics` instance keyed `fanout.submit`: the fan-out
   times the `await self._writer_thread.submit(emission)` call.
4. Add a new `WriterMetrics` instance keyed `fanout.publish`: the fan-out
   times the `await self._databus.publish(emission)` call. This isolates a
   slow subscriber from a slow writer.
5. Surface all three through the existing `snapshot_for_manifest` shape;
   they land in `manifest.json.queue_health` automatically.

### Files
- [engine.py:_producer_task / _fanout_task](src/capa/experiment/engine.py#L1130-L1225)
- [metrics.py](src/capa/core/metrics.py) — no schema change; the
  `WriterMetrics` collector already supports `time_write()`.

### Acceptance
- Running a sim recipe produces three new entries in `manifest.json.queue_health`:
  `queue.producer-fanout` shows non-zero `lag_s_max`, and
  `writer.fanout.submit` / `writer.fanout.publish` show non-zero `write_p99_s`.
- A new test (`tests/unit/test_metrics_fanout.py`) drives a synthetic
  blocked subscriber and confirms `fanout.publish` lag rises while
  `fanout.submit` stays flat.

### Risk
None. Reservoir sampler is fixed-size; `id()` is O(1); the existing
`_enqueue_times: dict` already pops stale entries. Telemetry-only change.

### Effort
Half a day.

---

## P0-2 · Rate-aware producer queue capacity *and* a deadline policy ✅ (landed 2026-05-10)

### Problem
Two coupled defects, not one:

1. `PRODUCER_QUEUE_CAPACITY = 256` ([engine.py:112](src/capa/experiment/engine.py#L112))
   is a hard-coded constant. For 8 producers at 50 Hz each (aggregate 400/s)
   that's ~640 ms of head-room, and a tick can emit `SourceRecord` plus N
   `ChannelSample` instances per channel, so the *emission* rate runs ahead
   of the *poll* rate. Aggregate emissions can fill the queue in well under
   a second.
2. The queue is constructed with `policy=BackpressurePolicy.BLOCK`
   ([engine.py:1026-1030](src/capa/experiment/engine.py#L1026-L1030)).
   `BLOCK` in `BoundedQueue.put` ([backpressure.py:131-138](src/capa/core/backpressure.py#L131-L138))
   does **not** honor `abort_after_s` — only `ABORT_RUN` does
   ([backpressure.py:139-160](src/capa/core/backpressure.py#L139-L160)).
   A stalled fan-out therefore blocks every adapter indefinitely with no
   crash-but-sealed escape hatch. Resizing the queue alone does not fix
   this; whatever capacity we pick, a stuck consumer will eventually fill
   it.

### Approach
1. **Switch the producer queue to `ABORT_RUN` with an explicit deadline.**
   The producer→fan-out queue is the right place for a deadline: durable
   sinks past their grace window should fault the run rather than freeze
   acquisition. Pick a default `abort_after_s` of 5 s and surface it on
   `EngineConfig` so the rig can tune it.
2. Compute aggregate emission rate at engine arm. Use *emission* rate
   (`SourceRecord` + per-channel `ChannelSample`s + any per-tick events),
   not just adapter poll rate. NI block in particular emits
   `rate_hz × bound_channels` samples per second per task.
3. Size the queue: `cap = clamp(aggregate_emission_rate * abort_after_s * 1.5, 256, 32768)`.
   The 1.5× absorbs short bursts above the steady rate; the upper clamp
   keeps in-flight loss bounded if the process dies.
4. Add an `expected_emission_rate_hz: float | None` property to the
   `DeviceAdapter` protocol. NIDAQ polled = `params.rate_hz ×
   (1 + bound_channels)`; NIDAQ block = `timing.rate_hz × bound_channels`;
   Alicat/Watlow/Sartorius = `poll_rate × (1 + bound_channels)`. Adapters
   that don't expose a hint contribute a conservative default (60 Hz × 4
   channels = 240 emissions/s).
5. Log the chosen capacity and deadline at engine start so they're
   auditable post-run.

### Files
- [engine.py:_run_task_group](src/capa/experiment/engine.py#L1010) — switch
  policy to `ABORT_RUN`, pass `abort_after_s`, compute and pass capacity.
- [adapter.py:DeviceAdapter](src/capa/devices/adapter.py#L112) — protocol addition.
- One-line addition to each adapter (`expected_emission_rate_hz` property).

### Acceptance
- A unit test with 10 adapters at 100 Hz × 4 channels each computes
  ≥ 30 000 capacity (clamped to 32768), not 256.
- An adapter that omits `expected_emission_rate_hz` is accepted and
  contributes the default; a log line records the fallback.
- A unit test parks the fan-out for longer than `abort_after_s` and
  asserts the producer's `put()` raises `BackpressureAbortError` (not
  blocks forever); the engine surfaces this as a `crashed` run with the
  bundle sealed by the existing crashed-but-sealed path.
- Manifest's `queue.producer-fanout.depth_max` over a sustained sim run
  stays comfortably under `capacity`.

### Risk
- Larger queue ⇒ more in-flight data lost on hard kill. Mitigation: keep
  the upper clamp at 32768; the writer thread is the durability boundary,
  not the queue.
- Memory: 32768 × ~1 KB per pydantic instance = ~32 MB worst case. Fine.
- Switching to `ABORT_RUN` means a sustained writer-thread stall now
  *aborts the run* instead of pausing acquisition. That's the correct
  behavior (you can't safely continue if the durable sink is gone), but
  it does shift the failure mode visible to operators — document this in
  the operator runbook alongside the deadline knob.

### Effort
One to two days.

---

## P0-3 · DataBus subscriber index ✅ (landed 2026-05-10)

### Problem
[databus.py:172-175](src/capa/core/databus.py#L172-L175) is `for sub in
list(self._subscriptions): if not sub.predicate(emission): continue`. With
N subscribers and one channel-of-interest each, the cost is O(N) predicate
calls per emission even though only one matches. At 10 channels × 10 Hz
that's invisible; at 1000 emissions/s × 10 subscribers it's 10 000 Python
function calls per second of dead work.

### Approach
1. Maintain three parallel dicts on `DataBus`:
   ```
   _by_channel: dict[str, list[Subscription]]   # channel-filter subs
   _by_adapter: dict[str, list[Subscription]]   # adapter-filter subs
   _wildcard:   list[Subscription]              # subscribe_all
   ```
2. `subscribe_channel` / `subscribe_adapter` / `subscribe_all` register
   into the right bucket.
3. `publish(emission)` does:
   - `isinstance(emission, ChannelSample)` → iterate `_by_channel.get(emission.channel, ())` + `_wildcard`.
   - else → iterate `_by_adapter.get(emission.adapter, ())` + `_wildcard`.
4. The general `subscribe(predicate=...)` escape hatch keeps a fourth
   `_custom` list and falls back to the current per-emission filter. Used
   only by tests today; not a hot-path concern.
5. `unsubscribe` removes from whichever bucket the sub was registered in.
   Track the bucket on the `Subscription` itself.

### Files
- [databus.py](src/capa/core/databus.py) — internal refactor only; public
  API surface (`subscribe`, `subscribe_channel`, etc.) is unchanged.

### Acceptance
- All existing databus tests pass without modification.
- A new benchmark test (`tests/unit/test_databus_publish_perf.py`) registers
  100 channel subscribers and 1 wildcard; publishing 10k samples for one
  channel should be ~100× faster than the linear baseline (use
  `pytest-benchmark` or a hand-rolled timing assertion with a generous
  threshold).

### Risk
- Subscription removal becomes O(B) where B is the bucket size, not O(N).
  Net win.
- The custom-predicate escape hatch is still O(N_custom). Document that
  custom predicates are second-class for hot paths.

### Effort
One day including the benchmark.

---

## P0-4 · Require `ABORT_RUN` for must-not-drop DataBus subscribers ✅ (landed 2026-05-10)

### Problem (latent)
`DataBus.publish` awaits each subscriber's `queue.put` serially
([databus.py:172-175](src/capa/core/databus.py#L172-L175)). A `BLOCK`
subscriber that stops draining would freeze the entire fan-out, which
freezes the producer queue, which freezes every adapter.

No current subscriber uses `BLOCK` — the UI uses `DROP_OLDEST`, and
`SafetyMonitor` is still a placeholder
([statusbar.py:49](src/capa/ui/statusbar.py#L49),
[engine.py:1186](src/capa/experiment/engine.py#L1186)). But nothing in
the `DataBus` API prevents a future caller from passing
`policy=BackpressurePolicy.BLOCK`, and the obvious place this would land
(real safety monitor) is exactly where a missed evaluation matters most.
Land the guardrail now, before there's a real BLOCK subscriber to migrate.

### Approach
**Make `ABORT_RUN` the contract for must-not-drop subscribers.** Don't
soften `BLOCK`'s global semantics; instead steer must-not-drop callers
toward `ABORT_RUN`, which already has a deadline.

1. Add a `subscribe_critical(name, *, predicate, abort_after_s)` helper
   on `DataBus` that wraps `subscribe(...)` with
   `policy=BackpressurePolicy.ABORT_RUN`. Document it as the
   safety-monitor path.
2. Reject `subscribe(policy=BackpressurePolicy.BLOCK)` at the `DataBus`
   layer (raise on construction) with a message pointing at
   `subscribe_critical`. `BLOCK` keeps its existing meaning for the
   producer→fan-out queue (P0-2 already moves that to `ABORT_RUN` too;
   no other in-tree call site uses it).
3. When the critical subscriber's stuck window expires, the
   `ABORT_RUN` `put()` raises `BackpressureAbortError` inside
   `publish()`. The engine catches it, logs `databus.subscriber.stuck`
   with the subscriber name, and the run goes through the existing
   crashed-but-sealed path. Bounded blast radius without redefining
   `BLOCK`.

Concurrent fan-out (`await_all=False`, one task group per publish) is
**not** in scope — adds cancellation/ordering complexity. Re-evaluate
only if P0-1 telemetry shows `fanout.publish` lag is the real
bottleneck under realistic load.

### Files
- [databus.py](src/capa/core/databus.py) — new `subscribe_critical`,
  reject `BLOCK` in `subscribe`, surface `subscriber.stuck` events
  through the engine's logger when `BackpressureAbortError` fires.

### Acceptance
- A unit test creates a critical subscription whose consumer never
  reads, publishes past the deadline, and asserts the publish call
  raises `BackpressureAbortError` with the subscriber name in the
  message.
- A unit test asserts `DataBus.subscribe(policy=BLOCK)` raises with a
  message pointing at `subscribe_critical`.
- The `BackpressurePolicy.BLOCK` docstring explicitly states it is for
  bounded internal queues only; `DataBus` subscribers must use
  `ABORT_RUN`.

### Risk
Surfacing the contract may flag tests or in-progress branches that
already construct BLOCK subscribers — those callers should migrate.

### Effort
Half a day.

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
evidence** from P0-1: only proceed if `fanout.submit` lag or writer-thread
CPU correlates with sample construction rate.

If the evidence lands, the right shape is a non-Pydantic internal
hot-path type, not a Pydantic fast path:

1. Define a slotted dataclass `_ChannelSampleRecord` (or use `attrs` with
   `slots=True, frozen=True`) carrying the same field set. No validators,
   no `extra="forbid"` machinery, no `model_validator`.
2. Adapters emit `_ChannelSampleRecord` through the fan-out and writer.
   Pydantic `ChannelSample` is materialized only at boundaries that
   actually need validation: the public `DataBus.subscribe` surface, the
   procedure API, the manifest emitters.
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
2. The fan-out detects "burst" emissions from a single producer and
   coalesces them up to a small bound (e.g. 64). Specifically:
   - When `producer_queue.depth > 8`, the fan-out drains up to 64 items in
     a tight loop (`get_nowait`) before submitting them as a single
     `SampleBatch` to the writer thread.
   - The databus still gets one-at-a-time publishes (per-sample
     subscribers expect per-sample arrival).
3. `WriterThread._dispatch` learns the `SampleBatch` case and calls a new
   `RunBundleWriter.record_sample_batch(samples)` that appends all rows
   into the `_Buffer` lists in one go.
4. NIDAQ block-mode unroll skips the per-sample yield entirely and submits
   a batch directly — biggest win lives here.

### Files
- [writer_thread.py](src/capa/storage/writer_thread.py) — new `SampleBatch` variant.
- [bundle.py](src/capa/storage/bundle.py) — `record_sample_batch`.
- [channel_samples_sink.py:write](src/capa/storage/channel_samples_sink.py#L216) — new `write_many(samples)`.
- [engine.py:_fanout_task](src/capa/experiment/engine.py#L1205-L1225) — coalesce logic.
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

## P1-3 · BoundedQueue event reuse (defer behind telemetry)

### Problem
[backpressure.py:95](src/capa/core/backpressure.py#L95) and
[:106](src/capa/core/backpressure.py#L106) construct fresh `anyio.Event`
instances on full↔not-full and empty↔not-empty boundary crossings. This
is correct (anyio events are single-shot) but wasteful: at sustained high
churn it's an alloc + GC entry per put/get pair.

**Low priority.** Allocator churn is real but cheap; the cost only
matters if profiling shows allocation/GC pressure during sustained
high-rate runs. Land P0-1 telemetry first, look at the producer-fanout
queue stats under a realistic workload, then revisit. Do not block P1-2
or P2-1 on this.

### Approach
Replace the two `anyio.Event` slots with `anyio.Condition`-based
signalling, or — simpler — switch to `anyio.create_memory_object_stream`
under the hood. The memory object stream is the AnyIO-blessed bounded
queue primitive; it already handles wake-up without per-cycle allocation
and supports both BLOCK (default) and DROP_OLDEST via wrapper logic.

Two paths:

**(a) Direct rewrite using `anyio.create_memory_object_stream`.**
`BoundedQueue` becomes a thin wrapper that holds the send/recv streams
and re-implements DROP_OLDEST and ABORT_RUN on top. Loses the bespoke
`anyio.Event` pair; gains AnyIO's optimised primitives.

**(b) Manual condition variable.**
Keep `deque` + two `anyio.Lock`s and `anyio.Condition`s. More code but
keeps the existing structure recognisable.

(a) is cleaner; (b) is closer to the current shape. Go with (a) unless
benchmarking shows a regression on the BLOCK or DROP_OLDEST paths.

### Files
- [backpressure.py](src/capa/core/backpressure.py).

### Acceptance
- All existing `tests/unit/test_backpressure.py` cases pass unchanged.
- A microbenchmark cycling 100k puts/gets at capacity shows lower
  allocation count via `tracemalloc`.

### Risk
- Memory-object-stream semantics around close/cancel may differ subtly;
  walk every test case. The ABORT_RUN deadline logic must be preserved.

### Effort
One to two days.

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
   producer→fan-out queue and into the normalized scalars sink.
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
per second of unnecessary churn through the fan-out if the procedure
only reads the channel at 10 Hz.

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
  `ABORT_RUN`, writes `<task>.blocks.parquet`, and the per-sample fan-out
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

## P2-2 · Multi-camera encode isolation (deferred)

### Problem
At 3+ simultaneous cameras (or 60+ fps on one), the anyio thread pool
becomes a contention point: every frame goes through `to_thread.run_sync`,
and the pool's default size (40) is shared across the whole engine. PyAV's
Python-side handling holds the GIL during packet encode/mux, so cameras
also compete with each other and with adapter I/O for CPU.

### Approach
Decision point first, code second. The current path is sufficient through
two cameras at 30 fps. **Do not pre-build this.** Land P0-1 lag metrics;
when the camera path shows up as the bottleneck (writer or fan-out
latency degrading correlated with frame count), revisit with real data.

If/when implemented, the cheapest shape is:

1. **Dedicated thread per camera, not a pool.** Replace
   `anyio.to_thread.run_sync(self._push_frame_sync, ...)` with a per-camera
   long-lived `threading.Thread` that reads frames from a per-camera
   `queue.Queue[ndarray]` and runs `_push_frame_sync` synchronously.
   Frame receipts flow back via an `anyio` memory object stream the
   async side already drains. This isolates encoders from each other and
   from the global thread pool. Effort: one to two days.

2. **Per-camera subprocess (if step 1 isn't enough).** Use
   `multiprocessing.shared_memory` for the raw frame ring; the encoder
   process owns the MKV container and the libx264 instance. Hand back
   small `FrameReceipt` pickles via `multiprocessing.Queue`. The engine
   gains a `CameraProcessSupervisor` that owns lifecycle (spawn, watchdog,
   reap). Effort: a week, plus the always-present "did the encoder process
   crash silently" failure mode.

The Camera abstraction is already shaped to absorb either change —
`writer_thread.record_frame(receipt)` is the only thing the engine sees.

### Files
None yet. This is a deferred decision, recorded here so it isn't
re-discovered when load increases.

### Acceptance
N/A; deferred.

### Effort
Deferred. Plan revisit when telemetry shows camera-driven contention.

---

# Suggested order of work

1. **P0-1** ✅ (fan-out telemetry) — must come first; everything else
   becomes measurable. *Landed 2026-05-10.*
2. **P0-2** ✅ (rate-aware queue + deadline policy) — small, isolating, no
   downstream churn. The policy switch to `ABORT_RUN` is the real fix;
   the capacity sizing is the secondary benefit. *Landed 2026-05-10.*
3. **P0-3** ✅ (databus index) — independent refactor. *Landed 2026-05-10.*
4. **P0-4** ✅ (require `ABORT_RUN` for must-not-drop subscribers) —
   independent; lands the guardrail before a real safety subscriber
   exists to migrate. *Landed 2026-05-10.*
5. **P1-2** (batch writer submission) — telemetry-gated; do this if
   `writer.bundle.write_p50_s` or fan-out lag rises with sample rate.
   Independent of P1-1.
6. **P2-1** (NI block rectangular sidecar) — the largest piece; do this
   only when there's a confirmed plan to run NI block at kHz, or when an
   operator needs the raw waveform persisted.
7. **P1-1** (internal hot-path record) — only if, after P1-2 lands,
   telemetry still shows construction cost as the dominant Python
   load. The naive Pydantic-fast-path approach does not work
   (`model_construct` is slower than `__init__` on this Pydantic v2
   build); proceed only if the slotted-dataclass microbenchmark
   demonstrates a real speedup first.
8. **P1-3** (BoundedQueue event reuse) — telemetry-gated; only if
   allocation/GC pressure shows up in profiles.
9. **P2-2** (multi-camera) — deferred; revisit on telemetry signal.

# Out of scope

- Replacing the asyncio/anyio event loop. AsyncIO is the right tool for
  the I/O-concurrency shape here; no plan changes this.
- Adopting general multiprocessing for the data path. The GIL hurts only
  in well-defined hotspots (item 9 for cameras, items 5–7 for high-rate
  CPython object churn). Each is addressed in-process above. Multi-process
  is reserved for camera encode and only on a measured signal.
- Trio backend. AnyIO already abstracts; if a future need arises,
  switching backends is a one-line config change.
