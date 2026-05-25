# Configuration overview

**Audience:** anyone editing a capa config.
**Scope:** the four config kinds, how they compose, where each lives on
disk, and how the runtime materialises them. This page is the table of
contents for the rest of the Configuration section.

---

## The four config kinds

| Kind | Format | Default location | Pydantic model | Documented in |
|---|---|---|---|---|
| Experiment recipe | YAML or TOML | `configs/experiments/*.yaml` | [`ExperimentConfig`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/config.py) | [Experiment YAML](experiment-yaml.md) |
| Hardware profile | TOML | `configs/hardware/*.toml` | [`HardwareProfile`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/config.py) | [Hardware TOML](hardware-toml.md) |
| Method | TOML | `configs/methods/*.method.toml` | [`Method`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/method.py) | [Method TOML](method-toml.md) |
| Calibration set | TOML | `configs/calibrations/<family>/*.toml` | calibration runtime types | [Calibrations on disk](calibrations.md) |

A run starts from **one** experiment file. The experiment file
*references* a hardware profile and a calibration set, plus optionally a
method and a domain profile (the CAPA-pyrolysis scientific layer or any
future variant).

## How an experiment composes

The minimal composition graph for a typical CAPA run:

```
experiment YAML  ────────────────►  hardware TOML
       │                                  │
       │                                  └──► references named channels
       │                                       (each with a binding into
       │                                        a device adapter family)
       │
       ├──► method TOML            (only when procedure.uses_method = True)
       │
       ├──► calibration_set        (by name; resolved against configs/calibrations/)
       │
       ├──► procedure.id           (plugin id, matched against plugins.lock)
       │
       └──► domain_profile.id      (plugin id; CAPA-pyrolysis is the default)
```

YAML is the recommended format for experiment files because the nested
metadata (specimen, atmosphere, operator) reads better than TOML's
flatter table shape. capa accepts either; the parser picks based on
extension.

Hardware, method, and calibration files are TOML. The reasons differ
slightly: hardware profiles benefit from `[[devices]]` and
`[[channels]]` array-of-tables; methods are step sequences; calibration
sets are flat key-value pairs with embedded timestamps. None of them
have enough deep nesting to need YAML.

## File-ref resolution

`hardware:` and `method:` accept either a string path or an inline
table:

```yaml
# external file (the common case)
hardware: ../hardware/sim_capa.toml

# inline (for self-contained one-offs and tests)
hardware:
  name: minimal
  devices: []
  channels: []
```

Relative paths resolve against the experiment file's directory.
`calibration_set.name` is a logical name. The current bundle writer records
the chosen set's name and revision in `calibration.json`; full resolved-curve
snapshots are planned but not wired yet.

When the UI auto-loads an external method via a string ref, the
`method_source_path` on the resulting `ExperimentConfig` records the
original path so a subsequent "save method" writes back to the source
file rather than inlining the method into the experiment YAML.

## CAPA profile fields

The CAPA-pyrolysis [domain profile](../glossary.md#profile-domain-profile)
adds a scientific layer on top of the generic experiment recipe —
specimen, atmosphere, target heat flux, leak-check provenance, and
required channel groups (heater_setpoint, heater_pv, sample_temperature,
mass, purge_gas_flow). The fields are detailed on the [CAPA profile
fields](capa-profile.md) page.

A profile **is not a separate file**. The fields live under
`domain_profile.metadata:` in the experiment YAML, and the active
profile is identified by `domain_profile.id`. The bundle writer
snapshots the metadata block into `profiles/<short_id>.toml` for
diff-friendliness, but the source of truth is the experiment file.

## The two layers: `ConfigDocument` vs `ExperimentConfig`

A subtle distinction matters when reading code:

- [**`ConfigDocument`**](https://github.com/GraysonBellamy/capa/blob/main/src/capa/config/document.py)
  is the *source-tracking* layer. It owns raw dict payloads, paths,
  formats, and inline-vs-external modes. The UI edits a
  `ConfigDocument` because mid-edit invalid state can live there
  without fighting frozen Pydantic models. Atomic multi-file save lives
  here too.
- **`ExperimentConfig`** is the *validated, frozen* runtime object
  built from a `ConfigDocument` at save / apply / validate boundaries.
  Frozen Pydantic with `extra="forbid"`. The conductor, workers, and
  every consumer downstream see only `ExperimentConfig` and its
  sub-models.

`ExperimentConfig.load(path)` is the headless / CLI entry point —
under the hood it delegates to `ConfigDocument.load(path).build_config()`.

## Validation pipeline

Each save / apply / validate runs the same multi-layer pipeline against
the document:

1. **Schema validation.** Pydantic against the model classes.
   Unknown keys are rejected (`extra="forbid"`).
2. **Cross-reference validation.** Every channel binding must reference
   a declared device; every method step's `target.name` must resolve
   to a declared channel; camera and device names must not collide.
3. **Domain validation.** When `domain_profile` is set, the profile's
   `required_channel_groups` and `preflight_checks` run against the
   resolved hardware + method.
4. **Resource validation.** Passive adapter/materialization dry run and
   resource-conflict checks. This does not open hardware.
5. **Live validation.** Optional discovery and handshakes from explicit
   "Check Hardware" flows such as `capa config validate --live`.

Procedure-specific preflight runs later, at arm time. It may return
[`Problem`s](../configuration/validation-and-problems.md) that block the arm
even if the saved config passed every static validation layer.

`Problem` records surfaces in the UI's Problems panel and in the
`capa validate` CLI output. See [Validation and
problems](validation-and-problems.md) for severity rules and
problem-to-field navigation.

## What triggers a worker-pool rebuild?

The runtime distinguishes config changes that require tearing down the
[`WorkerPool`](../glossary.md#worker) from ones it can apply hot:

| Change | Hot-edit? | Reason |
|---|---|---|
| Add / remove / rename a device | no — rebuild | per-resource worker construction is keyed on declared devices |
| Change a device's `params` | no — rebuild | adapters parse params at construction |
| Change a device's `resource_id` | no — rebuild | resource grouping changes |
| Add / remove / rename a channel | no — rebuild | channel registry is constructed at apply |
| Change a channel's calibration | yes — hot | calibrations are applied per-sample |
| Change a channel's `alarms` | yes — hot | safety reads alarms at evaluation |
| Change a channel's `plot_group` | yes — hot | UI-only metadata |
| Change anything under `domain_profile.metadata` | yes — hot | profile metadata is read at arm |
| Change the procedure id or config | yes — hot | procedure is constructed at arm |
| Change method steps | yes — hot | method is loaded at arm |

The Apply & Connect action in the Setup tab is what triggers the
rebuild when needed. Between runs the WorkerPool stays open so the
Sartorius cold-open race and other open-once costs are paid once per
config load, not once per run.

## Workspace conventions

Capa expects a workspace laid out as:

```
<workspace>/
├── configs/
│   ├── experiments/
│   ├── hardware/
│   ├── methods/
│   └── calibrations/
│       └── flux/        # one subdirectory per calibration family
├── runs/                # bundle output root (overridable per experiment)
└── plugins.lock         # trusted-plugin manifest (optional; XDG fallback)
```

[Workspace layout](workspace-layout.md) covers path overrides
(`CAPA_CONFIGS`, `CAPA_RUNS_ROOT`), disk-space monitoring, and backup
recommendations.

## See also

- [Experiment YAML](experiment-yaml.md) — every field, end-to-end.
- [Hardware TOML](hardware-toml.md) — devices and channels.
- [Method TOML](method-toml.md) — step kinds.
- [Channel bindings](channel-bindings.md) — how channels read from
  adapters.
- [CAPA profile fields](capa-profile.md) — the scientific layer.
- [Validation and problems](validation-and-problems.md) — when each
  check runs and how problems surface.
