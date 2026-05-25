# Reading event logs

**Audience:** analysts and contributors reconstructing what happened during a past run.
**Scope:** the two log surfaces in every sealed [bundle](../bundles/what-is-a-bundle.md) — `events.sqlite` (transactional event log) and `run.log` (structlog JSON Lines) — and how to query them.

A capa run produces two complementary record streams:

- **`events.sqlite`** — a SQLite database of milestone events: segment transitions, operator notes, alarms, errors, adapter command receipts, saturation trips. Authored deliberately by the engine, procedures, and adapters. Crash-safe (WAL + `synchronous=NORMAL`).
- **`run.log`** — a JSON Lines file of every structlog call from the runtime. Higher volume; lower curation. Includes startup, shutdown, internal warnings, and the full structured record of anything that called `_logger.info(...)`.

For a typical post-mortem, start with `events.sqlite` (the deliberate signal), then fall back to `run.log` (the raw stream) when you need more detail than the curated event captured.

---

## `events.sqlite` — schema

Every row in the `events` table answers the same shape of question: *what happened at this time, and what should I know about it?*

```sql
CREATE TABLE events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    t_mono_ns     INTEGER NOT NULL,    -- monotonic ns since run start (join key)
    t_utc         TEXT    NOT NULL,    -- ISO-8601 UTC wall clock
    kind          TEXT    NOT NULL,    -- dotted enum (see taxonomy below)
    severity      TEXT    NOT NULL,    -- 'info' | 'warning' | 'error'
    source        TEXT    NOT NULL,    -- 'watlow:heater', 'procedure:capa.recipe_runner', ...
    message       TEXT    NOT NULL,    -- free-form one-liner
    metadata_json TEXT                 -- JSON-encoded open set
);
CREATE INDEX idx_events_t_mono_ns ON events (t_mono_ns);
CREATE INDEX idx_events_kind      ON events (kind);
```

`t_mono_ns` is the run's monotonic timestamp from the `RunClock`; this is the column you join against the [parquet sample files](../bundles/parquet-channel-samples.md). `t_utc` is wall-clock and may drift, jump, or skew if the host clock is adjusted mid-run — never use it as a join key.

Schema and writer in [`events_sink.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/events_sink.py).

### Event taxonomy

The runtime, procedures, and adapters all write into this table. Today's `kind` values, grouped by origin:

| Origin | `kind` values |
|---|---|
| **Method executor** | `method.step.entered`, `method.step.exited`, `method.step.failed`, `method.prompt.shown`, `method.prompt.acknowledged`, `method.prompt.unanswered`, `method.command.issued`, `method.wait.timeout` |
| **Procedures (builtin)** | `free_run.started`, `free_run.ended`, `batch.started`, `batch.child.started`, `batch.child.ended`, `batch.ended`, `heat_flux_tune.started`, `heat_flux_tune.iteration`, `heat_flux_tune.holding`, `heat_flux_tune.target_accepted`, `heat_flux_tune.aborted`, `heat_flux_tune.completed`, `heat_flux_tune.operator_command`, `heat_flux_tune.command.issued` |
| **Procedure preflight** | `profile.preflight.warning`, `procedure.preflight.warning` |
| **Conductor (saturation)** | `saturation_deadline` |
| **Worker / adapter** | `worker_adapter_error`; camera events prefixed `camera.*` (`camera.recording_started`, `camera.recording_stopped`, `camera.nuc_triggered`) |
| **Adapter command receipts** | One per dispatched command — `set_setpoint`, `tare`, `zero`, `set_gas`, `hold_valves`, `cancel_valve_hold`, `set_filter_mode`, etc. (full list in the per-device pages) |

The taxonomy is open — plugin procedures and custom adapters add their own `kind` values. Treat unknown kinds as informational unless the row's `severity` says otherwise.

### Common queries

Open the database with any SQLite client (`sqlite3` CLI, DB Browser, [DuckDB](https://duckdb.org/)):

```sql
-- Everything that went wrong
SELECT t_utc, severity, kind, source, message
FROM events
WHERE severity IN ('warning', 'error')
ORDER BY t_mono_ns;

-- Was the saturation deadline tripped?
SELECT t_utc, message, metadata_json
FROM events
WHERE kind = 'saturation_deadline';

-- Walk the procedure timeline
SELECT t_utc, kind, message
FROM events
WHERE kind LIKE 'method.step.%' OR kind LIKE '%.started' OR kind LIKE '%.ended'
ORDER BY t_mono_ns;

-- All adapter commands the operator issued during the run
SELECT t_utc, source, kind, message, metadata_json
FROM events
WHERE source LIKE '%:%'  -- adapter:device convention
  AND kind NOT LIKE 'method.%'
  AND kind NOT LIKE 'camera.%'
ORDER BY t_mono_ns;

-- Decode metadata into columns (SQLite ≥ 3.38, or via DuckDB)
SELECT t_utc, kind,
       json_extract(metadata_json, '$.resource_id') AS rid,
       json_extract(metadata_json, '$.blocked_s')   AS blocked_s
FROM events
WHERE kind = 'saturation_deadline';
```

### Cross-referencing with samples

`t_mono_ns` is the same monotonic clock that stamps every row in `scalars.parquet` and every per-device records parquet. Find the channel values around an alarm:

```python
import polars as pl
import sqlite3

# Anchor on the first error event
with sqlite3.connect("events.sqlite") as conn:
    anchor = conn.execute(
        "SELECT t_mono_ns FROM events WHERE severity='error' "
        "ORDER BY t_mono_ns LIMIT 1"
    ).fetchone()
t0 = anchor[0]

# Pull a ±10 s window of samples around it
samples = (
    pl.scan_parquet("scalars.parquet")
    .filter(pl.col("t_mono_ns").is_between(t0 - 10_000_000_000, t0 + 10_000_000_000))
    .collect()
)
```

See [Reading bundles](../bundles/reading-bundles.md) for the full sample-side schema.

---

## `run.log` — structlog JSON Lines

Every line is one self-contained JSON object — same schema as a structlog dict. Stable fields:

| Field | Meaning |
|---|---|
| `timestamp` | ISO-8601 UTC. Wall-clock; not monotonic. |
| `level` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. |
| `event` | The structlog event name, e.g. `worker.stream.exit`, `saturation_monitor.deadline_exceeded`, or `conductor.saturation_escalation`. |
| `run_id` | Bound at run start; present on every line that originated under the run's context-var scope. |
| `procedure_id`, `step_id`, `operator_id` | Bound as the run progresses. May be absent on lines that fired outside a step. |
| (everything else) | Free-form kwargs from the call site. |

Configuration: [`capa/core/logging.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/logging.py). On-disk sink: [`storage/log_sink.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/log_sink.py).

### Common recipes

```bash
# Every error and warning, pretty
jq -c 'select(.level | IN("WARNING","ERROR"))' run.log

# Saturation escalation details
jq -c 'select(.event | IN("saturation_monitor.deadline_exceeded","conductor.saturation_escalation"))' run.log

# One worker's lifecycle, top-to-bottom
jq -c 'select(.event | startswith("worker.")) | select(.resource_id == "serial:COM6")' run.log

# Aggregate event-name counts (sanity check that nothing is firing in a loop)
jq -r '.event' run.log | sort | uniq -c | sort -rn | head -20
```

The file is line-buffered and never fsync'd per-line, so the very last entries before a hard crash may not be on disk. The bundle writer flushes at finalize; a sealed bundle's `run.log` is complete.

### When the log lies

Three failure modes worth knowing about:

1. **Clock skew on `timestamp`.** `timestamp` is wall-clock UTC. If the host clock is adjusted mid-run (NTP step, manual change), timestamps will jump. The `t_mono_ns` on `events.sqlite` rows is immune; cross-reference there if order matters.
2. **Pre-run lines aren't here.** Anything logged before the bundle was opened — config errors, plugin discovery failures, adapter `open()` failures during pool start — lands in `~/.capa/logs/capa-YYYYMMDD.log` instead, *not* in the bundle's `run.log`. Check both files when investigating a run that failed before sampling started.
3. **Final-shutdown line may be missing or truncated.** If the engine exits ungracefully (SIGKILL, power loss, hard panic), the line-buffered writer may not have flushed its final lines. Use `events.sqlite` as the authoritative end-of-run record — every meaningful end-of-run condition that *should* be observable also lands as an event row, and SQLite's WAL gives it stronger durability than line-buffered text.

---

## When to read which

| You want to know… | Start in |
|---|---|
| What the procedure was doing at time T | `events.sqlite` (`method.step.*`, procedure-specific events) |
| Why a run was sealed `crashed_but_sealed` | `events.sqlite` (`kind = 'saturation_deadline'`), then `run.log` (`saturation_monitor.deadline_exceeded`, `conductor.saturation_escalation`) |
| Which adapter raised what | `events.sqlite` (`kind = 'worker_adapter_error'`), then `run.log` filtered to that `resource_id` |
| Whether a manual command actually reached the device | `events.sqlite` (adapter command kinds, `method.command.issued`) |
| Detailed internal state at an exact moment | `run.log` |
| Anything that happened before the bundle existed | `~/.capa/logs/capa-YYYYMMDD.log` |

---

*See also: [events.sqlite reference](../bundles/events-sqlite.md), [Bundle layout](../bundles/what-is-a-bundle.md), [Status-bar symptoms](status-bar-symptoms.md).*
