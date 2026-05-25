# Bundle write path

**Audience:** contributors touching `capa.storage`; anyone debugging "the bundle didn't seal" or "the writer is at 100% CPU."
**Scope:** the writer thread and its sinks — the mechanics of how emissions become on-disk bytes during a run, and how those bytes are sealed at the end.

This page is the **mechanics** companion. The **protocol** (outcome states, manifest hash, what external readers verify) lives in [Integrity and sealing](../bundles/integrity-and-sealing.md). The two pages link to each other; if you find yourself wanting both, you're in the right neighbourhood.

---

## Why a dedicated thread

Earlier versions of capa wrote sinks directly from the asyncio loop. Every `record_sample` call landed on the channel-samples sink's batch buffer, which flushed an Arrow record batch + `os.fsync` every 1024 rows. On Windows that fsync is a full `FlushFileBuffers` — tens of milliseconds, sometimes more under antivirus interference. Multiplied across high-rate NI-DAQ block-mode unrolls, the loop got bursty enough to push the producer-fanout queue into `ABORT_RUN` territory under disk contention.

The fix is one dedicated [writer thread](../glossary.md#worker) ([`writer_thread.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/writer_thread.py)) that owns every sink. The asyncio loop hands items off via a bounded `queue.Queue`; in the steady state the hand-off is a microsecond-cost `put_nowait`. End-to-end backpressure is preserved — a saturated writer thread eventually pushes the producer-fanout queue into BLOCK and from there into ABORT_RUN — same shape as before, just one layer down.

**Threading boundary:** only the writer thread ever touches the `RunBundleWriter` and its sinks while the thread is alive. PyArrow's `RecordBatchStreamWriter` and `OSFile` are not documented as thread-safe, and the SQLite connection — though created with `check_same_thread=False` — gets the same single-thread discipline. See [`threading-model.md`](threading-model.md) for where the writer thread sits relative to the conductor and workers.

---

## Writer inbox — the queue

The inbox is a `queue.Queue` with a soft cap (default 4096 items, `DEFAULT_CAPACITY` in [`writer_thread.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/writer_thread.py)). The conductor's drain task submits items through a two-tier hand-off:

1. **Fast path — `put_nowait`.** Steady state. Microsecond cost.
2. **Blocking fallback — `submit_blocking` via `anyio.to_thread.run_sync`.** When the inbox is full, the loop transparently switches to a blocking `put` running off-loop. The conductor's task yields to its peers while waiting; the loop is not blocked.

Two pieces of instrumentation surface stalls:

- **`depth`** — current inbox occupancy, exposed as `WriterThread.depth`. Read by the saturation monitor and the diagnostics block.
- **`last_accept_monotonic_ns`** — monotonic-ns of the last successful inbox pop. The [saturation monitor](../safety/saturation-and-deadlines.md) compares `now - last_accept_monotonic_ns` against `saturation_deadline_s` whenever `depth > 0`. A non-empty inbox that hasn't advanced for 10 s is the canonical wedged-writer signal.

When the writer thread itself crashes (uncaught exception in the drain loop), the exception is captured and re-raised on the next `submit` or `close` so the conductor never silently loses a dead writer. The run is marked crashed; the bundle finalizes through whatever sinks completed before the crash.

---

## The sinks

The writer owns one instance per sink type. Each is opened in `RunBundleWriter.open()` and closed during finalize.

| Sink                                                                                                       | Inputs                                          | Output(s)                                  | Format          | Flush trigger                        |
|------------------------------------------------------------------------------------------------------------|-------------------------------------------------|--------------------------------------------|-----------------|--------------------------------------|
| [`ChannelSamplesSink`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/channel_samples_sink.py) | `ChannelSample`                                 | `scalars.in-flight.arrows`                 | Arrow IPC stream | every 1024 rows                      |
| [`DeviceRecordsSink`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/device_records_sink.py)  | `SourceRecord`                                  | `device_records/<adapter>.in-flight.arrows` | Arrow IPC stream | every 1024 rows                     |
| [`EventsSink`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/events_sink.py)           | `DeviceEvent`, procedure/operator events        | `events.sqlite`                            | SQLite (WAL)    | commit per write                     |
| [`FramesSink`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/video_sink.py)            | `FrameReceipt`                                  | `video/<camera>.frames.in-flight.arrows`   | Arrow IPC stream | every 256 rows (~8 s @ 30 Hz)        |
| [`StatusSink`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/status_sink.py)           | `DeviceSnapshot`                                | `status.sqlite`                            | SQLite          | commit per write                     |
| [`LogSink`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/log_sink.py)                 | structlog records                               | `run.log`                                  | JSON lines      | every write                          |

Camera *containers* (`.mkv`, `.csq`) are not managed by these sinks — each camera adapter owns its own container handle and writes from the worker loop. The `FramesSink` only writes the per-camera **frame-index sidecar** that maps `frame_idx` → `t_mono_ns` so analysts can correlate video frames to channel samples without re-parsing the container. See [Video](../bundles/video.md).

---

## Arrow IPC stream → Parquet (the two-phase write)

The three Parquet-bound sinks (`scalars`, `device_records`, `frames`) do not write Parquet during the run. They write **Arrow IPC streams** to `.in-flight.arrows` files, which the finalize stage rewrites into `.parquet` at end of run. Why two phases:

1. **Tear-tolerance.** Arrow IPC's canonical framing is `[schema][batch][batch]...` — every flush boundary is a recoverable seam. A SIGKILL or power-loss mid-write tears at most the trailing message; everything before the tear can be read with `pa.ipc.open_stream(...).read_all()` and recovered. Parquet's footer-at-end layout, by contrast, makes a torn file unreadable: the row-group index lives at the end, never written if the process died.
2. **Append efficiency.** Streaming Arrow IPC is genuine `append-only` at the byte level. Parquet's write path buffers entire row groups and emits them on a cadence; appending a single sample is not its design point.
3. **Final-form quality at end-of-run.** The finalize rewrite then produces *better* Parquet than streaming would: large row groups (256k rows, tuned for DuckDB/Polars/Arrow scan throughput), sorted by `t_mono_ns`, zstd:6-compressed. Streaming-mode Parquet writers can't choose row-group size that aggressively.

The trade-off is twice the write volume (once to IPC, once to Parquet) plus a finalize-time read-modify-write. CAPA bundles are at most tens of GB; the rewrite is bound by sequential disk throughput and completes in seconds to a few minutes. The crash-safety win is worth it.

See [`_ipc.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/_ipc.py) for the streaming-write implementation (and a note on the pyarrow 24 `OSFile.fileno()` workaround — fixed upstream in pyarrow 25).

---

## SQLite WAL for events

`events.sqlite` is opened in WAL mode and commits after every write. The two reasons:

1. **Events are tiny but precious.** A run produces tens to low thousands of events. Losing them to a crash is the worst-possible outcome: the operator notes, the alarm escalations, the safe-shutdown attestations all live here. The per-write commit cost is invisible at this volume.
2. **WAL lets external readers tail the file mid-run.** The diagnostics dock and the operator-events dock both read live; WAL gives them a consistent point-in-time view without blocking the writer.

The schema is narrow on purpose:

```sql
CREATE TABLE events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    t_mono_ns    INTEGER NOT NULL,
    t_utc        TEXT    NOT NULL,
    kind         TEXT    NOT NULL,
    severity     TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    message      TEXT    NOT NULL,
    metadata_json TEXT
);
```

Every column maps to a question someone will ask reading the bundle; the open set lives in `metadata_json`. See [Events SQLite](../bundles/events-sqlite.md) for the event taxonomy.

---

## Finalize protocol (mechanics only)

The protocol details — outcome states, hash semantics, what `capa validate` checks — are in [Integrity and sealing](../bundles/integrity-and-sealing.md). This section is the mechanical sequence the writer drives.

When the conductor signals end-of-run, the writer thread:

1. **Closes every sink.** Each Arrow IPC stream gets its final `EOS` message; each SQLite connection is committed and closed. The `.in-flight.arrows` files now contain a complete, well-framed stream — but are not yet Parquet.
2. **Calls `finalize_in_place(...)`** ([`finalize.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/finalize.py)).
   - For every `.in-flight.arrows` file (top-level scalars, `device_records/*`, `video/*.frames`):
     - Read with `read_recoverable(...)` — recovers everything before any tear.
     - Sort by `t_mono_ns` (always present on telemetry tables).
     - Write to `<name>.parquet.tmp` with row-group size 256k, zstd:6, Parquet data page v2.
     - `os.replace(tmp, final)` — atomic rename.
   - Torn / unreadable in-flight files are logged to `manifest.custom["finalize_warnings"]` and removed.
   - Update manifest: `ended_utc`, `run_status`, `data_shape`.
   - Compute `manifest.sha256` and write it.
3. **Stamps `bundle_status`** through `finalizing → finalized_unverified → sealed | verification_failed` along the way.

`finalize_in_place` is **idempotent**: running it on an already-sealed bundle is a no-op (same digest, manifest unchanged). Running it on a bundle whose in-flight files are missing but final files exist behaves as "verify and seal." This is the property that lets `capa finalize` work as crash-recovery (see below).

The final Parquet is the canonical artifact. Analyses read `scalars.parquet`, not `scalars.in-flight.arrows`. After finalize the in-flight files are removed.

---

## Crash recovery via `capa finalize`

A capa process that exits hard — the [`ShutdownCoordinator`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/ui/shutdown.py) wall-clock fuse, an OS kill, power loss — leaves no in-process signal behind. The recovery story:

1. **Active-bundle checkpoint.** [`runtime/recovery.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/recovery.py) writes `<runs_root>/.runtime-active.json` at run-open with the bundle path and the capa PID. Atomic temp-file + `os.replace`.
2. **Startup detection.** The next capa launch calls `recover_active_bundle_checkpoint(...)`. If the checkpoint exists and its PID is no longer alive, capa knows the previous run crashed.
3. **`finalize_in_place(...)`.** The recovery path calls the same finalize function the normal-exit path calls. Because the in-flight Arrow files are tear-tolerant, every flushed batch is recoverable. The bundle seals as `run_status=crashed`, `bundle_status=sealed`.

Operators can also run `capa finalize RUN_ID` manually — same code path, no daemon involvement. The CLI resolves the bundle through `--runs-root`, `CAPA_RUNS_ROOT`, or `./runs` and is documented at [`capa finalize`](../cli/capa-finalize.md).

The catalog row inserted at run-open is the *intent* to run; the manifest finalize is the *commitment*. The checkpoint is the side-channel between them — separate from the catalog so a torn catalog write doesn't strand the recovery.

---

## Backpressure and observability

End-to-end backpressure shapes how the writer behaves under load:

```
Worker outbound bridge (BLOCK)        ──► saturation deadline catches sustained block
            │
Conductor drain task                  ──► await writer.submit(...)
            │
Writer inbox (4096 cap)               ──► put_nowait fast path / put blocking fallback
            │
Writer thread drain loop              ──► per-sink dispatch, each can fsync
            │
Sink (e.g. ChannelSamplesSink)        ──► batch buffer, flushes at 1024 rows + fsync
            │
Disk
```

A slow disk fills the sink batch buffer → flushes don't drain fast enough → writer thread sits in `_dispatch` longer → inbox depth climbs → conductor's `submit` switches to blocking → the worker outbound bridge backs up → `blocked_since_ms` ticks → at 10 s the saturation monitor escalates to `crashed_but_sealed`.

The metrics exposed for diagnosis (all on `WriterThread`):

- `depth` — current inbox depth.
- `depth_high_water` — high-water mark across the run.
- `submit_blocked_count` — how many times `submit` had to wait for inbox space.
- `last_accept_monotonic_ns` — when the drain loop last popped successfully.

These land in the bundle manifest's `diagnostics.runtime` block and the status bar's pills. See [Reading status-bar symptoms](../troubleshooting/status-bar-symptoms.md).

---

## What `RunBundleWriter` owns vs what the writer thread owns

There are two objects to keep straight:

- **`RunBundleWriter`** ([`bundle.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/bundle.py)) is the *bundle*. It opens the run directory, builds the sinks, writes `manifest.json` at start and finalize, drives the `bundle_status` state machine, and calls `finalize_in_place(...)` at end of run.
- **`WriterThread`** ([`writer_thread.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/writer_thread.py)) is the *thread*. It hosts the `RunBundleWriter`, owns the inbox, runs the drain loop, and dispatches each item to the right sink method.

The conductor never talks to `RunBundleWriter` directly during a run — only via the `WriterThread`'s async surface (`record_sample`, `record_frame`, `write_event`, ...). The bundle writer is constructed first, handed to the thread at construction, and the thread takes exclusive access from `start()` onward.

---

## Where to read more

- The protocol side (outcome states, hashes, `capa validate`): [Integrity and sealing](../bundles/integrity-and-sealing.md).
- The on-disk layout: [What's in a bundle](../bundles/what-is-a-bundle.md).
- The schemas of the Parquet files: [Channel samples parquet](../bundles/parquet-channel-samples.md), [Device records parquet](../bundles/parquet-device-records.md), [Video](../bundles/video.md).
- How a sample reaches the writer in the first place: [`data-flow.md`](data-flow.md).
- Why the writer is on its own thread: [`threading-model.md`](threading-model.md).
- The deadline that catches a wedged writer: [Saturation and deadlines](../safety/saturation-and-deadlines.md).
