---
description: capa bundle sealing protocol — bundle_status state machine, two-stage finalize, manifest.sha256 integrity verdict, crashed_but_sealed recovery semantics.
---

# Integrity and sealing

**Audience:** anyone debugging "is this bundle complete?" — operators triaging an `open` or `verification_failed` bundle, tool authors confirming a bundle is safe to copy, plugin authors writing finalize-aware code.
**Scope:** the sealing protocol, the two-stage finalize, the `bundle_status` state machine, the `integrity` verdict, and what `crashed_but_sealed` actually means.

A bundle is **sealed** when `manifest.sha256` exists at the bundle root. That single file is the operator-facing signal: if it's present, the rest of the bundle has been hashed and the manifest itself is the last byte-stable record of what was written. The file is `sha256sum`-compatible — five years from now, a researcher with no `capa` install can run `sha256sum -c manifest.sha256` and get the same verdict the tool would give. Until that file lands, the bundle is some flavor of in-progress, and the [`bundle_status`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/manifest.py) field in `manifest.json` says which flavor.

---

## The bundle_status state machine

`BundleStatus` is the five-value enum that tracks where the bundle is in its lifecycle. It is deliberately independent of `run_status` (the scientific outcome); a `crashed` run can produce a fully `sealed` bundle, and a `completed` run can land in `verification_failed` if something corrupted the bytes between rewrite and hash.

| `bundle_status` | Means | When you see it |
|---|---|---|
| `open` | Sinks may still be mid-write. In-flight Arrow IPC streams (`*.in-flight.arrows`) are live on disk. | Acquisition is active, or the process exited before finalize ran. |
| `finalizing` | Sinks closed, two-stage rewrite is in progress. | A reader catching the bundle mid-rewrite; should be transient (seconds to a minute). |
| `finalized_unverified` | Data files are readable, but `manifest.sha256` has not been written yet. | Legal recovery state for data-complete bundles that still lack a digest. The normal finalize path writes `sealed` before creating `manifest.sha256`, so it does not pause here. |
| `sealed` | `manifest.sha256` present and verified. Safe to copy, archive, or hand to an analysis tool. | The normal terminal state. |
| `verification_failed` | The bundle finished finalizing, but the integrity walk found a mismatch (or threw). Data may still be inspectable; the seal is not trustworthy. | After a finalize where hashing succeeded but the verdict was not `ok`, or where the hash step itself raised. |

Stamps happen in a fixed order inside [`finalize_in_place`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/finalize.py):
`open` → `finalizing` (written before the rewrite begins) → `sealed` (written atomically *before* the hash file, alongside `integrity.status="ok"`) → either left at `sealed` after a clean `verify()` or rolled back to `verification_failed` if `verify()` returns a non-`ok` status. The intermediate `finalized_unverified` value exists in the enum for crash-recovery scenarios where a bundle is found data-complete but unhashed; the normal path doesn't dwell there.

---

## The two-stage finalize

Finalize is two distinct stages with very different failure semantics.

### Stage 1 — rewrite

[`_rewrite_inflight_to_parquet`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/finalize.py) reads each `*.in-flight.arrows` (an Arrow IPC stream — recoverable in the face of a truncated tail) and writes a final Parquet next to it. Three things happen during the rewrite:

1. **Sort by `t_mono_ns`** when that column is present. Channel-samples and device-records files always carry it; the result is order-stable so two finalize runs on the same in-flight files produce byte-identical Parquet.
2. **Large row groups** — `FINAL_ROW_GROUP_ROWS = 262_144` (256K rows). Tuned for DuckDB / Polars / Arrow scan throughput.
3. **`zstd:6` compression** with `data_page_version="2.0"`. Same defaults across every artifact.

The write itself uses the same `<path>.tmp` + rename pattern the manifest uses. A reader following the directory mid-rewrite never sees a half-written `.parquet`.

Torn in-flight files (truncated before the IPC stream's first flush boundary) are unrecoverable. When that happens the rewrite logs an entry into `manifest.custom["finalize_warnings"]` and unlinks the unreadable file. The bundle proceeds — one missing artifact does not block sealing the rest.

Before and after, with a one-camera one-Watlow run:

```
# before finalize (bundle_status="open")
runs/20260524-101500-abc/
├── manifest.json
├── scalars.in-flight.arrows
├── device_records/
│   └── watlow.in-flight.arrows
└── video/
    ├── webcam0.mkv
    └── webcam0.frames.in-flight.arrows

# after finalize (bundle_status="sealed")
runs/20260524-101500-abc/
├── manifest.json
├── manifest.sha256
├── scalars.parquet
├── device_records/
│   └── watlow.parquet
└── video/
    ├── webcam0.mkv
    └── webcam0.frames.parquet
```

The container files themselves (`.mkv` for visible cameras, `.csq` for IR) are written directly by the camera adapter and need no rewrite — they pass straight through finalize.

### Stage 2 — verification

After the rewrite, [`write_manifest_sha256`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/integrity.py) walks every regular file under the bundle root (sorted by relative POSIX path), streams each through `sha256` in 64 KiB chunks, and emits the `sha256sum`-format lines:

```
<64-hex>  <relative/posix/path>
```

`manifest.sha256` excludes itself (it cannot describe its own hash) and any leftover `*.in-flight.*` files (none should exist after a clean rewrite; the exclusion is a belt-and-braces safety net). Then [`verify`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/integrity.py) re-walks the bundle and compares. If the verdict is anything other than `ok`, `bundle_status` is rolled back to `verification_failed` and the verdict is written into the manifest's `integrity.status` field.

---

## Atomic manifest writes

Every write of `manifest.json` and `manifest.sha256` goes through the same two-step dance: write to `<path>.tmp`, then `tmp.replace(target)`. On any modern filesystem the rename is atomic, which means a concurrent reader sees either the old contents or the new contents — never a half-written file.

This matters more than it looks. The catalog, the `capa finalize` recovery CLI, and the UI's run-completion watcher all read the manifest opportunistically without taking a lock. If those readers could observe a truncated JSON document, every consumer would need to either retry-on-parse-fail (clunky) or take a flock (cross-platform pain). The atomic-rename invariant means readers can be one-shot and naive, and the writer is the only side that carries any complexity.

The same invariant covers the rewrite stage: each `.parquet` lands via `.parquet.tmp` + rename, and `manifest.sha256` lands via `manifest.sha256.tmp` + rename. There is no point during finalize when a reader sees a partial file at a final name.

---

## Integrity verdict states

`IntegrityStatus` is recorded in `manifest.integrity.status` and produced by `verify()` as it walks the bundle.

| `integrity.status` | What triggers it |
|---|---|
| `unknown` | The default for a fresh manifest, and the value finalize sets if the hashing step itself raised (disk full, race with an external process). |
| `ok` | Every file recorded in `manifest.sha256` exists on disk and its recomputed digest matches. No extra files. |
| `mismatch` | At least one file's recorded digest disagrees with its recomputed digest. The bytes have changed since the seal — bit-rot, tampering, or a botched copy. |
| `partial` | Files recorded in `manifest.sha256` are missing, or files under the bundle root are not recorded in `manifest.sha256`, but every file that exists *and* is recorded matches. |

`mismatch` is always more serious than `partial`. A bundle that comes back
`partial` may still have trustworthy artifacts for the files that remain, but
the bundle directory is no longer byte-complete relative to its seal. External
camera containers named by `CameraEntry.output_path_external` are outside the
bundle-root integrity walk; verify those paths separately when archiving.

---

## Legal status combinations

`run_status` and `bundle_status` evolve independently. The only impossible combination is enforced by [`is_legal_finalize_combination`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/manifest.py): the bundle cannot transition past `open` while the run is still `running`.

| `run_status` | `bundle_status` | Legal? |
|---|---|---|
| `running` | `open` | yes — the only state during acquisition |
| `running` | anything else | **no** — caught by `is_legal_finalize_combination` |
| `completed` / `aborted` / `crashed` | `open` | yes — process exited before finalize ran; recoverable via `capa finalize` |
| `completed` / `aborted` / `crashed` | `finalizing` | yes — observed mid-rewrite |
| `completed` / `aborted` / `crashed` | `finalized_unverified` | yes — data-complete but not sealed; possible after interrupted or hand-authored recovery, not a normal-path pause |
| `completed` / `aborted` / `crashed` | `sealed` | yes — the normal terminal pairing |
| `completed` / `aborted` / `crashed` | `verification_failed` | yes — bundle was inspectable but the seal failed |

The "running can only coexist with open" rule exists because everything past `open` involves either closing sinks or rewriting files. While the run is still emitting samples, neither is safe. `finalize_in_place` re-checks `is_legal_finalize_combination` before stamping `sealed` and raises `FinalizeError` if the caller hands it an impossible pair.

---

## `crashed_but_sealed` — the special case

`crashed_but_sealed` is a [`RunOutcome`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/conductor.py) value, not a `run_status` value. It maps to `run_status="crashed"` in the manifest (via [`run_status_for_outcome`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/outcomes.py)) but tells a more specific story: the [saturation deadline](../safety/saturation-and-deadlines.md) tripped, and the conductor responded by orchestrating a *clean* shutdown anyway.

The sequence inside [`_on_saturated`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/conductor.py):

1. Log `conductor.saturation_escalation` with the trip reason.
2. Set `_outcome = RunOutcome.CRASHED_BUT_SEALED` and record the trip reason as `_exit_reason`.
3. Best-effort `writer.write_event(kind="saturation_deadline", …)` into the bundle. Best-effort because the writer itself may be the wedged component.
4. Set the completion event. From here the normal shutdown path takes over: the procedure unwinds, every adapter's `stop()` runs, the pool disarms, and `finalize_in_place` is called the same way it would be on a clean exit.

The resulting bundle is **fully sealed**: in-flight rewrites complete, `manifest.sha256` lands, `bundle_status="sealed"`, `integrity.status="ok"`. The manifest records `run_status="crashed"` with `exit_reason` describing the saturation trip, and a `saturation_deadline` event row lives in `events.sqlite`.

This differs from a bare `RunOutcome.CRASHED` in one important way. A generic crash (unhandled exception in the procedure, drain task, or pool) usually still seals — the conductor's `finally` blocks call finalize the same way — but the failure may have happened in a way that left in-flight files torn, or the manifest write itself may have failed. A `crashed_but_sealed` bundle, by contrast, is the *controlled* response to a specific failure class. The conductor knew the writer might be wedged, gave the shutdown sequence its full run, and produced an artifact you can read with the same confidence as a `completed` run.

If you find a bundle on disk with `run_status="crashed"` and `bundle_status="open"`, that is the bare-crash case: the process died before finalize. Run [`capa finalize`](../cli/capa-finalize.md) against it.

---

## Recovery: `capa finalize`

When a bundle is left at `bundle_status="open"` or `bundle_status="finalizing"` after a process crash, the [`capa finalize`](../cli/capa-finalize.md) CLI is the recovery path. It reads the manifest, infers `ended_utc` only when the manifest lacks one, preserves terminal statuses such as `completed` and `aborted`, maps `running` / `crashed` to `crashed`, and calls `finalize_in_place(...)`. It exits non-zero for malformed bundles or `FinalizeError`; a `verification_failed` result is printed but still exits zero because the bundle reached a stable, inspectable state. It needs no live runtime — no devices, no Watlow, nothing armed — because finalize is a pure function over the bundle directory.

See [`capa finalize`](../cli/capa-finalize.md) for the full CLI surface, exit codes, and the operator-triage flow.

---

## Idempotency

Re-running finalize on a bundle is safe. If the bundle is already `sealed`, the rewrite stage finds no `*.in-flight.arrows` files (they were unlinked the first time), the manifest's `data_shape`, `bundle_status`, and `integrity` are re-stamped to the same values, and the hash walk re-verifies. The result is a no-op modulo the manifest's serialized bytes (which are deterministic per the Pydantic config).

If `manifest.sha256` is intact and the bundle has not been tampered with, the post-finalize integrity verdict will still be `ok`. If something has changed on disk since the original seal, the second run will downgrade the verdict accordingly — re-running finalize is also the way you *re-verify* a bundle without an external tool.

---

## Implementation notes for contributors

A few details that aren't obvious from the public API:

- **Finalize is a pure function over the bundle directory.** [`finalize_in_place`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/finalize.py) takes a `Path` and a `RunStatus` and nothing else from the runtime — no pool reference, no conductor handle, no live device. This is what makes `capa finalize` work as a separate process against a bundle written by a process that no longer exists.
- **The rewrite is order-stable.** Sort by `t_mono_ns` is applied unconditionally when the column is present, so two finalize runs on the same `*.in-flight.arrows` produce byte-identical `.parquet` (modulo timestamps the Parquet writer itself doesn't control — there are no such timestamps in the current writer settings, so in practice the bytes do match).
- **`IntegrityStatus.partial` only covers the bundle root.** `verify()` walks regular files under the bundle directory and compares them with `manifest.sha256`. A missing external camera container is an archival-completeness issue, but it will not by itself produce `partial` because `output_path_external` lives outside the seal.
- **The sealing handshake writes `manifest.json` *last*, then `manifest.sha256`.** Stage 3 of `finalize_in_place` constructs the *target* manifest (with `bundle_status="sealed"`, `integrity.status="ok"`) and writes it before the hash file. The hash file is then computed over the full bundle including that final manifest. The chicken-and-egg ("the manifest claims to be sealed before the seal exists") resolves cleanly because the hash is computed over the bytes that already include the `sealed` claim — there is no second manifest write after hashing.
- **Schema drift on the in-flight file is not finalize's problem.** The sinks raise `SchemaDriftError` at write time. By the time finalize runs, the file on disk either has a coherent schema or is torn — finalize handles the torn case by logging a finalize-warning and removing the file.
- **`_iter_artifact_files` skips symlinks.** Bundles are self-contained directories by convention. If you symlink something into a bundle and expect it to be sealed, it won't be.

---

## See also

- [What's in a bundle](what-is-a-bundle.md) — directory tour
- [Manifest and schema](manifest-and-schema.md) — every key in `manifest.json`
- [Bundle versioning](bundle-versioning.md) — schema-bump policy
- [Saturation and deadlines](../safety/saturation-and-deadlines.md) — where `crashed_but_sealed` comes from
- [capa finalize](../cli/capa-finalize.md) — the recovery CLI
- [Crash recovery](../troubleshooting/crash-recovery.md) — operator triage
