"""``capa`` CLI package.

The ``[project.scripts]`` entry point in ``pyproject.toml`` points at
:func:`capa.cli.main`; tests and ad-hoc callers can import ``app`` (the
root Typer instance) or ``main`` directly from this package.
"""

from __future__ import annotations

from capa.cli.main import app, main

__all__ = ["app", "main"]
