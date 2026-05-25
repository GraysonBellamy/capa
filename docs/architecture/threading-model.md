# Threading model

**Audience:** contributors changing anything inside `src/capa/runtime/`, `src/capa/ui/`, or `src/capa/storage/`, or adding adapters that touch I/O at unusual cadences.
**Scope:** every thread CAPA creates while a run is live, what each one owns, and the rules for crossing between them. This is the *contributor's rule book* — the topology overview lives in [`runtime-architecture.md` §2](runtime-architecture.md).

---

## Why threads, not one big asyncio loop

A single-loop design would put every adapter `await` on the same scheduler as the UI repaint. A Watlow read that takes 80 ms (RX-buffer hiccup, slow USB-serial adapter) would stall every plot tick, every drain task, and every other adapter's poll cycle for those 80 ms. We tried that. It produced exactly the saturated, jittery behavior CAPA is supposed to make impossible.

CAPA instead **gives every hardware contention domain its own thread**. A serial port, a DAQmx chassis, or a camera handle gets one [worker](../glossary.md#worker), and the [conductor](../glossary.md#conductor) — the per-run coordinator — also lives on its own thread. The UI thread is then free to do nothing but UI. A wedged adapter cannot freeze the rest.

---

## Thread inventory

At full real config (six devices + two cameras), nine threads are alive during a run:

| Thread name        | Loop                   | Lifetime | Owns                                                                                       |
|--------------------|------------------------|----------|--------------------------------------------------------------------------------------------|
| `ui-main`          | qasync (asyncio + Qt)  | process  | Qt widgets, plot timers, [`UIDataBus`](../glossary.md#bridge) subscribers, [`ManualClient`](../glossary.md#worker) cards |
| `conductor`        | asyncio (per-run)      | run      | `ProcedureRunner`, drain tasks, heartbeat, [`SaturationMonitor`](../safety/saturation-and-deadlines.md), `RunClock`, `ConductorDataBus`, ingest |
| `worker-<resource>`| asyncio (per-resource) | config   | One adapter set (1..N adapters sharing one `resource_id`) and its transport handles        |
| `writer`           | sync (no loop)         | run      | `RunBundleWriter` and every sink (`scalars.in-flight.arrows`, `events.sqlite`, `frames/*`) |

Worker thread names follow `worker-<device-name>` (e.g. `worker-heater`, `worker-cdaq1`). The thread name is bound into every log record by [`heartbeat.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/heartbeat.py), so every line in `run.log` identifies which thread emitted it.

**Lifetimes nest:**

- `ui-main` lives for the process.
- Each `worker-*` thread is opened when [`WorkerPool.open()`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/pool.py) is called (Apply & Connect) and stays alive across runs until config-reload or app-quit.
- `conductor` and `writer` are per-run: constructed at `Conductor.start()`, joined at the end of `Conductor.stop()`. A fresh pair is created for each new run; the workers underneath survive.

See [`runtime-architecture.md` §3](runtime-architecture.md#3-three-lifecycles) for the lifetime diagram.

---

## Per-thread allowed/forbidden operations

This is the table to land on after a `wrong-loop` runtime error.

| From this thread...   | You may                                                                                                | You must not                                                                              |
|-----------------------|--------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------|
| `ui-main`             | Touch Qt; subscribe to `UIDataBus`; `await ManualClient.dispatch(...)`; read `RunController` state     | Touch a worker's adapter directly; publish to `ConductorDataBus`; `await` a worker future without `wrap_future` |
| `conductor`           | `await` worker dispatch futures (via `wrap_future`); publish to `ConductorDataBus`; submit to writer   | Touch Qt; touch an adapter; publish to `UIDataBus` (one-way mirror, drains feed it)        |
| `worker-<resource>`   | Call the adapters it owns; put onto its outbound `ThreadBridge`                                        | Touch any other worker's adapter; touch Qt; publish to either `DataBus`                    |
| `writer`              | Touch sinks; `os.fsync`                                                                                | `await` anything (no loop); touch adapters; touch Qt                                       |

**Invariants behind the table:**

1. **One resource per worker, one worker per resource.** Adapters are never touched off-thread. See `runtime-architecture.md` §21 invariant 1.
2. **Cancellation shield.** In-flight `adapter.command(...)` calls run inside `asyncio.shield(...)` on the worker loop. A caller cancelling its future never aborts the hardware transaction in flight. The shield is the single most load-bearing rule in the runtime — see [`runtime-architecture.md` §5](runtime-architecture.md#5-the-cancellation-shield).
3. **DataBus is loop-affine.** Publishing to a `DataBus` from a different loop than the one that owns it raises `DataBusLoopError` ([`databus.py:DataBusLoopError`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/databus.py)). Two separate bus instances exist — `ConductorDataBus` on the conductor loop, `UIDataBus` on the qasync loop — and the only legal flow from conductor → UI is through the [UIBridge](#crossing-the-seam-threadbridge) drain.
4. **Loop-affine asyncio primitives.** `asyncio.Queue`, `asyncio.Event`, `asyncio.Lock` instances must be constructed inside the loop that will own them. Cross-thread signalling goes through `loop.call_soon_threadsafe(...)` — never `event.set()` directly from another thread.

---

## Crossing the seam: ThreadBridge

[`ThreadBridge`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/bridge.py) is the *only* primitive used to move data between two asyncio loops on two different threads. Stdlib only — an `asyncio.Queue` on the consumer side, a `Semaphore` on the producer side, and `loop.call_soon_threadsafe` for the hand-off.

The contract:

```python
class ThreadBridge(Generic[T]):
    async def put(self, item: T) -> None: ...
    def put_nowait(self, item: T) -> bool: ...
    async def get(self) -> T | None: ...           # None = closed
    def __aiter__(self) -> AsyncIterator[T]: ...
    def close(self) -> None: ...

    @property
    def metrics(self) -> ThreadBridgeMetrics: ...
```

Two bridges live in the runtime:

- **Worker outbound bridge.** One per worker, conductor-side consumer. `BridgePolicy.BLOCK` — sustained block surfaces as `blocked_since_ms` and the saturation deadline picks it up.
- **UIBridge.** One process-wide, UI-side consumer. `BridgePolicy.DROP_OLDEST` — the UI loop cannot block on subscriber backpressure.

There is **no third bridge**. Commands cross thread seams via `asyncio.run_coroutine_threadsafe(...)` returning a `concurrent.futures.Future`; the caller bridges back with `asyncio.wrap_future(...)`. The conductor's call to `Worker.dispatch(...)` and the UI's call into `Conductor.dispatch(...)` both ride that pattern.

**Why not `janus` / `culsans` / `anyio.create_memory_object_stream`:** every cross-thread channel in this design is async↔async across two *different* asyncio loops. `janus` is bound to one loop. `anyio.create_memory_object_stream` is per-loop. `culsans` has the right shape but is alpha-quality. The stdlib hand-roll is ~150 LOC, free-threaded ready, and adds no dependency. See [`runtime-architecture.md` §7](runtime-architecture.md#7-threadbridge).

---

## Why not `asyncio.run_in_executor`

The natural reflex when "this call blocks" is to push it to a thread-pool executor. CAPA does not, for two specific reasons:

1. **Workers own persistent state.** An adapter (`watlowlib.WatlowManager`, `nidaqlib` task handle, FLIR Spinnaker camera handle) is not stateless — it holds transport buffers, transaction sequence numbers, and SDK callbacks. It cannot move between threads without violating per-port locks and SDK affinity rules. A pool would either need to pin each adapter to a specific worker (which is what we already have) or accept hardware-state corruption.
2. **Per-resource means O(N_resources) threads, not O(N_emissions/sec).** At full real config that's <10 threads. An executor pool would buy nothing — there is no aggregate-throughput problem to solve.

`anyio.to_thread.run_sync` *is* used inside adapters to wrap genuinely blocking calls (`serial.read`, `av.open`, ffmpeg encoder steps). That offload still runs on the worker's loop's executor — the adapter doesn't leave its worker.

---

## GIL and free-threaded readiness

Under the GIL today:

- Every worker's I/O calls (`serial.read`, `nidaqmx.read`, `av.open`) release the GIL during their syscall, so wall-clock parallelism for I/O is real.
- Python-level work (Pydantic validation, `ChannelSample` construction, numpy alloc) is serialized through the GIL.

At target load (≤200 Hz aggregate samples + 60 fps video decode), ~200 Python wake-ups/sec × <1 ms each ≈ <20% GIL utilization. Headroom is comfortable.

Under PEP 703 free-threaded Python (3.13t+), no code change is required. Threads gain true parallelism on Python bytecode. The design is forward-compatible by virtue of being thread-based.

---

## Observability — every loop has a heartbeat

Every worker, the conductor, and the UI loop run a [`heartbeat_task`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/heartbeat.py) that wakes at 20 Hz and observes its own scheduling lag. The metric is `loop_lag.p99`; the status bar's [loop-lag pill](../user-guide/status-bar-guide.md) surfaces the highest p99 across loops. Yellow > 50 ms, red > 200 ms.

When debugging a stutter the first question is "which loop?" — read `loop_lag` per thread before guessing. A red conductor loop with green workers points at the procedure or a saturated databus subscriber; a red worker loop points at an adapter not wrapping a blocking call in `anyio.to_thread.run_sync`.

Per-thread CPU is polled at 1 Hz from inside the conductor via `psutil.Process().threads()` and lands in `diagnostics.runtime` in the bundle manifest.

---

## Anti-patterns

- **Calling `worker.dispatch(...)` directly from `ui-main`.** Returns a `concurrent.futures.Future`. Awaiting it from a qasync coroutine without `asyncio.wrap_future(...)` either blocks the UI loop or raises. Route through [`ManualClient`](../glossary.md#worker) instead.
- **Constructing an `asyncio.Event` on one thread and `set()`ting it from another.** The set will not wake the waiter reliably across loops. Use `loop.call_soon_threadsafe(event.set)`.
- **Subscribing to `UIDataBus` from a non-UI thread.** The subscription's queue is loop-affine; the publisher fails on `DataBusLoopError`. UI widgets subscribe; nothing else does.
- **Sleeping inside an adapter `command()` without `anyio.to_thread.run_sync`.** A blocking `time.sleep(2.0)` freezes the entire worker for 2 s, including its heartbeat and every other adapter sharing the same resource.
- **Force-cancelling a worker future to "speed up shutdown".** The cancel does not propagate into the shielded `adapter.command(...)`. The transaction completes anyway; the result is dropped. Honour the disarm grace ([`runtime-architecture.md` §6.2](runtime-architecture.md#62-run-shutdown-protocol)) instead.

---

## Where to read more

- Full topology and lifetimes: [`runtime-architecture.md` §2–§3](runtime-architecture.md#2-topology).
- One sample's complete journey across threads: [`data-flow.md`](data-flow.md).
- The UI side of the seam: [`ui-runtime-boundary.md`](ui-runtime-boundary.md).
- The writer thread internals: [`bundle-write-path.md`](bundle-write-path.md).
- The cancellation shield's failure mode in detail: [`runtime-architecture.md` §5](runtime-architecture.md#5-the-cancellation-shield).
