---
description: Reading the capa status bar pills — state, sat, loop, q, safety queue, disk, cam, op, bundle — with healthy, warn, fail thresholds polled at 1 Hz.
---

# Status bar — how to read it

**Audience:** operators running CAPA on the rig, contributors triaging "why does my run look weird?"
**Scope:** what each pill in the bottom status bar means, what counts as healthy / unhealthy, what to do when it isn't healthy.

The status bar polls live metrics from the conductor and UI ring buffers at 1 Hz. Every pill is designed to mean something **right now** — counters that grow by-design or percentiles over stale windows have been deliberately removed. See [`statusbar.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/statusbar.py) for the implementation and [`runtime-architecture.md`](../architecture/runtime-architecture.md) for the runtime concepts each pill exposes.

---

## Pill order, left to right

`state` · `elapsed` · `UI overflow` · `sat` · `loop` · `q` · `safety queue` · `disk` · `cam` · `op` · `bundle`

| Pill | What it shows | Good | Warn | Fail |
|---|---|---|---|---|
| **state** | Run lifecycle | `RUNNING` (green) / `IDLE` (gray) / `SEALED` (green) | `PREPARING` / `DRAINING` / `FINALIZING` | `FAILED` |
| **elapsed** | Run wall time | n/a | n/a | n/a |
| **UI overflow** | UI ring buffer rollovers — oldest sample evicted (excludes decimation) | informational | — | — |
| **sat** | Worst current `blocked_since_ms` across worker→conductor bridges | `sat ok` (green) | `blocked Ns` ≥ 25% of `saturation_deadline_s` (yellow) | ≥ 50% of deadline (red) |
| **loop** | Conductor loop p99 lag (sliding 1024-sample window, ~51 s at 20 Hz) | `< runtime.loop_lag_warn_ms` | `warn–4×warn` (yellow) | `≥ 4×warn` (red) |
| **q** | Worst `current depth / lifetime max depth` across bridges | `cur` near 0 | `cur` climbing toward `max` | `cur ≈ capacity` |
| **safety queue** | Reserved safety monitor queue | always 0 today | — | — |
| **disk** | Free space on `runs_root` | `> 15%` free | `< 15%` (yellow) | `< 5%` (red) |
| **cam** | Camera health | `cam n/a` when no camera is active | — | — |
| **op** | Operator id | non-empty | — | — |
| **bundle** | Path of the active run's bundle | shown when sealed | — | — |

---

## UI overflow

**What it shows.** Total ring rollovers summed across every channel's UI ring buffer ([`ringbuffer.py:total_overflow`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/ringbuffer.py)). Each rollover = a new sample arrived while the ring was already at capacity, so the oldest sample was evicted to make room.

**This is not a backpressure signal.** Plot snapshots are non-draining copies — the ring is *never* emptied by the consumer. So once a buffer accumulates its capacity-worth of samples, every subsequent push rolls over by construction, regardless of how fast or slow the UI thread is running. The pill is informational; for actual UI / conductor distress use `sat`, `loop`, and `q`.

**Sizing.** [`RingBufferRegistry.register`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/ringbuffer.py) defaults to enough capacity to hold `DEFAULT_HISTORY_S` (10 min) of samples at the channel's `decimate_to_hz`. So under normal config a 50 Hz balance ring holds ~30 000 samples and only starts rolling over after ten minutes of run time.

**Decimation tooltip.** Each ring buffer has a `decimate_to_hz` (ChannelSpec default 60 Hz); samples that arrive faster than that interval are dropped by the ring on push. The tooltip surfaces this count separately so you can tell whether a fast producer is pushing past the channel's configured decimate rate.

**When the rollover rate matters.** If overflow grows much faster than `(producer_rate − decimate_to_hz)` × elapsed-past-fill, something is registering the buffer with an unexpectedly small capacity — check that `decimate_to_hz` on the ChannelSpec is set high enough to keep the samples you care about, and that no caller is overriding capacity downward.

---

## sat (saturation)

**What it shows.** Worst `blocked_since_ms` across worker→conductor bridges. A bridge's `blocked_since_ms` is non-None whenever a producer (worker) is *currently waiting* for outbound bridge space ([`bridge.py:blocked_since_ms`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/bridge.py)). This is the saturation-deadline signal the conductor's `SaturationMonitor` polls.

**Healthy.** `sat ok` (green). No bridge has a blocked producer.

**Warn (yellow).** `blocked Ns` where N ≥ 25% of `saturation_deadline_s` (default 10s, so ≥ 2.5 s). Drain is falling behind a producer; not yet fatal but trending.

**Fail (red).** N ≥ 50% of `saturation_deadline_s` (≥ 5 s by default). The run is in real danger of being sealed as `crashed_but_sealed` ([runtime-architecture.md §6.3](../architecture/runtime-architecture.md#63-saturation-deadline)).

**What to check, in order.**
1. **Loop lag pill** — high conductor loop lag means the loop itself is CPU-starved; nothing downstream gets done. Likely a `CustomStep` procedure handler doing inline CPU work, see [runtime-architecture.md §11](../architecture/runtime-architecture.md#11-procedure-cpu-offload).
2. **Disk pill** — writer fsync slows when disk is full or slow; writer inbox fills; conductor drain blocks on `await writer.submit/record_frame`; bridges back up.
3. **Camera config** — high-resolution / high-fps video encode (libx264) on a CPU-bound writer thread is the canonical cause. Swap to `h264_qsv` (Intel iGPU) / `h264_nvenc` (NVIDIA) / `mjpeg` (no encode) in the camera params.
4. **A BLOCK-policy DataBus subscriber doing slow work** — the conductor drain `await`s `bus.publish`, so a slow subscriber stalls it.

**What it is NOT.** This pill does not reflect bridge latency p99. The legacy "sink lag" metric used `latency_p99_ms`, a 1024-sample ring — which on low-rate bridges (Watlow at 2 Hz = ~8.5 minutes per window) meant one bad startup observation pinned the readout for ages. `sat` reads current state only.

---

## loop (conductor loop lag)

**What it shows.** Conductor loop p99 lag in ms, over a sliding 1024-observation window at 20 Hz heartbeat (≈51 seconds of memory) ([`heartbeat.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/heartbeat.py)). Lag is the difference between when a 50 ms heartbeat *should* have fired and when it actually did — i.e. how long the loop was not scheduling tasks.

**Healthy.** Below `runtime.loop_lag_warn_ms` (default 50 ms).

**Warn.** Between `runtime.loop_lag_warn_ms` and four times that value (default 50–200 ms, yellow). The conductor's drain tasks, heartbeat, and saturation monitor are not getting fair scheduling. Procedure preflight `_wait_for` may start missing its target cadence.

**Fail.** At or above four times `runtime.loop_lag_warn_ms` (default ≥ 200 ms, red). Something on the conductor loop is doing heavy synchronous work. The most common cause is a `CustomStep` handler ignoring the [§11 contract](../architecture/runtime-architecture.md#11-procedure-cpu-offload) — every plugin author's custom step MUST wrap CPU work in `anyio.to_thread.run_sync`.

**How to triage.**
- `loop` high + `sat ok` → conductor loop is CPU-busy but bridges aren't filling yet (busy loop is processing emissions, just slowly). Will become saturation if sustained.
- `loop` low + `sat` warning/fail → conductor loop is fine, the *downstream* (writer or BLOCK subscriber) is the bottleneck. Drain task is parked on an await.
- Both high → CPU starvation that's already caused downstream backup.

---

## q (queue depth)

**What it shows.** `cur/max` — worst current `depth` and lifetime `depth_max` across worker→conductor bridges.

**Healthy.** `cur` stays low (single-digit, or near zero), regardless of `max`. The `max` may be high if there was a one-time spike; that's history.

**Unhealthy.** `cur` climbing toward `max`, or `cur` consistently near bridge capacity. Bridge capacities are `max(64, ceil(8 * rate_hz))`, so:

| Worker | Rate | Capacity |
|---|---|---|
| Watlow | 2 Hz | 64 |
| Alicat | 2 Hz | 64 |
| NI-DAQ | 5 Hz | 64 |
| Balance | 50 Hz | 400 |
| Webcam | 30 fps | 240 |
| FLIR IR | 30 fps | 240 |

A balance at `cur 380/400` means the drain is about to block the balance producer.

**What to check.** Same triage as `sat` — `q` climbing precedes `sat` going red.

---

## What "healthy steady state" looks like for capa_real_full

After ~30 s of run time:

```
RUNNING  00:00:42  UI overflow 0  sat ok  loop 8 ms  q 1/47  …
```

- `UI overflow` stays at 0 for the first ~10 min, then grows at roughly the producer rate (informational).
- `sat ok` stays green.
- `loop` stays well under 50 ms.
- `q` `cur` stays under ~10; `max` may show a startup spike (50-ish on the balance is normal for the first second).

Anything else means something is wrong — and the **pill that goes red first** is the most diagnostic. `loop` first → CPU on conductor. `sat` first (with `loop` low) → writer / disk / camera encode. `UI overflow` only → UI thread is the issue (heavy widget callback).

---

## Quick reference: which pill points where

```
UI overflow climbing       → informational (ring at capacity); use sat/loop/q for real distress
loop > 50 ms              → CPU on conductor loop (CustomStep, missing to_thread.run_sync)
sat blocked, loop low     → downstream stall: writer, disk, slow BLOCK subscriber
sat blocked, loop high    → CPU on conductor cascaded into downstream backup
q cur high, sat ok        → transient burst; watch for trend
disk red                  → free space; writer will stall imminently
```

---

*See also: [`runtime-architecture.md`](../architecture/runtime-architecture.md) for the runtime concepts.*
