---
description: capa — Python control and DAQ for a controlled-atmosphere pyrolysis lab instrument driving NI-DAQ, Watlow, Alicat, Sartorius, FLIR into sealed bundles.
hide:
  - navigation
  - toc
---

# capa

Control and DAQ for a custom **controlled-atmosphere pyrolysis** lab
instrument (cone-calorimeter-class). capa drives a heterogeneous rig —
NI-DAQ, Watlow heaters, Alicat mass-flow controllers, Sartorius balances,
USB and FLIR IR cameras — through async-first device libraries, records
every run as a self-contained on-disk **bundle**, and treats research
workflows (calibrations, custom routines) as first-class plugins.

!!! warning "Pre-alpha"
    The public surface of `capa.runtime`, `capa.devices`, and
    `capa.config` is unstable. Pin to a commit.

## Pick a path

<div class="grid cards" markdown>

-   :lucide-rocket:{ .lg .middle } &nbsp; **I'm new — give me a feel for it**

    ---

    First sealed bundle in about five minutes, no hardware required.
    Walks you from launch to a finished run on the built-in simulator.

    [:octicons-arrow-right-24: Quick start](getting-started/quick-start.md)

-   :lucide-cog:{ .lg .middle } &nbsp; **I'm setting up a real rig**

    ---

    Hardware install, first wiring, your first real bundle. Then the
    daily cadence and where to look when something feels off.

    [:octicons-arrow-right-24: Your first real run](getting-started/first-real-run.md) ·
    [Hardware TOML](configuration/hardware-toml.md)

-   :lucide-flask-conical:{ .lg .middle } &nbsp; **I run experiments here every day**

    ---

    The operator handbook, the Setup / Run / Method tabs, the manual
    controls, and how to abort safely.

    [:octicons-arrow-right-24: Operator handbook](user-guide/operator-handbook.md) ·
    [Daily workflow](getting-started/daily-workflow.md)

-   :lucide-bar-chart-3:{ .lg .middle } &nbsp; **I'm reading a finished bundle**

    ---

    What's in the directory, how to read the parquet and sqlite files,
    and how to verify a bundle is sealed and intact.

    [:octicons-arrow-right-24: What's in a bundle](bundles/what-is-a-bundle.md) ·
    [Reading a bundle](bundles/reading-bundles.md)

-   :lucide-blocks:{ .lg .middle } &nbsp; **I want to write a plugin**

    ---

    Procedures, profiles, device adapters, custom sinks, and method
    steps — all of these are entry-point-discovered plugins.

    [:octicons-arrow-right-24: Writing a procedure](extending/writing-a-procedure.md) ·
    [Plugin system](extending/plugin-system.md)

-   :lucide-code-2:{ .lg .middle } &nbsp; **I'm touching the source**

    ---

    The runtime topology, the threading model, the channel pipeline,
    and how to run the test suite locally.

    [:octicons-arrow-right-24: Runtime topology](architecture/runtime-architecture.md) ·
    [Dev setup](contributing/dev-setup.md)

</div>

## Three concepts that unlock the rest

<div class="grid cards" markdown>

-   :lucide-package:{ .lg .middle } &nbsp; **Bundle**

    ---

    A self-contained on-disk record of one run — config, method,
    calibration snapshot, equipment manifest, events, samples, video.
    Five years from now you can open it and know exactly what was
    measured.

    [:octicons-arrow-right-24: Deep dive](bundles/what-is-a-bundle.md)

-   :lucide-radio:{ .lg .middle } &nbsp; **Channel**

    ---

    A named quantity (`heater_pv`, `mfc_flow`) bound to a device
    parameter. Channels never store data themselves — they read from
    bindings.

    [:octicons-arrow-right-24: Channel bindings](configuration/channel-bindings.md)

-   :lucide-workflow:{ .lg .middle } &nbsp; **Procedure**

    ---

    The class of experiment (free run, recipe runner, heat-flux tune,
    …). Procedures are plugins, not core code.

    [:octicons-arrow-right-24: What is a procedure](procedures/what-is-a-procedure.md)

</div>

See the [Glossary](glossary.md) for the full vocabulary.

## Status

Beta runtime, alpha UX, pre-alpha API. The per-resource-worker runtime
(`Conductor` + `WorkerPool`) is the current production runtime — the
single-loop `ExperimentEngine` is gone. The unstable-API and development
sections also live in the [README](https://github.com/GraysonBellamy/capa).
