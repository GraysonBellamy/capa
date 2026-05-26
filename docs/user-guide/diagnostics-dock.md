---
description: capa Acquisition Diagnostics dock — per-worker Rate, p50, jitter, Age columns from WorkerMetrics for triaging heater, MFC, balance, NI-DAQ, and camera polls.
---

# Acquisition Diagnostics dock

**Audience:** operators triaging "why is this run weird?", contributors debugging adapters.
**Scope:** every column of the Acquisition Diagnostics dock — per-worker rate, poll-period p50, jitter, and last-sample age — and how to read those rows alongside the [status bar](status-bar-guide.md) when something looks off.

The dock is a per-[worker](../glossary.md#worker) view of acquisition health. The status bar tells you that *something* is wrong; the diagnostics dock tells you *which device*. Both surfaces poll [`Conductor.runtime_diagnostics()`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/conductor.py) at 1 Hz — they are looking at exactly the same numbers, just sliced differently.

See [`docks/diagnostics.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/docks/diagnostics.py) for the implementation and [`runtime/metrics.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/metrics.py) for the underlying `WorkerMetrics` struct.

---

## Layout

One row per worker (one `resource_id`). On the full real config that is six rows — one each for the heater, purge MFC, balance, NI-DAQ chassis, visible camera, and IR camera.

| Column | What it shows |
|---|---|
| **Device** | The adapter name(s) hosted by this worker. A worker with one adapter shows just that name; a worker that hosts two adapters that share a serial port shows both, comma-separated. |
| **Rate (Hz)** | Measured poll rate — `1000 / poll_period_p50_ms`. This is the operator-facing acquisition rate to compare against the device's configured `rate_hz`. |
| **p50 (ms)** | Median wall-clock gap between consecutive polls. Stable rates have a tight p50; loop-lag drives p50 up. |
| **Jitter (ms)** | `p99 − p50` of the poll-period ring. The long tail of poll lateness. |
| **Age (s)** | Wall-clock seconds since the most recent poll on this worker. Colour-coded: green ≤ 2 s, yellow 2–5 s, red > 5 s. |

All four numeric columns hold an em-dash (`—`) until at least two polls have landed on that worker. Poll-period needs two timestamps to compute a gap, so showing `0.00` after a single poll would lie about the cadence.

---

## What each column actually measures

### Rate (Hz)

The displayed rate is **inverse of poll-period p50**, not poll-count divided by elapsed time. The distinction matters: the underlying counter, [`WorkerMetrics.poll_rate_hz`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/metrics.py), responds within ~50 samples to a rate change, where a naive `count / elapsed` would lag for the full run length.

A second subtlety: rate is keyed on `SourceRecord` emissions — one per actual device poll — not on every `adapter.stream()` yield. Every adapter yields a *burst* of emissions per poll (1 `SourceRecord` + N `ChannelSample`s + the occasional `DeviceSnapshot`), so a `count-all-yields / elapsed` calculation would report tens of thousands of Hz for a 1 Hz device. See [`runtime/metrics.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/metrics.py)'s `polls_emitted` vs `samples_emitted`.

What "healthy" looks like for the real configurations:

| Worker | Configured `rate_hz` | Displayed Rate |
|---|---|---|
| Heater (Watlow) | 2 Hz | ~2.00 |
| Purge MFC (Alicat) | 2 Hz | ~2.00 |
| Balance (Sartorius) | 50 Hz | ~50.0 |
| NI-DAQ chassis | 5 Hz | ~5.00 |
| Visible camera | 30 fps | ~30.0 |
| FLIR IR | 30 fps | ~30.0 |

A persistent disagreement between configured rate and displayed rate (more than a few percent low) means the worker's loop is missing its target cadence — see Age and Jitter for which signal points where.

### p50 (ms) and Jitter (ms)

p50 is the median gap between consecutive polls. Jitter is `p99 − p50`, so it captures the long tail without being dominated by the median.

- For a clean 50 Hz balance: p50 ≈ 20 ms, jitter ≈ 1–3 ms.
- For a 2 Hz heater: p50 ≈ 500 ms, jitter ≈ 5–10 ms.

Healthy jitter is a single-digit fraction of p50. Jitter that grows toward p50 (e.g. p50=500 ms, jitter=200 ms) means polls are landing in clumps — the worker is alternately stalling and catching up.

Both percentiles read from a 1024-observation ring inside `WorkerMetrics.poll_period_ms`, so the readout reflects the last ~20 s of a 50 Hz worker or the last ~8.5 min of a 2 Hz worker. Slow workers update slowly; that's the cost of a fixed-size ring.

### Age (s)

Wall-clock seconds since this worker last produced a `SourceRecord`. The colouring is harsh on purpose:

- **Green** (≤ 2 s): normal.
- **Yellow** (2–5 s, or any worker with `loop_lag_p99_ms` above the configured warn threshold): the worker is degraded but still producing.
- **Red** (> 5 s): the worker has not polled in five seconds. For a 2 Hz device that's 10 missed polls; for a 50 Hz balance that's 250 missed polls. Something is wrong — either the adapter has wedged, the serial transport has died, or the worker's loop is starved.

A row goes neutral grey (idle) when no run is active, or when the worker exists in the pool but has not produced a poll yet — distinguishable from red because the numeric cells stay as em-dashes rather than displaying the last-known value.

---

## Reading the dock alongside the status bar

The [status bar](status-bar-guide.md) is an aggregated view: one `sat` pill summarising the worst bridge, one `loop` pill summarising the conductor. The diagnostics dock is the per-worker drill-down. Use them together:

- **`sat` red, single Age column also red.** A single worker is stuck. Likely a serial-port wedge or an adapter that has stopped yielding. Check that device's events in [`events.sqlite`](../bundles/events-sqlite.md) and the worker's section of `run.log`.
- **`sat` red, *every* Age column climbing in lockstep.** The downstream — writer thread, disk, or a slow `BLOCK`-policy databus subscriber — is the bottleneck. No single worker is at fault; they are all backed up because their drain task is blocked downstream. See [Saturation and deadlines](../safety/saturation-and-deadlines.md).
- **`loop` red, p50 columns drifting upward across all workers.** The conductor loop is CPU-starved. Most common cause is a `CustomStep` procedure handler doing inline CPU work; see [runtime-architecture.md §11](../architecture/runtime-architecture.md).
- **`loop` red, p50 columns *not* drifting.** Each worker is on its own loop, so worker-side p50 is insensitive to conductor-side starvation. The mismatch tells you the loop lag is real on the conductor but the workers are still polling fine — the data is queued at the bridge, not delayed at the device.
- **Age red on the IR camera, everything else green.** The FLIR SDK has stopped delivering frames. Confirm in `events.sqlite` for a `camera.recording_stopped` event without a matching `recording_started`. The cancellation shield (see [runtime-architecture §5](../architecture/runtime-architecture.md)) means a wedged vendor SDK call cannot be safely interrupted; recovery requires restarting capa.

---

## What the dock does *not* show

- **No latency / inbox / fsync metrics.** Writer-thread health is observable only through the `sat` pill and the downstream effect on every worker's Age. The diagnostics dock is producer-side.
- **No per-channel rates.** A worker may host multiple channels (a Watlow heater emits both PV and setpoint as channels under one poll). The dock measures the poll cadence, not the channel emission count.
- **No history beyond the percentile ring.** Closing and reopening the run resets every row; the dock is a live view, not a recording. The bundle's [`manifest.json` `queue_health` block](../bundles/manifest-and-schema.md#queue-health) captures the final snapshot on seal — that is the post-run archival source.

---

*See also: [Status bar](status-bar-guide.md), [Runtime architecture](../architecture/runtime-architecture.md), [Channel pipeline](../architecture/channel-pipeline.md).*
