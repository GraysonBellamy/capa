---
description: Four capa shutdown paths — graceful Stop, Emergency Stop, saturation deadline, and process crash — covering disarm, drain, finalize, and bundle sealing.
---

# Shutdown sequence

**Audience:** operators (so you know what hitting Stop will do), plugin authors writing procedures (so your procedure honours the stop contract), contributors debugging "why didn't the rig cool down?"
**Scope:** the four paths from `RUNNING` to a sealed bundle — graceful Stop, Emergency Stop, saturation-deadline trip, and process crash. What runs in each, in what order, and what's guaranteed vs. best-effort.

The defining property of CAPA's shutdown is [safety principle 3](principles.md#invariant-3-fail-loud-seal-the-bundle): in-process exits go through disarm and finalize, and a host process death leaves a recoverable bundle for `capa finalize`. A clean recovery seals the orphaned bundle; an integrity mismatch is recorded as `verification_failed` rather than silently blessed.

---

## The four shutdown paths

| Path | Trigger | `RunOutcome` | `run_status` | What runs |
|---|---|---|---|---|
| Graceful Stop | Operator clicks "Stop run" on the Run tab | `ABORTED` | `aborted` | `exit_reason=operator_safe_shutdown` → procedure honours its cleanup → disarm → drain → finalize → seal |
| Emergency Stop | Operator holds "⛔ Emergency stop" for 1 s | `ABORTED` | `aborted` | `exit_reason=operator_immediate` → procedure may skip cleanup → disarm → drain → finalize → seal |
| Saturation deadline | `SaturationMonitor` trips | `CRASHED_BUT_SEALED` | `crashed` | Event logged → outcome set → disarm → drain (best-effort) → seal |
| Process crash | `kill -9`, power loss, OS crash | `CRASHED` (set by recovery) | `crashed` | No in-process shutdown; next CAPA startup runs crash recovery on the orphaned bundle |

The [conductor](../glossary.md#conductor)'s [`RunOutcome`](../glossary.md#bundle-outcome) and the bundle manifest's [`RunStatus`](../glossary.md#runstatus) are slightly different vocabularies: `RunOutcome` is what the conductor knows; `RunStatus` is what gets recorded into the manifest. Conversion happens at finalize.

---

## The conductor's shutdown machinery

Regardless of which trigger fires, the in-process portion of shutdown drives through the same four phases. The conductor's [`stop()`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/conductor.py) is the entry point:

```python
def stop(self, *, reason: str = "operator_stop") -> Future[RunResult]:
    if self._stop_requested:
        return self._result_future
    self._stop_requested = True
    self._exit_reason = reason
    if self._outcome is RunOutcome.COMPLETED:
        self._outcome = RunOutcome.ABORTED
    loop = self._loop
    ev = self._completion_event
    if loop is not None and ev is not None and not ev.is_set():
        loop.call_soon_threadsafe(ev.set)
    return self._result_future
```

Two things to notice:

- **`stop()` is idempotent and the conductor records only the first reason.** A double-click on Stop, or Stop followed by Emergency Stop, will not change the recorded reason or re-run shutdown.
- **The conductor itself does not differentiate between abort modes.** It sets the completion event and records the reason string. The *procedure* observes the completion event (wired through to `ctx.external_stop`) and decides what to do — see the next section.

After `stop()` returns, the conductor's main coroutine observes the completion event and proceeds through:

1. **Procedure unwinds.** Whatever it was doing — running a method step, sleeping, waiting on a databus subscription — is interrupted by `external_stop`. The procedure's `finally` blocks run.
2. **Authorization disarms.** `auth.disarm()` is called in the conductor's `finally`. After this, no further procedure-issued commands can leave the system. See [authorization gates](authorization-gates.md#lifecycle).
3. **Pool drains.** Each [worker](../glossary.md#worker) is asked to wind down via `pool.disarm_all(grace_s=shutdown_grace_s)`, which calls each adapter's `stop()`. Workers transition `SAMPLING → DRAINING → IDLE`. The [`grace_s`](../glossary.md#grace_s-shutdown-grace) timeout bounds this phase.
4. **Bundle finalizes and seals.** Sinks close, `.in-flight.arrows` files are rewritten to `.parquet`, the manifest gets `ended_utc` and `run_status`, the manifest's SHA256 is computed and written. The bundle's [status](../glossary.md#bundlestatus) progresses `open → finalizing → sealed` on success, or `verification_failed` if the integrity walk reports a mismatch.

---

## Graceful Stop vs Emergency Stop

The Run tab has two buttons that both call [`request_abort()`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/state.py) on the controller, differing in their `mode` argument:

| Button | Click discipline | `mode` arg | `exit_reason` stamped | Intended procedure behaviour |
|---|---|---|---|---|
| **Stop run** | Single click | `"safe_shutdown"` | `operator_safe_shutdown` | Procedure honours its safe-shutdown discipline before unwinding |
| **⛔ Emergency stop** | Hold 1 s | `"immediate"` | `operator_immediate` | Procedure exits as fast as it can; safe-shutdown discipline skipped |

The 1-second hold on Emergency Stop is enforced by [`HoldToConfirmButton`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/hold_to_confirm.py) — see [destructive operations](destructive-operations.md#gate-2-hold-to-confirm-run-tab-emergency-stop). The hold exists to prevent mis-clicks from killing a 30-minute run.

**Important:** the conductor does the same thing in both cases — sets the completion event with a different `exit_reason`. The actual difference in behaviour comes from the procedure observing `exit_reason` (or `external_stop` plus some procedure-local state) and choosing whether to run cleanup. This is by design — different procedures need different cleanup, and pushing the decision into the procedure keeps the conductor's contract narrow.

**For method-based procedures (RecipeRunner):** the [`MethodExecutor`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/executor.py)'s main step loop checks `external_stop.is_set()` between every step and returns immediately when it fires. **A `SafeShutdownStep` at the end of the method does not automatically run when external_stop fires mid-method.** The procedure must invoke it explicitly via `MethodExecutor.run_segment(step)` in its `finally` clause — that's what `run_segment` exists for.

`SafetyPolicy.default_abort` stores the lab's preferred abort mode in the config/UI. The current Run-tab buttons are fixed: Stop uses `"safe_shutdown"` and Emergency uses `"immediate"`.

---

## `SafeShutdownStep` — schema and what it does

[`SafeShutdownStep`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/method.py) is a method step like any other; it just has well-defined cooldown semantics:

```python
class SafeShutdownStep:
    kind: Literal["safe_shutdown"] = "safe_shutdown"
    cool_target: dict[str, float]   # channel_name -> safe setpoint value
    duration_s: float | None = None # optional hold-at-safe duration
```

The executor's [`_run_safe_shutdown`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/executor.py) drives each channel in `cool_target` to its commanded value (using `_command_setpoint` — same authorization path as any other setpoint), then if `duration_s` is set, waits up to that duration *or* until `external_stop` fires.

A channel-resolve failure during cooldown is logged but does not abort the rest of the cooldown — the other targets still get their safe values. This is the "best-effort but keep going" pattern: if your Watlow is missing from the registry, you still want the Alicat to close its valves.

---

## Adapter `stop()` — the unconditional hook

Every adapter implements an async `stop()` defined on the [`DeviceAdapter`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/adapter.py) protocol. `stop()` runs on **every** shutdown path — graceful, emergency, saturation, or unhandled exception in the procedure. It is the one piece of cleanup the conductor *can* guarantee.

What `stop()` actually does varies by adapter:

| Adapter | What `stop()` does |
|---|---|
| Watlow | Requests the streaming loop to exit; the next batch arrival from `watlowlib.record` lets the stream observe the request and break out. **Does NOT drive the heater to a safe setpoint** — the controller keeps its last commanded setpoint. ([`watlow.py:414`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/watlow.py#L414)) |
| Alicat | Same pattern — exit the streaming loop. Valves stay wherever the procedure left them. ([`alicat.py:300`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/alicat.py#L300)) |
| Sartorius balance | Stream exit; no destructive write. ([`sartorius.py:292`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sartorius.py#L292)) |
| NI-DAQ | Stop the streaming task. Read-only adapter; nothing to drive to safe. ([`nidaq.py:427`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/nidaq.py#L427)) |
| Simulator adapters | Match their real-hardware counterparts for parity. |

**This means: adapter `stop()` is necessary for clean shutdown but is NOT sufficient for hardware safety.** A 600 °C cone with the heater commanded to 600 °C will stay at 600 °C across a CAPA shutdown unless the procedure or method explicitly drove it lower first (via `SafeShutdownStep` or equivalent procedure logic), or the Watlow's own alarm trips (see [safety principle 4](principles.md#invariant-4-capa-is-not-a-hard-real-time-control-system)).

If you write a custom adapter that *does* have a "safe state" worth driving to (a valve that should be closed, a coil that should be de-energised), implement that in your adapter's `stop()`. The conductor will call it on every shutdown path.

---

## Saturation deadline shutdown

When the [`SaturationMonitor`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/saturation.py) trips its deadline, the conductor's [`_on_saturated`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/conductor.py) callback runs:

1. Records the event reason on the conductor.
2. Sets `_outcome = RunOutcome.CRASHED_BUT_SEALED` (the only outcome that uses the `_but_sealed` suffix).
3. Best-effort write of a `saturation_deadline` event into the bundle event log. Best-effort because the writer thread itself may be the wedged component.
4. Sets the completion event — which triggers normal shutdown (disarm, drain, seal) just like an operator stop.

The "but sealed" is the important part: even when the writer itself is the wedged component, the conductor still tries to drain whatever it can and emit a sealed manifest. The downstream tooling sees a [`crashed_but_sealed`](../glossary.md#crashed_but_sealed) outcome and knows the bundle is partial but trustworthy.

See [saturation and deadlines](saturation-and-deadlines.md) for the trip conditions and tuning.

---

## Crash recovery

If the CAPA host process dies hard — `kill -9`, OS crash, power loss, a Python `BaseException` like `KeyboardInterrupt` propagating past the conductor's task group — no in-process shutdown runs. The bundle on disk is left with whatever files the writer thread had flushed, the manifest still says `run_status="running"`, and the runtime checkpoint file (`.runtime-active.json` in the bundle) still points at a now-dead PID.

The next CAPA invocation runs [`recover_active_bundle_checkpoint()`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/recovery.py) on startup:

1. Look for `.runtime-active.json` checkpoints in `runs_root`.
2. For each, check if the owning PID is still alive.
3. If dead: mark manifest `run_status="crashed"`, set `ended_utc=now`, delete the checkpoint, leave the bundle in `finalizing` state.
4. If alive: no-op (another CAPA instance owns the bundle).

The actual sealing then happens via the [`capa finalize`](../cli/capa-finalize.md) CLI, which calls [`finalize_in_place`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/finalize.py) to rewrite `.in-flight.arrows` files to `.parquet`, sort by `t_mono_ns`, compute SHA256, and stamp `bundle_status="sealed"`.

This recovery path is documented in detail in [troubleshooting/crash-recovery.md](../troubleshooting/crash-recovery.md). What matters here: **a crashed CAPA process always leaves a recoverable bundle.** A subsequent invocation will find it, mark it crashed, and finalize it — operator data is never lost to a process death.

---

## Bundle outcome states

The conductor's [`RunOutcome`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/conductor.py) maps to the manifest's `RunStatus` × `BundleStatus` at finalize:

| `RunOutcome` | `RunStatus` | Typical `BundleStatus` | Reader contract |
|---|---|---|---|
| `COMPLETED` | `completed` | `sealed` | Trust everything |
| `ABORTED` | `aborted` | `sealed` | Trust everything; run is shorter than the method would imply |
| `CRASHED` | `crashed` | `sealed` (or `verification_failed` if integrity failed) | Trust the data only after inspection; check the event log for the exception |
| `CRASHED_BUT_SEALED` | `crashed` | `sealed` | Trust the data up to the `saturation_deadline` event |

Every reader downstream — analysis notebooks, `capa validate`, the bundle catalog — can branch on these two strings to decide how to treat the bundle.

---

## What's NOT in the shutdown contract

For symmetry with what *is* guaranteed:

- **CAPA does not guarantee the hardware reaches a safe state by itself.** Configure your Watlow alarms; rely on physical interlocks. See [safety principle 4](principles.md#invariant-4-capa-is-not-a-hard-real-time-control-system).
- **CAPA does not retry shutdown on partial failure.** If a worker's `stop()` raises, that's logged and the conductor proceeds to the next worker. A single bad adapter does not stall the rest of shutdown.
- **CAPA does not roll back partial writes.** Whatever the writer flushed before shutdown is what's in the bundle. The finalize step rewrites the partial Arrow IPC streams to Parquet but does not synthesise samples that were in-flight in the worker outbound bridges.
- **CAPA does not preserve the procedure's in-memory state.** Procedure metadata (`ctx.metadata`) is per-run scratch — it does not persist across a shutdown. If your procedure needs to recover state across crashes, persist it explicitly to the bundle.

---

*See also:* [Safety principles](principles.md) · [Authorization gates](authorization-gates.md) · [Saturation and deadlines](saturation-and-deadlines.md) · [Aborting safely](../user-guide/aborting-safely.md) · [Crash recovery](../troubleshooting/crash-recovery.md) · [`capa finalize`](../cli/capa-finalize.md)
