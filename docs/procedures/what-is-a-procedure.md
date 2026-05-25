# What is a procedure

**Audience:** config authors, operators, plugin authors.
**Scope:** the procedure concept and how it differs from methods and
profiles. Builds the mental model that every later page assumes.

---

## The three-axis split

A capa run is defined by three orthogonal pieces:

| Axis | Answers | Lives in | Plugin? |
|---|---|---|---|
| **Procedure** | *How* should the run be driven? | `experiment.procedure.id` | yes (`capa.procedures` entry point) |
| **Method** | *What* sequence of setpoints / waits should happen? | `*.method.toml` | no — declarative TOML |
| **Profile** | *What domain* is being run, and what extra metadata / preflight does it need? | `experiment.domain_profile.id` | yes (`capa.domain_profiles` entry point) |

The axes are independent. The same `capa.builtin.recipe_runner`
procedure can walk a cone-calorimeter method or a pyrolysis method;
the same `capa.profiles.capa_pyrolysis` profile can ride on top of
`recipe_runner`, `batch`, or `heat_flux_tune`. The procedure picks the
driver, the method scripts the setpoints, the profile adds the
scientific layer.

Two corollaries fall out of the split:

- **Some procedures ignore methods.** [`FreeRun`](#free-run) and
  [`HeatFluxTune`](#heat-flux-tune) declare `uses_method = False`; the
  Method tab is disabled when one of them is selected.
- **Profiles never drive the run.** A profile contributes
  [`ChannelRequirement`s](../configuration/channel-bindings.md#channel-bindings-and-required-channel-groups)
  and [`PreflightCheck`s](../configuration/validation-and-problems.md);
  it does not implement `run()`. Code lives in
  [`src/capa/experiment/profiles/`](https://github.com/GraysonBellamy/capa/tree/main/src/capa/experiment/profiles).

## A procedure is a state machine

The [`Procedure`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/base.py)
protocol declares four class attributes and two async methods:

```python
class Procedure(Protocol):
    id: ClassVar[str]                              # plugin id
    name: ClassVar[str]                            # display name
    version: ClassVar[str]                         # PEP 440
    config_model: ClassVar[type[BaseModel]]        # validates procedure.config
    required_capabilities: ClassVar[tuple[str, ...]]
    required_channels: ClassVar[tuple[ChannelRequirement, ...]]
    uses_method: ClassVar[bool]                    # Method tab on/off

    async def preflight(self, ctx) -> list[Problem]: ...
    async def run(self, ctx) -> None: ...
    def plan_capture(self, default_plan) -> ResolvedRecordingPlan | None: ...
```

The conductor's responsibilities are:

1. **Construct** the procedure with `procedure.config` from the
   experiment YAML, validated against `config_model`.
2. **Preflight.** Call `preflight(ctx)`. Returned
   [`Problem`s](../configuration/validation-and-problems.md) with
   `blocking=True` abort the arm; non-blocking ones land in the
   bundle as warnings.
3. **Plan capture.** Call `plan_capture()` once on the conductor loop,
   *before* `RunContext` is frozen, so the recording plan
   (channels + cameras the bundle should keep) is known by the time
   workers start.
4. **Run.** Call `run(ctx)` inside the conductor task group. Returning
   normally → `run_status="completed"`. Raising → `crashed` (the
   [bundle is still sealed](../bundles/what-is-a-bundle.md#bundle-outcomes)).
   Setting `ctx.external_stop` → `aborted`.

The procedure never touches the bundle directly. Channel-sample fan-out
is the conductor's job; the procedure reads live values through
`ctx.databus` and writes via `ctx.dispatcher` (commands) and
`ctx.bundle_writer.write_event` (audit events). See [Runtime
topology](../architecture/runtime-architecture.md) for the surrounding
machinery.

## The four built-ins

Four procedures ship with capa. Custom procedures register the same
way (see [Writing a procedure](../extending/writing-a-procedure.md)).

| Procedure | id | `uses_method` | Method tab? | Bundle shape |
|---|---|:-:|:-:|---|
| [Free run](#free-run) | `capa.builtin.free_run` | False | hidden | one bundle |
| [Recipe runner](#recipe-runner) | `capa.builtin.recipe_runner` | True | required | one bundle |
| [Batch](#batch) | `capa.builtin.batch` | inherited from child | varies | one parent + N child bundles |
| [Heat-flux tune](#heat-flux-tune) | `capa.builtin.heat_flux_tune` | False | hidden | one bundle + a tune artifact TOML |

### Free run

Record-only. Writes a `free_run.started` event, sleeps for
`duration_s` (or until the operator hits stop), writes `free_run.ended`.
The heater holds whatever setpoint it had when the run began; nothing
in capa drives it.

This is the **simplest valid procedure** and the smoke-test path for
the engine pipeline. About 90% of CAPA experiments are free runs —
the dynamic-program (ramp / multi-step) case is the minority. See
[CAPA method profile mix](../glossary.md#free-run).

Config (`procedure.config`):

```yaml
procedure:
  id: capa.builtin.free_run
  config:
    duration_s: 1800   # optional; null = run until external stop
```

### Recipe runner

The standard "walk a method" procedure. Body is one line: hand
`config.method` to
[`MethodExecutor`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/executor.py)
and let the executor dispatch every step against the channel
registry. Used by ~every method-bearing run; subclass it when you
need extra hooks but the step-walking logic is unchanged.

```yaml
procedure:
  id: capa.builtin.recipe_runner
  config:
    auto_acknowledge_prompts: false  # true for headless / batch runs
    notes: "PMMA replicate 3"
```

The `Method` itself is documented in [Method TOML](../configuration/method-toml.md);
this procedure just *executes* it.

### Batch

Wraps a child procedure and runs it N times with cooldown between
iterations. Each iteration produces its own bundle via a fresh
`run_headless()` call, so a crashed iteration never contaminates its
siblings. The parent batch id is mirrored into every child bundle's
`manifest.json.custom['batch']` block so the catalog can pull the
family back together.

Lifecycle quirk: Batch runs as a procedure in the *parent* run, but it
does not need the parent's adapters / fan-out — its job is to
orchestrate child runs. The simplest, least-surprising shape is for
the parent to arm a zero-device hardware profile; the linter does not
enforce this because some experiments may legitimately want shared
sensor data correlated against children.

### Heat-flux tune

Calibration procedure: per target heat flux (kW/m²), drive the heater
to a setpoint, wait for stability, log the resulting flux, iterate.
Emits a *tune artifact* — a `*.flux.toml` file in
`configs/calibrations/flux/` — that subsequent runs use to convert
operator-set heat flux into a heater setpoint. See [Heat-flux tune
procedure](../calibration/heat-flux-tune-procedure.md).

Notable: `HeatFluxTune` overrides `plan_capture` to record only
heater, flux gauge, and stability channels — no cameras, no purge MFC.
The resulting bundle is a calibration receipt, not a science run.

## Picking the right built-in

The decision tree is short:

1. **Just record sensors, no setpoint changes** → Free run.
2. **Drive a scripted sequence of setpoints / waits / prompts** →
   Recipe runner with a method.
3. **Run the same recipe N times with cooldowns** → Batch wrapping
   Recipe runner.
4. **Calibrate heat flux against heater setpoint** → Heat-flux tune.
5. **Anything else** → custom procedure plugin (see [Writing a
   procedure](../extending/writing-a-procedure.md)). If you find
   yourself wanting one custom step inside an otherwise-standard
   method, write a [custom method
   step](../extending/custom-method-steps.md) instead — much less
   surface area.

## Procedure discovery and trust

Procedures are loaded from the `capa.procedures` entry-point group at
startup. The set of allowed plugins is pinned in
[`plugins.lock`](../extending/plugin-lockfile.md): the conductor
refuses to arm a run whose `procedure.id` matches a plugin that is
not in the lock, or whose installed distribution hash differs from the
locked hash. `capa plugins trust <id>` is the verb that admits a new
plugin into the lock; until then, the engine treats it as
unregistered.

Trusted procedures are constructed by the
[`ProcedureRegistry`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/__init__.py)
and handed a frozen
[`ProcedureContext`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/base.py)
at preflight / run time. The context references engine-owned
services (clock, databus, dispatcher, authorization handle, bundle
writer) and is invalidated when the run ends — a procedure must not
store any field past its own lifetime.

## Where the boundary actually sits

Three rules of thumb keep the procedure surface from creeping:

- **A method step does not need a procedure.** New step kinds belong
  in [custom method steps](../extending/custom-method-steps.md), not
  in a new procedure.
- **A new domain (different specimen kind, different standard) does
  not need a procedure.** It needs a [profile](#the-three-axis-split).
- **A new control law (PID variant, MPC, adaptive ramp) might need a
  procedure**, but more often it belongs in the device adapter or as
  a method-step extension.

When you do need a new procedure, you almost always need one of these
shapes: an outer loop over targets (heat-flux tune), an outer loop
over replicates (batch), or a state machine that the standard
declarative `[[steps]]` cannot express.

## See also

- [Method TOML](../configuration/method-toml.md) — the step kinds the
  recipe runner walks.
- [Experiment YAML](../configuration/experiment-yaml.md) — how
  `procedure:` and `domain_profile:` are referenced.
- [Plugin system](../extending/plugin-system.md) — entry points,
  registries, lockfile.
- [Writing a procedure](../extending/writing-a-procedure.md) —
  tutorial for the custom-procedure path.
