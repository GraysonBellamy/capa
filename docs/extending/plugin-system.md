---
description: capa's plugin model — entry-point discovery for procedures, device adapter descriptors, and domain profiles, with plugins.lock gating third-party procedures.
---

# Plugin system

**Audience:** plugin authors and integrators, on their first read.
**Scope:** what a "plugin" means in capa today, which kinds are entry-point-discoverable, and what the trust model is.

---

## What a plugin is

A capa **plugin** is a Python distribution installed in the same environment as capa that exposes a contracted object through a [PEP 621 entry point](https://packaging.python.org/en/latest/specifications/entry-points/). In the current engine, the fully supported plugin surface is **procedure plugins**: capa walks `capa.procedures`, loads each class, runs a load-time contract check, and (in production mode) gates it against `plugins.lock` before adding it to the procedure registry.

Other extension points use entry points too, but they do not all share that lifecycle. Device adapter descriptors can be discovered through `capa.adapters` / `capa.cameras` for Setup and discovery surfaces, and profile entry points are declared for the shipped profiles, but neither is gated by `plugins.lock` today.

There is no hot-reload and no per-user plugin store. A plugin is either *installed* (visible to `pip` / `uv` in this interpreter) or it does not exist as far as capa is concerned.

## What kinds capa supports today

One kind is fully wired through entry-point discovery and the trust gate. Two more are partially wired or descriptor-only. Further extension points exist in the codebase but are not yet entry-point-discoverable; they live as in-code patterns that a plugin can use only by being imported as a library, not by being installed.

| Plugin kind | Entry-point group | Contract source | Built-in examples |
|---|---|---|---|
| Procedure | `capa.procedures` | [`Procedure` Protocol](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/procedures/base.py) | `capa.builtin.free_run`, `recipe_runner`, `batch`, `heat_flux_tune` |
| Device adapter descriptor | `capa.adapters` / `capa.cameras` | [`AdapterDescriptor`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/registry.py) + [`DeviceAdapter` Protocol](https://github.com/GraysonBellamy/capa/blob/main/src/capa/devices/adapter.py) | watlow, alicat, sartorius, nidaq, webcam |
| Domain profile | `capa.profiles` (declared/reserved) | [`DomainProfile` Protocol](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/profiles/base.py) | `capa.profiles.capa_pyrolysis`, `cone_calorimeter` |
| Custom method step | — (not yet) | [`CustomHandler` callable](https://github.com/GraysonBellamy/capa/blob/main/src/capa/experiment/executor.py) | none — registered in-procedure |
| Writer sink | — (not yet) | engine-internal | channel-samples, device-records, video |
| Channel binding kind | — (not yet) | engine-internal | scalar, derived |

Read the row labels precisely: a *procedure* is the only extension type loaded by `capa.core.plugins_runtime` and governed by `plugins.lock`. A *device adapter descriptor* can be offered through `capa.adapters` / `capa.cameras`, but that descriptor path is used by the registry, Setup, and discovery surfaces rather than by the procedure trust gate. Domain profile entry points are declared, but the runtime resolver still hard-matches the shipped profile ids today. See [Writing a device adapter](writing-a-device-adapter.md) and [Writing a profile](writing-a-profile.md) for the current caveats.

The pyproject declaration for the shipped procedure and profile entry points lives at [`pyproject.toml`](https://github.com/GraysonBellamy/capa/blob/main/pyproject.toml):

```toml
[project.entry-points."capa.procedures"]
"capa.builtin.free_run" = "capa.experiment.procedures.builtin.free_run:FreeRun"
# ...

[project.entry-points."capa.profiles"]
"capa.profiles.capa_pyrolysis" = "capa.experiment.profiles.capa_pyrolysis"
"capa.profiles.cone_calorimeter" = "capa.experiment.profiles.cone_calorimeter"
```

Note the right-hand-side shape: procedures point at `module.path:Class` (a class), profiles point at `module.path` (a module whose top-level attributes are the profile fields). The procedure shape is the production plugin path today; the profile shape is documented for authors, but third-party profile runtime discovery still needs resolver work.

---

## What discovery looks like

Procedure discovery happens once at engine startup (and on demand from the CLI). The flow:

```
                        pip install your_plugin
                                  │
                                  ▼
   ┌──────────────────────────────────────────────────────┐
   │  importlib.metadata.entry_points(group=…)            │
   │  → every EntryPoint registered under the group       │
   └──────────────────────────────────────────────────────┘
                                  │
                                  ▼
   ┌──────────────────────────────────────────────────────┐
   │  load_time contract check                            │
   │   • required ClassVars present                       │
   │   • config_model is a Pydantic BaseModel             │
   │   • preflight() and run() are coroutines             │
   └──────────────────────────────────────────────────────┘
                                  │
              ┌───────────────────┴───────────────────┐
              ▼                                       ▼
     production mode                            dev mode (default)
              │                                       │
              ▼                                       ▼
   gate against plugins.lock              load every contract-pass plugin
   • missing from lock → reject           • record drift but do not gate
   • hash/version drift → reject
              │                                       │
              └───────────────────┬───────────────────┘
                                  ▼
                       ProcedureRegistry
                       (visible in Setup tab,
                        callable from `capa run`)
```

The procedure work is done by [`capa.core.plugins_runtime`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/plugins_runtime.py). The function you would call yourself if scripting against this is `discover_procedures(...)`; it returns a [`DiscoveryReport`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/plugins_runtime.py) with two lists — `loaded` and `rejected` — so the CLI can tell the operator about *why* a procedure plugin did not surface.

The plain-language version: an installed procedure plugin appears in capa if it (a) declares the right entry point, (b) survives the contract check, and (c) — in production mode — has a matching entry in `plugins.lock`.

---

## The load-time contract check

`check_procedure_class` in [`plugins_runtime.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/core/plugins_runtime.py) is the only gate between "your code loads" and "your plugin appears in the picker." It is intentionally cheap — it does no I/O — but it is unforgiving about shape mismatches. A class that fails the check is recorded in `DiscoveryReport.rejected` with a one-line reason and never enters the registry.

The check refuses the plugin if any of the following are true:

- It loaded to something other than a class.
- It is missing any of `id`, `name`, `version`, `config_model`.
- `config_model` is not a Pydantic `BaseModel` subclass.
- `preflight` or `run` is missing.
- Either method is defined as a regular function rather than `async def`.

These are the same checks the [Writing a procedure](writing-a-procedure.md) tutorial uses as its acceptance criteria. The tutorial walks you through satisfying them; this page only documents the existence of the gate.

There is no analogous load-time check for third-party profiles today — the shipped profile ids are resolved directly, and the engine reads profile attributes when a run references one of those ids. A general third-party profile resolver still needs a small runtime patch, so treat profiles as a reserved/experimental plugin surface.

---

## The trust model

Procedure discovery runs in one of two modes. `capa run` and `capa gui` read the `CAPA_PLUGIN_MODE` environment variable; `capa plugins list` also has `--plugin-mode` for inspection. There is no `capa run --plugin-mode` flag today.

| Mode | Default | Behaviour |
|---|---|---|
| `dev` | yes | Every contract-pass procedure plugin loads. Drift versus `plugins.lock` is recorded but not enforced. The public CLI discovers installed entry points; the lower-level API has an opt-in `enable_local_plugins_dir=True` hook for tests and embedded harnesses. |
| `production` | no | Procedure plugins not present in `plugins.lock`, or whose hash/version drifted from the lock, are rejected. Local directory loading is disabled because it has no distribution hash. |

Built-in procedures (`capa.builtin.free_run`, `recipe_runner`, `batch`, `heat_flux_tune`) are **always trusted**. They ship with the engine itself, so their hash is computed against the running engine's own distribution; they need no entry in `plugins.lock`, though one may be present for completeness. Third-party plugins enjoy no such pass.

The hash used for the trust gate is computed by `_hash_distribution` over the distribution's `METADATA` and `RECORD` files. This is deliberately cheaper than hashing every file:

- It detects a *wheel swap* — a different version of the package installed over the previous one, or a tampered `RECORD` file.
- It does **not** detect source-file edits in an editable install (`pip install -e .`), because `METADATA` and `RECORD` do not change when an installed source file changes.
- It does **not** detect runtime monkey-patching of the loaded class.

If your trust model demands "any code change invalidates trust," install plugins as wheels (not editable) and treat each new wheel build as requiring a fresh `capa plugins trust`. Editable installs in dev are still valuable for plugin authors; production runs against an editable plugin path are not.

The mechanics of writing and verifying `plugins.lock` live on a separate page — see [Plugin lockfile](plugin-lockfile.md).

---

## The `capa plugins` CLI

Two subcommands sit on top of the discovery and trust logic. Both live in [`src/capa/cli/plugins.py`](https://github.com/GraysonBellamy/capa/blob/main/src/capa/cli/plugins.py).

### `capa plugins list`

Walks `capa.procedures`, runs the contract check, and prints loaded plugins plus any rejections and any drift versus `plugins.lock`. The mode in the header tells you whether the result reflects production gating or dev permissiveness.

```text
plugin mode: dev
plugins.lock: ./plugins.lock (schema v1)

id                                       package              version    hash
capa.builtin.free_run                    capa                 0.1.0      sha256:7b3a2c…
capa.builtin.recipe_runner               capa                 0.1.0      sha256:7b3a2c…
hello.procedure.hold_setpoint            hello-procedure      0.1.0      sha256:f1e9d4…

Rejected:
  bad.example: contract check: class BadProcedure missing async method 'run'
```

Use this any time a procedure plugin you installed does not appear in the procedure picker. Either it failed the contract check (the message will say so) or, in production mode, it failed the trust gate (the drift section will say so).

### `capa plugins trust <plugin-id> --reason "..."`

Adds or refreshes one plugin in `plugins.lock`. Discovers the live entry point, snapshots its current `(version, distribution_hash, entry_point)`, writes the entry, and appends one line to the audit journal (`plugins.lock.journal`). Production runs after this command will accept the plugin.

`--reason` is required and is recorded verbatim in the journal — it is not a comment, it is the audit trail. The expectation is something like `"approved by lab safety officer after demo"`, not `"trust"`.

---

## Where to go next

- Author a procedure — [Writing a procedure](writing-a-procedure.md).
- Author a domain profile (experimental runtime path) — [Writing a profile](writing-a-profile.md).
- Read or maintain `plugins.lock` — [Plugin lockfile](plugin-lockfile.md).
- Author a device adapter descriptor — [Writing a device adapter](writing-a-device-adapter.md).
- Add a one-off method step inside a procedure — [Custom method steps](custom-method-steps.md).
- Wire capa to extra storage (engine-internal contract only today) — [Custom sinks](custom-sinks.md).
