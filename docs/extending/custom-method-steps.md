---
description: Extending capa methods with custom step kinds — declaring kind="custom" in .method.toml, registering handlers in a procedure, upgrading to a plugin.
---

# Custom method steps

**Audience:** method authors with a one-off step that the built-in step kinds (`hold`, `ramp`, `setpoint`, `wait`, `prompt`, `acquire`, `safe_shutdown`) do not cover.
**Scope:** the `custom` step kind — how to declare one in a `.method.toml`, implement its handler in a procedure, and decide when to upgrade to a full procedure plugin instead.

---

## What this is, and what it is not

A *custom step* is a method step whose body is a Python callable supplied by the procedure that is running the method. It is **not** a plugin in the entry-point sense: there is no `capa.step_kinds` group, no contract check at engine startup, no `plugins.lock` entry. A custom step is a private contract between your `.method.toml` and the procedure that owns the run.

The mental model: a custom step is a *named hook* in a method file. The procedure that walks the method must register a handler under that name before it calls `executor.run_to_completion(...)`. If the handler is missing at runtime, the executor raises and the run aborts.

If your need is "many procedures should be able to use this step," you want a new procedure plugin, not a custom step. Custom steps are deliberately scoped to one procedure-and-method pair.

---

## Declaring the step in `.method.toml`

A custom step has `kind = "custom"` and two extra fields:

```toml
[[steps]]
kind = "custom"
handler_id = "myproc.purge_then_arm"
notes = "Purge for 60 s, then arm the analyzer."

[steps.params]
purge_duration_s = 60
analyzer_channel = "ftir.scan_trigger"
```

| Field | Meaning |
|---|---|
| `handler_id` | The name the procedure will register the handler under. Convention: `<procedure-prefix>.<step-name>`. |
| `params` | Free-form dict passed to the handler. The handler decides the shape — capa does not validate it. |
| `notes` | Optional free-text. Mirrored into the `method.step.entered` event. |

The model that backs this is [`CustomStep`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/method.py) (a `_StepBase` variant under the `Step` discriminated union). Pydantic validates the *shape* of the custom step block; it does not validate `params` against a schema. That is your handler's job — see below.

---

## The handler contract

The handler is an async callable with the signature:

```python
from capa.experiment.executor import MethodExecutor
from capa.experiment.method import CustomStep
from capa.experiment.procedures.base import ProcedureContext

async def my_handler(
    executor: MethodExecutor,
    step: CustomStep,
    ctx: ProcedureContext,
) -> None:
    ...
```

This is the [`CustomHandler`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/executor.py) type alias in `executor.py`.

What it receives:

- `executor` — the `MethodExecutor` that is walking the method. You can call its helpers (`_command_setpoint`, `_wait_for`) if you need them; their underscored names are deliberate — they are internal to the executor's step kinds. Most handlers should issue commands through `ctx.dispatcher` directly rather than reaching into the executor.
- `step` — the `CustomStep` instance from the method, including `step.params`. Validate `params` here.
- `ctx` — the full `ProcedureContext`. Same surface a procedure's `run()` body has — `dispatcher`, `authorization`, `bundle_writer`, `instruments`, `external_stop`.

What it returns:

- Returning normally signals the executor to move to the next step. The executor writes a `method.step.exited` event automatically.
- Raising aborts the method — the executor wraps the raise in a `method.step.failed` event and re-raises into the procedure. Most handlers should raise `MethodExecutorError` for "this step cannot complete"; the engine treats that as a configuration error rather than a crash.

---

## Registering the handler

The handler dict lives on the executor:

```python
# capa.experiment.executor.MethodExecutor
custom_handlers: dict[str, CustomHandler] = field(default_factory=dict)
```

The procedure that owns the run populates this dict **before** calling `executor.run_to_completion(method)`. A typical shape:

```python
async def run(self, ctx: ProcedureContext) -> None:
    executor = ctx.method_executor  # provided by the engine when uses_method=True
    assert executor is not None

    executor.custom_handlers["myproc.purge_then_arm"] = self._handle_purge_then_arm
    executor.custom_handlers["myproc.snapshot_analyzer"] = self._handle_snapshot_analyzer

    await executor.run_to_completion(ctx.config.method)


async def _handle_purge_then_arm(
    self,
    executor: MethodExecutor,
    step: CustomStep,
    ctx: ProcedureContext,
) -> None:
    duration_s = float(step.params.get("purge_duration_s", 60))
    analyzer_channel = str(step.params["analyzer_channel"])

    # … do the purge …
    with anyio.move_on_after(duration_s):
        await ctx.external_stop.wait()

    # … arm the analyzer …
    cmd = ctx.authorization.issue(
        kind="trigger",
        target=analyzer_channel,
        payload={"channel": analyzer_channel, "device": "..."},
    )
    await ctx.dispatcher.dispatch("...", cmd)

    ctx.bundle_writer.write_event(
        kind="myproc.analyzer.armed",
        message="analyzer armed after purge",
        severity="info",
        source=f"procedure:{self.id}",
        t_mono_ns=ctx.clock.t_mono_ns(),
        t_utc=datetime.now(UTC),
    )
```

If the method references `handler_id = "myproc.purge_then_arm"` and the procedure never registers a handler under that name, the executor raises `MethodExecutorError` at the step:

```
custom step references unknown handler_id 'myproc.purge_then_arm';
loading procedure must register it before run
```

That error is the contract enforcement. There is no startup-time check that catches a missing registration — it surfaces at the step.

---

## The same rules as procedure code apply

A custom-step handler runs on the engine task, inside the procedure's `run()`. Everything that is true for procedure code is true here:

- **Wrap CPU work in `anyio.to_thread.run_sync`.** The handler is on the loop; blocking it trips the [saturation deadline](../safety/saturation-and-deadlines.md).
- **Issue device commands through `ctx.authorization.issue(...)` + `ctx.dispatcher.dispatch(...)`.** Never call adapter methods directly. See [Authorization gates](../safety/authorization-gates.md) for the reason — the trust audit log relies on `authorization_id` being stamped at the dispatcher.
- **Honor `ctx.external_stop`.** A handler that ignores aborts cannot be stopped.

These are the same rules as [Writing a procedure](writing-a-procedure.md); the page is worth a quick re-read if you have not authored a procedure recently.

---

## Recording events from a custom step

The executor writes `method.step.entered` and `method.step.exited` events automatically when your handler runs and returns. If you want the handler to land additional events in the bundle — `myproc.analyzer.zero_taken`, `myproc.purge.complete` — use `ctx.bundle_writer.write_event(...)` exactly as a procedure's `run()` body would. See [Events SQLite](../bundles/events-sqlite.md) for the event-record schema.

A useful pattern: emit a structured event at every observable state change inside the handler. The `method.step.entered`/`exited` pair tells the bundle reader "we were inside step N at time T"; the handler-emitted events tell them "and here is what happened inside that step." Without the handler events, custom steps look opaque in the timeline.

---

## When to upgrade to a full procedure plugin

The custom-step path is the right answer when:

- The step is **one-off** — used by one procedure, in one method, for one experiment.
- It does **not need its own UI** — no dock, no live ticks beyond what the procedure's existing events provide.
- It would be **awkward to express as a `wait` + a `setpoint`** but you can describe its work in a paragraph.

Upgrade to a full procedure plugin (see [Writing a procedure](writing-a-procedure.md)) when:

- **Multiple methods or procedures should be able to use it.** Custom-step handlers are private to one procedure; a separate procedure plugin can be installed wherever it is needed.
- **Its config has enough fields to warrant a Pydantic model.** `step.params` is an untyped dict; a procedure has a `config_model` that the auto-form generator turns into a Run-tab section.
- **It needs to live across multiple method runs in a session.** A custom step is run-time scoped; a procedure is session-time scoped and can persist artifacts.
- **It would benefit from a contract check at engine startup.** Procedures are gated at load time; custom-step handlers are not.

The shipped heat-flux-tune procedure is an example of "started as a custom-step idea, grew into a full procedure" — it now has its own dock, its own config model, its own persistence path, and its own operator-command consumer. Custom steps are the right scope for the first iteration; full procedures are the right scope when the step has earned an entry-point group registration of its own.

*See also:* [Method TOML](../configuration/method-toml.md), [Writing a procedure](writing-a-procedure.md), [Events SQLite](../bundles/events-sqlite.md).
