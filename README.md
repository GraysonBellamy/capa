# capa

Control and DAQ application for a custom cone-calorimeter-class lab instrument.

See [docs/capa-plan.md](docs/capa-plan.md) for the high-level architecture
plan, and [docs/per-resource-worker-migration.md](docs/per-resource-worker-migration.md)
for the runtime layer (per-resource workers, conductor / pool / manual
client) that supersedes the legacy single-loop engine described in
`capa-plan.md`.

## Status

Pre-alpha. Phase 4 of the per-resource-worker migration is complete:
the single-loop `ExperimentEngine` has been replaced by `Conductor` +
`WorkerPool` (`src/capa/runtime/`); the GUI's `RunController` runs
against the new stack; the legacy `engine.py`, `cameras.py`, and
`registry.py` modules are deleted.

## Development

Requires Python 3.13 and [uv](https://github.com/astral-sh/uv). Sibling device
libraries (`alicatlib`, `watlowlib`, `sartoriuslib`, `nidaqlib`) must be checked
out in the parent directory.

```sh
uv sync --extra dev
uv run ruff check
uv run mypy
uv run pytest
```
