# capa catalog

**Audience:** anyone working with the run catalog — operators looking up a recent run, analysts walking a fleet of bundles, CI verifying bundle integrity.
**Scope:** sub-app for the on-disk run catalog (`runs.sqlite` at the runs root). Three commands: list, verify, rebuild.

```
$ capa catalog --help
Usage: capa catalog [OPTIONS] COMMAND [ARGS]...

  Manage the run catalog.

Commands:
  list     Print runs from ``runs.sqlite``.
  verify   Re-walk a bundle's artifacts and compare against ``manifest.sha256``.
  rebuild  Re-scan ``runs/`` and rebuild ``runs.sqlite`` from each manifest.
```

The catalog is an index — never the source of truth. Each bundle's `manifest.json` is authoritative; `runs.sqlite` is a queryable summary the GUI's Run-history pane and the CLI both read. `rebuild` reconstructs it from disk if it gets out of sync.

---

## `capa catalog list`

```
$ capa catalog list --help
Usage: capa catalog list [OPTIONS]

  Print runs from ``runs.sqlite``.

Options:
  --runs-root      PATH
  --run-status     TEXT
  --bundle-status  TEXT
  --since          [%Y-%m-%d|%Y-%m-%dT%H:%M:%S]
  --json           JSON-format output.
```

Prints the catalog as a table, with optional filters.

```bash
# Every run in the default runs root
uv run capa catalog list

# Only sealed completed runs from the last 30 days
uv run capa catalog list --run-status completed --bundle-status sealed --since 2026-04-24

# JSON output for downstream tooling
uv run capa catalog list --json > runs.json
```

### Filters

| Flag | What it filters on |
|---|---|
| `--run-status TEXT` | `running`, `completed`, `aborted`, `crashed` |
| `--bundle-status TEXT` | `open`, `sealed`, `verification_failed`, `crashed_but_sealed` |
| `--since YYYY-MM-DD` | `started_utc >= since`. Also accepts `YYYY-MM-DDTHH:MM:SS`. |

Filters are AND-combined. No filter = every row.

### Table output

```
$ uv run capa catalog list
run_id                                    run         bundle                  integrity   sample
20260524T143200-7f3a                      completed   sealed                  ok          PMMA-024
20260524T141055-3c19                      aborted     sealed                  ok          PMMA-023
20260523T223301-9ea1                      crashed     crashed_but_sealed      ok          PMMA-022
20260523T185940-0b88                      completed   verification_failed     mismatch    PMMA-021
```

`(no runs)` is printed when the filtered result is empty.

### JSON output

```
$ uv run capa catalog list --json
[
  {
    "run_id": "20260524T143200-7f3a",
    "started_utc": "2026-05-24T14:32:00",
    "ended_utc": "2026-05-24T14:38:11",
    "operator_id": "lab.gb",
    "sample_id": "PMMA-024",
    "procedure": "capa.builtin.free_run",
    "run_status": "completed",
    "bundle_status": "sealed",
    "integrity_status": "ok",
    "path": "/data/runs/20260524T143200-7f3a"
  },
  ...
]
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Listing returned (empty or not). |

---

## `capa catalog verify`

```
$ capa catalog verify --help
Usage: capa catalog verify [OPTIONS] [RUN_ID]

  Re-walk a bundle's artifacts and compare against ``manifest.sha256``.

Arguments:
  RUN_ID  Specific run id to verify; omit to use --all.

Options:
  --all         Verify every run.
  --runs-root   PATH
```

Walks the bundle's directory, recomputes the sha256 of every artifact, and compares against the checksums sealed into `manifest.sha256`. **This is the canonical bundle-integrity check.**

```bash
# Verify one run
uv run capa catalog verify 20260524T143200-7f3a

# Verify every run in the catalog
uv run capa catalog verify --all
```

### Output

```
$ uv run capa catalog verify 20260524T143200-7f3a
20260524T143200-7f3a: ok

$ uv run capa catalog verify --all
20260524T143200-7f3a: ok
20260524T141055-3c19: ok
20260523T185940-0b88: mismatch
  parquet: device_records/balance_sim.parquet
  parquet: scalars.parquet
```

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Every verified bundle returned `ok`. |
| 2 | The argument shape was wrong (e.g. `CatalogError` finding the run). |
| 3 | At least one bundle reported a mismatch or a per-run error. |
| 64 | Neither `RUN_ID` nor `--all` was supplied. |

**Recommended CI pattern** — nightly integrity sweep:

```bash
uv run capa catalog verify --all --runs-root /data/runs || {
    echo "INTEGRITY FAILURE — investigate" ; exit 3 ;
}
```

Exit 3 is the signal to stop the line. Treat it as severely as a `capa run` exit 3.

---

## `capa catalog rebuild`

```
$ capa catalog rebuild --help
Usage: capa catalog rebuild [OPTIONS]

  Re-scan ``runs/`` and rebuild ``runs.sqlite`` from each manifest.

Options:
  --runs-root  PATH
```

Drops the existing `runs.sqlite` index and re-indexes every bundle under the runs root by reading each `manifest.json`. The bundles themselves are not touched.

```bash
$ uv run capa catalog rebuild --runs-root /data/runs
rebuilt: 187 run(s) indexed at /data/runs/runs.sqlite
```

Use this when:

- You moved a runs root onto a new host and want the catalog rebuilt locally.
- The catalog got into a strange state (the sqlite was deleted, or some manual sqlite3 fiddling left it inconsistent).
- `capa finalize` reported `catalog update failed (bundle still sealed)` and you want the catalog re-synced.

Exit codes: 0 on success; any IO failure raises and prints the traceback.

---

## Roadmap

The earlier stub for this page listed `channels`, `bindings`, `steps`, `procedures`, `profiles` — those were filed under "catalog" by mistake; they belong to a future *registry-introspection* sub-app (probably `capa catalog show <kind>`). None of them exist today. The right place to look for "what channel kinds exist?" is currently the source code or the [glossary](../glossary.md).

---

## See also

- [`capa finalize`](capa-finalize.md) — seal a bundle so it can be cataloged
- [Integrity and sealing](../bundles/integrity-and-sealing.md) — the protocol behind `manifest.sha256`
- [Headless runs](headless-runs.md) — where `bundle_status` and `integrity_status` get set
- [Reviewing a run](../user-guide/reviewing-a-run.md) — operator-facing equivalent
