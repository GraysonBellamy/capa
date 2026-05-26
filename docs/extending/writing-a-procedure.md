---
description: Building a capa procedure plugin — Procedure Protocol, ProcedureContext, picking free-run vs method-walking vs self-driving, shipping as installable package.
---

# Writing a procedure

**Audience:** researchers and integrators adding a new procedure plugin.
**Scope:** the `Procedure` Protocol, how to ship a procedure as an installable package, and what the engine demands of it at runtime.

A procedure drives one run from start to finish. It is the only object the engine hands the `ProcedureContext` to, which means it is also the only object that can issue device commands. This page is the entry point to every other thing on the rig.

A worked example backs this tutorial: [`examples/plugins/hello_procedure`](https://github.com/GraysonBellamy/capa/blob/main/examples/plugins/hello_procedure). It is the smallest viable procedure that does something useful — drive one channel to a setpoint, hold for N seconds, exit. Read the source alongside this page.

---

## Pick a size first

There are three real "sizes" of procedure in the codebase. Decide which one you are before you write a line:

| Size | Built-in example | When to pick it |
|---|---|---|
| **No method** | [`FreeRun`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/free_run.py) | You record samples and maybe issue one or two commands. Your run is not a sequence of declared steps. `uses_method = False`. |
| **Method-walking** | [`RecipeRunner`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/recipe_runner.py) | Your run is a list of declared steps the operator authors in `.method.toml`. You delegate to the shared `MethodExecutor`. `uses_method = True`. |
| **Self-driving with state** | [`HeatFluxTune`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/builtin/heat_flux_tune/controller.py) | Your run has a closed-loop or supervisory algorithm and is not expressible as a flat sequence. You may emit `ProcedureTick`s to a UI dock, register `custom_handlers` for in-method-step hooks, and persist your own per-target artifacts. `uses_method = False`. |

If you started with "I want to add a step kind to the existing method runner," what you actually want is [Custom method steps](custom-method-steps.md) — a handler registered inside an existing procedure, not a whole new procedure.

---

## The contract

A procedure plugin is a class that implements the `Procedure` Protocol defined in [`capa/experiment/procedures/base.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/base.py). The Protocol has seven required ClassVars and two required async methods, plus one optional method:

```python
class Procedure(Protocol):
    id: ClassVar[str]                                    # "myplugin.procedure.foo"
    name: ClassVar[str]                                  # "Foo Procedure" (shown in UI picker)
    version: ClassVar[str]                               # PEP 440
    config_model: ClassVar[type[BaseModel]]              # Pydantic — validates `procedure.config`
    required_capabilities: ClassVar[tuple[str, ...]]     # Capability flag names
    required_channels: ClassVar[tuple[ChannelRequirement, ...]]
    uses_method: ClassVar[bool]                          # do you consume `config.method`?

    async def preflight(self, ctx: ProcedureContext) -> list[Problem]: ...
    async def run(self, ctx: ProcedureContext) -> None: ...
    def plan_capture(self, default_plan) -> ResolvedRecordingPlan | None: ...  # optional
```

The load-time contract check ([Plugin system → Load-time contract check](plugin-system.md#the-load-time-contract-check)) is unforgiving about all of these:

- `id`, `name`, `version`, `config_model` must be present.
- `config_model` must be a Pydantic `BaseModel` subclass.
- `preflight` and `run` must be defined as `async def`.

Mark every ClassVar as exactly `ClassVar[…]`. The check inspects the *class*, not an instance — a bare default value on a dataclass field becomes a descriptor and reads as missing.

---

## The config model

Every procedure declares a Pydantic model that validates the `procedure.config` block of the experiment YAML. The UI's auto-form generator (`capa.ui.forms.from_model`) reads this model and renders a Run-tab form from it — descriptions become field hints, `Field(gt=0, ...)` becomes a validator, `Literal[...]` becomes a dropdown. You generally do not write any UI code for your procedure.

Two conventions matter here:

- **Freeze the model.** `model_config = ConfigDict(frozen=True, extra="forbid")`. Frozen catches accidental mid-run mutation; `extra="forbid"` catches typos in the YAML.
- **Provide `from_config`.** A classmethod that takes the raw dict and returns a constructed instance. The registry calls this when it builds your procedure from the YAML. The fallback is to validate the dict and pass the dumped fields as kwargs — but if your constructor's field shape diverges from `config_model`, `from_config` is the explicit translation point.

```python
class HoldSetpointConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    target_channel: str = Field(description="Channel name to command.")
    value: float
    duration_s: float = Field(gt=0)


@dataclass(slots=True)
class HoldSetpoint(Procedure):
    id: ClassVar[str] = "hello.procedure.hold_setpoint"
    # … other ClassVars …
    config_model: ClassVar[type] = HoldSetpointConfig

    target_channel: str = ""
    value: float = 0.0
    duration_s: float = 0.0

    @classmethod
    def from_config(cls, raw: dict[str, object] | None) -> "HoldSetpoint":
        cfg = HoldSetpointConfig.model_validate(raw or {})
        return cls(target_channel=cfg.target_channel, value=cfg.value, duration_s=cfg.duration_s)
```

---

## Preflight

`preflight()` runs before the bundle is opened. It returns a list of [`Problem`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/base.py) records — an empty list means "good to arm." The engine refuses to arm the run if any returned problem has `blocking=True`.

What belongs here:

- Cross-checks against the hardware profile that Pydantic could not express: "channel X must be bound," "channel Y must be of kind `MASS`."
- Range checks on `cfg` fields that depend on rig limits the procedure knows about: "`t_set_max_c > 1000 °C` is past the rig's survival limit."
- Static config sanity that doesn't fit the Pydantic model: "if the operator picked atmosphere=oxidative, they must have a reactive-gas MFC bound."

What does *not* belong in preflight: live device reads, anything that takes more than a second, or anything that mutates rig state. Preflight runs before the engine has opened the bundle or started the adapters; touching hardware here is undefined.

Raise `ProcedureError` only for "this run is structurally misconfigured" — e.g. `RecipeRunner` raises if `config.method is None`. The engine translates the raise into a single blocking problem with code `"procedure.error"`. Use the return-a-`Problem` path for everything that has a stable error code worth filtering on.

---

## `run()` — the procedure body

This is where the actual run happens. The contract is short:

- Returning normally signals "run completed cleanly." The engine marks `run_status="completed"` and finalizes the bundle.
- Raising propagates as a crash. The engine catches and records a crashed-run outcome in the bundle.
- Honoring `ctx.external_stop` is mandatory. If the operator hits the abort button (or the CLI receives SIGINT), `external_stop` is set. Procedures must either poll or `await` on it.

The minimum-viable body looks like this:

```python
async def run(self, ctx: ProcedureContext) -> None:
    ctx.bundle_writer.write_event(
        kind="hello.started",
        message="…",
        severity="info",
        source=f"procedure:{self.id}",
        t_mono_ns=ctx.clock.t_mono_ns(),
        t_utc=datetime.now(UTC),
    )
    # … do work …
    with anyio.move_on_after(self.duration_s):
        await ctx.external_stop.wait()
```

Note the idiom on the last two lines: `anyio.move_on_after` plus an `await` on `external_stop`. That pattern is "sleep this long, but wake up immediately if abort fires." Plain `await anyio.sleep(self.duration_s)` ignores aborts; plain `await ctx.external_stop.wait()` never sleeps. You almost always want both, composed like this.

---

## Issuing device commands

Every device write must flow through `ctx.dispatcher`. The dispatcher takes a `DeviceCommand` built by `ctx.authorization.issue(...)`, which stamps it with the run's `issued_by` and `authorization_id`. Skipping authorization is not a style choice — it bypasses the audit log that downstream analysis depends on. See [Authorization gates](../safety/authorization-gates.md) for the longer version.

```python
resolved = ctx.instruments.resolve(self.target_channel)
device = resolved.binding.device

cmd = ctx.authorization.issue(
    kind="set_setpoint",
    target=self.target_channel,
    payload={"value": self.value, "channel": self.target_channel, "device": device},
)
result = await ctx.dispatcher.dispatch(device, cmd)
ctx.bundle_writer.write_event(
    kind="hello.setpoint.commanded",
    message=f"set {self.target_channel}={self.value} (accepted={result.accepted})",
    severity="info",
    source=f"procedure:{self.id}",
    t_mono_ns=ctx.clock.t_mono_ns(),
    t_utc=datetime.now(UTC),
    metadata={"value": self.value, "accepted": result.accepted},
)
```

If you are calling adapter methods directly — `ctx.adapters["heater"].command(...)` — you are doing it wrong. That path bypasses the dispatcher's concurrency layer (single-loop engine vs per-resource-worker conductor) and the authorization stamp. The `adapters` dict on the context exists for *introspection only* (reading `resource_id`, `capabilities`, etc.) — not for issuing commands.

The `MethodExecutor` already follows this rule — every command it issues in `_command_setpoint` flows through `ctx.authorization` and `ctx.dispatcher`. If your procedure delegates to the executor for the heavy lifting, you inherit the right behaviour.

---

## Talking to the UI

Two channels are available for live communication with the Run tab:

- **`ctx.bundle_writer.write_event(...)`** — anything that should be in the bundle for posterity. Events also surface in the diagnostics dock and the event log. Use for: state transitions, command issuance, anomalies.
- **`ctx.ui_sink.publish(tick)`** — pure operator-facing telemetry that should *not* land in the bundle. Used by self-driving procedures (heat-flux tune emits a `ProcedureTick` after each iteration with live numerics for the dock). The sink is `None` when no UI is attached (CLI headless, tests); null-check before publishing.

If you need bidirectional control — pause, resume, "accept the current measurement" — wire `ctx.operator_commands` (an `ObjectReceiveStream` of `OperatorCommand`) into a background task that owns the procedure's "what to do next" flag. Heat-flux tune does this; FreeRun and RecipeRunner do not.

---

## Threading and the anyio rule

capa runs on AnyIO. Procedures execute on the engine task. Two consequences:

- **Wrap CPU work in `anyio.to_thread.run_sync`.** Anything that takes more than a few milliseconds — Pydantic validation of a large config, a numpy reduction over a rolling window, a file write — should run on a thread, not the event loop. Failing to do this lengthens the loop's iteration and can trip the saturation deadline.
- **Wrap blocking I/O the same way.** Reading a config file off disk inline is fine for a few hundred bytes; reading a multi-megabyte artifact is not. Use `anyio.to_thread.run_sync` for the load.

The saturation deadline is the engine's "the loop has been blocked too long, something is wrong" tripwire. See [Saturation and deadlines](../safety/saturation-and-deadlines.md) for the contract a procedure has to live within.

A common mistake to avoid: `time.sleep(...)` inside an async function. That blocks the event loop; use `await anyio.sleep(...)` instead.

---

## Optional: `plan_capture`

If your procedure only consumes a few channels and would prefer not to record the full rig (heat-flux tune is a calibration; it does not need video, mass, every TC), implement `plan_capture`:

```python
def plan_capture(self, default_plan: ResolvedRecordingPlan) -> ResolvedRecordingPlan:
    return ResolvedRecordingPlan(
        channel_mode="only",
        recorded_channels=(self.cfg.flux_channel, self.cfg.heater_pv_channel, …),
        camera_mode="none",
        recorded_cameras=(),
        source="procedure_default",
    )
```

Called once at arm time, *before* `ProcedureContext` is constructed — so this method works off `self.cfg`, not off any per-run resolved state. Returning `None` (or omitting the method entirely) inherits the default plan: record everything declared in the hardware profile.

---

## Packaging and registration

A procedure plugin is a normal installable distribution that declares one entry point per procedure under the `capa.procedures` group. The minimum `pyproject.toml`:

```toml
[project]
name = "hello-procedure"
version = "0.1.0"
dependencies = ["capa", "pydantic>=2.9", "anyio>=4.6"]

[project.entry-points."capa.procedures"]
"hello.procedure.hold_setpoint" = "hello_procedure.procedure:HoldSetpoint"
```

The left-hand string is the entry-point name; the right-hand is `module.path:Class`. The engine reads your class's `id` ClassVar as the canonical id, but the entry-point name should match — `capa plugins list` falls back to the entry-point name if the class fails to load.

The class must be importable from the named module without side effects on the engine. In particular, do not run any I/O at import time and do not register anything into engine globals — your class becomes "live" only when the engine instantiates it.

Install in development with `uv pip install -e examples/plugins/hello_procedure`. Then:

```text
$ capa plugins list
plugin mode: dev
plugins.lock: (none — production mode requires one to gate trust)

id                                       package              version    hash
capa.builtin.free_run                    capa                 0.1.0      …
…
hello.procedure.hold_setpoint            hello-procedure      0.1.0      …
```

To use the plugin in a real run, also add a trust entry — see [Plugin lockfile](plugin-lockfile.md).

---

## Testing without hardware

Two approaches, depending on what you want to cover.

**Unit-test the body around a fake context.** Construct a `ProcedureContext` by hand with a stub `dispatcher`, an in-memory `bundle_writer`, and a fake `RunClock`. Exercise `run()` and assert against the events written. This is fast and gives you no hardware coverage — but most procedure bugs are state-machine bugs, not adapter bugs, so it goes a long way.

**Integration-test against the simulator adapters.** `capa.devices.sim` ships simulators for every adapter family. Build a hardware profile that binds your procedure's required channels to simulator devices and run the procedure through the actual engine. Look at `tests/integration/test_*.py` in the capa repo for the pattern.

For an example of unit-level testing of a procedure, see [`tests/unit/test_executor.py`](https://github.com/GraysonBellamy/capa/blob/main/tests/unit/test_executor.py) — it constructs a context and drives the executor's step kinds through it without any real adapter.

---

## A complete walk-through

The full source of `HoldSetpoint` lives at [`examples/plugins/hello_procedure/src/hello_procedure/procedure.py`](https://github.com/GraysonBellamy/capa/blob/main/examples/plugins/hello_procedure/src/hello_procedure/procedure.py). Read it as one piece — config model, dataclass with ClassVars, `from_config`, `preflight`, `run` — and the patterns on this page should map directly onto its 80-odd lines.

When you write your own procedure, the same shape will get you about 90% of the way; the remaining 10% is whatever your procedure actually does to the rig.

*See also:* [Authorization gates](../safety/authorization-gates.md), [Saturation and deadlines](../safety/saturation-and-deadlines.md), [Custom method steps](custom-method-steps.md).
