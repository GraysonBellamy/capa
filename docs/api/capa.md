# `capa`

Top-level package — re-exports ``__version__`` and otherwise stays
intentionally thin. Public surface lives in the subpackages below.

**Subpackages:**

- [`capa.runtime`](runtime.md) — conductor, worker pool, bridges, dispatchers.
- [`capa.devices`](devices.md) — adapter contract and per-family adapters.
- [`capa.config`](config.md) — config IO, validation, problems.
- [`capa.channels`](channels.md) — channel registry, spec, bindings.
- [`capa.experiment`](experiment.md) — procedures, profiles, method executor.
- [`capa.storage`](storage.md) — run bundle and writer thread.
- [`capa.ui`](ui.md) — PySide6 widgets and main window.
- [`capa.core`](core.md) — primitives (clock, databus, units, plugins).
- [`capa.calibration`](calibration.md) — tune-procedure artifacts.
- [`capa.cli`](cli.md) — Typer dispatcher.

::: capa
