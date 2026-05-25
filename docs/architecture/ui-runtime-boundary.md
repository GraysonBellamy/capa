# UI / runtime boundary

**Audience:** anyone adding a widget, a manual card, a new dock, or wiring a new live signal into the UI.
**Scope:** the rules that keep Qt on the UI thread and the runtime everywhere else. Why the seam looks the way it does, what crosses it, and what does not.

This page sits below [`threading-model.md`](threading-model.md) — read that first if "the conductor lives on its own thread" is new information.

---

## The rule in one sentence

> The UI thread may *reference* runtime types, but it must only *call* into the runtime through [`ManualClient`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/dispatch.py) for commands, [`UIDataBus`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/databus.py) for live emissions, and the [`RunController`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/state.py) for lifecycle. Nothing else.

There is no `from capa.runtime.worker import Worker` in any file under `capa.ui.*`. There is no `await pool.dispatch(...)` from a Qt slot. There is no `worker.adapters["heater"]` from a widget. The runtime owns hardware; the UI owns pixels. The single seam through which they speak is the controller and its three exports.

---

## What lives on `ui-main`

The UI thread runs the qasync event loop — one asyncio loop merged with Qt's main loop. Everything Qt touches must run here:

- All `QWidget` subclasses (every tab, dock, plot, card).
- The status bar's badges and timers.
- `RunController` ([`state.py:RunController`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/state.py)) — owns the long-lived `WorkerPool`, the per-run `Conductor`, and the `ManualClient`. Lives here because it emits Qt signals.
- The `RingBufferRegistry` ([`ringbuffer.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/ringbuffer.py)) — the per-channel ring buffers the plot panes read from.
- The `UIDataBus` mirror — a `DataBus` instance whose subscriptions are loop-affine to the qasync loop. See [`runtime-architecture.md` §8](runtime-architecture.md#8-databus-topology).

`RunController` is itself a `QObject` ([`state.py:34`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/state.py)) so it can emit Qt signals to widgets, but its `_pool`, `_conductor`, and `_ui_bridge` fields hold runtime references that live across the seam. Reading those fields is fine; calling methods on them that are loop-affine to *another* loop is the boundary violation this page is about.

---

## ManualClient — the single command surface

UI manual cards never reach into a worker directly. They take a [`ManualClient`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/dispatch.py) reference at construction and call its single async surface:

```python
async def dispatch(self, device: str, cmd: DeviceCommand) -> CommandResult: ...
async def snapshot(self, device: str) -> DeviceEmission: ...
async def camera_metadata(self, device: str) -> WebcamMetadata | None: ...
async def device_readback(self, device: str) -> object: ...
```

The cards stay mode-agnostic — they never check "is a run armed?" because the client does:

- A run armed and `permits_dispatch()` (state is `PREPARING` or `RUNNING`) → routes to `Conductor.dispatch(...)`. Records a `CommandIssued` event into the bundle; gates on conductor state.
- Otherwise → routes to `WorkerPool.dispatch(...)`. No bundle event, no gating. The Watlow read between runs is a pool dispatch; the same widget click during a run is a conductor dispatch.

The conductor-vs-pool decision happens inside `ManualClient._active_conductor()` ([`dispatch.py:332`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/dispatch.py)). When the conductor is in `DRAINING` / `FINALIZING` / `SEALED` / `FAILED`, the client falls through to the pool: the pool is still alive, the previous run's finalize doesn't justify refusing a between-runs read.

**There is no sync `dispatch` surface.** Cards `await` the client from inside an `asyncio.ensure_future(...)` or `qasync.asyncSlot(...)` wrapper. The "sync facade" wording you may see in older comments refers to the *single* facade — there's one client, not two — not to a non-async surface.

---

## UIDataBus — live emissions for widgets

The conductor publishes every emission to two buses: `ConductorDataBus` (authoritative; procedure subscribers and analyzers read it) and `UIDataBus` (mirror; widgets read it). Widgets always subscribe to the mirror.

The flow is one-way:

```
Worker outbound bridge ──► Conductor drain task ──┬──► writer.submit
                                                   ├──► ConductorDataBus.publish    (await — honours BLOCK / ABORT_RUN)
                                                   └──► UIBridge.put_nowait  ──►  UI drain  ──►  UIDataBus.publish_nowait
```

See the full diagram at [`runtime-architecture.md` §8](runtime-architecture.md#8-databus-topology).

The widget side subscribes through one of `subscribe_channel(channel_name, ...)`, `subscribe_adapter(adapter_name, ...)`, or `subscribe_all(...)` ([`databus.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/databus.py)). Each subscription owns its own bounded queue and declares its own `BackpressurePolicy`.

**Widgets must use `BackpressurePolicy.DROP_OLDEST`.** This is an invariant — the UI loop cannot block on subscriber backpressure. If your widget needs *every* sample (e.g. analysis), do that off-loop or off-process, not in a Qt widget. The plot pane's [decimating ring buffer](../glossary.md#scalarsparquet) is the existing answer for "I want every sample but only repaint at 10 Hz".

The publisher (the UI-side drain) calls `publish_nowait` ([`conductor.py:_publish_ui`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/conductor.py)), so the publish itself never blocks. A misbehaving subscriber with the wrong policy still cannot block the conductor; the worst it can do is fill its own queue and drop the rest.

---

## The cold-start problem

A widget that appears mid-run needs *current* state, not just future emissions. Two surfaces solve this without breaking the boundary:

- **`ManualClient.snapshot(device)`** — one-shot read against the device. Routes through the conductor (when armed) or the pool (otherwise); the worker runs `adapter.snapshot()` on its own loop and returns the result.
- **`ManualClient.device_readback(device)`** — adapter-specific cached state (e.g. `WatlowStateSnapshot` carrying the last known PV / SP / output%). Pool-only — same call whether or not a run is armed.

For camera handles, `ManualClient.camera_metadata(device)` returns the probe (UVC ranges, supported resolutions). The bare `camera(device)` accessor exists only for tests — UI code must not introduce new callers.

---

## RunUiState — what the controller exports for lifecycle

`Conductor.state` is read by `RunController` and surfaced as a UI-facing enum:

| `RunUiState`  | When                                                 | Manual cards |
|---------------|------------------------------------------------------|--------------|
| `IDLE`        | No conductor (between runs, before first config-load) | Enabled, route through pool |
| `PREPARING`   | Conductor opening the session, arming workers        | Disabled (write-blocked)     |
| `RUNNING`     | Procedure running; samples flowing                   | Disabled (write-blocked) — manual commands during a run go through procedure steps, not the panel |
| `DRAINING`    | Procedure ended; workers disarming                   | Disabled (write-blocked)     |
| `FINALIZING`  | Bundle being finalized                               | Disabled (write-blocked)     |
| `SEALED`      | Bundle finalized; result inspectable                 | Enabled, route through pool  |
| `FAILED`      | Finalize itself failed                               | Enabled, route through pool  |

The write-blocked set lives in [`manual/cards/base.py:_WRITE_BLOCKED_STATES`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/manual/cards/base.py). Cards subscribe to the controller's state-change signal and call `_is_write_blocked(state)` to gate their buttons.

`Conductor.state` is an atomic read; `RunController` polls it on its own signal-emit cadence and translates into `RunUiState`. No thread-safety primitive is needed on the Qt side because the read is lock-free and emits go direct on the qasync loop.

---

## Testing widgets without a runtime

Spinning up a full `WorkerPool` and `Conductor` for every widget test is wasteful and flaky. The seam types are protocol-shaped to make stand-ins easy:

- `ManualClient` is a class, not a protocol, but its dependencies (`pool: WorkerPool`, `conductor_provider: Callable[[], Conductor | None]`) accept fakes. Existing tests under `tests/unit/ui/manual/` build a `FakePool` exposing the same `dispatch(name, cmd) -> Future` surface and pass `lambda: None` for the conductor provider.
- `UIDataBus` is a `DataBus` instance — tests construct one on the test's loop and `publish_nowait(emission)` synthetic samples. Widgets subscribe normally; the assertions then drive the loop forward.
- `RunController` is the hardest piece to fake because it owns the lifecycle wiring. Tests that need it use the `InlineRunner` ([`runner.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/runner.py)) variant or talk to a `NoOpRunner` conductor against sim adapters — both are honest and fast.

The boundary itself is what makes testing tractable: every UI test needs at most two stubs (a pool fake and a controller), never an entire device stack.

---

## Anti-patterns

The list below is the cheat sheet for code review on a PR that touches `src/capa/ui/`.

- **`from capa.runtime.worker import Worker`** — boundary violation. The UI must not reference `Worker`, `Conductor`'s private helpers, or any internal in `capa.runtime` beyond what the controller exposes.
- **`await pool.dispatch(...)`** from a Qt slot — bypasses `ManualClient`'s conductor-vs-pool routing. During a run, the command would skip bundle recording and conductor-state gating. Always go through `ManualClient`.
- **Holding a `Worker` reference on the UI thread.** The handle is fine to look at; calling its `dispatch(...)` from `ui-main` returns a `concurrent.futures.Future` that you must `asyncio.wrap_future` before awaiting. The clean alternative is to not hold it at all.
- **Subscribing to `UIDataBus` with `BackpressurePolicy.BLOCK` or `ABORT_RUN`.** Either freezes the UI or escalates to a run abort because the UI was slow. Widgets must use `DROP_OLDEST`.
- **Subscribing to `ConductorDataBus` directly from a widget.** The bus is loop-affine to the conductor loop; publishing from the qasync loop raises `DataBusLoopError`. Subscribe through `UIDataBus` instead; the drain feeds it.
- **Calling `loop.run_until_complete(...)` from a Qt slot to await a runtime coroutine.** Re-enters the qasync loop and deadlocks. Use `qasync.asyncSlot` or `asyncio.ensure_future`.
- **`worker._adapters[name]`** — even from a test. The internal map is not a stable surface; the supported reads are `worker.adapters` (property) and `pool.adapter(name)`.
- **Polling `Conductor.state` in a tight `QTimer` loop instead of subscribing to the controller's state-change signal.** Wastes a tick budget; `RunController` already emits transitions.

---

## Checklist for adding a new UI feature

A new widget or dock that needs runtime data is the common case. The checklist:

1. **What data does it read?**
   - A specific channel → `UIDataBus.subscribe_channel(name, policy=DROP_OLDEST)`.
   - A specific adapter's records → `UIDataBus.subscribe_adapter(name, policy=DROP_OLDEST)`.
   - Cold-start state → `ManualClient.snapshot(...)` or `device_readback(...)`.
2. **What commands does it issue?**
   - Manual command to a device → `ManualClient.dispatch(...)`.
   - A run-lifecycle action (start, stop) → `RunController.start_run(...)` / `request_stop(...)`. Don't reach into the conductor.
3. **What lifecycle does it follow?**
   - Subscribe to `RunController.ui_state_changed`; gate enabled/disabled state by `RunUiState`.
   - Unsubscribe from `UIDataBus` on `closeEvent` (or use a `LifecycleRegistry` registration so the shutdown coordinator does it for you).
4. **Tests.** Build a `FakeManualClient` and a fresh `UIDataBus` on the test loop. Publish synthetic emissions; assert widget state. No `Conductor`, no `Pool`, no real adapters.

If a feature can't be expressed through these surfaces, the seam needs to grow — propose the new method on `ManualClient` or a new signal on `RunController` rather than reaching across the boundary.

---

## Where to read more

- The threads behind the seam: [`threading-model.md`](threading-model.md).
- The full DataBus topology: [`runtime-architecture.md` §8](runtime-architecture.md#8-databus-topology).
- The command paths (with run-armed and idle variants): [`runtime-architecture.md` §9](runtime-architecture.md#9-command-flow).
- How a sample reaches a widget end-to-end: [`data-flow.md`](data-flow.md).
- The state badge's source of truth: the [run-tab guide](../user-guide/the-run-tab.md).
