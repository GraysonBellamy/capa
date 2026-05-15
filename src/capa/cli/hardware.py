"""``capa hardware {validate,check,discover,new}`` — hardware-profile commands."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import anyio
import typer

from capa.cli._helpers import (
    collect_discovery_rows,
    emit_discovery_rows,
    render_problems,
)
from capa.core.errors import CapaError
from capa.core.logging import configure_pre_run_logging
from capa.devices.discovery import discoverable_descriptors

hardware_app = typer.Typer(
    name="hardware",
    help="Author and probe hardware profiles.",
    no_args_is_help=True,
)


_HARDWARE_VALIDATE_STUB_EXPERIMENT: dict[str, Any] = {
    "operator": {"id": "_hardware_validate_stub"},
    "sample": {"id": "_hardware_validate_stub"},
    "procedure": {"id": "capa.builtin.recipe_runner", "version": "0.1"},
    "calibration_set": {"name": "default"},
}
"""Placeholder experiment-side fields injected by ``capa hardware
validate`` so the layered pipeline can run against a hardware-only
TOML. The values are never persisted — they live in the in-memory
:class:`ConfigDocument` for the duration of the call so Layers 1-2 +
Layer 4 can run their hardware-relevant checks without complaining
about missing operator/sample/procedure entries."""


@hardware_app.command("validate")
def hardware_validate(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
) -> None:
    """Validate a hardware TOML against Layers 1-2 + 4 (no live probes).

    Stubs the experiment-side fields that an
    experiment file would normally carry so the layered pipeline
    surfaces *hardware* errors (channel binding references a missing
    device, duplicate device names, resource-id conflicts on the same
    serial port) without complaining about absent
    ``procedure`` / ``operator`` / ``sample`` entries. For full
    experiment-level validation use ``capa config validate``.
    """
    configure_pre_run_logging()
    from capa.config import ConfigDocument  # noqa: PLC0415
    from capa.config import validate as run_validate  # noqa: PLC0415

    try:
        document = ConfigDocument.load_hardware_only(path)
    except CapaError as exc:
        typer.secho(f"hardware validate: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc

    document.experiment_payload.update(_HARDWARE_VALIDATE_STUB_EXPERIMENT)
    problems = run_validate(document, with_live_checks=False)
    errors, _, _ = render_problems(problems, source=path)
    if errors > 0:
        raise typer.Exit(code=2)


@hardware_app.command("check")
def hardware_check(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
) -> None:
    """Run Layer 5 (live handshake) against an experiment / hardware file.

    Read-only: opens each adapter, identifies it, and closes — no
    setpoints written. Headless equivalent of the Setup tab's
    *Check Hardware* button.
    """
    configure_pre_run_logging()
    from capa.config import (  # noqa: PLC0415
        ConfigDocument,
        validate_live_async,
    )

    try:
        # ``check`` accepts either an experiment or a hardware file —
        # try the experiment path first and fall back to hardware-only.
        try:
            document = ConfigDocument.load(path)
        except CapaError:
            document = ConfigDocument.load_hardware_only(path)
    except CapaError as exc:
        typer.secho(f"hardware check: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc

    problems = anyio.run(validate_live_async, document)
    errors, _, _ = render_problems(problems, source=path)
    if errors > 0:
        raise typer.Exit(code=2)


@hardware_app.command("discover")
def hardware_discover(
    adapter: Annotated[
        str | None,
        typer.Option(
            "--adapter",
            help=(
                "Probe only the named adapter (watlow|alicat|sartorius|"
                "nidaq|camera_visible|camera_ir). Default: probe every"
                " discoverable adapter."
            ),
        ),
    ] = None,
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON instead of a table."),
    ] = False,
) -> None:
    """Discover every adapter family visible on the local system.

    Differs from ``capa devices discover`` by routing through the
    :class:`AdapterDescriptor` registry, which means the output
    includes cameras and any plugin adapters that registered via
    the ``capa.adapters`` / ``capa.cameras`` entry-point groups.
    """
    configure_pre_run_logging()
    descriptors = discoverable_descriptors(adapter=adapter, include_cameras=True)
    if adapter is not None and not descriptors:
        typer.secho(
            f"discover: no discoverable adapter matches {adapter!r}",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)

    rows, notes = anyio.run(collect_discovery_rows, descriptors)
    emit_discovery_rows(rows, notes, json_out=json_out)


@hardware_app.command("new")
def hardware_new(
    path: Annotated[Path, typer.Argument(dir_okay=False)],
    name: Annotated[
        str,
        typer.Option("--name", help="HardwareProfile.name field."),
    ] = "new_profile",
) -> None:
    """Write a minimal blank hardware TOML at ``path``.

    Produces a valid empty :class:`HardwareProfile` so operators can
    ``capa hardware new ./configs/hardware/x.toml`` and immediately
    open the file in the Setup tab.
    """
    if path.exists():
        typer.secho(
            f"hardware new: refusing to overwrite existing file {path}",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)

    from capa.config import ConfigDocument, SourceLayout  # noqa: PLC0415

    doc = ConfigDocument(
        hardware_payload={
            "name": name,
            "devices": [],
            "channels": [],
            "cameras": [],
        },
    )
    layout = SourceLayout(
        experiment_path=None,
        experiment_format=None,
        hardware_path=path,
        hardware_format="toml",
        hardware_mode="external",
        method_path=None,
        method_format=None,
        method_mode="none",
    )
    try:
        doc.save_as(layout)
    except CapaError as exc:
        typer.secho(f"hardware new: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc
    typer.secho(f"wrote {path}", fg=typer.colors.GREEN)
