"""``capa config validate`` — layered diagnostics for the Setup editor."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from capa.core.errors import CapaError
from capa.core.logging import configure_pre_run_logging

config_app = typer.Typer(
    name="config",
    help="Validate and inspect experiment configs.",
    no_args_is_help=True,
)


@config_app.command("validate")
def config_validate(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    live: Annotated[
        bool,
        typer.Option(
            "--live",
            help="Also run Layer 5 (live discovery + handshake) — touches hardware.",
        ),
    ] = False,
) -> None:
    """Run the layered validation pipeline against an experiment config.

    Headless equivalent of the Setup tab's Problems panel: prints each
    finding with section + path + severity. Exits non-zero if any error
    is found; warnings and info do not change the exit code.
    """
    configure_pre_run_logging()
    # Local imports keep capa's startup path lean — the validation
    # surface only loads when the operator actually asks for it.
    from capa.config import ConfigDocument  # noqa: PLC0415
    from capa.config import validate as run_validate  # noqa: PLC0415

    try:
        document = ConfigDocument.load(path)
    except CapaError as exc:
        typer.secho(f"config validate: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc

    problems = run_validate(document, with_live_checks=live)
    error_count = sum(1 for p in problems if p.severity == "error")
    warn_count = sum(1 for p in problems if p.severity == "warning")
    info_count = sum(1 for p in problems if p.severity == "info")

    for p in problems:
        path_str = ".".join(str(x) for x in p.path) if p.path else "-"
        colour = {
            "error": typer.colors.RED,
            "warning": typer.colors.YELLOW,
            "info": typer.colors.BLUE,
        }.get(p.severity, typer.colors.WHITE)
        typer.secho(
            f"[{p.severity}] {p.section}.{path_str} :: {p.code}",
            fg=colour,
        )
        typer.echo(f"    {p.message}")
        if p.source_file is not None:
            typer.echo(f"    source: {p.source_file}")

    summary = f"{error_count} error(s), {warn_count} warning(s), {info_count} info"
    if error_count == 0 and warn_count == 0:
        typer.secho(f"OK: {path}  ({summary})", fg=typer.colors.GREEN)
    else:
        typer.echo(summary)
    if error_count > 0:
        raise typer.Exit(code=2)
