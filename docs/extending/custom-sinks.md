# Custom sinks

> **Status:** stub — content to be written.

**Audience:** integrators wiring capa to additional storage.
**Scope:** building a custom writer sink (e.g. InfluxDB, Postgres, S3) that the writer thread invokes.

## Will cover

- The sink contract
- Per-sink batching responsibilities
- Backpressure and the writer-inbox depth
- Failure modes (sink down vs sink slow)
- Registering via entry point

