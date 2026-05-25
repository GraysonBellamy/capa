# Crash recovery

**Audience:** operators after an unclean shutdown (power loss, OS crash, SIGKILL, force-quit).
**Scope:** detecting a partial bundle, running `capa finalize`, what data survives, and when to salvage versus discard.

A normal capa shutdown — operator stop, method completion, abort — runs the finalize-in-place sweep before exiting: in-flight Arrow IPC streams get rewritten to compressed parquet, the manifest is updated and hashed, and `bundle_status` advances to `sealed`. If capa exits ungracefully, the bundle is left at whatever finalize state it was in. Recovery is a single command.

This page is about the *unclean* path. For the *clean* abort path see [Aborting safely](../user-guide/aborting-safely.md); for the runtime's normal shutdown protocol see [Shutdown sequence](../safety/shutdown-sequence.md).

---

## Vocabulary: three different "abnormal exit" categories

These all leave the operator looking at the same UI, but the bundle state and recovery action differ:

| Category | Bundle's recorded outcome | Trigger |
|---|---|---|
| **Operator abort** | `run_status = "aborted"` | Operator-initiated stop. Normal shutdown ran. Bundle is `sealed`. No recovery needed. |
| **`crashed_but_sealed`** | `run_status = "crashed"`, `bundle_status = "sealed"` | Saturation deadline tripped. Normal shutdown ran via the deadline's escalation path. Bundle is sealed. No recovery needed, but the run is degraded — see [Saturation and deadlines](../safety/saturation-and-deadlines.md). |
| **Hard crash** | `bundle_status` may be `open`, `finalizing`, or `finalized_unverified` | Power loss, SIGKILL, kernel panic, force-quit. Normal shutdown did *not* run. **This is the case `capa finalize` exists for.** |

Only the third category needs intervention.

---

## Detecting a partial bundle

The authoritative signal is the `bundle_status` field in the bundle's `manifest.json`. The `BundleStatus` enum lives in [`manifest.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/manifest.py):

| `bundle_status` | What it means | Action |
|---|---|---|
| `open` | Sinks were still mid-write at exit. In-flight Arrow IPC files (`*.in-flight.arrows`) exist; final parquets do not. | Run `capa finalize`. |
| `finalizing` | The finalize sweep started but did not complete. Some files may already be rewritten to parquet, some not. | Run `capa finalize` — it picks up where the previous attempt left off (idempotent). |
| `finalized_unverified` | Rewrite completed but the manifest hash was not written. Data is readable but not yet sealed. | Run `capa finalize` to seal. |
| `sealed` | Bundle is complete. `manifest.sha256` is on disk. Safe to copy and archive. | Nothing. |
| `verification_failed` | Finalize completed, but the post-write integrity walk found a mismatch. The bundle is readable but not trustworthy without inspection. | Investigate before relying on the data — see "When to discard" below. |

Inspect a manifest directly:

```bash
python -c "import json, sys; print(json.load(open(sys.argv[1]))['bundle_status'])" \
    /path/to/runs/<run_id>/manifest.json
```

Or by glob:

```bash
# Any bundle that isn't sealed
for m in runs/*/manifest.json; do
    status=$(python -c "import json,sys; print(json.load(open(sys.argv[1]))['bundle_status'])" "$m")
    case "$status" in
        sealed) ;;
        *) echo "$status  $m" ;;
    esac
done
```

The current capa UI does *not* flag unsealed bundles in the recents list. If the recents list grows that affordance in a future release, it will read this same `bundle_status` field.

---

## Recovery: `capa finalize`

The CLI command:

```
capa finalize <run_id> [--runs-root PATH]
```

`<run_id>` is the bundle directory name (the timestamped folder name, e.g. `2026-05-25T14-32-08_<sample>`). The command resolves it against your default `runs_root` unless you pass `--runs-root`.

What it does, in [`storage/finalize.py:finalize_in_place`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/finalize.py):

1. **Rewrite in-flight files.** Every `*.in-flight.arrows` in the bundle (scalars, per-device records, per-camera frame indexes) is read, sorted by `t_mono_ns` if present, and written to its final compressed parquet form. Torn or unreadable in-flight files are logged to `manifest.custom["finalize_warnings"]` and removed.
2. **Update the manifest.** `ended_utc` is set if absent. `run_status` advances to `completed` (if it was already `completed`) or `crashed` (if it was `running` or `crashed`). `data_shape` is recomputed from the on-disk final files, and a clean candidate manifest is stamped `bundle_status="sealed"` with `integrity.status="ok"`.
3. **Compute and verify the manifest digest.** `manifest.sha256` lands in the bundle, then the integrity walk verifies it.
4. **Revise `bundle_status` if needed.** The bundle stays `sealed` on success, or is rewritten to `verification_failed` if the integrity walk reports a mismatch. `finalized_unverified` exists for data-complete bundles that still lack a digest, but the normal finalize path does not dwell there.

The whole operation is **idempotent**. Running `capa finalize` on a sealed bundle is a no-op (same digest in, same digest out, no files touched). Running it on a bundle whose finalize was previously interrupted picks up cleanly from whatever state it left behind.

Example output:

```
$ capa finalize 2026-05-25T14-32-08_calibration_run_07
finalized: 2026-05-25T14-32-08_calibration_run_07
  rewrote:  4 file(s)
  skipped:  0 already-final file(s)
  integrity: ok
```

Exit codes: `0` on success, `2` if the bundle directory or manifest is missing/malformed, `3` if finalize itself raised.

See [the `capa finalize` CLI reference](../cli/capa-finalize.md) for the full surface.

---

## What survives, what doesn't

A clean understanding of capa's durability boundaries makes recovery decisions easier:

**Always survives (modulo disk integrity):**

- **Every event written to `events.sqlite` before the crash.** SQLite is opened with `journal_mode=WAL` and `synchronous=NORMAL`, so every committed row is durable across process death. The schema commits after every write — see [`events_sink.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/events_sink.py).
- **Every Arrow IPC frame flushed before the crash.** Writes happen in chunks; chunks already flushed to disk are recoverable even if the file was never closed.
- **Every `run.log` line that was actually flushed.** The file is line-buffered; the most recent ~1 KiB of writes may be in the kernel's page cache rather than on disk, so the tail of `run.log` is occasionally truncated.

**May not survive:**

- **The final partial Arrow chunk in each in-flight file.** If the process died mid-write, the trailing bytes of one chunk may be torn. `read_recoverable` in [`storage/_ipc.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/_ipc.py) drops these and surfaces a finalize warning. Everything before the last fully-flushed chunk is intact.
- **The last few `run.log` lines.** See above.

**Definitely doesn't survive:**

- **Video frames after the last container flush.** Visible-camera containers are `.mkv`; IR camera output is `.csq`. Matroska is more recoverable than MP4, but a hard crash can still leave the final frames or container index incomplete. The frame-index parquet that pairs each frame to a `t_mono_ns` is still recoverable from its in-flight file; remux the container if your video tooling refuses to open it.
- **In-memory state that wasn't written.** Procedure state, runtime metrics not yet sampled into the manifest, anything the UI had but hadn't yet pushed to a sink.

---

## When to salvage versus discard

A `verification_failed` outcome or a finalize warning is a yellow flag, not a red one. Use the data, but with awareness:

- **Sim runs, tuning runs, dry runs.** Discard. The cost of re-running is low; the value of recovered data is low.
- **Real material runs (anything that consumed a physical sample).** Always finalize and inspect, even on `verification_failed`. The parquet data inside is typically readable — the verification failure usually flags a metadata-level mismatch (manifest digest, file count) rather than a corrupted sample. Use `polars.scan_parquet("scalars.parquet").describe()` to sanity-check.
- **Runs you intend to publish or archive.** Re-run if you can. A `sealed` bundle with no finalize warnings is the only state with a clean integrity story; anything less needs caveats in any downstream paper or report.

`manifest.custom["finalize_warnings"]` lists every file that needed recovery and any chunks that were dropped. Always read it on a recovered bundle:

```bash
python -c "import json,sys; print(json.load(open(sys.argv[1])).get('custom',{}).get('finalize_warnings'))" \
    runs/<run_id>/manifest.json
```

---

## Edge cases

**Finalize was never started.** `bundle_status` is `open`. The in-flight files exist; no `.parquet` files exist yet. `capa finalize` runs the full rewrite.

**Finalize completed but the process died before the integrity hash was written.** You may see `bundle_status="sealed"` with `manifest.sha256` missing, or a hand-authored/recovered `finalized_unverified` state. `capa finalize` is fast in this case — the rewrite is a no-op (everything is already final), and the hash-and-verify step runs.

**Disk filled up mid-rewrite.** Finalize fails with `FinalizeError`; the bundle is left at `finalizing` with a partial set of final parquets. Free space, re-run `capa finalize` — the rewrite is idempotent, so already-final files are skipped.

**The bundle's `runs_root` is on a network mount that's currently offline.** Finalize will fail at the `manifest.json` read. Mount the volume and retry; no on-disk state is changed.

---

*See also: [`capa finalize` CLI](../cli/capa-finalize.md), [Bundle layout](../bundles/what-is-a-bundle.md), [Integrity and sealing](../bundles/integrity-and-sealing.md), [Shutdown sequence](../safety/shutdown-sequence.md), [Saturation and deadlines](../safety/saturation-and-deadlines.md).*
