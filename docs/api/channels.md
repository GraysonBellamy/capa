---
description: capa.channels API — runtime channel registry, channel spec, source bindings (device_field, derived, manual), units, and per-channel calibration glue.
---

# `capa.channels`

Channel registry, specs, source bindings, and per-channel calibration
glue. The registry is the runtime authority for "what channels exist,
in what units, sourced from where."

**Narrative guides:**

- [Channel bindings](../configuration/channel-bindings.md) — binding
  kinds (`device_field`, `derived`, `manual`) and how records become
  samples.
- [Channel pipeline](../architecture/channel-pipeline.md) — the
  device-emission → sample path the registry sits on.
- [Calibrations on disk](../configuration/calibrations.md) — file
  format for per-channel calibration entries.

::: capa.channels
