---
description: capa SaturationMonitor — durable-output deadline catching wedged writer threads via bridge blocked_since_ms and inbox depth, escalates to crashed_but_sealed.
---

# Saturation and deadlines

**Audience:** contributors writing adapters or custom procedure steps; anyone debugging the runtime's saturation monitor or chasing a `crashed_but_sealed` outcome.
**Scope:** the [`SaturationMonitor`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/saturation.py) — the end-to-end durable-output deadline that catches "the system has silently stopped writing" failures the per-channel backpressure policies cannot see.

This is the **observability boundary**, not the hardware-safety boundary. The Watlow's own loop is what keeps the rig safe in a hardware sense (see [safety principle 4](principles.md#invariant-4-capa-is-not-a-hard-real-time-control-system)); the saturation deadline is what makes sure a wedged writer thread doesn't quietly let the rig run blind for half an hour.

---

## The problem the deadline solves

Per-channel backpressure policies catch individual queue fill-ups. A Watlow [worker](../glossary.md#worker) producing emissions faster than the [conductor](../glossary.md#conductor) can drain them will eventually see its outbound [bridge](../glossary.md#bridge) fill, and the bridge's `BLOCK` policy will park the producer. From any single bridge's perspective, that's the expected behaviour.

The macro condition this misses: **the durable side has stopped accepting work.** If the writer thread wedges on an fsync, or the conductor's drain task is starved by a busy `CustomStep`, then *every* worker outbound bridge backs up in unison. No single bridge looks wrong. No backpressure policy escalates. The rig keeps running, the operator keeps believing data is being recorded, and nothing is.

The saturation deadline is the cross-cutting check for that condition. It watches two signals and escalates if either stays tripped for a configured duration:

1. **Per-bridge `blocked_since_ms`** — how long has any outbound bridge's producer been parked waiting for space?
2. **Writer-thread `last_accept_monotonic_ns` vs `depth`** — is the writer's inbox non-empty *and* not advancing?

Both signals are passive reads on existing instrumentation. The monitor adds no overhead to the hot path.

---

## Constants and tuning rationale

The defaults live in [`saturation.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/saturation.py):

| Constant | Default | Rationale |
|---|---|---|
| `DEFAULT_SATURATION_DEADLINE_S` | **10.0 s** | How long any single signal may stay tripped before escalation. Conservative — captures genuinely wedged disks and hung adapters while ignoring 10s-of-ms hiccups from GC pauses or transient I/O slowdowns. |
| `DEFAULT_POLL_PERIOD_S` | **1.0 s** | How often the monitor wakes to recheck. The doc's recommendation when tuning is `deadline_s / 10` clamped to `[1.0, 5.0]`. |

**Why 10 seconds and not 1 or 100?**

- **Below 1 s:** Windows scheduler jitter, GC pauses, and ordinary fsync stalls on a Synology NAS over SMB will trip the deadline routinely. False positives kill operator trust in the signal.
- **1–5 s:** Captures hiccups; misses genuine wedges that take a few seconds to manifest (the failure mode is slow, not fast — usually the writer goes from "fast" to "very slow" before going to "stopped").
- **10 s:** Captures genuine wedges; tolerates GC and OS-level pauses. A heater commanded at 600 °C for an extra 10 s before the run is sealed is acceptable because the heater is *already* commanded there by the procedure — the Watlow continues to enforce its own setpoint regardless of CAPA's status.
- **Above 60 s:** Defeats the purpose. The whole point is to fail loud while the operator is still in the room.

The 10 s default has been validated across the full set of supported configurations. **Tune it down only for diagnostic runs where you actively want the deadline to fire on small stalls;** tune it up only if you're running on a deliberately slow medium (SD-card writes, encrypted FUSE mount) and accept the longer detection window.

---

## How the two signals work

The monitor's `_check` evaluates the two paths every poll tick:

### Signal 1 — per-bridge blocked-since

For every outbound bridge in the conductor's bridge map:

```
if bridge.metrics.blocked_since_ms is not None
   and (blocked_since_ms × 1e6) > deadline_ns:
    trip("worker_<rid>_outbound_saturated",
         details={resource_id, blocked_s, deadline_s})
```

`blocked_since_ms` is non-`None` only while a producer is *currently* parked on the bridge's BLOCK policy waiting for space. The instant the consumer pulls one item, the field resets to `None`. So this fires only when a single worker has been continuously stuck.

### Signal 2 — writer-inbox stall

```
depth = writer.depth
last_accept = writer.last_accept_monotonic_ns
now = monotonic_ns()

if depth > 0 and (now − last_accept) > deadline_ns:
    trip("writer_inbox_stalled",
         details={depth, since_last_accept_s, deadline_s})
```

This catches the case where the writer's inbox has items queued (`depth > 0`) but `last_accept_monotonic_ns` hasn't advanced for `deadline_s` — meaning the writer thread has stopped pulling items off its inbox.

The monitor also tracks a secondary "writer wedged before the monitor saw a successful tick" path, to handle the case where the writer was already stuck at run-open. Both paths produce the same `writer_inbox_stalled` event.

---

## What happens when the deadline trips

The monitor's `on_saturated` callback wires into the conductor's [`_on_saturated`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/conductor.py):

1. **Log it.** A `conductor.saturation_escalation` structured log event records the reason.
2. **Mark the outcome.** `_outcome = RunOutcome.CRASHED_BUT_SEALED`. This is the only [bundle outcome](../glossary.md#bundle-outcome) that uses the `_but_sealed` suffix — distinguishes saturation trips from generic crashes.
3. **Write the event into the bundle.** Best-effort `writer.write_event(kind="saturation_deadline", message=reason, metadata=details)`. Best-effort because the writer itself may be the wedged component.
4. **Fire the completion event.** This triggers the conductor's normal shutdown sequence (see [shutdown sequence](shutdown-sequence.md)): the procedure unwinds, authorization disarms, the pool drains, the bundle finalizes and seals.

The monitor fires **at most once per run.** After the first trip, the monitor coroutine exits — the conductor handles everything from there. There is no "retry" or "second deadline" mechanism.

**Adapter `stop()` still runs after a saturation trip.** The hardware does not stay in an inconsistent state because of the trip — each worker's adapter gets its `stop()` called the same way it would on a graceful shutdown. See [shutdown sequence § adapter `stop()`](shutdown-sequence.md#adapter-stop-the-unconditional-hook) for what that does (and does *not*) guarantee.

---

## The `sat` status pill

The Run tab's status bar shows live saturation health via the `sat` pill, driven by [`statusbar.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/statusbar.py). The pill reads the worst `blocked_since_ms` across outbound bridges every second and colors against the configured `saturation_deadline_s`:

| State | Trigger | Meaning |
|---|---|---|
| `sat ok` (green) | No bridge has a blocked producer | Healthy |
| `blocked N s` (yellow) | Worst blocked ≥ 25% of `saturation_deadline_s` (≥ 2.5 s at default) | Drain is falling behind; trending |
| `blocked N s` (red) | Worst blocked ≥ 50% of `saturation_deadline_s` (≥ 5 s at default) | Real danger of `crashed_but_sealed` |
| `sat —` (gray) | No diagnostics yet | Before the first heartbeat |

**The 25% / 50% bands are pure UI ergonomics.** The deadline itself only escalates at 100%. The colored warnings exist so an operator can intervene (kill a noisy `CustomStep`, free disk space, swap a slow codec) *before* the deadline trips.

For triage when the pill goes yellow or red, see [status bar guide § sat](../user-guide/status-bar-guide.md#sat-saturation) — that page documents the diagnostic flow ("check loop lag first, then disk, then camera codec, then BLOCK-policy subscribers").

---

## Tuning knobs

The deadline and poll period live on [`ConductorConfig`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/conductor.py) as code-level knobs:

```python
ConductorConfig(
    saturation_deadline_s=10.0,
    saturation_poll_period_s=1.0,
)
```

These are not fields on `ExperimentConfig.runtime`, and the shipped
[`capa run`](../cli/headless-runs.md) CLI does not expose a saturation
flag today. Production runs use the defaults through
`ConductorConfig.from_runtime(config.runtime)`. Tests or embedding code
can pass an explicit `ConductorConfig`, or call the programmatic
`run_headless(..., saturation_deadline_s=...)` helper for diagnostics.

**When to tune down (toward 1–5 s):**
- You're deliberately stress-testing the system on a known-slow medium.
- You're chasing a "the writer wedges but the deadline never fires" bug and want faster feedback.
- You're running on a hosted CI environment with predictable I/O and want false-positive-on-slow as the diagnostic mode.

**When to tune up (toward 30–60 s):**
- You're running on an encrypted FUSE mount, an SD card, or another genuinely-slow target where 10 s false-positives are routine.
- You're recording with a CPU-bound codec (libx264 4K at 60 fps) and the writer occasionally needs more than 10 s to drain a backlog.

**When NOT to tune:**
- The default fires too often in your normal config. *That's the signal telling you the codec, BLOCK subscriber, or writer setup needs attention* — not the deadline. Bumping the deadline to silence the alarm hides the bug.

---

## What's NOT in scope

For symmetry with what *is* watched:

- **Per-bridge latency p99 / p50.** The monitor reads `blocked_since_ms` (current state), not historical percentiles. The legacy `latency_p99_ms` metric was removed precisely because it created stale-window false-positives on low-rate bridges.
- **Adapter-level silence.** "Watlow hasn't produced a sample in 5 s" is a per-device concern. Today it's not actively enforced; the saturation deadline only watches *blocked producers*, not *silent producers*. Per-device silence escalation is a future policy slot (the `on_failure` field on channel specs).
- **Memory pressure, CPU load, network health.** Those are operator-visible via the status bar, but the saturation monitor does not act on them.
- **External processes.** A separate sidecar process the operator started (e.g. `ffplay` reading the bundle live) is invisible to the monitor.

The monitor's scope is intentionally narrow: it watches the two signals that, taken together, mean "data is not flowing end-to-end." That's it.

---

## Implementation notes for contributors

A few details about the monitor that aren't obvious from the public API:

- **Read-only by design.** The monitor signals via the callback; it never calls `stop()` directly. The conductor decides what to do. This means the monitor can be unit-tested without a full conductor instance.
- **The `WriterSaturationSource` protocol.** The monitor doesn't depend on the concrete `WriterThread` class. Anything that exposes `last_accept_monotonic_ns` and `depth` properties satisfies the protocol — including test stubs.
- **Baseline tracking.** The monitor snapshots the writer's `last_accept_monotonic_ns` at entry, so a pre-existing backlog at run-open isn't immediately read as a stall. The first deadline-honest tick is `entry + deadline_s` from start.
- **The monitor runs on the conductor loop, not its own thread.** It's a coroutine that competes for the same loop time as drain tasks, the heartbeat, and the procedure. If the conductor loop itself wedges (a `CustomStep` doing inline CPU work), the monitor wedges with it — but so does the `sat` pill update, the loop-lag pill, and everything else, so the operator sees the symptom one way or another.

---

*See also:* [Safety principles](principles.md) · [Shutdown sequence](shutdown-sequence.md) · [Status bar guide — sat](../user-guide/status-bar-guide.md#sat-saturation) · [Runtime architecture §6.3](../architecture/runtime-architecture.md#63-saturation-deadline)
