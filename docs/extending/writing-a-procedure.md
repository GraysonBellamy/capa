# Writing a procedure

> **Status:** stub — content to be written.

**Audience:** plugin authors.
**Scope:** building a custom procedure: subclass ``ProcedureBase``, declare config schema, run a step graph.

## Will cover

- Subclass and entry-point registration
- Pydantic config schema for procedure parameters
- Step graph vs free-form ``run()``
- Authorizing device writes via ``capa.experiment.authorization``
- **Required:** wrap CPU work in ``anyio.to_thread.run_sync``
- Testing without hardware

*See also:* [Authorization gates](../safety/authorization-gates.md), [Saturation and deadlines](../safety/saturation-and-deadlines.md).

