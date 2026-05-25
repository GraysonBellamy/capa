# capa

Control and DAQ for a custom **controlled-atmosphere pyrolysis** lab
instrument (cone-calorimeter-class). capa drives a heterogeneous rig —
NI-DAQ, Watlow heaters, Alicat mass-flow controllers, Sartorius balances,
USB and FLIR IR cameras — through async-first device libraries, records
every run as a self-contained on-disk **bundle**, and treats research
workflows (calibrations, custom routines) as first-class plugins.

> **Status:** pre-alpha. The public surface of `capa.runtime`,
> `capa.devices`, and `capa.config` is unstable; pin to a commit.

## Where to start

| If you are… | Start here |
|---|---|
| New, want a feel for the app | [Quick start (simulator)](getting-started/quick-start.md) |
| Setting up a real rig | [Your first real run](getting-started/first-real-run.md) → [Hardware TOML](configuration/hardware-toml.md) |
| Running daily experiments | [Daily operator workflow](getting-started/daily-workflow.md) → [The Setup tab](user-guide/the-setup-tab.md) |
| Building methods | [The Method tab](user-guide/the-method-tab.md) → [Method TOML](configuration/method-toml.md) |
| Writing a procedure plugin | [Writing a procedure](extending/writing-a-procedure.md) |
| Reading a bundle | [What's in a bundle](bundles/what-is-a-bundle.md) → [Reading a bundle](bundles/reading-bundles.md) |
| Touching `src/capa/runtime/` | [Runtime topology](architecture/runtime-architecture.md) |
| Just want a definition | [Glossary](glossary.md) |

## Three concepts that unlock the rest

- **Bundle.** Every run produces a self-contained on-disk record (config,
  method, calibration snapshot, equipment manifest, events, samples,
  video). Five years from now you can open it and know exactly what was
  measured.
- **Channel.** A named quantity (`heater_pv`, `mfc_flow`) bound to a
  device parameter. Channels never store data themselves — they read
  from bindings.
- **Procedure.** The class of experiment (free run, recipe runner,
  heat-flux tune, …). Procedures are plugins, not core code.

See [Glossary](glossary.md) for the full vocabulary.

## Status

Beta runtime, alpha UX, pre-alpha API. The per-resource-worker runtime
(`Conductor` + `WorkerPool`) is the current production runtime —
the single-loop `ExperimentEngine` is gone.

The unstable-API and development sections below also live in the
[README](https://github.com/GraysonBellamy/capa).
