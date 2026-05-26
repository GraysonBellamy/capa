---
description: capa.builtin.batch procedure for unattended replicate runs — orchestrates N child bundles with cooldowns, fail_fast control, manifest.custom.batch cross-refs.
---

# Procedure: batch

**Audience:** operators running the same method many times in a row, typically unattended.
**Scope:** how `capa.builtin.batch` orchestrates N replicate runs, what each child bundle gets, and the catalog cross-reference.

Batch wraps another procedure and runs it N times with optional cooldown between iterations. Each iteration produces its **own bundle** via a fresh `run_headless()` call, so a crashed iteration never contaminates its siblings. The parent batch id lands in every child bundle's `manifest.json.custom['batch']` so the catalog can pull the family back together.

---

## Quick reference

| | |
|---|---|
| Plugin id | `capa.builtin.batch` |
| Module | [src/capa/experiment/procedures/builtin/batch.py](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/batch.py) |
| `uses_method` | inherited from child |
| Required channels | None (the parent run's adapters are typically irrelevant; the child runs do the work) |
| Bundle shape | One parent bundle + N child bundles |

---

## When to use it

The decision tree from [What is a procedure](what-is-a-procedure.md#picking-the-right-built-in):

- **Run the same recipe N times with cooldowns** → Batch wrapping Recipe Runner.
- Parameter sweep where each child has a slightly different config → Batch with `fail_fast=false` so one bad config doesn't block the rest.
- One-off run → no — use the inner procedure directly.

The lifecycle quirk worth understanding upfront: Batch runs as a *procedure* in the **parent** run, but it does not need the parent run's adapters or fan-out. Its job is to orchestrate child runs. The simplest, least-surprising shape is for the parent to arm a zero-device hardware profile, run Batch, and let Batch execute children one at a time inside its own task. The config-time linter does **not** enforce this — some experiments may legitimately want shared sensor data correlated against children — but it's the recommended default.

---

## Config fields

```yaml
procedure:
  id: capa.builtin.batch
  config:
    iterations: 10
    cooldown_s: 120.0
    sample_id_template: "{base}_rep_{idx:02d}"
    fail_fast: false
    inner:
      id: capa.builtin.recipe_runner
      config:
        notes: "PMMA sweep"
```

| Field | Required | Default | Notes |
|---|---|---|---|
| `iterations` | yes | — | How many child runs to execute. Range `[1, 10_000]`; the upper bound is a sanity cap, raise it if you genuinely need more. |
| `cooldown_s` | no | `0.0` | Delay between iterations. Useful when the rig physically needs to cool / re-stabilize. ≥ 0. |
| `inner` | yes | — | The procedure each child run executes (a `ProcedureRef` — id + config). Typically `capa.builtin.recipe_runner` but Batch deliberately does not hardcode that. |
| `sample_id_template` | no | `"{base}_{idx:03d}"` | `str.format` template for each child's `sample.id`. `{base}` is the parent id, `{idx}` is the 0-indexed iteration. Validated at construction. |
| `fail_fast` | no | `true` | When `true`, the first crashed child stops the batch. When `false`, the batch keeps going — useful for parameter sweeps. |

### Schema-time validations

[`BatchConfig`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/batch.py) refuses two configurations at validation time:

- **Invalid `sample_id_template`** — the template is test-formatted with `{base="x", idx=0}`; any `KeyError`, `IndexError`, or `ValueError` fails validation.
- **Recursive batching** — `inner.id == capa.builtin.batch` is rejected. Batch wrapping Batch is almost always a misuse and the lifecycle gets hairy. There is no escape hatch.

Sample-id template examples:

| Template | Iteration 3 sample id for `base=PMMA_2026-05` |
|---|---|
| `"{base}_{idx:03d}"` (default) | `PMMA_2026-05_003` |
| `"{base}_rep_{idx:02d}"` | `PMMA_2026-05_rep_03` |
| `"{base}-{idx}"` | `PMMA_2026-05-3` |

---

## What each child bundle looks like

Each iteration produces a fresh bundle via [`run_headless`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/runtime/headless.py). The child config is derived from the parent's `ExperimentConfig` with three changes:

1. **`procedure`** swaps to the inner `ProcedureRef`.
2. **`sample.id`** is the templated child id; other sample fields carry over.
3. **`custom['batch']`** gains:
   ```json
   {
     "batch_id": "<8-byte hex>",
     "iteration": 0,
     "parent_sample_id": "<parent's sample.id>"
   }
   ```

The `batch_id` is minted from `secrets.token_hex(8)` at the start of `run()`. It's mirrored into every child bundle's `manifest.json.custom['batch']` block so the [runs.sqlite catalog](../bundles/what-is-a-bundle.md) can join the family back together.

Each child runs in its own nested conductor stack — its own `WorkerPool`, its own bundle writer, its own seal. Children stay isolated from the parent's pool and from each other.

---

## Lifecycle and events

The Batch procedure emits its own audit events:

| Event kind | When | Metadata |
|---|---|---|
| `batch.started` | Once, at `run()` entry | `batch_id`, `iterations`, `inner` |
| `batch.child.started` | Per iteration | `batch_id`, `child_idx`, `child_run_id`, `child_sample_id` |
| `batch.child.ended` | After each child returns | `batch_id`, `child_idx`, `child_run_id`, `run_status`, `bundle_status`, `exit_reason`, `bundle_path` |
| `batch.ended` | Once, after the loop | `batch_id`, `completed`, `crashed`, `fail_fast` |

`batch.child.ended` carries `severity=warning` when the child's `run_status != "completed"`. Reading these events from the parent bundle is enough to retrace a batch session without opening every child.

The structlog records — `batch.start`, `batch.child.start`, `batch.fail_fast`, `batch.interrupted`, `batch.end` — land in the engine log, distinct from the bundle audit events.

---

## Aborting

Two granularities of abort:

### Aborting the *parent* (the batch)

When `ctx.external_stop` fires (operator hits Abort in the Run tab, or upstream safety triggers shutdown), the parent's loop checks at the **top of each iteration** and breaks before starting the next child. Any in-progress child runs to completion or until its own external_stop fires; the next child does not start. The parent writes `batch.interrupted` to the engine log.

A `cooldown_s` between iterations is also abort-aware — the cooldown sleeps with `move_on_after` racing against `external_stop.wait()`, so an Abort during cooldown wakes within ~100 ms.

### Aborting an *iteration*

Each child runs as its own `run_headless()` call with its own `RunController`. The parent operator's Abort button targets the parent run, not the child — the engine plumbing for "abort just the current child" is not exposed in the shipped UI.

What happens in practice: hitting Abort on the parent sets external_stop, which propagates into the currently-running child via the headless runtime's shared event. The child finishes cleanly (its bundle still seals as `aborted`), and the parent stops the batch.

### `fail_fast`

When a child returns with `run_status != "completed"` (i.e. `crashed` or `aborted`):

- The child's run id lands in the parent's `crashed` list.
- If `fail_fast=true` (default), the parent writes `batch.fail_fast` to the log and breaks the iteration loop.
- If `fail_fast=false`, the parent records the crash and continues to the next iteration.

The `crashed` list is included in the `batch.ended` event's metadata so the bundle records every iteration's outcome.

---

## What does NOT exist today

Two features the stub originally promised but the code does not implement:

### Resume-from-partial

There is no shipped feature to "resume a batch that crashed on iteration 4 of 10." Each child bundle is independent — the parent's events identify which iterations completed and which did not, and the catalog can reassemble the family — but kicking off a fresh batch picks up at iteration 0 again.

The workaround for the parameter-sweep case: set `fail_fast=false` so the whole sweep runs end-to-end and you re-run only the failed configs manually after the fact.

### Per-iteration config overrides

`sample_id_template` is the only per-iteration variation today. You cannot, e.g., set a different target heat flux per iteration via the Batch config. For parameter sweeps that vary a single field per iteration, either:

- Write a custom procedure that wraps Batch and edits the child config (see [Writing a procedure](../extending/writing-a-procedure.md)), or
- Run separate experiment YAMLs by hand.

The lifecycle for nested per-iteration overrides is invasive enough that the conservative shipped Batch sticks to "same config, different sample_id."

---

## Recommended parent hardware profile

The parent run technically has its own adapter/fan-out machinery active. Its data lands in the parent bundle. Almost always, this is *not what you want* — the substance is in the children. Two common shapes:

| Parent profile | When right |
|---|---|
| **Zero-device profile** (`*_empty.toml`) | The standard recommendation. The parent has no adapters; its bundle is essentially metadata + batch audit events. Lightest, least surprising. |
| **Shared sensor profile** | The rig has an ambient sensor or fume-hood monitor that runs continuously across all iterations. The parent profile mounts only those channels; children mount the rig channels they actually use. The parent bundle captures the continuous outer signal; children capture the iteration-specific signals. |

The config-time linter does not enforce zero-device. Operators who deliberately want correlated outer signals can have them; everyone else should default to zero-device.

---

## See also

- [Procedure: recipe runner](builtin-recipe-runner.md) — the typical `inner` procedure.
- [What is a procedure](what-is-a-procedure.md) — three-axis split; when to write a procedure vs. a method step.
- [The Run tab](../user-guide/the-run-tab.md) — operator UI during a batch.
- [Reading bundles](../bundles/reading-bundles.md) — how to query the runs.sqlite catalog for a batch's children.
