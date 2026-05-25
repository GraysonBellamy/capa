# Writing a profile

**Audience:** integrators adding a new scientific domain (e.g. drying study, char yield, micro-scale calorimetry).
**Scope:** how to ship a `DomainProfile` plugin that contributes Setup-tab metadata fields, required channel groups, and preflight checks.

A worked example backs this tutorial: [`examples/plugins/drying_profile`](https://github.com/GraysonBellamy/capa/blob/main/examples/plugins/drying_profile).

---

## Profile vs. procedure

A common point of confusion: a *profile* and a *procedure* are not alternatives. They are orthogonal.

| | Domain profile | Procedure |
|---|---|---|
| What it answers | "What does *this kind of experiment* need to be a complete record?" | "How do I drive *this run* from start to finish?" |
| Contributes | Setup-tab metadata fields, required channel groups, preflight checks, manifest block | The `run()` body, device commands, the run loop |
| Active per-run | Exactly one (or zero) | Exactly one |
| Lifetime | Static — read once at arm | Stateful — instantiated per run |

A cone-calorimeter *profile* declares that "specimen.thickness_mm" is required metadata and that "the heat-flux gauge calibration must be < 30 days old"; the *procedure* (likely `RecipeRunner` walking an authored method) is what actually drives the heater, shutter, and balance during the run.

If you find yourself wanting to write closed-loop control or a state machine, you want a procedure, not a profile. See [Writing a procedure](writing-a-procedure.md).

---

## The contract

A profile plugin is a **module** whose top-level attributes implement the `DomainProfile` Protocol from [`capa/experiment/profiles/base.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/profiles/base.py). This shape is deliberately unlike a procedure (which is a class):

```python
# capa/experiment/profiles/base.py
@runtime_checkable
class DomainProfile(Protocol):
    id: str
    standard_refs: tuple[str, ...]
    metadata_model: type[BaseModel]
    required_channel_groups: tuple[ChannelRequirement, ...]
    preflight_checks: tuple[PreflightCheck, ...]
```

Each is a module-level attribute. The shipped CAPA profile at [`capa/experiment/profiles/capa_pyrolysis.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/profiles/capa_pyrolysis.py) is the canonical reference:

```python
id: str = PROFILE_ID
standard_refs: tuple[str, ...] = DEFAULT_STANDARD_REFS
metadata_model: type[BaseModel] = CapaPyrolysisMetadata
required_channel_groups: tuple[ChannelRequirement, ...] = REQUIRED_CHANNEL_GROUPS
preflight_checks: tuple[PreflightCheck, ...] = PREFLIGHT_CHECKS
```

The cone-calorimeter profile at [`cone_calorimeter.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/profiles/cone_calorimeter.py) is the parallel implementation, lightly different in scope (oxygen-depletion HRR rather than controlled-atmosphere pyrolysis). Pick the one nearer your domain and use it as a template.

---

## Building `metadata_model`

The metadata model is a Pydantic `BaseModel` (frozen, `extra="forbid"`) that validates the metadata block of `DomainProfileRef.metadata` in the experiment YAML. It also drives the Setup-tab form — the UI's auto-form generator reads field types, `Field(..., description=...)` strings, and `Literal[...]` choices and renders an editable section.

The CAPA profile is a useful template here because it shows the right amount of nesting. Top-level:

```python
class CapaPyrolysisMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    specimen: CapaSpecimen
    program: HeaterProgram
    atmosphere: Atmosphere
    analyzer: DownstreamAnalyzer | None = None
    sop_revision: str | None = None
```

Each sub-model owns one coherent group of fields. The Setup tab will render each sub-model as a separate section; the order of fields here determines the order in the UI.

A few conventions that pay off:

- **Always freeze sub-models.** Metadata is captured at run-arm and snapshotted into the bundle. Mutation after that would invalidate the snapshot; freezing makes it a type error.
- **Forbid extras at every level.** Operators mistype field names. `extra="forbid"` catches it at validation rather than at "I cannot find this field three months later."
- **Use `json_schema_extra` for unit hints.** `Field(gt=0, json_schema_extra={"capa_unit": "kW/m²", "capa_help": "..."})` — the UI reads these.
- **Use `Literal[...]` for closed sets.** "atmosphere.mode" should be `Literal["inert", "oxidative", "reducing", "reactive_blend"]`, not `str`. The UI then renders a dropdown.

The shipped CAPA profile's [specimen / method / atmosphere sub-models](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/profiles/capa_pyrolysis.py) are the clearest reference for unit hints and dropdown patterns.

---

## Required channel groups

`required_channel_groups: tuple[ChannelRequirement, ...]` declares the channels that must be bound on the active hardware profile for the run to arm. Each requirement names a *group*, a list of acceptable `ChannelKind`s, and a `min_count`:

```python
ChannelRequirement(
    group="mass",
    kinds=(ChannelKind.MASS.value,),
    min_count=1,
)
```

The link between the requirement and the bound channel is operator-authored metadata. When the operator builds the hardware profile, they tag each channel with `metadata["<profile>_group"] = "<group name>"`. The CAPA profile uses `capa_group`; the cone profile uses `cone_group`. Your profile should follow the same convention — `<profile-prefix>_group` — so an operator running multiple profiles can keep group annotations distinct.

The `capa.required_channel_mappings` preflight check (shared by both shipped profiles) is what actually verifies the binding. If you do not include this check in your `preflight_checks` tuple, the profile will load but the requirement is informational only.

---

## Preflight checks

`preflight_checks: tuple[PreflightCheck, ...]` declares the *what*; the runtime registry in [`profiles/runtime.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/profiles/runtime.py) declares the *how*. Each check has an id, a description, and a `blocking` flag:

```python
PreflightCheck(
    id="drying.required_channel_mappings",
    description="Mass and sample temperature channels are bound.",
    blocking=True,
)
```

Two kinds of check exist, distinguished by *when* they run:

- **Static checks** (the default) read only config / filesystem state. They run before any adapter is opened. Use for: required-channel-mapping checks, leak-test recency, calibration-artifact freshness, disk-space projection.
- **Dynamic checks** read live channel samples. They run inside the engine task group, after every adapter has called `start()`. Use for: balance stability, heater PV in safe range, purge-flow established.

The check callable is registered via the `@register("<id>", category="...")` decorator in `profiles/runtime.py`. A check function takes a `ProfilePreflightContext` and returns `Problem | None` — return `None` for "passed," return a `Problem` to surface a warning or error:

```python
@register("drying.required_channel_mappings")
async def _required_channel_mappings(ctx: ProfilePreflightContext) -> Problem | None:
    has_mass = any(ch.metadata.get("drying_group") == "mass"
                   for ch in ctx.config.hardware.channels)
    if not has_mass:
        return Problem(
            code="drying.missing_mass_channel",
            message="drying profile requires a channel tagged drying_group='mass'",
            severity="error",
            blocking=True,
        )
    return None
```

Two practical conventions to follow:

- Reuse the shipped check ids when the implementation is identical. Both CAPA and cone profiles register `"<prefix>.required_channel_mappings"` and `"<prefix>.balance_stability"` against the same Python function. The registry allows multiple ids to point at one callable.
- A non-blocking check (`blocking=False`) records a warning into the bundle but does not refuse to arm. Use this for "the lab SOP says this calibration should be < 7 days old" and similar policy-rather-than-safety gates.

If your profile references a check id that is not registered, the engine surfaces a `profile.unknown_check` blocking problem at arm time. The registry catches typos early.

---

## Manifest snapshotting

Once a run arms, the engine validates the experiment YAML's `domain_profile.metadata` block against your `metadata_model` and snapshots it verbatim into `profiles/<short_id>.toml` inside the bundle. The bundle's [`manifest.json`](../bundles/manifest-and-schema.md) carries the active profile id and `standard_refs` under `domain_profile`; the profile metadata itself lives in the `profiles/` snapshot file.

You do not write any code to make this happen — the bundle writer does it for every profile that has a `metadata_model`. The implication for you: anything you want in the bundle goes into the metadata model. Anything *not* in the model is not snapshotted.

A few specific consequences:

- Add the SOP / standard revision as a field on your model, not as a comment in the YAML. Comments are not snapshotted.
- If your profile is dated (a specific revision of a lab procedure), put the revision in the model as `sop_revision: str | None`. CAPA does this.
- Free-form notes are fine, but make them an explicit field (`notes: str | None`) rather than relying on the operator to put them somewhere.

---

## Packaging and registration

A profile plugin is a normal installable distribution with one entry point per profile under the `capa.profiles` group:

```toml
[project]
name = "drying-profile"
version = "0.1.0"
dependencies = ["capa", "pydantic>=2.9"]

[project.entry-points."capa.profiles"]
"drying.profile" = "drying_profile.profile"
```

Note the right-hand side: `drying_profile.profile` (a module), **not** `drying_profile.profile:DryingProfile` (a class). Profiles are imported and their top-level attributes are read. This matches the shape of the shipped profiles.

The module must define every attribute the Protocol names — missing attributes raise an `AttributeError` when the engine tries to resolve them. There is no separate load-time contract check for profiles today (procedures get one; profiles do not yet), so the failure surfaces at run-arm.

The entry-point group `capa.profiles` is declared in `pyproject.toml` and reserved for this purpose, but the runtime side that loads it from entry points is wired only partially today — the shipped profiles are resolved by id. A third-party profile installed via entry point will need a small patch to the engine's profile resolver (see [`capa/experiment/profiles/runtime.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/profiles/runtime.py) — `_resolve_required_groups` and `resolve_preflight_check_ids` both hard-match on the shipped profile ids). Until that is generalised, treat third-party profiles as "in development" — they work for testing the metadata model and Setup tab, but the engine's profile-resolution path does not yet pick them up at run time.

---

## Testing

Two test surfaces are worth covering:

**Round-trip the metadata model.** Pydantic does the heavy lifting; what you want to confirm is that a representative YAML block validates, that intentionally bad input is rejected, and that the model serialises to the same TOML the bundle writer would emit.

**Run the checks against a synthetic context.** Construct a `ProfilePreflightContext` (it is a slotted dataclass; instantiate it directly) with a fixture `config` and `instruments`. Call each check function and assert on the returned `Problem`. The shipped CAPA preflight tests in the capa repo are a useful template — they cover static checks without spinning up the data bus.

---

## A complete walk-through

The full source of the drying-loss profile lives at [`examples/plugins/drying_profile/src/drying_profile/profile.py`](https://github.com/GraysonBellamy/capa/blob/main/examples/plugins/drying_profile/src/drying_profile/profile.py). It is intentionally tiny — three sub-models, two required channel groups, one preflight check — so the contract is visible at a glance. The shipped CAPA profile is the long form; the drying example is the short form. Use whichever is closer to your domain.

*See also:* [Plugin system](plugin-system.md), [Manifest and schema](../bundles/manifest-and-schema.md), [Writing a procedure](writing-a-procedure.md).
