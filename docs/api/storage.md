---
description: capa.storage API — writer thread, per-sink writers, Arrow-IPC-then-rewrite bundle finalize, sha256 integrity sealing, and the bundle_status state machine.
---

# `capa.storage`

The run bundle: writer thread, per-sink writers, finalize-in-place,
integrity hashing. The runtime emits; this layer is the only place
that touches disk in the data path.

**Narrative guides:**

- [What's in a bundle](../bundles/what-is-a-bundle.md) — the on-disk
  tour.
- [Manifest and schema](../bundles/manifest-and-schema.md).
- [Bundle write path](../architecture/bundle-write-path.md) — the
  Arrow-IPC-then-rewrite finalize protocol.
- [Integrity and sealing](../bundles/integrity-and-sealing.md) —
  ``sha256sum``-compatible artifact hashing and the
  ``bundle_status`` state machine.
- [Reading a bundle](../bundles/reading-bundles.md) — analyst recipes.

::: capa.storage
