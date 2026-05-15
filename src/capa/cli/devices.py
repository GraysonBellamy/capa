"""``capa devices discover`` — non-camera device probe."""

from __future__ import annotations

from typing import Annotated

import anyio
import typer

from capa.cli._helpers import collect_discovery_rows, emit_discovery_rows
from capa.core.logging import configure_pre_run_logging
from capa.devices.discovery import discoverable_descriptors

devices_app = typer.Typer(
    name="devices",
    help="Discover hardware visible on the local system.",
    no_args_is_help=True,
)


@devices_app.command("discover")
def devices_discover(
    adapter: Annotated[
        str | None,
        typer.Option(
            "--adapter",
            help="Only probe the named adapter (watlow|alicat|sartorius|nidaq). "
            "Default: probe every real adapter.",
        ),
    ] = None,
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON instead of a table."),
    ] = False,
) -> None:
    """Discover non-camera devices visible on the local system.

    Uses the same descriptor-driven path as ``capa hardware discover``.
    This older command stays scoped to non-camera adapters so scripts that
    expected serial/DAQ output do not suddenly see video devices.
    """
    configure_pre_run_logging()
    descriptors = discoverable_descriptors(adapter=adapter, include_cameras=False)
    if adapter is not None and not descriptors:
        typer.secho(
            f"discover: unknown adapter {adapter!r}",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)

    rows, notes = anyio.run(collect_discovery_rows, descriptors)
    emit_discovery_rows(rows, notes, json_out=json_out)
