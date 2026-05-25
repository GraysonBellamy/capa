# Method TOML

**Audience:** authors and operators building methods.
**Scope:** every step kind a `.method.toml` can carry, how the
[`MethodExecutor`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/executor.py)
walks them, and the cross-validation against the active hardware
profile.

---

## What a method is

A method is a typed segmented profile — a list of steps the
[recipe runner procedure](../procedures/what-is-a-procedure.md#recipe-runner)
walks at run-time. Methods live on disk as `*.method.toml` files
under `configs/methods/`, and a single method file is reusable across
hardware profiles as long as the channel names line up.

A method is **declarative**. The recipe runner does not need to know
anything specific about a method; new control logic that cannot be
expressed as a list of steps belongs in a [custom
procedure](../procedures/what-is-a-procedure.md#picking-the-right-built-in),
not in a richer method format.

Methods are optional. Procedures with `uses_method = False`
([Free run](../procedures/what-is-a-procedure.md#free-run),
[Heat-flux tune](../procedures/what-is-a-procedure.md#heat-flux-tune))
disable the Method tab entirely.

## Top-level shape

```toml
name = "sim_capa_pyrolysis"
description = "Heat-up + soak under N2 purge, then safe shutdown."

[[steps]]
# ... first step

[[steps]]
# ... second step
```

| Field | Type | Required | Notes |
|---|---|:-:|---|
| `name` | str | yes | Stable identifier; surfaced in events and the manifest. |
| `description` | str | no | Free-text. Defaults to empty. |
| `steps` | array of tables | yes | At least one step. Discriminated by `kind`. |

The shipped CAPA simulator method is short but exercises the two
most-common step kinds:

```toml title="configs/methods/sim_capa_pyrolysis.method.toml"
name = "sim_capa_pyrolysis"
description = "Heat-up + soak under N2 purge, then safe shutdown."

[[steps]]
kind = "hold"
value = 600.0
duration_s = 600.0
safety_overrides = []
[steps.target]
name = "heater.setpoint"

[[steps]]
kind = "safe_shutdown"
duration_s = 0.0
safety_overrides = []
[steps.cool_target]
"heater.setpoint" = 20.0
"purge.flow" = 0.0
```

## Step variants

The eight step variants live in
[`capa.experiment.method`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/method.py).
The `kind` field is the discriminator. Pydantic refuses any value not
in this list.

| Kind | Drives a setpoint? | Waits? | Typical use |
|---|:-:|:-:|---|
| [`hold`](#hold) | yes | duration or condition | command a value, hold for a time |
| [`ramp`](#ramp) | yes (linear) | duration | drive a setpoint linearly to a new value |
| [`setpoint`](#setpoint) | yes | no | command a value, do not wait |
| [`wait`](#wait) | no | duration or condition | dwell |
| [`prompt`](#prompt) | no | until operator clears | "ignite specimen, then Continue" |
| [`acquire`](#acquire) | no | duration | record without changing outputs |
| [`safe_shutdown`](#safe_shutdown) | yes | duration | explicit cooldown step |
| [`custom`](#custom) | plugin-defined | plugin-defined | dispatch to a registered handler |

Common fields available on every variant:

| Field | Notes |
|---|---|
| `notes` | Free-text comment carried into events / bundle. |
| `safety_overrides` | Array of `AlarmOverride` tables. Parsed and preserved today; active enforcement is reserved for the safety monitor path. |

### `hold`

Command a value and wait for a duration *or* an end condition. At
least one of `duration_s` and `end_condition` must be set; both may
be set (first-to-fire wins).

```toml
[[steps]]
kind = "hold"
value = 600.0
duration_s = 600.0
[steps.target]
name = "heater.setpoint"
```

| Field | Required | Notes |
|---|:-:|---|
| `target.name` | yes | Channel to command. Must resolve to a declared channel. |
| `value` | yes | Setpoint value (channel-unit). |
| `duration_s` | conditional | Hold duration. Required unless `end_condition` is set. |
| `end_condition` | conditional | End the step when the condition fires. |

End conditions use a simple op-and-value shape:

```toml
[steps.end_condition]
channel = "mass_loss_fraction"
op = ">"
value = 0.1
```

Operators: `>`, `>=`, `<`, `<=`, `==`.

### `ramp`

Drive a setpoint linearly. At least one of `rate_per_second` or
`duration_s` must be set. If both are present, `duration_s` controls
execution; if `duration_s` is absent, capa derives it from
`rate_per_second` and the endpoints.

```toml
[[steps]]
kind = "ramp"
end_value = 600.0
duration_s = 300.0
[steps.target]
name = "heater.setpoint"
```

| Field | Required | Notes |
|---|:-:|---|
| `target.name` | yes | Channel to command. |
| `start_value` | no | `None` = start from the current setpoint. |
| `end_value` | yes | Target setpoint. |
| `rate_per_second` | conditional | Used when `duration_s` is unset. |
| `duration_s` | conditional | Execution duration. Takes precedence when both fields are present. |

### `setpoint`

Command an immediate setpoint change and continue without waiting.
Useful for staging multiple commands inside a fast sequence where
the next `hold` or `wait` provides the dwell.

```toml
[[steps]]
kind = "setpoint"
value = 100.0
[steps.target]
name = "purge.flow"
```

### `wait`

Dwell — no setpoint change. Either `duration_s` or `end_condition`
must be set. `timeout_s` is an engine-side deadline used with
`end_condition`; on timeout, `on_timeout` decides what to do.

```toml
[[steps]]
kind = "wait"
duration_s = 30.0
```

```toml
[[steps]]
kind = "wait"
timeout_s = 120.0
on_timeout = "warn"
[steps.end_condition]
channel = "balance.stable"
op = "=="
value = 1.0
```

| Field | Required | Notes |
|---|:-:|---|
| `duration_s` | conditional | Set this **or** `end_condition`. |
| `end_condition` | conditional | Set this **or** `duration_s`. |
| `timeout_s` | no | Deadline for `end_condition`. |
| `on_timeout` | no | `"warn"` (default), `"abort"`, or `"safe_shutdown"`. |

### `prompt`

Block until the operator acknowledges. The headless `RecipeRunner`
config can set `auto_acknowledge_prompts = true` to skip without
operator interaction — common for batch and CI runs.

```toml
[[steps]]
kind = "prompt"
title = "Ignite specimen"
message = "Apply spark for 3 seconds, then click Continue."
timeout_s = 60.0
```

| Field | Required | Notes |
|---|:-:|---|
| `title` | no | Dialog title. Defaults to `"Operator confirmation"`. |
| `message` | yes | Body text. |
| `timeout_s` | no | If the operator does not respond by this deadline, the procedure raises. |

### `acquire`

Record without changing any control outputs. The step exists so a
method can mark "this segment is the measurement window" without
modifying setpoints.

```toml
[[steps]]
kind = "acquire"
duration_s = 600.0
```

### `safe_shutdown`

Reusable cooldown step. Procedures can include it directly or run it
from their own cleanup path with `MethodExecutor.run_segment(...)`.
`cool_target` is a map of
`channel_name → setpoint` to drive during shutdown.

```toml
[[steps]]
kind = "safe_shutdown"
duration_s = 0.0
[steps.cool_target]
"heater.setpoint" = 20.0
"purge.flow" = 0.0
```

| Field | Required | Notes |
|---|:-:|---|
| `cool_target` | no | Channel → setpoint map. Defaults to `{}`. |
| `duration_s` | no | Cooldown dwell after commanding `cool_target`. |

External stops do not automatically skip to a trailing `safe_shutdown`
step. If a procedure needs that behavior, it must invoke the cleanup
step explicitly. See [Shutdown sequence](../safety/shutdown-sequence.md).

### `custom`

Plugin-defined step. Dispatched at runtime to a handler keyed by
`handler_id`. The plugin's Pydantic config schema validates `params`
once the executor loads the plugin.

```toml
[[steps]]
kind = "custom"
handler_id = "my_lab.purge_check"
[steps.params]
target_flow_sccm = 100.0
tolerance_sccm = 5.0
window_s = 10.0
```

See [Custom method steps](../extending/custom-method-steps.md) for
writing one.

## `safety_overrides` — reserved alarm tweaks

Every step accepts a `safety_overrides` array of `AlarmOverride`
tables. The schema exists so methods can carry intended per-step alarm
tweaks, but the current runtime does not yet apply these fields to an
active safety monitor. The intended future use is widening an alarm band
during a spike at the start of a fresh setpoint:

```toml
[[steps]]
kind = "hold"
value = 800.0
duration_s = 300.0
[steps.target]
name = "heater.setpoint"

[[steps.safety_overrides]]
alarm_id = "heater_overtemp"
threshold = 850.0   # widen from default 820 for this step
```

| Field | Required | Notes |
|---|:-:|---|
| `alarm_id` | yes | Alarm identifier to override when the safety monitor path lands. |
| `threshold` | no | Override value. |
| `disable` | no | Intended to disable the band for the duration of this step. |

## `total_duration_s` — when it's known

The method's
[`total_duration_s()`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/method.py)
sums every step's `duration_s` when all steps have one. It returns
`None` when at least one step has no fixed duration — a `hold` with
only an `end_condition`, a `wait` with no time bound, a `prompt` that
the operator clears manually, a `safe_shutdown` with no dwell.

The disk-space preflight uses this for the cameras' `estimated_bps`
projection. When the duration is unknowable, the preflight falls back
to a configured default rather than refusing to arm.

## Cross-validation with the hardware profile

Loading a method against an `ExperimentConfig` triggers one extra
check beyond per-step validation: **every step's `target.name` must
resolve to a declared channel** in the active hardware profile.

```
hardware.channels = ["heater.setpoint", "heater.pv", "purge.flow", ...]

method step:
    target.name = "heater.setpoint"   ✓ resolves
    target.name = "heater_setpt"      ✗ rejected
```

A `setpoint` mismatch fails fast at apply time, not at the point where
the conductor would otherwise issue the command mid-run.

## Worked example: ramp + soak

```toml
name = "ramp_then_soak"
description = "Ramp heater at 10 °C/min to 600 °C, soak 10 min, cool down."

[[steps]]
kind = "ramp"
end_value = 600.0
rate_per_second = 0.1667     # 10 °C / min
[steps.target]
name = "heater.setpoint"

[[steps]]
kind = "hold"
value = 600.0
duration_s = 600.0
[steps.target]
name = "heater.setpoint"

[[steps]]
kind = "safe_shutdown"
duration_s = 60.0
[steps.cool_target]
"heater.setpoint" = 20.0
"purge.flow" = 0.0
```

## See also

- [What is a procedure](../procedures/what-is-a-procedure.md) — the
  driver layer that walks a method.
- [Method step reference](../procedures/method-steps-reference.md) —
  per-step deep dives and edge cases.
- [Custom method steps](../extending/custom-method-steps.md) — how to
  register a `custom` handler.
- [Channel bindings](channel-bindings.md) — what `target.name`
  resolves against.
