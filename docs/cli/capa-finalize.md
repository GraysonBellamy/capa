# capa finalize

**Audience:** anyone recovering a partial bundle — operators after a power loss, CI after a runner crash, anyone whose `capa run` did not exit cleanly.
**Scope:** re-seal a bundle left open after a crash or interrupted shutdown. Idempotent and safe to run on already-sealed bundles.

```
$ capa finalize --help
Usage: capa finalize [OPTIONS] RUN_ID

  Finalize an open or crashed bundle.

  Idempotent: rewrite in-flight Arrow IPC streams, compute checksums, set
  ``ended_utc`` if absent, progress ``bundle_status`` to ``sealed``
  (or ``verification_failed``). Safe to run on already-sealed bundles.

Arguments:
  RUN_ID  Run id (the bundle directory name). [required]

Options:
  --runs-root  PATH
  --help       Show this message and exit.
```

---

## What it does

Given a run id (the bundle directory name — *not* a path), the command:

1. Resolves the bundle path: `<runs-root>/<run_id>/`.
2. Loads `manifest.json`. If it does not parse, exits with code 2.
3. Decides the target `run_status`:
   - `completed` stays `completed` (re-running on a sealed bundle is a no-op).
   - `running` or `crashed` becomes `crashed`.
   - Anything else passes through.
4. If `manifest.ended_utc` is missing, marks it as `inferred_ended_utc=True` and uses the file system mtime of the last touched artifact.
5. Calls [`finalize_in_place`](../../src/capa/storage/finalize.py): rewrites any in-flight Arrow IPC streams into final Parquet files, recomputes `manifest.sha256` against every artifact on disk, and writes the final manifest.
6. Re-inserts the run into the catalog (`runs.sqlite`) — upserting the operator, inserting the open-row, and updating the finalize-row. If the catalog is locked or otherwise unhappy, the bundle still seals; only the catalog update is skipped (with a yellow warning).

The argument is a **run id** (directory name like `20260524T143200-7f3a`), not a path. The bundle is resolved via `--runs-root` / `$CAPA_RUNS_ROOT` / `./runs`. This mirrors how the rest of the CLI thinks about runs.

---

## What finalize can recover

| Artifact | Recoverable? | Why |
|---|---|---|
| Channel-sample rows flushed to Arrow IPC | Yes | `finalize_in_place` rewrites in-flight rows into a closed Parquet file. |
| Device-record rows flushed to Arrow IPC | Yes | Same path. |
| `events.sqlite` | Yes | SQLite is already durable; no rewrite needed. |
| Video files | Yes | Same containers, sealed in place. |
| Manifest with checksums | Yes | Recomputed from what is actually on disk. |
| In-flight buffers that never flushed | **No** | They are gone. Anything the writer held at the moment of crash is lost. |

The result is a bundle with `run_status="crashed"` when recovery started from
a live/crashed manifest, `bundle_status="sealed"` when integrity passes, and
`integrity.status="ok"` against whatever the writer actually durably wrote.
The runtime-level outcome name `crashed_but_sealed` is not a manifest enum.

---

## Synopsis

```bash
# Most common: finalize the bundle from the last failed run
uv run capa finalize 20260524T143200-7f3a

# With a custom runs root
uv run capa finalize 20260524T143200-7f3a --runs-root /data/runs

# Idempotent — re-running on an already-sealed bundle is safe
uv run capa finalize 20260524T143200-7f3a
```

---

## Output

```
$ uv run capa finalize 20260524T143200-7f3a --runs-root /data/runs
finalized: 20260524T143200-7f3a
  rewrote:  3 file(s)
  skipped:  2 already-final file(s)
  integrity: ok
```

- `rewrote:` counts artifacts that had in-flight Arrow IPC streams finalized.
- `skipped:` counts artifacts that were already in their final form.
- `integrity:` is the post-finalize verification status: `ok`, `partial`, or `mismatch`.

If the catalog update step fails (lock contention, sqlite I/O error), the message
`finalize: catalog update failed (bundle still sealed): …` is printed in yellow. The bundle is still sealed — you may need a [`capa catalog rebuild`](capa-catalog.md) afterwards.

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Bundle was finalized (or was already final). This includes stable `verification_failed` bundles. |
| 2 | Bundle directory missing, or `manifest.json` is malformed beyond repair. |
| 3 | `FinalizeError` raised during the rewrite — for example, a Parquet file was truncated below the metadata footer. |

A finalize that prints `integrity: partial` or `integrity: mismatch` exits
**0**, not 3 — the bundle reached a stable state, even if it is not
trustworthy. The next `capa catalog verify` will surface the mismatch.

---

## When `capa finalize` is the right move

- A `capa run` was force-killed (`kill -9`, OOM-killer, machine reboot).
- A second SIGINT during graceful shutdown terminated the writer before it could seal.
- The saturation monitor tripped and the run sealed as `crashed_but_sealed` — but the bundle is *already* finalized in this case; re-running is harmless.
- Power loss on the host, or the runs-root NFS share dropped mid-write.

It is *not* the move for:

- A `verification_failed` bundle. Finalize will not fix bad checksums; investigate the storage substrate.
- Bundles whose in-flight Arrow IPC stream is too corrupt to recover. Finalize bails with exit 3.

---

## See also

- [Headless runs](headless-runs.md) — how a bundle reaches the state finalize repairs
- [`capa catalog verify`](capa-catalog.md) — re-check integrity after finalize
- [`capa catalog rebuild`](capa-catalog.md) — re-scan all bundles into the catalog
- [Crash recovery](../troubleshooting/crash-recovery.md) — end-to-end recovery walkthrough
- [Integrity and sealing](../bundles/integrity-and-sealing.md) — the sealing protocol
