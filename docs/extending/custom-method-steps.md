# Custom method steps

> **Status:** stub — content to be written.

**Audience:** method authors with one-off needs.
**Scope:** the ``custom`` step kind: declaring a handler, schema, runtime contract.

## Will cover

- Declaring a custom step in ``.method.toml``
- Implementing the handler
- **Required:** wrap CPU work in ``anyio.to_thread.run_sync``
- Recording events from a custom step
- When to upgrade to a full procedure plugin

*See also:* [Writing a procedure](writing-a-procedure.md).

