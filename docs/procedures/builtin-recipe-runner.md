# Procedure: recipe runner

**Audience:** operators running a scripted multi-step method.
**Scope:** how `capa.builtin.recipe_runner` walks a loaded `.method.toml` step by step.

The recipe runner is the standard "drive a method to completion" procedure — about 90% of method-bearing runs use it. The body is intentionally one line: hand the method to a [`MethodExecutor`](../procedures/method-steps-reference.md) and let the executor walk every step. Custom procedures subclass this (or write their own) when they need additional phases the declarative `[[steps]]` cannot express.

---

## Quick reference

| | |
|---|---|
| Plugin id | `capa.builtin.recipe_runner` |
| Module | [src/capa/experiment/procedures/builtin/recipe_runner.py](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/recipe_runner.py) |
| `uses_method` | `True` — the Method tab is required |
| Required channels | None (the executor validates per-step against the active hardware profile) |
| Bundle shape | One bundle |

---

## When to use it

The decision tree from [What is a procedure](what-is-a-procedure.md#picking-the-right-built-in):

- **Drive a scripted sequence of setpoints / waits / prompts** → recipe runner with a method.
- Just recording sensors → [free run](builtin-free-run.md).
- Same recipe N times with cooldowns → [batch](builtin-batch.md) wrapping the recipe runner.
- Calibrating heat flux → [heat-flux tune](builtin-heat-flux-tune.md).

If a method contains a custom step kind your recipe needs, the path is **not** a new procedure — write a [custom method step](../extending/custom-method-steps.md) and let the recipe runner walk it like any other.

---

## Config fields

```yaml
procedure:
  id: capa.builtin.recipe_runner
  config:
    auto_acknowledge_prompts: false   # default
    notes: "PMMA replicate 3"         # optional
```

| Field | Default | Notes |
|---|---|---|
| `auto_acknowledge_prompts` | `false` | When `true`, `prompt` steps auto-acknowledge after a zero-second yield instead of blocking on operator input. Headless tests and batch runs set this; interactive runs leave it `false`. |
| `notes` | `null` | Operator-typed comment captured into `manifest.json.custom`. Free-text. |

The config schema is `extra="forbid"` — typos fail validation rather than silently default.

---

## Preflight contract

The procedure refuses to arm in two cases:

1. **`ExperimentConfig.method is None`** — recipe runner requires a method. The preflight raises `ProcedureError` with a hint to use `capa.builtin.free_run` instead.
2. **`ctx.method_executor is None`** — the engine didn't wire a `MethodExecutor`. Reported as a blocking [`Problem`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/base.py) with code `recipe_runner.no_executor`. This is an internal-invariant violation, not an operator-correctable misconfiguration.

The procedure declares **no** `required_capabilities` or `required_channels` of its own. Every step that issues a device command goes through the executor's `_command_setpoint`, which validates against the hardware profile at dispatch time — channel-mapping checks happen as steps run, not at procedure preflight.

---

## Step lifecycle

The executor's `_dispatch` writes three events per step. From [executor.py](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/executor.py):

| Event | When | Severity |
|---|---|---|
| `method.step.entered` | Top of dispatch | `info` |
| `method.step.exited` | Step returned normally | `info` |
| `method.step.failed` | Step raised an exception | `error` |

These bracket every step regardless of kind. Setpoint-issuing steps also emit `method.command.issued` with the channel, device, value, and authorization id.

Beyond the three lifecycle events, the procedure itself emits two events from [`recipe_runner.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/recipe_runner.py):

| Logger record | When | Carries |
|---|---|---|
| `recipe_runner.start` | Right before `run_to_completion` | `method` name, step count, `notes` |
| `recipe_runner.end` | After `run_to_completion` returns | — |

These are structlog records, not bundle audit events — they land in the engine log, not in the SQLite events table.

The current step index is exposed on `ctx.method_executor.current_step_index` for any UI consumer that wants to display progress. The Run tab uses this to paint the active step in the method graph.

---

## On-failure semantics

If any step raises, the executor:

1. Writes a `method.step.failed` event with the error type and message.
2. Re-raises the exception.

The recipe runner does not catch it. The exception propagates out of `run()`, the engine classifies the run as **crashed**, and the bundle is still **sealed** (see [bundle outcomes](../bundles/what-is-a-bundle.md)). The crash is durable; the data up to the failing step is preserved.

A `wait` step with `on_timeout = "abort"` triggers this path explicitly when the wait times out — see [Method step reference: wait](method-steps-reference.md#wait).

There is no per-step retry or recovery loop. A method either runs to completion, hits a deliberate `safe_shutdown`, or crashes. The procedure deliberately keeps that surface small — the place to put recovery logic is in the method itself (a `wait` with end_condition + `on_timeout=safe_shutdown`, or a `prompt` for operator intervention), not in the procedure.

---

## External stop

The executor polls `ctx.external_stop` at every step boundary and inside every inner loop (ramp tick, wait poll, prompt poll). When the operator hits Abort:

1. The current step exits at its next poll cycle (typically within ~100 ms for a ramp, ~50 ms for a wait).
2. The executor logs `method.executor.stopped` with the step index where it stopped.
3. `run_to_completion` returns normally — the procedure does not raise.
4. The engine classifies the run as **aborted** and seals the bundle.

`external_stop` is the operator-friendly path. A crash from inside a step is *crashed*; an Abort is *aborted*. The bundle outcome carries the distinction.

---

## How method progress shows up in the Run tab

The Run tab's method panel reads `ctx.method_executor.current_step_index` (via the UI sink) and paints:

- **Pending** steps grey.
- The **active** step highlighted, with the executor's structlog records visible in the diagnostics dock.
- **Completed** steps in their final state (exited / failed / skipped-by-external-stop).
- The `method.command.issued` audit events render as the setpoint deltas the executor produced.

For the detailed tab walk-through, see [The Run tab](../user-guide/the-run-tab.md).

---

## Subclassing

When you need extra logic but the step-walking is unchanged, subclass `RecipeRunner` rather than writing a new procedure from scratch:

```python
from capa.experiment.procedures.builtin.recipe_runner import RecipeRunner

class MyRecipeRunner(RecipeRunner):
    id: ClassVar[str] = "myorg.recipe_runner_with_cooldown"
    name: ClassVar[str] = "Recipe with mandatory cooldown"

    async def run(self, ctx):
        await super().run(ctx)
        await self._cooldown(ctx)
```

The pattern fits "wrap the method walk with extra setup or teardown." If you find yourself wanting to interleave custom phases *between* steps, look at [`MethodExecutor.advance_until`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/executor.py) — it lets a procedure run steps `[current..N)`, do something custom, then continue.

Full plugin authoring is documented in [Writing a procedure](../extending/writing-a-procedure.md).

---

## See also

- [Method step reference](method-steps-reference.md) — every step kind the recipe runner walks.
- [Method TOML](../configuration/method-toml.md) — the file format.
- [What is a procedure](what-is-a-procedure.md) — three-axis split, when to write a procedure vs. a method step.
- [The Run tab](../user-guide/the-run-tab.md) — operator UI for a running method.
