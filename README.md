# capa

Control and DAQ application for a custom cone-calorimeter-class lab instrument.

See [docs/architecture/runtime-architecture.md](docs/architecture/runtime-architecture.md)
for the current runtime layer (per-resource workers, conductor / pool /
manual client) and [docs/architecture/capa-plan.md](docs/architecture/capa-plan.md)
for the high-level architecture plan (parts predating the runtime cutover
are marked stale).

## Status

Pre-alpha. The per-resource-worker runtime is the current runtime:
the single-loop `ExperimentEngine` has been replaced by `Conductor` +
`WorkerPool` (`src/capa/runtime/`), and the GUI's `RunController` runs
against that stack.

## Unstable API

The public surface of `capa.runtime`, `capa.devices`, and `capa.config` is
**unstable**. Imports, exception types, and adapter contracts may change
between commits without backward-compatibility shims. Pin to a specific
commit if you are building against capa today.

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
