# Diagnostics dock

> **Status:** stub — content to be written.

**Audience:** operators triaging "why is this run weird?", contributors debugging adapters.
**Scope:** every metric panel in the diagnostics dock: per-worker rates, bridge depths, conductor heartbeat, writer-thread health.

## Will cover

- Per-worker emission rate vs configured rate
- Per-bridge depth and ``blocked_since_ms``
- Conductor loop lag distribution
- Writer-thread inbox depth and fsync latency
- How diagnostics relate to the status bar pills

*See also:* [Status bar](status-bar-guide.md), [Runtime topology](../architecture/runtime-architecture.md).

