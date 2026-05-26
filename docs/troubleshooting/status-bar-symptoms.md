---
description: Field guide for diagnosing a degraded capa run from the status bar — which pill turned red first (sat, loop, q), and drilling into Acquisition Diagnostics.
---

# Reading status-bar symptoms

**Audience:** operators triaging a degraded run.
**Scope:** the diagnostic flowchart that starts from "which status-bar pill went red first?" and lands on "what to check, in order."

The [status bar](../user-guide/status-bar-guide.md) page describes what each pill *means*. This page is the field guide for *what to do* when one of them turns yellow or red. Use the [Acquisition Diagnostics dock](../user-guide/diagnostics-dock.md) for the per-worker drill-down once you know which pill is the lead signal.

---

## The lead-signal principle

The status bar's pills do not fail independently. A wedged writer thread reliably reddens `sat` (the conductor's drain stalls), which reddens `q` (bridges fill), which can eventually redden `loop` (drain task starvation feeds back). A CPU-busy `CustomStep` reddens `loop` first, then `sat`, then `q`.

**The pill that goes red *first* is the most diagnostic.** By the time the cascade is in full swing, three pills are red and they all look like the cause. If you're watching live, write down which pill turned colour first.

For after-the-fact triage on a sealed bundle: open `events.sqlite` and walk the events in time order — a `saturation_deadline` event with a `blocked_s` in its metadata identifies which signal tripped first. The matching `run.log` saturation entries (`saturation_monitor.deadline_exceeded`, `conductor.saturation_escalation`) carry the raw source details. Loop-lag values themselves are live diagnostics and final `manifest.queue_health` fields, not dedicated event rows.

---

## Symptom 1: `loop` red first

**What it means.** Conductor loop p99 lag is ≥ 4× the warn threshold (default ≥ 200 ms). Something on the conductor loop is doing heavy synchronous work and not yielding.

**Likely cause, ranked.**

1. **A `CustomStep` procedure handler doing inline CPU work.** This is the single most common cause. Custom steps run on the conductor loop; they MUST wrap CPU-bound work in `anyio.to_thread.run_sync`. See [runtime-architecture.md §11](../architecture/runtime-architecture.md).
2. **A `BLOCK`-policy DataBus subscriber running slow code on the conductor loop.** The drain `await`s `bus.publish`; a slow subscriber stalls every drain.
3. **A flood of saturation-monitor escalations** (rare; only seen when the saturation deadline is tuned implausibly low).

**Verify in the dock.**

- Open the [Acquisition Diagnostics dock](../user-guide/diagnostics-dock.md). If **every worker's p50 is climbing in lockstep**, that confirms conductor-side starvation. If only one worker is degraded, the lead signal is the worker, not the loop.
- The `loop_lag_p99_ms` column inside [`runtime_diagnostics()`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/conductor.py) is the same number the pill displays.

**Search the bundle.**

```sql
-- events.sqlite: did saturation trip too?
SELECT t_utc, kind, message, metadata_json
FROM events
WHERE kind = 'saturation_deadline';
```

**What to do.**

- If a custom procedure was active, audit it for un-offloaded CPU work. The §11 contract is non-optional.
- If the run is still going and the operator can intervene safely, abort and re-run after the offending step has been fixed.
- If `loop` is red but `sat` is still green, the run can usually continue — the conductor is busy but bridges aren't filling fast enough to hit the deadline yet.

---

## Symptom 2: `sat` red, `loop` low

**What it means.** A worker has been blocked on its outbound [bridge](../glossary.md#bridge) for ≥ 50% of `saturation_deadline_s` (default ≥ 5 s). The drain task on the conductor side is not pulling fast enough. Since `loop` is low, the conductor itself is fine — the bottleneck is *downstream* of the conductor's drain.

**Likely cause, ranked.**

1. **Writer thread stalled on fsync.** Slow disk, full disk, SMB share dropping packets, encrypted FUSE mount, antivirus scanning every parquet write. Check the `disk` pill — if it's yellow or red, this is almost certainly your cause.
2. **Camera encode pinning a writer-thread CPU.** A high-resolution / high-fps `libx264` encode can saturate one core; the writer thread can't keep up with the inbound frame rate. The mitigation lives in the per-camera params: swap to `h264_qsv` (Intel iGPU), `h264_nvenc` (NVIDIA), or `mjpeg` (no encode).
3. **A slow `BLOCK`-policy databus subscriber on the conductor side.** Rare today; only relevant if a plugin has wired a safety-critical subscriber that does heavy work.

**Verify in the dock.**

Open the diagnostics dock. If **every worker's Age is climbing in lockstep**, the bottleneck is shared across workers — i.e. downstream. If only one worker's Age is climbing while others are fine, see Symptom 4.

**Search the bundle.**

```sql
-- Which bridge tripped?
SELECT t_utc, message, metadata_json
FROM events
WHERE kind = 'saturation_deadline'
ORDER BY t_mono_ns;
```

The `metadata_json` field typically contains `resource_id`, `blocked_s`, and `deadline_s`. A `resource_id` for a camera worker (`webcam:0`, `flir_ir:0`) is a strong signal that encode is the cause.

**What to do.**

- **Disk-related:** free space, switch `runs_root` to a faster volume, or pause antivirus on the runs directory.
- **Encode-related:** edit the camera params in your [hardware.toml](../configuration/hardware-toml.md) to `codec = "h264_qsv"` (or `h264_nvenc` / `mjpeg`) and reload. See [cameras-webcam.md](../devices/cameras-webcam.md) and [cameras-flir.md](../devices/cameras-flir.md).
- **Subscriber-related:** identify the plugin via `capa plugins list`, audit its `DataBus.subscribe` calls. A `BLOCK` policy on a subscriber that does anything CPU-heavy is a bug.

The full mechanics live in [Saturation and deadlines](../safety/saturation-and-deadlines.md).

---

## Symptom 3: `loop` and `sat` both red

**What it means.** CPU starvation that has cascaded into downstream backup. The order in which the pills *turned* red matters here:

- **`loop` red first, then `sat`** → CPU starvation is primary; bridges backed up because drains couldn't run. Triage as Symptom 1.
- **`sat` red first, then `loop`** → downstream stall is primary; drain tasks blocking on `await writer.submit(...)` eventually starve the loop. Triage as Symptom 2.

If you didn't catch the order live, the events.sqlite `saturation_deadline` row's metadata identifies which signal tripped the deadline; if that signal is a worker-bridge `blocked_since_ms`, treat as Symptom 2; if the writer inbox stall, treat as Symptom 2 also (still downstream).

---

## Symptom 4: One worker red on the diagnostics dock, `sat` still green

**What it means.** A single worker has stopped polling. The status bar may not have escalated yet because the worker hasn't been blocked *on its outbound bridge for the full deadline* — it may simply not be producing.

**Likely cause, ranked.**

1. **Serial transport wedged.** The Watlow, Alicat, or Sartorius has stopped responding. The cancellation shield (see [runtime-architecture.md §5](../architecture/runtime-architecture.md)) ensures any in-flight transaction completes, but a *new* stream poll waiting for a reply that never comes will sit there indefinitely.
2. **Vendor SDK callback queue stalled.** For FLIR or NI-DAQ, the worker depends on an SDK-owned callback to deliver data; if the SDK stops calling back, the worker's stream loop is parked.
3. **Adapter raised and exited stream.** The worker emitted a `worker_adapter_error` event, the stream task exited, and the worker is now sitting in SAMPLING with no producer. Confirm in `events.sqlite`:

   ```sql
   SELECT t_utc, source, message, metadata_json
   FROM events
   WHERE kind = 'worker_adapter_error'
   ORDER BY t_mono_ns DESC LIMIT 5;
   ```

**What to do.** In all three cases the worker cannot be revived without restarting the run. The shielded cancellation rule means capa cannot safely interrupt the wedged transaction; the safe move is to stop the run, restart capa to reset the worker pool, and re-run.

---

## Symptom 5: `UI overflow` climbing, everything else green

**Not a problem.** UI ring buffers are non-draining — once they reach capacity, every additional sample rolls over by construction. The rollover rate equals the producer rate after the first ~10 min of run time at default sizing. See the [status bar guide §UI overflow](../user-guide/status-bar-guide.md#ui-overflow) for the full explanation.

The only condition that makes `UI overflow` diagnostic is if it grows *much faster than the producer rate × elapsed* — that means a buffer was registered with an unexpectedly small capacity. Verify the ChannelSpec's `decimate_to_hz` is set high enough to keep the samples you care about.

---

## Symptom 6: `q` climbing, `sat` still green

**What it means.** A bridge's current depth is approaching its capacity. This is a *predictive* signal — `sat` will follow if the trend continues. It can also be a transient burst (the balance enqueues a few hundred samples at startup before the conductor's drain catches up) that resolves on its own.

**Triage.**

- If `q cur` returns to single-digits within 5–10 s, the burst was transient — no action.
- If `q cur` stays near capacity for more than ~10 s, treat as a pre-`sat` Symptom 2 and investigate downstream causes (disk, encode, writer).

---

## Symptom 7: `disk` yellow or red

**What it means.** Less than 15% free (yellow) or 5% free (red) on `runs_root`. The writer will start stalling on fsync as the kernel struggles to allocate blocks; `sat` will follow shortly.

**What to do.**

- **Yellow:** finish the current run if you can. Do not start a new run on this volume.
- **Red:** stop the run cleanly if the rig allows it. Free space before starting another run.

Estimating bundle size: roughly `(sum of producer Hz × bytes per sample × duration_s) + video bitrate × duration_s`. A capa_real_full run at 50 Hz balance + 30 fps dual-camera is typically 100–500 MB per 10 min depending on encoder.

---

## Cross-references

- [Status bar — how to read it](../user-guide/status-bar-guide.md) — the colour-code reference.
- [Acquisition Diagnostics dock](../user-guide/diagnostics-dock.md) — per-worker drill-down.
- [Saturation and deadlines](../safety/saturation-and-deadlines.md) — the mechanism behind `sat`.
- [Reading event logs](reading-event-logs.md) — query recipes for `events.sqlite` and `run.log`.
