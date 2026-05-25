# CAPA runtime architecture

**Audience:** contributors touching `src/capa/runtime/`, adapter authors, procedure / plugin authors.
**Scope:** how the per-resource-worker runtime is shaped today — components, data flow, lifetimes, invariants. Reference doc, not a plan.
**Companion docs:** [`capa-plan.md`](capa-plan.md) (product surface).

---

## 1. The model in one page

CAPA runs every hardware resource (one serial port, one DAQmx chassis, one camera handle) on **its own thread, its own asyncio loop**. Adapters keep their familiar `open/close/start/stop/stream/command/snapshot` surface; they are hosted inside per-resource [`Worker`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/worker.py) instances.

The system has three nested lifetimes:

1. **Config lifetime — `WorkerPool`.** Opened when a config loads. Builds one `Worker` per resource, opens every adapter, and lives across many runs. Closed only on config-reload or app-quit. Operators can issue manual commands while no run is active; the Sartorius cold-open race and other open-once costs are paid once per config load.
2. **Run lifetime — `Conductor`.** Constructed when a run starts. Owns run-only state (clock, writer, procedure, drain tasks, heartbeat, saturation monitor). Arms the existing workers, drives sampling, disarms on stop. Workers stay open for the next run.
3. **Command lifetime — single dispatch.** A UI command crosses the thread seam once or twice and resolves.

The **UI thread** owns Qt only and crosses to the Conductor (during a run) or to the WorkerPool (between runs) through thread-safe channels.

There is no parallel `engine_v2` / `engine_v1` path. The old single-loop `Engine`, `DeviceRegistry`, and standalone `camera_task` are gone.

---

## 2. Topology

```
┌────────────────────────────────────────────────────────────────────┐
│ UI thread (qasync: Qt + asyncio merged)                            │
│                                                                    │
│  Widgets │ Plots │ Manual cards │ Numerics │ Status bar │ UIDataBus│
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ ManualClient — sync facade; lives on UI loop                 │  │
│  │   Routes to Conductor.dispatch (run armed) or                │  │
│  │   WorkerPool.dispatch (idle). Cards stay mode-agnostic.      │  │
│  └──────────────────────────────────────────────────────────────┘  │
└────┬────────────────────────────────────────────────────┬──────────┘
     │ commands during run                                 │ commands when idle
     │ (run_coroutine_threadsafe → conductor)              │ (direct to pool)
     │                                                     │
     ▼                                                     │
┌────────────────────────────────────────────────────┐    │
│ Conductor thread (per-run; pure asyncio, no Qt)    │    │
│                                                    │    │
│  ┌──────────────┐  ┌─────────────┐  ┌──────────┐   │    │
│  │ ProcedureRun │  │ Drain tasks │  │ Heartbeat│   │    │
│  └──────┬───────┘  │ (one per    │  │ Sat-dead-│   │    │
│         │          │  worker)    │  │ line     │   │    │
│  ┌──────┴───────┐  └──────┬──────┘  └──────────┘   │    │
│  │ConductorData │         │                        │    │
│  │Bus (authori- │         │                        │    │
│  │tative)       │         │                        │    │
│  └──────────────┘         │                        │    │
│                           ▼                        │    │
│  per drain: writer.submit + conductor_databus.put  │    │
│             + ui_bridge.put                        │    │
└─────┬───────────────────────────────────────┬──────┘    │
      │                                       │           │
      │ ThreadBridge per worker (BLOCK + sat- │           │
      │ uration deadline at Conductor side)   │           │
      │                                       │           │
      ▼                                       ▼           ▼
┌─────────────────────────────────────────────────────────────────────┐
│ WorkerPool (config-lived; survives runs)                            │
│                                                                     │
│ ┌───────┐ ┌───────┐ ┌───────┐ ┌───────┐ ┌───────┐ ┌───────┐         │
│ │Worker │ │Worker │ │Worker │ │Worker │ │Worker │ │Worker │         │
│ │COM6   │ │COM7   │ │COM4   │ │DAQmx  │ │webcam │ │FLIR-IR│         │
│ │Watlow │ │Alicat │ │Sartor.│ │cdaq1  │ │PyAV   │ │SDK    │         │
│ └───────┘ └───────┘ └───────┘ └───────┘ └───────┘ └───────┘         │
│ each: own thread + asyncio loop + adapter instances + state machine │
│                                                                     │
│ States: IDLE ↔ ARMED ↔ SAMPLING ↔ DRAINING ↔ IDLE  (§4.1)           │
└─────────────────────────────────────────────────────────────────────┘
                                                              ┌───┐
                                                              │WT │ (writer
                                                              └───┘  thread,
                                                                     constructed
                                                                     per run by
                                                                     Conductor)
```

**Thread inventory at full real config (6 devices + 2 cameras):**

| Thread | Loop | Lifetime | Owns |
|---|---|---|---|
| `ui-main` | qasync | process | Qt widgets, plot timers, UIDataBus subscribers, manual cards |
| `conductor` | asyncio | per-run | ProcedureRunner, drain tasks, heartbeat, saturation monitor, ingest, RunClock |
| `worker-heater` | asyncio | config | WatlowAdapter + serial transport |
| `worker-purge_mfc` | asyncio | config | AlicatAdapter + serial transport |
| `worker-balance` | asyncio | config | SartoriusAdapter + serial transport |
| `worker-cdaq1` | asyncio | config | NIDAQAdapter + DAQmx callbacks |
| `worker-visible_cam0` | asyncio | config | WebcamAdapter + PyAV input |
| `worker-ir_cam0` | asyncio | config | FlirIrAdapter + SDK callbacks |
| `writer` | (sync) | per-run | Bundle sinks, Arrow IPC streams, SQLite |

9 threads during a run (UI + conductor + 6 workers + writer). Each worker holds <15 MB resident.

---

## 3. Three lifecycles

```
Config lifetime ────────────────────────────────────────────────────────┐
                                                                        │
   pool.open()                          pool.close()                    │
   ──────────►  WorkerPool alive   ───────────►   torn down             │
                                                                        │
       Run lifetime ───────┐         Run lifetime ───────┐              │
         conductor.start() │           conductor.start() │              │
         ───►  arm  ───►   │           ───►  arm  ───►   │              │
              sample       │                sample       │              │
              disarm       │                disarm       │              │
         ◄─── finalize ────┘           ◄─── finalize ────┘              │
                                                                        │
       (idle between runs — workers in IDLE; manual commands accepted)  │
                                                                        │
            Command lifetime — single dispatch round-trip               │
            ManualClient.dispatch(...) → returns                        │
                                                                        │
────────────────────────────────────────────────────────────────────────┘
```

**Config lifetime** is owned by the app/UI. Loading a config triggers [`WorkerPool.open()`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/pool.py); reloading or quitting triggers `WorkerPool.close()`. Once `open()` returns, every adapter is open and every worker is IDLE.

**Run lifetime** is owned by the [`Conductor`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/conductor.py). `Conductor.start()` constructs the run context (clock, writer, run_id), calls `pool.arm_all(run_context)` to install per-run state on every worker, runs preflight against live data, then calls `pool.begin_sampling_all()` and starts drain tasks and the procedure. `Conductor.stop()` reverses this and finalizes the bundle.

**Command lifetime** is a single sync-facade dispatch through [`ManualClient`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/dispatch.py), which picks `ConductorDispatcher` when a run is armed (records into the bundle, gates by conductor state) or `PoolDispatcher` when idle (no recording, no gating).

---

## 4. The Worker

[`Worker`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/worker.py) owns one hardware resource and the 1..N adapters that share its `resource_id`. The constructor does no I/O; `start()` brings the runner up and opens every adapter inside the worker loop.

### 4.1 State machine

```
                 ┌──────────────────────────────────────────────┐
   pool.open():  │                                              │
   open() ────►  │  IDLE                                         │  pool.close():
                 │   adapters open, no streams running           │  close() ───►
                 │   dispatch() permitted (manual)               │  (torn down)
                 │   no clock, no writer ref, no run_id          │
                 └────────────┬─────────────────────────────────┘
                              │ Conductor.start():
                              │ pool.arm_all(run_context)
                              ▼
                 ┌──────────────────────────────────────────────┐
                 │  ARMED                                        │
                 │   per-run context installed:                  │
                 │     clock, writer ref, run_id, bundle ref     │
                 │   streams NOT yet running                     │
                 │   dispatch() permitted (records into bundle)  │
                 └────────────┬─────────────────────────────────┘
                              │ Conductor.start():
                              │ pool.begin_sampling_all()
                              ▼
                 ┌──────────────────────────────────────────────┐
                 │  SAMPLING                                     │
                 │   stream tasks running                        │
                 │   emissions flowing to outbound bridge        │
                 │   dispatch() permitted                        │
                 └────────────┬─────────────────────────────────┘
                              │ Conductor.stop():
                              │ pool.disarm_all(grace_s)
                              ▼
                 ┌──────────────────────────────────────────────┐
                 │  DRAINING                                     │
                 │   adapter.stop() in flight                    │
                 │   outbound bridge drains then closes          │
                 │   dispatch() REFUSED                          │
                 │   bounded by grace_s; hard-stop if exceeded   │
                 └────────────┬─────────────────────────────────┘
                              │ drain complete OR grace expired
                              ▼
                         back to IDLE
                         (per-run context cleared)
```

Legal edges live in [`lifecycle.LEGAL_WORKER_EDGES`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/lifecycle.py). Illegal transitions raise `WorkerStateError`.

### 4.2 Invariants

- A Worker owns exactly one `resource_id` and is the only thread that touches its adapters.
- Adapter `open()` runs once on the IDLE-entry edge (pool open). Adapter `close()` runs once on the close edge (pool close). Streams come up and down per run.
- `dispatch()` is permitted in IDLE, ARMED, SAMPLING; refused in DRAINING and when the pool is closed.
- All emissions are immutable. Once an adapter returns one, no thread mutates it.

### 4.3 Sync facade

The Worker exposes a thread-safe sync API returning `concurrent.futures.Future`:

```python
class Worker:
    def start(self) -> Future[None]: ...
    def close(self, *, grace_s: float = 5.0) -> Future[None]: ...
    def arm(self, run_context: RunContext) -> Future[None]: ...
    def begin_sampling(self) -> Future[ThreadBridge[DeviceEmission]]: ...
    def disarm(self, *, grace_s: float = 5.0) -> Future[DisarmResult]: ...
    def dispatch(self, adapter_name: str, cmd: DeviceCommand) -> Future[CommandResult]: ...
    def snapshot(self, adapter_name: str) -> Future[DeviceSnapshot]: ...
```

Every caller (Conductor, PoolDispatcher, UI cards) is on a different loop than the worker. Async callers `asyncio.wrap_future` it; sync CLI callers `.result()` it. This is the standard `loop.call_soon_threadsafe` bridge.

### 4.4 Why dedicated thread per resource (not a pool)

- Each Worker owns persistent state (adapter handles, transport buffers, watlowlib client). It cannot move between threads.
- Per-resource means O(N_resources) threads, not O(N_emissions/sec). N_resources < 20 at realistic configs.
- A pool buys nothing — it would add scheduler-affinity headaches and pickling for off-thread coroutines.

---

## 5. The cancellation shield

**This is the single most load-bearing rule in the runtime.**

> **In-flight hardware transactions are shielded against task cancellation.** When a caller cancels a pending `dispatch` future, the worker's `adapter.command()` call **continues to completion** inside `asyncio.shield(...)`. The caller's future is cancelled; the hardware transaction is not.

Why: a canceller mid-transaction can leave stale bytes in the serial RX buffer. The next request reads those bytes as its own reply. This is exactly what produced `unexpected reply payload for write: ReadResponse` on COM6 — event-loop starvation caused a read timeout, cancellation propagated through the adapter, but the device's response arrived a few ms later and sat in the buffer until the next `set_setpoint`'s read picked it up.

What "cancel" means under this rule:

- **Caller-side cancellation is observable but not enforceable.** The caller's `wrap_future` cancels the underlying `concurrent.futures.Future`, which raises `CancelledError` in the caller. The worker's shielded coroutine continues; its result is dropped on completion.
- **For long-lived calls (rare for commands, common for `stream()`):** commands are short (10–50 ms); cancellation is rarely meaningful. Streams are not driven through `dispatch()` — they live in `_stream_task` and exit only when the worker transitions out of SAMPLING.
- **UI surface:** manual cards show "cancelling…" and surface the eventual result when it lands.
- **Adapter-level timeouts bound the worst case.** The shield only protects against task-cancellation, not against an unbounded wait.

Every adapter has a corresponding `test_<adapter>_dispatch_cancellation_does_not_corrupt_next_call` test.

---

## 6. The Conductor

[`Conductor`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/conductor.py) is the per-run coordinator. It lives on its own thread on its own asyncio loop, separate from the UI and from the pool's worker threads. A hung adapter (or a stalled writer fsync) cannot freeze the UI: every cross-thread hand-off is bounded by a `ThreadBridge` and every blocking deadline is observed by the `SaturationMonitor`.

### 6.1 Run startup sequence

```
1. User triggers run (UI button) OR headless CLI
2. UI/CLI thread:
     conductor = Conductor(config, pool, runs_root, plugins_lock, operator_id)
     handle = await conductor.start()
3. Conductor thread (on its own loop):
     a. Authorization.arm(operator_id, run_id)
     b. Build RunClock (single authority)
     c. Build WriterThread for this run, start it
     d. Build run_context = RunContext(clock, run_id, writer_ref, bundle_ref)
     e. await pool.arm_all(run_context)
        — every worker transitions IDLE → ARMED
     f. Spawn per-worker drain tasks  (NB: drains start BEFORE preflight)
        — bridges are open; ConductorDataBus is publishable;
          procedure dynamic-preflight will see live samples
     g. await pool.begin_sampling_all()
        — every worker transitions ARMED → SAMPLING
     h. Run profile preflight (executor _wait_for path) via ConductorDataBus
        — samples now flowing; dynamic preflight can wait for stability
     i. Spawn procedure, heartbeat, saturation-deadline monitor, ingest
     j. UI's start_run future resolves with RunStarted(run_id, bundle_path)
4. Emissions flow; procedure executes; UI reflects state
```

**Drain-before-preflight is load-bearing.** With workers in SAMPLING but drains not yet running, samples reach the outbound bridge but never reach the bus — preflight would hang on its own `_wait_for` timeout.

**Worker startup is parallel.** Bus-collision avoidance is handled by `resource_id` grouping, not by serializing across workers: two adapters that share a bus share a `resource_id` and therefore share one worker (and one loop), where their `open()` calls serialize through the adapter's own per-port lock. Two workers by definition own disjoint resources and cannot collide.

### 6.2 Run shutdown protocol

Threaded-async systems fail here if the protocol isn't explicit and bounded.

```
Trigger: procedure-complete | external_stop | unrecoverable error | saturation deadline

Graceful disarm
1. Conductor stops procedure (procedure task exits)
2. Conductor calls pool.disarm_all(grace_s=5.0)
3. For each worker (in parallel):
     a. Worker transitions SAMPLING → DRAINING
     b. adapter.stop() called in worker loop (adapter-specific cleanup;
        not guaranteed to drive a safe setpoint)
     c. adapter.stream() exits naturally
     d. Outbound bridge drained, then closed (sentinel)
4. Conductor's drain tasks see sentinel → exit
5. Conductor awaits all disarm futures with grace_s

Forced stop + leak detection (if grace expires for any worker)
1. For each worker still in DRAINING:
     a. Capture stack via sys._current_frames()[worker.thread.ident]
     b. Record "worker_hard_stop_attempt" event into bundle (with stack)
     c. Call worker.loop.call_soon_threadsafe(worker.loop.stop)
     d. thread.join(timeout=2.0)
     e. If join fails:
          - Record "worker_thread_leaked" event (with stack)
          - Run marked degraded
          - Thread persists as daemon; no further action
            (interrupting wedged native state could corrupt vendor SDKs)

Drain and exit
1. Writer thread inbox stops accepting (Conductor signals it)
2. Writer thread drains its queue
3. Writer thread finalizes bundle
4. Conductor's loop exits; thread joins
5. Pool's workers return to IDLE (those that disarmed cleanly); leaked threads remain
6. UI sees the Conductor's start_run future resolve with run summary
7. WorkerPool remains alive — next run can be started against it
```

**Force-cancel does not guarantee a safe heater state.** The hard path is observable and still tries to disarm workers, but a method's trailing `SafeShutdownStep` is not automatically jumped to after `external_stop`, and current state-bearing adapters may only stop streaming. Procedures that require safe setpoints must issue them explicitly before or during cleanup; hardware interlocks remain the final safety boundary.

**Forced stop is observable.** Any time the engine takes the hard path, the bundle records it with a stack trace. Hard-stop attempts are not silent, and leaked threads are not pretended-away.

**There is no guaranteed terminate.** Python provides no safe way to interrupt a thread wedged inside a blocking vendor SDK call. The protocol's job is to be observable, not omnipotent. For crash-prone SDKs and safety-critical devices, see §11 (`SubprocessWorker`).

### 6.3 Saturation deadline

The Conductor enforces an end-to-end durable-output deadline. Per-channel backpressure policies are necessary but insufficient: a writer/disk stall produces silent worker-side blocking that no per-device silence policy will catch until that policy exists.

[`SaturationMonitor`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/saturation.py) polls per-bridge `blocked_since_ms` and the writer-inbox last-accept timestamp on a `saturation_deadline_s / 10` cadence (default 10s deadline, 1s poll). Escalation:

1. Log the saturation cause and per-bridge / inbox metrics.
2. Write a `saturation_deadline` event into the bundle.
3. Mark the run outcome `crashed_but_sealed`.
4. Trigger normal graceful shutdown — workers `disarm()` still runs `adapter.stop()`, but explicit safe setpoints remain procedure- or adapter-specific.

---

## 7. ThreadBridge

[`ThreadBridge`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/bridge.py) is the cross-thread emission channel. Stdlib only — `asyncio.Queue` on the consumer side, `Semaphore` on the producer side, `loop.call_soon_threadsafe` for the hand-off.

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

**Policies** mirror `BoundedQueue`:

- `BLOCK` — producer awaits space (default for emission bridges).
- `DROP_OLDEST` — evict head, enqueue new.
- `DROP_NEWEST` — discard new.

**No `ABORT_RUN` on a ThreadBridge.** Aborts are a run-level concern; the Conductor's saturation monitor decides whether sustained saturation is fatal.

**Why not `janus` / `culsans` / `anyio.create_memory_object_stream`:** the two cross-thread channels in this design are both **async↔async across two different asyncio loops** (Worker → Conductor; Conductor → UI/qasync). `janus.async_q` is bound to one loop. `anyio.create_memory_object_stream` is per-loop. `culsans` has the right shape but is alpha-quality. The stdlib hand-roll is ~150 LOC, free-threaded ready, and adds no dependency.

**`call_soon_threadsafe` is the only thread-crossing primitive used**; CPython implements it via the loop's self-pipe, which wakes the consumer's `epoll_wait` / `WaitForMultipleObjects` immediately — there is no polling overhead.

`ThreadBridgeMetrics.blocked_since_ms` is the saturation-deadline signal: Conductor polls per-bridge `blocked_since_ms` and escalates if any worker has been blocked for `ConductorConfig.saturation_deadline_s`.

---

## 8. DataBus topology

[`DataBus`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/databus.py) is built on `BoundedQueue` ([`backpressure.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/backpressure.py)), which is explicitly loop-local. The procedure handler `_wait_for` subscribes to a DataBus on the Conductor loop. UI widgets subscribe on the qasync loop. These are different loops on different threads.

**Resolution: two loop-affine DataBus instances connected one-way through UIBridge.**

```
Conductor loop:                      UI/qasync loop:
┌─────────────────────────┐          ┌─────────────────────────┐
│ ConductorDataBus (auth) │          │ UIDataBus (mirror)      │
│   ▲ publish (await)     │          │   ▲ publish_nowait      │
│   ├─ procedure subs     │          │   ├─ widget subs        │
│   │  (BLOCK / ABORT_RUN │          │   │  (DROP_OLDEST)      │
│   │   tolerated)        │          │   │                     │
│   └─ analyzer subs      │          │   └─ status bar subs    │
└─────────────────────────┘          └─────────────────────────┘
         ▲                                    ▲
         │ conductor drain publishes here     │ UIBridge drain
         │                                    │ re-publishes here
         │                                    │
  Worker outbound bridge ─┬──→ writer.submit (await)
                          ├──→ ConductorDataBus.publish (await)
                          └──→ UIBridge.put_nowait ─────────────┘
```

**Invariants:**

1. The UI side **never publishes back** into ConductorDataBus. UIDataBus is a one-way downstream mirror, fed exclusively by the UIBridge drain task.
2. The conductor drain `await`s `conductor_databus.publish(emission)`. This preserves existing DataBus backpressure (a safety-critical subscriber with BLOCK or ABORT_RUN policy still gets honored) and surfaces a stuck subscriber as drain-task blocking, which the saturation deadline catches.
3. The UI drain calls `ui_databus.publish_nowait(emission)`. Widget subscribers all use DROP_OLDEST. The UI loop cannot block on subscriber backpressure.
4. Headless runs use only ConductorDataBus; UIBridge is unwired.
5. DataBus publish is loop-affine. A runtime assertion in `publish` / `publish_nowait` verifies the call originates from the bus's owning loop.

---

## 9. Command flow

Two paths, depending on whether a run is armed:

### 9.1 Path A: run armed (Conductor exists)

```
UI: manual card "Set heater 600°C"
   │  (UI loop)
   ▼
ManualClient.dispatch("heater", cmd):
   if conductor is active:
       return conductor_dispatcher.dispatch(...)
   else:
       return pool_dispatcher.dispatch(...)
   │
   ▼  (Path A)
ConductorDispatcher.dispatch("heater", cmd)
   │  (asyncio.run_coroutine_threadsafe → conductor loop)
   ▼
Conductor._dispatch(name, cmd):
   if not self._state.permits_manual():
       reject Future with ConductorStateError
       return
   record CommandIssued event into bundle
   worker = self._pool.worker_for(name)
   return await asyncio.wrap_future(worker.dispatch(name, cmd))
   │  (asyncio.run_coroutine_threadsafe → worker loop)
   ▼
Worker.dispatch (on worker loop):
   adapter = self._adapters[name]
   return await asyncio.shield(adapter.command(cmd))   ← cancellation shield
   │
   ▼  (bubbles back across two thread seams)
UI awaits result, updates label
```

### 9.2 Path B: idle (no Conductor)

```
UI: manual card "Read setpoint" (between runs)
   │  (UI loop)
   ▼
ManualClient.dispatch("heater", cmd) → PoolDispatcher.dispatch
   │  (asyncio.run_coroutine_threadsafe → worker loop directly)
   ▼
Worker.dispatch:
   adapter = self._adapters[name]
   return await asyncio.shield(adapter.command(cmd))
   │
   ▼
UI awaits result
```

**Why two paths?** Path A records the command-issue event into the bundle and enforces conductor-state gating (commands refused during DRAINING / FINALIZING). Path B has neither — there is no bundle to record into and no engine state to gate against. Manual fiddling between runs does not leak into the next run's bundle.

The double-hop in Path A adds ~100–300 µs vs Path B's single hop. On a command path that already costs 10–50 ms of serial round-trip, this is invisible.

---

## 10. Adapter contract

Adapters are unchanged except for one property:

```python
@runtime_checkable
class DeviceAdapter(Protocol):
    name: str
    capabilities: frozenset[Capability]
    resource_id: str                      # the only new field

    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def stream(self) -> AsyncIterator[DeviceEmission]: ...
    async def command(self, cmd: DeviceCommand) -> CommandResult: ...
    async def snapshot(self) -> DeviceSnapshot: ...
```

### 10.1 `resource_id`

Identifies the hardware contention domain — one serial port, one DAQmx chassis, one camera handle:

```python
class WatlowAdapter:
    @property
    def resource_id(self) -> str:
        return f"serial:{self.params.port}"

class NIDAQAdapter:
    @property
    def resource_id(self) -> str:
        return f"daqmx:chassis:{_chassis_of(self.params.channels)}"

class WebcamAdapter:
    @property
    def resource_id(self) -> str:
        return f"webcam:{self.params.device_id}"
```

Sims use `f"sim:{self.name}"`.

When two adapters share a `resource_id`, they share a Worker and their I/O serializes through the adapter-level lock (e.g. `watlowlib.WatlowManager`'s per-port lock).

### 10.2 Adapter author responsibilities

- Implement the contract above; the runtime takes care of everything else.
- Wrap blocking calls in `anyio.to_thread.run_sync`. Adapters run on a worker's loop; blocking it freezes that worker and its heartbeat.
- Bound every blocking call with a timeout. The cancellation shield protects against task-cancel, not unbounded wait.
- Stamp emissions with the adapter's own timestamps. The Worker only *adds* `t_bridge_put_ns` for observability; it never overwrites adapter timestamps.

### 10.3 Resource validation

`build_workers` ([`build.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/build.py)) validates synchronously before any worker thread spawns:

1. **Serial port uniqueness.** Same port → same `resource_id`.
2. **DAQmx physical-channel uniqueness.** No two adapters claim the same channel. The default `resource_id` is keyed on the physical chassis (e.g. `daqmx:chassis:cDAQ1`) because tasks on the same chassis share timing engines — DAQmx will throw `-50103 resource reserved` if they're armed without coordination.
3. **Webcam handle uniqueness.** Same `webcam:N` may not be claimed by two adapters.
4. **Global SDK singletons documented.** NI-DAQmx system handle, PyAV format registration, FLIR Spinnaker `System.GetInstance()` are process-singletons regardless of `resource_id`. Materialization logs these as `devices.materialize.global_sdk_constraints` in the structlog stream; resource grouping does **not** isolate them. SubprocessWorker (§11) is the only structural isolation.

Validation failures raise `ResourceConflict`; pool open aborts before any hardware is touched.

---

## 11. Procedure CPU offload

[`ProcedureRunner`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/procedure.py) runs on the conductor loop, sharing it with per-worker drain tasks, the heartbeat, and the saturation monitor. Default step kinds (Hold, Ramp, Wait, Acquire, SafeShutdown, Prompt — see [`executor.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/executor.py)) are I/O-bound and behave.

The risk surface is `CustomStep`: plugin authors can register arbitrary handlers via the `capa.procedures` entry-point. A handler that performs heavy CPU work inline will stall every drain task on the conductor.

**Contract for procedure authors:** custom handlers MUST wrap any non-trivial CPU work in `anyio.to_thread.run_sync`, the same way the webcam adapter wraps ffmpeg / libjpeg calls in [`adapter.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/camera/webcam/adapter.py).

**Why not a third thread for the procedure?** It would mean another cross-thread channel for every `_command_setpoint` call and a thread-safe DataBus on the procedure side. The default handlers don't need it, and disciplined custom handlers are sufficient. If real-world plugins start consistently violating the contract, revisit with a dedicated `procedure` thread peer to the Conductor.

---

## 12. SubprocessWorker — escape hatch

**Not built.** The `Worker` interface is the abstraction boundary; a future `SubprocessWorker` satisfies it with no other code changes. Flagged for two specific cases:

1. **Crash-prone SDKs.** NI-DAQmx runtime faults and FLIR Spinnaker assertion failures can crash the entire Python process today. With a SubprocessWorker, the crash is contained: the child segfaults; the parent observes process exit and either restarts the child or marks the device failed and continues the run.
2. **Safety-critical devices.** The heater is the canonical case. `loop.stop()` + `thread.join` (§6.2 forced stop) cannot terminate a thread wedged inside a blocking vendor call. SIGKILL on a child process can.

**Costs:** per-emission IPC serialization (~5–10× latency vs in-thread), more memory per worker, more complex shutdown.

**Trigger to build:** the first time we either (a) ship a config with a known-crashy SDK or (b) need a guaranteed-terminable heater path.

---

## 13. Backpressure & overflow

| Channel | Capacity | Policy | Deadline | Rationale |
|---|---|---|---|---|
| Worker outbound bridge | `max(64, ceil(8 * Σ rate_hz))` | `BLOCK` | `saturation_deadline_s` (default 10s) | Producer (adapter) throttles if Conductor lags; sustained block triggers saturation escalation. Per-worker capacity derived from the sum of each hosted adapter's `expected_emission_rate_hz`; adapters that return `None` contribute nothing. Factors live as code constants in [`build.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/build.py). |
| Worker command inbox | `8` | `BLOCK` | — | Commands are rare; bridge full means worker is stuck. Conductor sees a slow future. |
| Conductor → UIBridge | `runtime.ui_bridge_capacity` (default `4096`) | `DROP_OLDEST` | — | UI is best-effort viewport. |
| Conductor → ConductorDataBus | (per-subscription) | (per-subscription) | implicit via saturation | Procedure subscribers may use BLOCK; sustained block surfaces as drain blocking → saturation. |
| Conductor → WriterThread inbox | (existing) | (existing) | `saturation_deadline_s` | Inbox stall is the canonical saturation trigger. |
| UIDataBus subscriptions | (existing) | `DROP_OLDEST` | — | Widgets cannot block UI loop. |

**No `ABORT_RUN` on the worker-outbound bridge.** A stuck worker doesn't abort the run via the bridge. Sustained bridge blockage is handled by the conductor's saturation-deadline path. Per-device silence escalation is not implemented today; `on_failure` is retained as resolved metadata for that future policy.

---

## 14. Observability

Every loop and every bridge is measured. The bundle's manifest gains a `queue_health` block with loop, bridge, worker, runtime, and writer-inbox diagnostics.

**Per-loop heartbeat** (every worker + conductor + UI), via [`heartbeat.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/heartbeat.py):

```python
async def _heartbeat_task(self) -> None:
    target = self._loop.time()
    while not self._stop_event.is_set():
        target += 0.05  # 20 Hz sample
        before = self._loop.time()
        await asyncio.sleep(max(0, target - before))
        actual = self._loop.time()
        lag_ms = (actual - target) * 1000
        self._metrics.loop_lag.observe(lag_ms)
```

`loop_lag.p99 > 50ms` is a smoke alarm. The UI status bar shows the highest p99 across loops with a small badge that turns yellow > 50 ms, red > 200 ms.

**Per-worker metrics** ([`metrics.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/metrics.py)):

```python
@dataclass
class WorkerMetrics:
    resource_id: str
    adapter_names: tuple[str, ...]
    on_failure: Mapping[str, FailurePolicy]
    state: WorkerState
    commands_total: int
    commands_inflight: int
    commands_failed: int
    samples_emitted: int
    polls_emitted: int
    tick_duration_ms: _PercentileRing
    poll_period_ms: _PercentileRing
    loop_lag: LoopLagMetric
```

`on_failure` is the resolved per-adapter failure policy from
[`ResolvedAdapter`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/resolved.py). It is retained on
`WorkerMetrics` as metadata; runtime enforcement for stream silence or
per-device fatal-error escalation is not implemented today.

**Archival snapshot:** before seal, the conductor hands `runtime_diagnostics()` to the run session, which folds it into `manifest.queue_health` alongside the writer and metrics-registry snapshots.

**Logging:** every worker thread sets a contextvar-bound `structlog` context at thread entry (`thread="worker"`, `resource_id`, `adapters`). Conductor and UI similarly bind. Every log line in a run identifies which thread emitted it.

---

## 15. Clock & timestamps

**Single RunClock authority per run.** The Conductor constructs one `RunClock` ([`clock.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/clock.py)) at run-arm and installs a reference into each Worker's `RunContext` via `arm()`. `RunClock.t_mono_ns()` calls `time.monotonic_ns()` which is process-global and thread-safe.

Between runs (workers in IDLE), no clock is installed. Manual commands don't need run-relative timestamps.

Wall clock (`datetime.now(UTC)`) is also thread-safe.

**Timestamp capture site:** unchanged — adapters stamp emissions where they do today. The Worker only *adds* `t_bridge_put_ns` for observability; it never overwrites the adapter's timestamps.

---

## 16. Error propagation

```
Inside worker:
  AdapterError raised in _stream_task →
    1. Record bundle event (via writer ref from run_context)
    2. Set worker._fatal_error
    3. Exit that adapter stream task

Conductor today:
  - Treats procedure, drain, preflight, pool, and conductor crashes as
    run-level crashes.
  - Treats saturation-deadline trips as `crashed_but_sealed`.
  - Does not yet enforce per-device `on_failure` from `worker._fatal_error`;
    that policy is advisory metadata until watchdog work is built.

Crashes (uncaught BaseException in worker thread):
  Worker._thread_main has top-level try/except BaseException
  Bundle records "worker_thread_crashed" with full traceback
  Conductor force-aborts the run
```

**Exception identity is preserved across threads.** `concurrent.futures.Future.set_exception(exc)` carries the exception by reference; `asyncio.wrap_future` re-raises the original.

---

## 17. Async style and event-loop implementation

**Inside loops: anyio.** All async code in this codebase uses anyio APIs — task groups (`anyio.create_task_group`), primitives (`anyio.Lock` / `Event` / `Semaphore`), offload (`anyio.to_thread.run_sync`) — with `backend="asyncio"`.

**At thread seams: raw asyncio.** The `ThreadBridge` (`asyncio.Queue` + `Semaphore` + `loop.call_soon_threadsafe`), command dispatch (`asyncio.run_coroutine_threadsafe`), and future bridging (`asyncio.wrap_future`) sit *below* anyio's abstraction layer.

**Backend: asyncio everywhere, by necessity.** A trio backend on any thread would require routing all cross-thread traffic through anyio portals. qasync is asyncio-only on the UI thread, and the existing cross-thread primitives are loop-affine to asyncio, so uniform asyncio everywhere is the simplest correct choice.

**Loop implementation is per-thread and swappable.** Each non-UI thread constructs its loop via `asyncio.new_event_loop()`. The Conductor and Workers can transparently swap in `winloop.new_event_loop()` (Windows) or `uvloop.new_event_loop()` (Linux/macOS) — both are drop-in `AbstractEventLoop` implementations. No bridge, no command-path, no shutdown protocol changes.

---

## 18. GIL & free-threaded readiness

Under the GIL:

- All worker I/O calls (`serial.read`, `nidaqmx.read`, `av.open`) release the GIL during their syscall. Wall-clock parallelism for I/O is real.
- Python-level work (Pydantic validation, Sample construction, np.array allocation) is serialized.

At target load (≤200 Hz aggregate samples + 60 fps video decode), ~200 Python wake-ups/sec × <1 ms each = <20% GIL utilization. Comfortable.

Under PEP 703 free-threaded Python (3.13t+), the code does not change. Threads gain true parallelism on Python bytecode. The design is forward-compatible by virtue of being thread-based; no migration needed.

---

## 19. Config schema

### 19.1 Per-runtime tunables

```toml
[runtime]
shutdown_grace_s = 5.0            # per-worker grace before hard-stop
loop_lag_warn_ms = 50.0           # logged when exceeded
ui_bridge_capacity = 4096         # Conductor → UI; DROP_OLDEST when full
```

All have sensible defaults. The corresponding schema type is
[`RuntimeConfig`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/config.py).

**Not in `[runtime]`** (and intentionally so):

- `bridge_capacity_factor` and `bridge_min_capacity` — internal
  constants in [`build.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/build.py)
  (`_BRIDGE_CAPACITY_FACTOR = 8.0`, `_BRIDGE_MIN_CAPACITY = 64`). The
  per-worker outbound bridge capacity is derived from the sum of each
  adapter's `expected_emission_rate_hz`: `max(64, ceil(8 * total_rate))`.
  An adapter that declines to declare a rate contributes nothing.
- `saturation_deadline_s` and `saturation_poll_period_s` — live on
  `ConductorConfig` (see [§6.3](#63-saturation-deadline)) but are
  internal timing knobs. The Python `run_headless(...)` helper accepts a
  diagnostic override; the `capa run` CLI does not expose a flag today.
- The five adapter-grace timers (`adapter_start_grace_s`,
  `adapter_stop_grace_s`, `adapter_close_grace_s`, `stream_cancel_grace_s`,
  `runner_stop_grace_s`) — live on
  [`WorkerShutdownConfig`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/shutdown.py) as code-level
  per-stage deadlines.

Promote a constant to `RuntimeConfig` only when an operator actually
needs to change it for a real experiment.

### 19.2 `on_failure` per device

```toml
[[devices]]
name = "heater"
adapter = "capa.devices.watlow"
on_failure = "abort"   # | "warn"
```

Resolved and recorded in
[`WorkerMetrics.on_failure`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/metrics.py) at
worker construction. Enforcement is not wired today, so `on_failure` is
advisory metadata. Default: `"abort"`.

The camera-spec field of the same name
([`CameraSpec.on_failure`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/camera/base.py)) is a
*separate* policy: it layers on the camera recording path, not the
runtime failure-policy metadata. The two enums are deliberately kept
distinct until a real case for unifying them appears.

### 19.3 `resource_id` per device

Each `DeviceConfig` may declare `resource_id` explicitly; otherwise the
runtime reads the adapter's own
[`DeviceAdapter.resource_id`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/adapter.py) (the
historical default). When two adapters share the same `resource_id` the
runtime groups them into one Worker so their I/O serialises through the
resource's lock.

```toml
[[devices]]
name = "heater"
adapter = "capa.devices.watlow"
# resource_id defaults to "serial:COM6" (from params.port)
[devices.params]
port = "COM6"

[[devices]]
name = "heater_secondary"
adapter = "capa.devices.watlow"
resource_id = "serial:COM6"   # explicit — share bus with heater
[devices.params]
port = "COM6"
address = 2
```

---

## 20. Module layout

```
src/capa/runtime/
├── __init__.py           # public exports
├── bridge.py             # ThreadBridge + ThreadBridgeMetrics
├── build.py              # build_workers
├── bundle_ref.py         # bundle reference plumbing
├── camera_adapter.py     # camera → DeviceAdapter wrapper
├── conductor.py          # Conductor
├── dispatch.py           # AdapterDispatcher, PoolDispatcher, ConductorDispatcher, ManualClient
├── emissions.py          # WorkerEmission type
├── errors.py             # PoolStateError, WorkerStateError, ResourceConflict, …
├── headless.py           # headless entrypoint glue
├── heartbeat.py          # loop-lag heartbeat task
├── lifecycle.py          # PoolState, WorkerState, legal-edge tables
├── metrics.py            # WorkerMetrics, DisarmResult
├── pool.py               # WorkerPool
├── preview.py            # PreviewFrame type for camera workers
├── procedure.py          # ProcedureRunner
├── runcontext.py         # RunContext, WriterRef, BundleRef
├── runner.py             # ThreadedRunner, InlineRunner (test mode)
├── saturation.py         # SaturationMonitor
├── session.py            # RealRunSession, run id minting
├── signals.py            # SIGINT install
├── state.py              # ConductorState + edges
├── worker.py             # Worker (with state machine)
└── writer_ref.py         # WriterThreadRef
```

Tests:

```
tests/unit/runtime/            — unit tests, mostly inline-runner
tests/integration/runtime/     — Worker / Pool / Conductor against sim adapters
tests/hardware/                — real-rig smoke tests (manual / off-by-default)
```

---

## 21. Topology invariants (enforced by tests)

1. **One resource per worker, one worker per resource.** A worker owns its `resource_id` exclusively.
2. **No two threads touch one adapter.** Once an adapter is constructed inside a worker, only that worker's loop calls into it.
3. **No raw `asyncio.Queue` crosses a thread boundary.** Cross-thread channels are always `ThreadBridge`.
4. **No `await` on a future from another loop without going through `asyncio.wrap_future`.**
5. **No `loop.run_until_complete` after startup.** The conductor and workers use `run_forever` with explicit stop signals.
6. **All `asyncio.Event` / `asyncio.Semaphore` / `asyncio.Queue` instances are constructed inside the loop that owns them.** Cross-thread signalling goes through `loop.call_soon_threadsafe(event.set)`.
7. **DataBus publish is loop-affine.** Asserted at runtime.
8. **In-flight `adapter.command()` calls are shielded against task cancellation** (§5).
9. **Worker state transitions are atomic per worker and verified.** Illegal transitions raise.

---

## 22. Glossary

- **Adapter** — A `DeviceAdapter` implementation. Owns one device's lifecycle, streams emissions, accepts commands. ([`src/capa/devices/adapter.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/adapter.py))
- **ARMED** — Worker state: per-run context installed; streams not yet running; dispatch permitted.
- **Bridge** (`ThreadBridge`) — Thread-safe bounded channel between two asyncio loops. ([`bridge.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/bridge.py))
- **Conductor** — Per-run coordinator. Owns clock, writer, procedure, drain tasks, heartbeat, saturation monitor. ([`conductor.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/conductor.py))
- **ConductorDispatcher** — `CommandDispatcher` that routes through the conductor; records command-issue events; gated by conductor state.
- **ConductorDataBus** — Authoritative DataBus instance, lives on conductor loop. Procedure handlers subscribe here.
- **DataBus** — Pub/sub for emissions, asyncio-loop-affine, with per-subscription backpressure. ([`databus.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/databus.py))
- **DeviceEmission** — Union of `SourceRecord` / `ChannelSample` / `DeviceEvent` / `DeviceSnapshot` / `FrameReceipt`. The atomic unit of acquisition output.
- **Drain task** — Conductor-side coroutine that iterates one worker's outbound bridge and routes each emission to writer + ConductorDataBus + UIBridge.
- **DRAINING** — Worker state: `adapter.stop()` in flight; outbound bridge drains then closes; dispatch refused.
- **Hard-stop attempt** — Last-resort shutdown: `loop.stop()` + `thread.join(timeout)`. Works only for threads parked at awaitable points. Records `worker_hard_stop_attempt` and (on failure) `worker_thread_leaked` events.
- **IDLE** — Worker state: adapters open, no streams running, no run context. Manual dispatch permitted.
- **ManualClient** — Single sync facade for UI manual cards. Routes to ConductorDispatcher when a run is armed, to PoolDispatcher otherwise.
- **PoolDispatcher** — `CommandDispatcher` that routes through a `WorkerPool`. No bundle recording, no state gating. Used by ManualClient between runs.
- **Resource** — A hardware contention domain. One serial port, one DAQmx chassis, one camera handle. Identified by `resource_id`.
- **`resource_id`** — String key (`"serial:COM6"`, `"daqmx:chassis:cDAQ1"`, `"webcam:cam0"`) used to group adapters into workers.
- **RunClock** — Single `RunClock` instance per run; thread-safe via `time.monotonic_ns()`.
- **RunContext** — The per-run state installed into workers via `arm()`: clock, run_id, writer ref, bundle ref.
- **SAMPLING** — Worker state: streams running, emissions flowing, dispatch permitted.
- **Saturation deadline** — Conductor-owned end-to-end output deadline. If writer inbox stalls or any worker outbound bridge stays blocked for `saturation_deadline_s`, the run seals as `crashed_but_sealed`.
- **SubprocessWorker** — Future variant of `Worker` hosting the adapter in a child process for crash isolation and guaranteed terminate. Not built; interface designed to admit it.
- **UIBridge** — `ThreadBridge` carrying emissions from Conductor thread to UI thread.
- **UIDataBus** — Mirror DataBus instance on UI loop. Widgets subscribe here. One-way downstream of ConductorDataBus.
- **Worker** — Thread + asyncio loop owning one resource and its 1..N adapters. ([`worker.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/worker.py))
- **Worker loop** — The asyncio event loop running inside a worker thread.
- **WorkerPool** — Config-lifetime container for Workers. Lives across runs; opens hardware once per config. ([`pool.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/pool.py))

---

*End of document.*
