---
description: Pointer page for capa calibration configs on disk — channel-calibration TOML sets under `configs/calibrations/` and orthogonal heat-flux tune artifact system.
---

# Calibrations on disk

**Audience:** config authors trying to find the calibration documentation from the Configuration nav.

**Scope:** this page is a pointer. The actual content lives in two pages under [Calibration](../calibration/overview.md):

- **[Calibration sets](../calibration/calibration-sets.md)** is the single source of truth for the channel-calibration TOML schema, every transform kind (`identity`, `linear_two_point`, `polynomial`, `lookup`, `piecewise`, `custom_callable`), the `UncertaintySpec` discipline, and the Setup-tab apply-set diff dialog.
- **[Tune artifacts](../calibration/tune-artifacts.md)** covers the orthogonal heat-flux artifact subsystem — `configs/calibrations/flux/<id>.toml` and `latest.toml`.

The two subsystems share a directory tree and nothing else. [Calibration overview](../calibration/overview.md) explains why.

---

## Where calibrations fit in the configuration story

The four configuration "kinds" in capa are:

| Kind | File | Topic |
|---|---|---|
| Hardware profile | `*.toml` | What devices exist, what channels they expose. See [Hardware TOML](hardware-toml.md). |
| Method | `*.method.toml` | The scripted step sequence. See [Method TOML](method-toml.md). |
| Experiment | `*.yaml` | Stitches the others together for one run. See [Experiment YAML](experiment-yaml.md). |
| **Calibrations** | `configs/calibrations/*.toml` | **This page.** |

An experiment YAML doesn't reference a calibration set. Each channel's curve
lives on the channel in the hardware profile; a set is a file of curves the
Setup tab's Calibration section copies onto channels, or exports them to. The
bundle's `config.toml` records every channel's curve as the run used it.

A tune artifact is **not** referenced from the experiment YAML directly — operators cite it (free-form) inside the [CAPA profile](capa-profile.md#heaterprogram)'s `HeaterProgram.flux_calibration_ref` field, and the tune-procedure itself reads `configs/calibrations/flux/latest.toml` to pick its initial-setpoint prior.

For everything else, read [Calibration sets](../calibration/calibration-sets.md).
