---
description: capa API reference index — auto-generated docstrings for runtime, devices, config, channels, experiment, storage, ui, core, calibration, and cli subpackages.
---

# API reference

Auto-generated from source docstrings via
[mkdocstrings-python](https://mkdocstrings.github.io/python/). The
narrative guides ([Devices](../devices/overview.md),
[Procedures](../procedures/what-is-a-procedure.md),
[Configuration](../configuration/overview.md), …) link back to the
relevant sections here.

## Top-level

- [`capa`](capa.md) — top-level re-exports.

## Subpackages

- [`capa.runtime`](runtime.md) — `Conductor`, `WorkerPool`, `Worker`, `ManualClient`, bridges, dispatchers.
- [`capa.devices`](devices.md) — adapter contract and per-family adapters (Watlow, Alicat, Sartorius, NI-DAQ, cameras, simulators).
- [`capa.config`](config.md) — config models, canonicalization, validation, problems.
- [`capa.channels`](channels.md) — channel registry, spec, calibration.
- [`capa.experiment`](experiment.md) — procedures, profiles, method executor, authorization.
- [`capa.storage`](storage.md) — writer thread, per-sink writers, bundle finalize / integrity.
- [`capa.ui`](ui.md) — PySide6 widgets, tabs, docks, main window.
- [`capa.core`](core.md) — clock, databus, ring buffers, backpressure, units, logging, plugins.
- [`capa.calibration`](calibration.md) — tune artifacts.
- [`capa.cli`](cli.md) — Typer dispatcher and sub-apps.

> **Status:** the public surface of `capa.runtime`, `capa.devices`,
> and `capa.config` is unstable. Pin to a specific commit if you
> build against capa today.
