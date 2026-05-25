# Crash recovery

> **Status:** stub — content to be written.

**Audience:** operators after an unclean shutdown.
**Scope:** detecting a partial bundle, running ``capa finalize``, and what gets preserved.

## Will cover

- Detecting a partial bundle on app start (recents list flag)
- ``capa finalize <path>``
- Outcome states and which data survives
- When to discard and re-run vs salvage

*See also:* [capa finalize CLI](../cli/capa-finalize.md), [Integrity and sealing](../bundles/integrity-and-sealing.md).

