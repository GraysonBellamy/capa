---
description: capa.devices API — the async DeviceAdapter Protocol and per-family adapters for Watlow, Alicat, Sartorius, NI-DAQ, USB webcams, FLIR IR cameras, and simulators.
---

# `capa.devices`

Device adapters and descriptor-driven discovery. Every adapter
implements the same :class:`DeviceAdapter` Protocol — the runtime
treats Watlow loops, Alicat MFCs, Sartorius balances, NI-DAQ tasks,
and cameras through the same lifecycle.

**Narrative guides:**

- [Devices overview](../devices/overview.md) — the adapter contract,
  resource grouping, emission shapes.
- [Discovery](../devices/discovery.md) — how the catalog probes hardware.
- Per-family pages: [Watlow](../devices/watlow.md),
  [Alicat](../devices/alicat.md), [Sartorius](../devices/sartorius.md),
  [NI-DAQ](../devices/nidaq.md), [Webcams](../devices/cameras-webcam.md),
  [FLIR](../devices/cameras-flir.md),
  [Simulators](../devices/simulators.md).
- [Writing a device adapter](../extending/writing-a-device-adapter.md) —
  authoring guide for plugin authors.

::: capa.devices
