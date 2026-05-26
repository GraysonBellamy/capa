---
description: One sample journey through capa — adapter stream callback through ChannelSample build, ThreadBridge, Conductor dispatch, and writer thread onto parquet.
---

# Data flow

**Audience:** contributors and analysts; anyone asking "what happens between the device callback and the parquet file?"
**Scope:** one sample's complete journey, end to end. From an adapter callback inside a worker to channel-sample bytes on disk, a calibrated point on a plot, and a procedure subscriber's `_wait_for` predicate firing — all in parallel.

This page synthesizes the other Phase 8 deep-dives. If you arrived here cold, you may want [`threading-model.md`](threading-model.md) first — it pins the threads down. The full topology with diagrams lives at [`runtime-architecture.md` §2](runtime-architecture.md#2-topology).

---

## The acquisition path in one diagram

```
Worker thread                Conductor thread              UI / writer threads
─────────────                ─────────────────             ────────────────────
adapter.stream() yields
SourceRecord                ┐
        │                   │
        ▼                   │
build_channel_sample(...)   │  (calibration applied here)
  → ChannelSample           │
        │                   │
worker stamps               │
  t_bridge_put_ns           │
        │                   │
        ▼                   │
ThreadBridge.put(...)  ─────┼────►  async for emission in bridge:
                            │           │
                            │           ▼  (Conductor._dispatch_emission)
                            │       ┌─── await writer.submit(emission)  ────► WriterThread inbox
                            │       │       │                                    │
                            │       │       │  (queue.Queue, put_nowait or       │
                            │       │       │   anyio.to_thread blocking fall-   │
                            │       │       │   back)                            │
                            │       │       ▼                                    ▼
                            │       │   ─────────────────────────────►   Writer thread drain
                            │       │                                    loop dispatches per
                            │       │                                    sink → fsync at 1024
                            │       │                                    rows → eventually
                            │       │                                    .in-flight.arrows
                            │       │                                    on disk
                            │       │
                            │       ├─── await bus.publish(emission)  ──►  ConductorDataBus
                            │       │                                       (procedure subs,
                            │       │                                        analyzers,
                            │       │                                        safety monitor)
                            │       │
                            │       └─── ui_bridge.put_nowait(emission) ──►  UIBridge
                            │                                                    │
                            │                                                    ▼
                            │                                              UI drain task →
                            │                                              UIDataBus.publish_nowait
                            │                                              → ring buffer push →
                            │                                              plot repaints next tick
                            ▼
                       saturation monitor polls bridge.blocked_since_ms
                       and writer.last_accept_monotonic_ns every 1 s
```

Every arrow on that diagram is described elsewhere; this page narrates one trip across it.

---

## Step 1 — adapter callback inside the worker

The story starts on the worker's loop. For a Watlow heater on `COM6`:

1. `WatlowAdapter.stream()` is iterating, driven by `watlowlib.record(...)`.
2. The library reads from the serial port, parses one Modbus reply, and returns a `watlowlib.streaming.Sample`.
3. The adapter wraps it as a `SourceRecord` and immediately calls `build_channel_sample(...)` for every channel whose `WatlowParameter` binding matches this sample's `(parameter, instance)` pair. See [`channel-pipeline.md`](channel-pipeline.md) for the binding match logic.
4. Each call applies the channel's calibration ([`_helpers.py:build_channel_sample`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/_helpers.py)) and produces a `ChannelSample` carrying:
   - `channel="heater_pv"` (or whatever the spec named it)
   - `t_mono_ns` from `RunClock.t_mono_ns()`
   - `value` = calibration applied to raw
   - `unit` = `spec.output_unit()`
   - `uncertainty` = propagated through the calibration's `UncertaintySpec`
   - `source_record_id` = `"watlow:heater:42"` — back-pointer to the original `SourceRecord`
5. The adapter yields the `SourceRecord` followed by N `ChannelSample`s, in order. The worker's stream task pulls each item out of the iterator.

Two threads are *not* involved yet. Everything so far is on the worker's loop in the worker's thread. The adapter's own serial transport is bound to this thread; nothing else can touch it.

---

## Step 2 — onto the worker outbound bridge

Each emission is `put` onto the worker's outbound `ThreadBridge`:

```python
# Inside the worker's _stream_task
async for emission in adapter.stream():
    # Worker-side observability: stamp t_bridge_put_ns into the emission
    # (only on items that carry it; SourceRecord and ChannelSample do).
    await self._outbound.put(emission)
```

The bridge ([`bridge.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/bridge.py)) is `BridgePolicy.BLOCK`. If the consumer (conductor drain task) is slow, the worker `await`s here. The bridge's `blocked_since_ms` metric ticks while the worker is parked.

**Three timestamps are now attached to the sample:**

- `t_mono_ns` — adapter-stamped from `RunClock.t_mono_ns()` at the read site. Single authoritative time-base for the run.
- `t_utc` — adapter-stamped wall-clock, for human-readable correlation. Not used for joining.
- `t_bridge_put_ns` — worker-stamped right before `put`. Diagnostic only; lets observability measure per-hop latency.

`t_mono_ns` is what every reader joins on. `t_utc` answers "what time was this?" for humans. `t_bridge_put_ns` answers "where did latency come from?" for capa contributors.

---

## Step 3 — the conductor drain task

The conductor spawns one drain task per worker bridge at run start. Each one is a small async iterator ([`conductor.py:_drain_worker`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/conductor.py)):

```python
async def _drain_worker(self, resource_id, bridge):
    async for emission in bridge:
        await self._dispatch_emission(emission, writer=writer, bus=bus)
```

`_dispatch_emission` is the single fan-out point. For our `ChannelSample`, three things happen:

```python
# 1. Durable side first — losing an emission off-disk is worse than off-bus.
if plan.allows_channel(emission.channel):
    await writer.submit(emission)
# 2. ConductorDataBus — honours subscriber BLOCK / ABORT_RUN.
await bus.publish(emission)
# 3. UI mirror — fire-and-forget.
self._publish_ui(emission)
```

The order is load-bearing. Durable submit first — if it would block, the saturation deadline catches it; this is the path that *must* not silently drop. DataBus publish second — procedure subscribers get the sample with no UI involvement. UI mirror last — non-essential consumer.

The full fan-out logic, including how camera frames and events route differently, is in [`conductor.py:_dispatch_emission`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/conductor.py).

---

## Step 4a — durable submit to the writer

`writer.submit(emission)` is the conductor's only hand to the writer thread. It's async; underneath it pushes into a bounded `queue.Queue` via `put_nowait` (or a blocking fallback when full — see [`bundle-write-path.md`](bundle-write-path.md)).

From here the sample is on the writer thread:

1. Drain loop pops the item, dispatches by type. A `ChannelSample` goes to `RunBundleWriter.record_sample(...)`.
2. `ChannelSamplesSink` appends the row to its in-memory PyArrow `RecordBatchBuilder`.
3. Every 1024 rows the sink builds an Arrow record batch, writes it to `scalars.in-flight.arrows` via the IPC streaming writer, and `os.fsync`s.
4. At end of run, `finalize_in_place(...)` rewrites `scalars.in-flight.arrows` into `scalars.parquet` with row-group size 256k, sorted by `t_mono_ns`, zstd:6-compressed.

The same path applies for `SourceRecord` (lands in `device_records/<adapter>.in-flight.arrows`) and `FrameReceipt` (lands in `video/<camera>.frames.in-flight.arrows`). `DeviceEvent` and procedure/operator events go to `events.sqlite` instead — commits per write because events are precious and tiny.

---

## Step 4b — ConductorDataBus publish

`ConductorDataBus` ([`databus.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/databus.py)) is loop-affine to the conductor loop. Procedure subscribers, analyzers, and the planned `SafetyMonitor` all subscribe here.

For our `ChannelSample`, the bus walks two indexed buckets:

- `subscribe_channel("heater_pv", ...)` subscribers — woken if any.
- `subscribe_adapter("watlow", ...)` subscribers — woken if any (the channel's `source_record_id` carries the adapter prefix).
- Plus any `subscribe_all(...)` subscribers (rare).

Each subscription owns its own `BoundedQueue` and declares its own `BackpressurePolicy`. The procedure executor's `_wait_for("heater_pv >= 580 C", ...)` is a `subscribe_channel` with `BLOCK` policy — sustained block surfaces as conductor drain blocking, which the saturation monitor escalates.

**`subscribe_all` is the procedure's general lever.** A `WaitFor` step that watches multiple channels subscribes to all of them; the predicate runs per emission.

---

## Step 4c — UIBridge → UIDataBus

The UI mirror is one-way and fire-and-forget:

1. `_publish_ui(emission)` calls `ui_bridge.put_nowait(emission)`. The UIBridge uses `BridgePolicy.DROP_OLDEST` — the conductor never blocks here.
2. On the UI thread (`ui-main`), a drain task `async for`s the UIBridge.
3. For each emission, it calls `UIDataBus.publish_nowait(emission)`. Widget subscribers (which all use `DROP_OLDEST`) wake on their loop.
4. For `ChannelSample`, the UI also pushes into the `RingBufferRegistry` ([`ringbuffer.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/ringbuffer.py)). The plot timer reads from the ring buffer at ~10 Hz and repaints PyQtGraph.

The decimating ring buffer is the reason the plot is smooth even when the channel produces faster than the repaint cadence. Decimation here is plot-only — disk capture stays at native rate. See [`channel-pipeline.md`](channel-pipeline.md).

**Headless mode:** `UIBridge` is not constructed. `_publish_ui` is a no-op. Conductor and writer paths are identical; the UI side is just absent. This is the property that makes headless and GUI flows share one code path — the conductor doesn't know whether a UI exists.

---

## Camera frames are different

A `FrameReceipt` is *not* the pixel bytes. It's a small record carrying:

- `frame_idx` — camera-assigned monotonic id
- `t_mono_ns` — RunClock-derived
- `t_utc` — wall-clock anchor
- `camera` — stable camera name
- `capture_latency_s` — SDK→Python hand-off latency

The pixel bytes live in a separate file the **camera adapter** writes directly. The visible camera writes `.mkv` via PyAV; the FLIR IR camera writes `.csq` via the FLIR Atlas SDK. Both containers are opened on the worker loop and written from there; they never go through the writer thread.

The `FrameReceipt` exists so analysts can correlate frame ids back to channel samples via `t_mono_ns` without re-parsing the container — container-level PTS timestamps are too coarse for ms-granularity alignment. The frame-index sidecar at `video/<camera>.frames.parquet` is the join table.

`FrameReceipt`s in the conductor fan-out skip the `ConductorDataBus` — nothing on the bus subscribes to frame receipts in practice. They go only to the writer (for the sidecar) and to the UI (for preview tiles). See [`conductor.py:_dispatch_emission`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/conductor.py) for the explicit branch.

This is the one place the data flow forks structurally rather than by type; it's worth knowing when reading the bundle.

---

## The counterflow — commands

Commands go the other direction. The full path is in [`runtime-architecture.md` §9](runtime-architecture.md#9-command-flow); the summary:

```
UI manual card                                Or: procedure step
   │                                              │
   ▼                                              ▼
ManualClient.dispatch(device, cmd)        executor.dispatcher.dispatch(...)
   │                                              │
   ▼                                              ▼
ConductorDispatcher (if run armed) ─────► Conductor._dispatch
or PoolDispatcher (if idle)                       │
   │                                              ▼
   │                                  worker = pool.worker_for(device)
   │                                              │
   ▼                                              ▼
WorkerPool.dispatch(device, cmd)  ──────► Worker.dispatch (run_coroutine_threadsafe)
                                                  │
                                                  ▼
                                          await asyncio.shield(adapter.command(cmd))
                                                  │
                                                  ▼
                                          (cancellation shield — see §5)
                                                  │
                                                  ▼ result
                                          asyncio.wrap_future bubbles back
                                          across two thread seams
```

When the run is armed, the conductor records a `CommandIssued` event into the bundle (via the same writer path described above) before forwarding to the worker. Between runs, no event is recorded — there's no bundle to record into.

The double-hop (UI → conductor → worker) adds ~100–300 µs over the single-hop pool path. On a serial-roundtrip command that already costs 10–50 ms, this is invisible.

---

## What "saturation" looks like in this picture

Three places can block:

1. **Worker outbound bridge.** `BridgePolicy.BLOCK`; the producer (adapter) parks if the consumer (drain task) is slow.
2. **Writer inbox.** Fills if the writer thread can't drain fast enough; the conductor's `submit` switches to a blocking fallback.
3. **DataBus subscriber queue.** A `BLOCK` subscriber that doesn't consume forces the conductor's `bus.publish(...)` to wait.

The [`SaturationMonitor`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/saturation.py) polls all three (the bridge metrics and the writer's `last_accept_monotonic_ns`) every 1 s and escalates if any signal stays tripped for `saturation_deadline_s` (default 10 s). The escalation seals the bundle as `crashed_but_sealed` after running safe-shutdown. See [Saturation and deadlines](../safety/saturation-and-deadlines.md).

The deadline is the cross-cutting check that no single bridge's local backpressure can catch: when the *durable side* stops accepting, every bridge backs up together and none of them individually looks wrong. The end-to-end deadline is what makes that condition visible.

---

## Where to read more

- The threads behind every arrow on the diagram: [`threading-model.md`](threading-model.md).
- How a `ChannelSample` was constructed in the first place: [`channel-pipeline.md`](channel-pipeline.md).
- What the writer thread does once it gets the sample: [`bundle-write-path.md`](bundle-write-path.md).
- The UI side, including how widgets actually consume the bus: [`ui-runtime-boundary.md`](ui-runtime-boundary.md).
- The cancellation shield that protects the command counterflow: [`runtime-architecture.md` §5](runtime-architecture.md#5-the-cancellation-shield).
- What the on-disk artifacts look like once the run is sealed: [Reading a bundle](../bundles/reading-bundles.md).
