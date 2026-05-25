# `capa.runtime`

Per-resource-worker runtime. Each hardware resource gets its own
thread hosting its own asyncio event loop; a per-run :class:`Conductor`
coordinates them through :class:`ThreadBridge` queues.

**Narrative guides:**

- [Runtime topology](../architecture/runtime-architecture.md) — the
  conductor / pool / worker structure end-to-end.
- [Threading model](../architecture/threading-model.md) — which loop
  owns which object, and what crosses a bridge.
- [Channel pipeline](../architecture/channel-pipeline.md) — how device
  emissions become channel samples.
- [Saturation and deadlines](../safety/saturation-and-deadlines.md) —
  the 10 s output-deadline contract enforced by :class:`SaturationMonitor`.
- [Authorization gates](../safety/authorization-gates.md) — the
  pre-arm contract every conductor run honors.

::: capa.runtime
