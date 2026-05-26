---
description: capa.config API — ConfigDocument IO, canonicalization, validation pipeline, ConfigProblem taxonomy, and the frozen ExperimentConfig produced for the runtime.
---

# `capa.config`

Config IO and validation surface. :class:`ConfigDocument` tracks
where a draft came from (file path, inline vs external hardware /
method); the validation pipeline turns it into a frozen
:class:`~capa.experiment.config.ExperimentConfig`.

**Narrative guides:**

- [Configuration overview](../configuration/overview.md) — the four
  config kinds and how they compose.
- [Experiment YAML](../configuration/experiment-yaml.md),
  [Hardware TOML](../configuration/hardware-toml.md),
  [Method TOML](../configuration/method-toml.md).
- [Validation and problems](../configuration/validation-and-problems.md) —
  the :class:`ConfigProblem` taxonomy.

::: capa.config
