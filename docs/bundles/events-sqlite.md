# Events and status (sqlite)

**Audience:** analysts reconstructing what happened during a run; operators triaging a `crashed_but_sealed` outcome; plugin authors deciding what their adapter should log.
**Scope:** the two SQLite databases in a bundle — `events.sqlite` (the run's event log) and `status.sqlite` (periodic health snapshots) — their schemas, the event taxonomy, and recipes for reading them.

A sealed bundle ships **two** SQLite databases, deliberately kept separate. [`events.sqlite`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/events_sink.py) holds the transactional event log: adapter commands, procedure milestones, safety trips, operator notes — every discrete thing worth remembering after the run. [`status.sqlite`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/status_sink.py) holds the 1 Hz [`DeviceSnapshot`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/records.py) heartbeat — connection state, comm-error counters, firmware version, alarm bits. The separation is structural rather than aesthetic: a noisy 1 Hz health stream from five adapters produces tens of thousands of rows per hour, and a combined table would drown the operator-relevant event view every time you opened it. By keeping the two streams in different files, `SELECT * FROM events ORDER BY t_mono_ns` stays useful no matter how chatty the rig is.

---

## `events.sqlite` — schema

The DDL is defined verbatim in [`events_sink.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/events_sink.py):

```sql
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    t_mono_ns    INTEGER NOT NULL,
    t_utc        TEXT    NOT NULL,
    kind         TEXT    NOT NULL,
    severity     TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    message      TEXT    NOT NULL,
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_t_mono_ns ON events (t_mono_ns);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events (kind);
```

There is exactly **one** table, named `events`. Every row — adapter command, procedure milestone, safety trip, preflight warning — lives in the same table, distinguished by the `kind` and `source` columns. (An earlier stub of this doc promised separate `method_steps`, `manual_commands`, and `safety` tables; they were never built. Filter by `kind` instead.)

| Column | Type | Notes |
|---|---|---|
| `id` | `INTEGER PK AUTOINCREMENT` | Row id. Useful only as an insertion-order tiebreaker — `t_mono_ns` is the real time key. |
| `t_mono_ns` | `INTEGER` | Monotonic-clock nanoseconds at emission. **This is the join key** for cross-referencing against [`scalars.parquet`](parquet-channel-samples.md) and the per-adapter [device records](parquet-device-records.md). Indexed. |
| `t_utc` | `TEXT` | ISO-8601 UTC timestamp at emission. For human display only; not monotonic across NTP corrections. |
| `kind` | `TEXT` | Open-string event discriminator (see [taxonomy](#event-kind-taxonomy) below). Indexed. |
| `severity` | `TEXT` | One of `info`, `warning`, `error` — enforced at write time by `ALLOWED_SEVERITIES` in `events_sink.py`. |
| `source` | `TEXT` | Free-form producer identifier. Adapter events use `"<adapter>:<device>"` (e.g. `"watlow:heater"`); procedure events use `"procedure:<name>"`; conductor-level events use `"engine"`; operator notes use `"operator"`. |
| `message` | `TEXT` | Human-readable summary. **Keep typed data in `metadata_json`**, not here. |
| `metadata_json` | `TEXT` | JSON-encoded `dict[str, Any]` (or `NULL`). Free-form per-`kind` payload — readers should `json.loads(row["metadata_json"])`. |

Two indexes ship by default: `idx_events_t_mono_ns` (for timeline scans) and `idx_events_kind` (for `WHERE kind = ...` filters). Nothing else is indexed; if you need `WHERE source = ...` to be fast on a large bundle, add an index in your reader copy.

---

## The two write paths

`EventsSink` exposes two entry points. Most callers should use one or the other, not invent a third:

- **`EventsSink.write_device_event(event: DeviceEvent)`** — the typed path. Adapters emit [`DeviceEvent`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/records.py) records through their normal emission stream, and the runtime fans them into this method. The wrapper sets `source = f"{event.adapter}:{event.device}"` automatically, so adapter authors don't have to name themselves.
- **`EventsSink.write(*, kind, message, severity, source, t_mono_ns, t_utc, metadata)`** — the generic path. Used by the conductor (`source="engine"`), the procedure runner (`source="procedure:..."`), the safety layer, and operator-note hooks. Callers are responsible for picking a meaningful `source`.

Both paths funnel into the same `INSERT INTO events ...` statement, so there is no schema split between them — `write_device_event` is sugar.

**Durability.** The connection runs with `journal_mode=WAL` and `synchronous=NORMAL`, and `isolation_level=None` (autocommit). Every `write()` commits before returning. That is deliberate: losing an event row to a crash is the worst possible outcome for a forensic log. Throughput is fine for CAPA's 3–60 Hz emission envelope (events are sparse — adapter commands, procedure transitions — not per-sample), and the WAL keeps a power-loss read consistent.

From the producer's perspective the call is fire-and-forget — `write_event` from the runtime context is awaited but the cost is a single SQLite insert. The actual serialization happens on the writer thread; concurrent `write_event` calls from the procedure and an adapter cannot interleave because the connection uses `check_same_thread=False` with a single owning writer task.

**A third "path" worth mentioning** is the runtime's `RunContext.writer.write_event(...)` helper. That is what the conductor and procedures actually call — it forwards into `EventsSink.write(...)` with `source` and `t_mono_ns` / `t_utc` pre-filled from the run context. Adapter authors don't need to touch it; their `DeviceEvent` emissions are routed through `write_device_event` automatically by the worker drain.

---

## Event-kind taxonomy

The `kind` column is an **open string**, not a database enum — anyone with a writer handle can introduce a new kind. The following table catalogs every kind currently produced in-tree, grouped by producer. New kinds should follow the dotted-namespace convention (`producer.event` or `producer.sub.event`) so the timeline stays grep-friendly.

| Kind | Producer | When fired | Typical metadata |
|---|---|---|---|
| `saturation_deadline` | conductor (`engine`) | Saturation monitor escalated — see [Saturation and deadlines](../safety/saturation-and-deadlines.md). | `reason`, `details` (`resource_id`, `blocked_s`, `deadline_s`, or `depth` + `since_last_accept_s`) |
| `worker_adapter_error` | worker (`engine`) | An adapter's streaming loop raised; the worker marked itself fatal and exited. | `resource_id`, `adapter`, `error_type` |
| `profile.preflight.warning` | procedure runner (`profile`) | A non-blocking problem from a domain-profile preflight check. | `code`, `message`, profile-specific keys |
| `procedure.preflight.warning` | procedure runner (`procedure`) | A non-blocking problem from the procedure's own preflight. | `code`, `message` |
| `free_run.started`, `free_run.ended` | `procedure:free_run` | Bracket the [Free Run](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/free_run.py) lifetime. | run parameters, end reason |
| `batch.started`, `batch.child.started`, `batch.child.ended`, `batch.ended` | `procedure:batch` | Outer + per-child brackets for the [Batch](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/batch.py) procedure. | `child_index`, `child_name`, totals at `batch.ended` |
| `heat_flux_tune.started` / `.holding` / `.iteration` / `.target_accepted` / `.completed` / `.aborted` / `.operator_command` / `.command.issued` | `procedure:heat_flux_tune` | State transitions, loop iterations, operator interventions, and adapter commands inside the [heat-flux tuning controller](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/heat_flux_tune/controller.py). | `target_kw_m2`, `measured_kw_m2`, `setpoint_c`, iteration index, etc. |
| `method.step.entered`, `method.step.exited`, `method.step.failed` | `procedure:method` | Per-step brackets emitted by the [method executor](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/executor.py). | `step_index`, `step_kind`, `target`, exception text on `.failed` |
| `method.prompt.shown`, `method.prompt.acknowledged`, `method.prompt.unanswered` | `procedure:method` | Operator-prompt lifecycle (waiting for an acknowledgement, timed out). | `prompt_id`, `text`, `timeout_s` |
| `method.command.issued`, `method.wait.timeout` | `procedure:method` | Method-level adapter command dispatched / a `wait` step expired. | `target`, `value`, `unit`, `timeout_s` |
| `set_setpoint`, `hold`, `ramp`, `safe_shutdown` | `procedure:method` (via `_command_setpoint(kind=…)`) | The four shapes a setpoint command can take from a method step. | `target`, `value`, `unit` |
| `set_setpoint`, `write_parameter`, `set_display_units`, `unit_mismatch` | `watlow:<device>` | Setpoint / EEPROM-parameter writes and the unit-sync diagnostic from the [Watlow adapter](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/watlow.py). | `register`, `value`, `units` |
| `set_setpoint`, `set_gas`, `set_units`, `tare_flow`, `hold_valves`, `hold_valves_closed`, `cancel_valve_hold`, `totalizer_reset`, `lock_display`, `unlock_display` | `alicat:<device>` | The MFC command surface (see [Alicat adapter](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/alicat.py)). One row per accepted command. | `setpoint`, `gas`, `units`, command-specific arguments |
| `tare`, `zero`, `internal_adjust`, `set_filter_mode`, `set_display_unit`, `set_auto_zero`, `save_menu`, `reload_menu` | `sartorius:<device>` | Balance commands from the [Sartorius adapter](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/sartorius.py). | command-specific arguments |
| `recording_started`, `recording_stopped`, `nuc_triggered` | `flir:<device>` / `webcam:<device>` | Camera lifecycle and (IR-only) non-uniformity correction trigger. | `path`, `fps`, `format` |
| `pump_warning`, `open_retry` | `webcam:<device>` | Webcam frame-pump warning or device open retry. | `attempt`, `error` |

The taxonomy is producer-namespaced by convention: procedure-emitted kinds use dots (`method.step.entered`, `heat_flux_tune.iteration`), adapter-emitted kinds tend to be flat (`set_setpoint`, `tare`) because the producer is already disambiguated by the `source` column. When you filter, combine both — `WHERE kind = 'set_setpoint' AND source LIKE 'watlow:%'` is unambiguous; `kind = 'set_setpoint'` alone matches the method executor, the Watlow adapter, and the Alicat adapter.

---

## `status.sqlite` — schema

The companion file is [`status.sqlite`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/storage/status_sink.py). Its DDL:

```sql
CREATE TABLE IF NOT EXISTS status (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    adapter     TEXT    NOT NULL,
    device      TEXT    NOT NULL,
    t_mono_ns   INTEGER NOT NULL,
    t_utc       TEXT    NOT NULL,
    health      TEXT    NOT NULL,
    fields_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_status_device ON status (adapter, device, t_mono_ns);
```

One row per [`DeviceSnapshot`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/records.py). Cadence is whatever the adapter chooses; the in-tree adapters emit at ~1 Hz, driven by their snapshot timer rather than the sample rate. The `(adapter, device, t_mono_ns)` index makes per-device time slicing fast.

The `health` column is the tri-state pill that drives the status-bar widgets:

| Value | Meaning |
|---|---|
| `ok` | Adapter is producing samples on schedule, no recent retries, no comm errors. |
| `degraded` | Transient trouble — auto-reconnect counter > 0 within the snapshot window, or one-off late samples. The adapter is still emitting, but something needed retrying. |
| `down` | Connection is lost or the producer has gone silent. No samples are flowing. |

`fields_json` is the adapter-specific health payload: firmware version, alarm bits, comm latency, frame counters, valve drive percentages. The shape is per-adapter — `json.loads` it, then inspect.

Same `WAL` + `NORMAL` durability story as `events.sqlite`. Snapshots are less critical (latest-value semantics; the producer queue drops old rows under pressure) but the connection setup is symmetric for consistency.

---

## Status vs. events: when to use which

Use the right file for the question:

| Question | File | Query shape |
|---|---|---|
| "Did the operator press abort?" | `events.sqlite` | `WHERE kind = 'free_run.ended'` or `WHERE kind LIKE 'method.step.%'` |
| "Did a saturation deadline trip?" | `events.sqlite` | `WHERE kind = 'saturation_deadline'` |
| "What setpoints did the Watlow receive?" | `events.sqlite` | `WHERE source LIKE 'watlow:%' AND kind = 'set_setpoint'` |
| "Was the Alicat reachable at t = 120 s?" | `status.sqlite` | `WHERE adapter = 'alicat' AND t_mono_ns < ? ORDER BY t_mono_ns DESC LIMIT 1` |
| "What was the Watlow comm-error rate during the run?" | `status.sqlite` | `WHERE adapter = 'watlow'` → unpack `fields_json` |
| "Did any device go `degraded` or `down`?" | `status.sqlite` | `WHERE health != 'ok'` |
| "What was the operator told (and did they ack it)?" | `events.sqlite` | `WHERE kind LIKE 'method.prompt.%'` |

A rough heuristic: **events answer "what happened?", status answers "what was the state?"**

---

## Recipes

### Reconstruct the Run-tab timeline

```sql
SELECT t_mono_ns, kind, severity, source, message
FROM events
ORDER BY t_mono_ns;
```

That single query produces the same chronological story the Run tab paints during the run.

### Find every command issued by the procedure

```sql
SELECT t_mono_ns, kind, source, message, metadata_json
FROM events
WHERE kind LIKE 'method.command.%'
   OR kind IN ('set_setpoint', 'hold', 'ramp', 'safe_shutdown')
ORDER BY t_mono_ns;
```

Add `AND source = 'procedure:method'` if you want to exclude the same kinds when they originate from an adapter (those are the resulting writes, not the procedure's intent).

### Cross-reference an event to channel samples via mono time

```python
import json
import sqlite3
import polars as pl

conn = sqlite3.connect("runs/<run_id>/events.sqlite")
abort_t = conn.execute(
    "SELECT t_mono_ns FROM events WHERE kind = 'free_run.ended' LIMIT 1"
).fetchone()[0]

df = pl.read_parquet("runs/<run_id>/scalars.parquet").filter(
    (pl.col("t_mono_ns") > abort_t - 5_000_000_000)
    & (pl.col("t_mono_ns") < abort_t + 5_000_000_000)
)
```

The 5-second window on either side captures whatever channel samples bracket the event. See [Reading a bundle](reading-bundles.md) for the longer treatment, including how to join against per-adapter `device_records/*.parquet` files.

### Decode an event's `metadata_json` column

```python
import json
import sqlite3

conn = sqlite3.connect("runs/<run_id>/events.sqlite")
conn.row_factory = sqlite3.Row
for row in conn.execute(
    "SELECT t_mono_ns, kind, message, metadata_json FROM events "
    "WHERE kind = 'heat_flux_tune.iteration' ORDER BY t_mono_ns"
):
    meta = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
    print(row["t_mono_ns"], meta.get("setpoint_c"), meta.get("measured_kw_m2"))
```

Always guard the `json.loads` with a `None` check — `metadata_json` is nullable and rows with no payload come back as `NULL`.

### Build a per-device health timeline from `status.sqlite`

```python
import json
import sqlite3
import polars as pl

conn = sqlite3.connect("runs/<run_id>/status.sqlite")
rows = conn.execute(
    "SELECT adapter, device, t_mono_ns, health, fields_json "
    "FROM status WHERE adapter = 'watlow' ORDER BY t_mono_ns"
).fetchall()

df = pl.DataFrame(
    [
        {
            "t_mono_ns": r[2],
            "health": r[3],
            **(json.loads(r[4]) if r[4] else {}),
        }
        for r in rows
    ]
)
```

The `fields_json` keys are adapter-specific — open one row's payload first to see what's available before assuming a column exists.

---

## `saturation_deadline` — the must-know event

When a row with `kind = 'saturation_deadline'` appears in `events.sqlite`, the bundle is in the `crashed_but_sealed` outcome state. The conductor's [`SaturationMonitor`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/saturation.py) tripped, the run short-circuited through the normal shutdown path, and the bundle was sealed despite the failure. `source` is `engine`, `severity` is `error`, `message` carries the trip reason, and `metadata_json` decodes to a `details` blob with `resource_id`, `blocked_s`, and `deadline_s` (or the writer-stall analogues `depth` and `since_last_accept_s`).

The write is best-effort because the writer itself may be the wedged component — if you see `crashed_but_sealed` in the manifest but no `saturation_deadline` row in `events.sqlite`, check `run.log` for `conductor.saturation_event_write_failed`. The full mechanism (signals, tuning, what happens to adapter `stop()`) is documented in [Saturation and deadlines](../safety/saturation-and-deadlines.md).

---

## Implementation notes for contributors

A few details that matter when you're adding events or writing a reader:

- **The `kind` column is an open string, not an enum.** Anyone calling `write_event(kind=…)` adds to the taxonomy. The catalog above is descriptive, not prescriptive — a third-party procedure plugin can introduce `myplugin.calibration.started` and nothing breaks.
- **Namespace by producer.** Procedure-level kinds use dotted namespaces (`heat_flux_tune.iteration`, `method.prompt.shown`); adapter-level kinds use flat names because `source` already carries the producer identity. New kinds should follow whichever convention matches their producer so timeline filters stay predictable.
- **`metadata_json` is the typed payload.** Readers `json.loads(row["metadata_json"])` and dig in. Don't shove machine-readable values into `message` — keep `message` human-readable for the Run tab and the operator handbook. Sample sizes are tiny (a few hundred bytes); there's no pressure to pack.
- **WAL means a mid-run bundle has a `events.sqlite-wal` file.** Don't be surprised by it. `EventsSink.close()` runs `PRAGMA wal_checkpoint(TRUNCATE)` at finalize, which folds the WAL back into the main file and removes the sidecar. Status sink does the same.
- **Two SQLite files exist precisely so a 1 Hz status stream cannot drown the event view.** Don't merge them, and don't add high-rate diagnostic emissions to `events.sqlite`. If you have a new low-rate health signal, extend `DeviceSnapshot.fields` and let it ride on `status.sqlite`.
- **Severity is enforced.** `ALLOWED_SEVERITIES = frozenset({"info", "warning", "error"})` — passing anything else to `write()` raises `EventsSinkError`. The `DeviceEvent.severity` field already constrains it at the typed boundary, so the runtime path is safe, but generic `write()` callers should pick from the set.
- **`t_mono_ns` is the cross-file join key.** `t_utc` is for humans only; it can jump backwards across NTP corrections. Every other bundle file (`scalars.parquet`, `device_records/*.parquet`, `video/*.frames.parquet`) carries the same monotonic clock, and joining on it is exact.

---

*See also:* [What's in a bundle](what-is-a-bundle.md) · [Reading a bundle](reading-bundles.md) · [Saturation and deadlines](../safety/saturation-and-deadlines.md) · [Channel samples (parquet)](parquet-channel-samples.md) · [Devices overview](../devices/overview.md)
