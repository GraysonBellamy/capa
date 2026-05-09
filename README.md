# capa

Control and DAQ application for a custom cone-calorimeter-class lab instrument.

See [capa-plan.md](capa-plan.md) for the full architecture plan.

## Status

Pre-alpha. Phase **P0a — Schema + sim substrate** in progress (see plan §16).

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
