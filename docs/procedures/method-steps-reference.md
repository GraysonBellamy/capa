---
description: Reference for every capa method step kind — hold, ramp, setpoint, wait, prompt, acquire, safe_shutdown, custom — schema, validation rules, emitted events.
---

# Method step reference

**Audience:** method authors writing `*.method.toml` files; plugin authors adding new step kinds.
**Scope:** every built-in step kind: schema, validation rules, on-failure semantics, what gets emitted into the bundle.

The step types live in [`src/capa/experiment/method.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/method.py); execution lives in [`src/capa/experiment/executor.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/executor.py). The `MethodExecutor` walks `Method.steps` in order; the [recipe runner](builtin-recipe-runner.md) is a one-line wrapper that hands the method to the executor.

---

## Step kinds at a glance

| Step | What it does | Has duration? | Issues a command? | Custom-handler? |
|---|---|---|---|---|
| [`hold`](#hold) | Command a setpoint, then wait | yes (or end_condition) | yes | no |
| [`ramp`](#ramp) | Linear ramp between setpoints | yes | yes (~10 Hz drip) | no |
| [`setpoint`](#setpoint) | Issue a setpoint and continue immediately | no | yes | no |
| [`wait`](#wait) | Wait for a condition or duration | yes (or end_condition) | no | no |
| [`prompt`](#prompt) | Block until operator confirms | optional timeout | no | no |
| [`acquire`](#acquire) | Record without changing controls | yes | no | no |
| [`safe_shutdown`](#safe_shutdown) | Drive channels to safe values | optional | yes (one per cool_target) | no |
| [`custom`](#custom) | Plugin-defined behavior | depends | depends | **yes** |

Every step also accepts two `_StepBase` fields:

| Field | Notes |
|---|---|
| `notes` | Free-text. Not rendered anywhere today; recorded in `method.step.entered` events for analysts. |
| `safety_overrides` | Tuple of [`AlarmOverride`](#alarmoverride). Parsed and preserved today; active safety-rule enforcement is reserved for the safety monitor path. |

The discriminator is the `kind` field. Pydantic dispatches on it at deserialisation, so an unknown `kind` fails fast with a clear error message rather than silently mis-parsing.

---

## Every step emits three events

Whatever the step kind, `MethodExecutor._dispatch` writes:

| Event | When | Severity |
|---|---|---|
| `method.step.entered` | Top of dispatch | `info` |
| `method.step.exited` | Step returned normally | `info` |
| `method.step.failed` | Step raised | `error` |

These carry `step_index`, `step_kind`, and (when applicable) `target` — enough that a bundle audit can walk the run step by step without consulting the original method file.

Setpoint-issuing steps (`hold`, `ramp`, `setpoint`, `safe_shutdown`) also emit a `method.command.issued` event per write, carrying `channel`, `device`, `value`, `step_kind`, `accepted`, `issued_by`, and `authorization_id`. Every device write is attributable.

---

## `hold`

Command a setpoint and wait for `duration_s` or `end_condition`.

```toml
[[steps]]
kind = "hold"
duration_s = 600.0
value = 600.0
notes = "main exposure"

[steps.target]
name = "heater.setpoint"
```

| Field | Required | Notes |
|---|---|---|
| `target` | yes | `ChannelRef` — the channel to write. |
| `value` | yes | The setpoint to command. |
| `duration_s` | one-of | Seconds to hold. ≥ 0. |
| `end_condition` | one-of | [`EndCondition`](#endcondition) that ends the hold early. |

**At least one of `duration_s` and `end_condition` must be set** — validated at model construction. Both set: whichever fires first wins. Both unset: rejected with `"hold step needs either duration_s or end_condition"`.

The setpoint write happens *first*, the wait runs *after*. So a `hold` with `duration_s=0` is functionally a `setpoint` step that takes the dispatch round-trip and exits.

---

## `ramp`

Drip setpoints linearly from a start value to an end value.

```toml
[[steps]]
kind = "ramp"
end_value = 600.0
rate_per_second = 0.5     # 30 °C/min

[steps.target]
name = "heater.setpoint"
```

| Field | Required | Notes |
|---|---|---|
| `target` | yes | `ChannelRef`. |
| `start_value` | no | `None` means "ramp from the current setpoint" — the executor reads the latest databus value. |
| `end_value` | yes | Final setpoint. |
| `rate_per_second` | one-of | Slope. |
| `duration_s` | one-of | Total ramp time. > 0. |

**At least one of `rate_per_second` and `duration_s` must be set** — validated at model construction. If both are present, `duration_s` controls execution and `rate_per_second` is retained as method metadata; the executor does not validate that they agree. If `duration_s` is absent, the executor derives it from `rate_per_second` and the endpoints.

Execution details:

- Setpoints drip at `DEFAULT_RAMP_TICK_HZ = 10 Hz` (the hardware closes the loop; capa just provides the setpoint trajectory).
- `start_value=None` + no live sample on the databus → executor logs a warning, jumps immediately to `end_value` (degenerate ramp). Rare but recoverable.
- A ramp with `rate_per_second=0` is rejected at runtime with `MethodExecutorError`.
- Each tick respects `external_stop` — the operator's Abort interrupts a ramp within ~100 ms.

---

## `setpoint`

Issue a setpoint and continue immediately. No wait, no end condition.

```toml
[[steps]]
kind = "setpoint"
value = 0.0

[steps.target]
name = "purge.flow"
```

Three required fields: `kind`, `target`, `value`. Useful for synchronised multi-channel sequences where the timing comes from a subsequent `wait` rather than from each setpoint having its own duration.

---

## `wait`

Wait for a condition, a duration, or whichever fires first.

```toml
[[steps]]
kind = "wait"
duration_s = 30.0          # safety cap
timeout_s = 35.0
on_timeout = "abort"

[steps.end_condition]
channel = "mass_loss_fraction"
op = ">"
value = 0.1
```

| Field | Required | Notes |
|---|---|---|
| `end_condition` | one-of | [`EndCondition`](#endcondition) — what to wait for on the databus. |
| `duration_s` | one-of | Maximum wait time. ≥ 0. |
| `timeout_s` | no | Engine-side deadline. > 0. Distinct from `duration_s`. |
| `on_timeout` | no | `"warn"` (default), `"abort"`, or `"safe_shutdown"`. |

**At least one of `duration_s` and `end_condition` must be set.**

The semantics of `duration_s` vs `timeout_s`:

- `duration_s` is the wait *itself* — when this elapses, the wait exits normally (no `method.wait.timeout` event).
- `timeout_s` is the **deadline** — if the wait hasn't exited by then, the executor writes a `method.wait.timeout` event and applies `on_timeout`.

The end-condition watcher subscribes to the databus and evaluates each sample for the target channel. A condition exits the wait as soon as a matching sample arrives.

`on_timeout` policies:

| Value | Behavior |
|---|---|
| `"warn"` | Write a `severity=warning` timeout event; continue to next step. |
| `"abort"` | Write a `severity=error` event; raise `MethodExecutorError`, crashing the run (bundle is still sealed). |
| `"safe_shutdown"` | Write a warning event; set `external_stop`, requesting the procedure/conductor shutdown path. |

---

## `prompt`

Block until the operator confirms via the Run-tab UI.

```toml
[[steps]]
kind = "prompt"
title = "Insert sample"
message = "Place specimen in the holder and close the door. Click OK when ready."
timeout_s = 600.0
```

| Field | Required | Notes |
|---|---|---|
| `title` | no | Default `"Operator confirmation"`. |
| `message` | yes | Body text shown to the operator. |
| `timeout_s` | no | Optional deadline. > 0. |

Lifecycle:

1. `method.prompt.shown` event written with `title`, `message`, `timeout_s` in metadata.
2. The executor polls `ctx.metadata["_prompt_confirmed"]` at 10 Hz; the Run tab sets it when the operator clicks Confirm.
3. On confirmation: `method.prompt.acknowledged` written; step exits.
4. On timeout: `method.prompt.unanswered` written with `reason="timeout"`; raises `MethodExecutorError`.
5. On `external_stop`: `method.prompt.unanswered` written with `reason="external_stop"`; step exits without raising (the run is shutting down anyway).

### Headless prompts

For tests, batch runs, and other non-interactive paths, the [recipe runner](builtin-recipe-runner.md) sets `auto_acknowledge_prompts=True` on the executor. With that:

- Prompts auto-acknowledge after a 0-second yield.
- `method.prompt.acknowledged` carries `by: "auto_acknowledge"` in metadata.

Without `auto_acknowledge_prompts` and with no UI wired up, a headless run hits `PROMPT_HEADLESS_DEFAULT_TIMEOUT_S = 30 s` as the soft default and times out. This is deliberately *not* infinite — a CI job hanging on an unconfigured prompt would be invisible failure.

---

## `acquire`

Record without changing any control output for `duration_s`.

```toml
[[steps]]
kind = "acquire"
duration_s = 60.0
notes = "baseline window"
```

One required field beyond `kind`: `duration_s` (> 0). No setpoint commands, no condition checks, no databus subscriptions — just a sleep that respects `external_stop`.

Useful as an explicit "this is a tagged measurement window" marker. The `method.step.entered` / `method.step.exited` events bracket the window so downstream analysis can pull the channel data between those timestamps.

---

## `safe_shutdown`

Drive channels to safe values and (optionally) wait. Procedures can include this explicitly in a method, or run it from their own cleanup path via `MethodExecutor.run_segment(...)`.

```toml
[[steps]]
kind = "safe_shutdown"
duration_s = 30.0

[steps.cool_target]
"heater.setpoint" = 25.0
"purge.flow" = 0.0
```

| Field | Required | Notes |
|---|---|---|
| `cool_target` | no | `{channel_name: setpoint}` map. |
| `duration_s` | no | Time to hold the cool targets before continuing. ≥ 0. |

Semantics that are easy to miss:

- A missing channel during cooldown **logs a warning but does not abort** the rest of the cooldown. The other targets still receive their safe values. This is by design: cleanup should be best-effort rather than first-failure-stops-everything.
- The post-command wait is breakable by `external_stop`. A second Abort during cooldown wakes the step immediately.
- `duration_s` defaulting to `None` skips the wait entirely — the step issues the commands and returns.

See [safety/shutdown-sequence.md](../safety/shutdown-sequence.md) for how `safe_shutdown` fits into the broader shutdown contract.

---

## `custom`

Plugin-defined step kind. Dispatched at runtime to a handler registered on the executor by the loading procedure.

```toml
[[steps]]
kind = "custom"
handler_id = "my_plugin.balance_zero"

[steps.params]
target_window_s = 30.0
acceptable_drift_g = 0.01
```

| Field | Required | Notes |
|---|---|---|
| `handler_id` | yes | The id to dispatch to. |
| `params` | no | Free-form dict. The plugin's own Pydantic schema validates these at handler invocation. |

The handler signature is documented in [Extending: custom method steps](../extending/custom-method-steps.md). Briefly: a coroutine `(executor, step, ctx) -> None` registered into `executor.custom_handlers` before `run_to_completion` is called.

An unregistered `handler_id` raises `MethodExecutorError` at dispatch time — methods reach `custom` steps without crashing the run as a whole until they actually try to execute one.

---

## Supporting types

### `ChannelRef`

```toml
[steps.target]
name = "heater.setpoint"
```

A typed pointer to a channel. Single `name` field today; the model is kept around so a future addressing scheme (`device.parameter`, indexed channels) can extend it without changing every step that points at a channel.

### `EndCondition`

```toml
[steps.end_condition]
channel = "mass_loss_fraction"
op = ">"
value = 0.1
```

| Field | Notes |
|---|---|
| `channel` | Channel to watch on the databus. |
| `op` | One of `>`, `>=`, `<`, `<=`, `==`. |
| `value` | The threshold. |

The executor subscribes to the channel and tests each emission. `==` is allowed but rarely useful for floating-point channels — operators almost always want `>=` against a slightly relaxed value instead.

### `AlarmOverride`

```toml
[[steps.safety_overrides]]
alarm_id = "heater_high_temp"
threshold = 850.0     # widen during a hot step
```

| Field | Notes |
|---|---|
| `alarm_id` | The alarm to override. |
| `threshold` | New threshold value. Optional. |
| `disable` | Boolean. When `true`, the alarm is suppressed entirely for this step. Default `false`. |

Per-step alarm-band overrides are parsed and preserved, but the current runtime does not yet apply them to an active safety monitor. A future `SafetyMonitor` can use these fields to widen a high-temp band during one hot step and restore the default threshold at `method.step.exited`.

---

## Method total duration

`Method.total_duration_s()` returns the sum of every step's `duration_s` if all steps have one. It returns `None` when at least one step has no fixed duration — a `hold` with only an end_condition, a `wait` with no time bound, an unbounded `prompt`. The camera disk-space preflight uses this — when the duration is unknowable, the preflight falls back to a configured default rather than refusing to arm.

---

## Tunables

Three named constants from [executor.py](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/executor.py) — they belong as constants so a perf-regression test can pin behaviour.

| Constant | Default | Where used |
|---|---|---|
| `DEFAULT_RAMP_TICK_HZ` | 10.0 | Setpoint-update cadence for `ramp` steps. |
| `DEFAULT_WAIT_POLL_HZ` | 20.0 | Legacy/exported compatibility constant. Current waits are databus-event driven. |
| `PROMPT_HEADLESS_DEFAULT_TIMEOUT_S` | 30.0 | Soft default when a headless run hits a `prompt` step without `auto_acknowledge_prompts`. |

---

## See also

- [Procedure: recipe runner](builtin-recipe-runner.md) — the procedure that walks `Method.steps`.
- [Custom method steps](../extending/custom-method-steps.md) — writing a `custom` handler.
- [What is a procedure](what-is-a-procedure.md) — where methods sit in the three-axis split.
- [Method TOML](../configuration/method-toml.md) — the file-format perspective.
- [Safety: shutdown sequence](../safety/shutdown-sequence.md) — how `safe_shutdown` integrates with the safety contract.
