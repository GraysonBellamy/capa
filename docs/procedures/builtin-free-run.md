---
description: capa.builtin.free_run procedure for ad-hoc capture — no method, fixed heater setpoint, smoke-test for engine pipeline and single-setpoint pyrolysis holds.
---

# Procedure: free run

**Audience:** operators doing ad-hoc data collection.
**Scope:** the free-run procedure — no scripted steps, no method, the heater holds whatever setpoint it had when the run began.

Free run is the **simplest valid procedure** and the smoke-test path for the engine pipeline. About 90% of CAPA experiments are free runs ([CAPA method profile mix](../glossary.md)); the dynamic-program (ramp / multi-step) case is the minority.

---

## Quick reference

| | |
|---|---|
| Plugin id | `capa.builtin.free_run` |
| Module | [src/capa/experiment/procedures/builtin/free_run.py](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/free_run.py) |
| `uses_method` | `False` — the Method tab is hidden when this procedure is selected |
| Required channels | None |
| Bundle shape | One bundle |

---

## When to use it

- **Just record sensors, no setpoint changes.** This is the default mental model for a CAPA single-setpoint hold: pre-position the heater + purge from the Manual Controls dock, then arm a free run for the data window.
- **Headless smoke test.** `capa run --headless freerun.yaml` exercises the full engine pipeline without needing a method file.
- **Simulator harness in tests.** Free run is what every unit/integration test arms when it only needs "the engine ran end-to-end."
- **Operator ad-hoc capture.** The rig is doing something interesting (a leak check, a manual ramp via Manual Controls) and you want a bundle to look at later.

Anything that needs scripted setpoint changes belongs in a [recipe runner](builtin-recipe-runner.md) with a method. The free-run preflight refuses a config with `method` set — see [Preflight](#preflight) below.

---

## Config

```yaml
procedure:
  id: capa.builtin.free_run
  config:
    duration_s: 1800   # optional
```

| Field | Required | Default | Notes |
|---|---|---|---|
| `duration_s` | no | `None` | Run length in seconds. `None` = run until external stop. `0` = exit immediately after writing started/ended (the integration-test path). ≥ 0. |

The config schema is `extra="forbid"` — typos fail validation.

### Three duration regimes

| `duration_s` | Behavior | `stopped_by` in `free_run.ended` |
|---|---|---|
| `None` | Wait on `external_stop` indefinitely. | `external_stop` |
| `0` | Write both events back-to-back and return. | `zero_duration` |
| `> 0` | Race `move_on_after(duration_s)` against `external_stop`. | `external_stop` if Abort fired; `duration_elapsed` otherwise |

The `stopped_by` value lands in the `free_run.ended` event's metadata so the bundle can distinguish "ran to its natural duration" from "operator hit Abort."

---

## Preflight

The procedure raises `ProcedureError` if the experiment config has a `method` set:

```
FreeRun cannot run with a method. Use capa.builtin.recipe_runner instead.
```

This is a *guardrail* — the Run tab disables the Method tab when free run is selected, so the misconfigured case usually only happens with hand-written experiment YAML. The error message points at the right replacement.

The procedure declares no required channels and no required capabilities. Whatever the hardware profile defines, free run will record.

---

## What gets recorded

Free run does not drive the rig. **The heater holds whatever setpoint it had when the run began.** If the operator pre-positioned the rig from Manual Controls — heater at 600 °C, N₂ purge at 100 sccm — the free run captures the resulting steady-state with no further commands.

Two bundle audit events are written by the procedure itself:

| Event kind | When | Metadata |
|---|---|---|
| `free_run.started` | At `run()` entry | `duration_s` |
| `free_run.ended` | When the wait completes | `stopped_by` (`external_stop` / `duration_elapsed` / `zero_duration`) |

All other bundle content — channel-sample parquet, events from device adapters, cameras (if any) — comes from the engine's standard fan-out. Free run does not narrow the recording plan; whatever the hardware profile produces, the bundle records.

The structlog records `free_run.start` and `free_run.end` land in the engine log.

---

## Manual commands during a free run

The procedure does not drive the rig, but the operator absolutely can — via the [Manual Controls dock](../user-guide/manual-controls.md). Setpoint writes from the dock go through the same authorized command path as method-driven writes; every command is audited into the bundle with `issued_by="manual_control"`.

A common pattern:

1. Arm a free run with `duration_s = null`.
2. Pre-position the rig from Manual Controls (heater setpoint, purge flow).
3. Adjust live as the experiment progresses — change setpoints, toggle purge.
4. Hit Stop in the Run tab when done.

The bundle records every command + the live channel data, even though no scripted procedure logic ran.

---

## Stop vs abort

Two ways a free run ends:

- **Stop (graceful)** — the Run tab's Stop button (or `duration_s` elapsing). Run status: `completed`. The bundle seals normally.
- **Abort (forced)** — the Run tab's Abort button. Sets `external_stop` aggressively. Run status: `aborted`. Bundle still seals.

In both cases, free run's `run()` returns normally — it has no `raise` paths of its own. The distinction between completed/aborted comes from the engine reading `external_stop` state at run-end. See [Aborting safely](../user-guide/aborting-safely.md).

---

## Limitations

- **No setpoint scheduling.** If you need to ramp or change setpoints automatically, use [recipe runner](builtin-recipe-runner.md).
- **No method tab.** The procedure declares `uses_method = False`; the Method tab in the Setup UI is disabled when free run is selected.
- **No batch mode.** A free run is one bundle. Use [batch](builtin-batch.md) wrapping recipe runner if you need N replicate captures — Batch *technically* accepts free run as its inner procedure, but without a method there's nothing to vary except the sample id, which is rarely the intent.

---

## See also

- [What is a procedure](what-is-a-procedure.md) — three-axis split; the simplest-valid-procedure framing.
- [Manual controls](../user-guide/manual-controls.md) — operator surface for the manual-driving workflow.
- [Procedure: recipe runner](builtin-recipe-runner.md) — when you need scripted steps instead.
- [Glossary: free run](../glossary.md) — CAPA terminology, why 90% of runs are free runs.
