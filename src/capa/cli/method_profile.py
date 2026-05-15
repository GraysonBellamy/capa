"""``capa method validate`` / ``capa profile validate`` — file-level checks."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from capa.core.errors import CapaError
from capa.core.logging import configure_pre_run_logging
from capa.experiment.config import ExperimentConfig

method_app = typer.Typer(name="method", help="Method-file utilities.", no_args_is_help=True)
profile_app = typer.Typer(name="profile", help="Domain-profile utilities.", no_args_is_help=True)


@method_app.command("validate")
def method_validate(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
) -> None:
    """Validate a standalone method file (TOML or YAML).

    Useful for a quick lint without loading a full experiment config."""
    import tomllib  # noqa: PLC0415

    from capa.experiment.method import Method  # noqa: PLC0415

    configure_pre_run_logging()
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        from ruamel.yaml import YAML  # noqa: PLC0415

        with open(path, encoding="utf-8") as fp:
            data = YAML(typ="safe").load(fp)
    elif suffix == ".toml":
        with open(path, "rb") as fp:
            data = tomllib.load(fp)
    else:
        typer.secho(f"validate: unsupported suffix {suffix!r}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=2)

    try:
        method = Method.model_validate(data)
    except Exception as exc:
        typer.secho(f"validate: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc
    typer.echo(f"OK: {path}")
    typer.echo(f"  method:  {method.name}")
    typer.echo(f"  steps:   {len(method.steps)}")
    for idx, step in enumerate(method.steps):
        target = getattr(step, "target", None)
        target_name = target.name if target is not None else "-"
        typer.echo(f"    [{idx:02d}] {step.kind:14s}  target={target_name}")


@profile_app.command("validate")
def profile_validate(
    config: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
) -> None:
    """Validate the domain-profile metadata block of an experiment config.

    Loads the full :class:`ExperimentConfig`, then re-validates the
    profile-specific metadata against the active profile's metadata model
    (CAPA's :class:`CapaPyrolysisMetadata`, etc.). Does not run preflight
    checks — those need a live engine."""
    configure_pre_run_logging()
    try:
        ec = ExperimentConfig.load(config)
    except CapaError as exc:
        typer.secho(f"profile validate: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc

    if ec.domain_profile is None:
        typer.echo(f"OK: {config} (no domain_profile)")
        return

    profile_id = ec.domain_profile.id
    meta_validator: Callable[[dict[str, object]], object]
    if "capa_pyrolysis" in profile_id:
        from capa.experiment.profiles.capa_pyrolysis import (  # noqa: PLC0415
            validate_metadata as meta_validator,
        )
    elif "cone_calorimeter" in profile_id:
        from capa.experiment.profiles.cone_calorimeter import (  # noqa: PLC0415
            validate_metadata as meta_validator,
        )
    else:
        typer.secho(
            f"profile validate: unknown profile {profile_id!r}",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)

    try:
        meta_validator(ec.domain_profile.metadata)
    except Exception as exc:
        typer.secho(f"profile validate: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc
    typer.echo(f"OK: {config}")
    typer.echo(f"  profile: {profile_id}")
    typer.echo(f"  standards: {', '.join(ec.domain_profile.standard_refs) or '(none)'}")
