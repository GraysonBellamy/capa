"""``capa validate`` — top-level config validation command."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import anyio
import typer

from capa.core.errors import CapaError
from capa.core.logging import configure_pre_run_logging
from capa.devices.registry import require_descriptor
from capa.experiment.config import ExperimentConfig


def validate(
    config: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Open + close each declared adapter (read-only handshake).",
        ),
    ] = False,
) -> None:
    """Validate an experiment config without running it.

    Pydantic-validates, resolves plugin refs, and (with ``--strict``) opens
    each declared adapter to surface wiring errors before arming. Non-zero
    on any problem.
    """
    configure_pre_run_logging()
    try:
        ec = ExperimentConfig.load(config)
    except CapaError as exc:
        typer.secho(f"validate: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        typer.secho(f"validate: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc

    typer.echo(f"OK: {config}")
    typer.echo(f"  hardware:  {ec.hardware.name} ({len(ec.hardware.devices)} devices)")
    typer.echo(f"  channels:  {len(ec.hardware.channels)}")
    typer.echo(f"  procedure: {ec.procedure.id}")
    if ec.method is not None:
        typer.echo("  method:    present")

    if strict:
        # Look each adapter up in the registry and, if its module
        # exposes a ``handshake(params)`` async function, run a
        # non-disruptive read-only open + identify + close against the
        # declared hardware — no setpoint writes. Modules without a
        # ``handshake`` hook fall back to the descriptor-only check.
        import importlib  # noqa: PLC0415

        for dev in ec.hardware.devices:
            try:
                descriptor = require_descriptor(dev.adapter)
            except KeyError as exc:
                typer.secho(f"  strict: {dev.name}: {exc}", err=True, fg=typer.colors.RED)
                raise typer.Exit(code=2) from exc

            factory = descriptor.adapter_factory
            module = importlib.import_module(dev.adapter)
            handshake_fn = getattr(module, "handshake", None)
            if handshake_fn is None:
                factory_label = getattr(factory, "__name__", repr(factory))
                typer.echo(
                    f"  strict: {dev.name} -> {dev.adapter}.{factory_label} "
                    f"(no handshake hook; descriptor-only check)"
                )
                continue
            try:
                summary = anyio.run(handshake_fn, dev.params)
            except CapaError as exc:
                typer.secho(
                    f"  strict: {dev.name}: handshake failed: {exc}",
                    err=True,
                    fg=typer.colors.RED,
                )
                raise typer.Exit(code=2) from exc
            except Exception as exc:
                typer.secho(
                    f"  strict: {dev.name}: handshake raised {type(exc).__name__}: {exc}",
                    err=True,
                    fg=typer.colors.RED,
                )
                raise typer.Exit(code=2) from exc
            typer.echo(f"  strict: {dev.name} -> {summary}")
