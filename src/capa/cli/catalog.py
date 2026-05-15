"""``capa catalog {list,verify,rebuild}`` — run-catalog commands."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from capa.cli._helpers import resolve_runs_root
from capa.core.logging import configure_pre_run_logging
from capa.storage.catalog import CatalogError, RunCatalog

catalog_app = typer.Typer(name="catalog", help="Manage the run catalog.", no_args_is_help=True)


@catalog_app.command("list")
def catalog_list(
    runs_root: Annotated[Path | None, typer.Option("--runs-root")] = None,
    run_status: Annotated[str | None, typer.Option("--run-status")] = None,
    bundle_status: Annotated[str | None, typer.Option("--bundle-status")] = None,
    since: Annotated[
        datetime | None,
        typer.Option("--since", formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"]),
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="JSON-format output.")] = False,
) -> None:
    """Print runs from ``runs.sqlite``."""
    configure_pre_run_logging()
    root = resolve_runs_root(runs_root)
    with RunCatalog(root) as cat:
        rows = cat.list(
            run_status=run_status,  # type: ignore[arg-type]
            bundle_status=bundle_status,  # type: ignore[arg-type]
            since=since,
        )
    if json_out:
        out = [
            {
                "run_id": r.run_id,
                "started_utc": r.started_utc.isoformat(),
                "ended_utc": r.ended_utc.isoformat() if r.ended_utc else None,
                "operator_id": r.operator_id,
                "sample_id": r.sample_id,
                "procedure": r.procedure,
                "run_status": r.run_status,
                "bundle_status": r.bundle_status,
                "integrity_status": r.integrity_status,
                "path": r.path,
            }
            for r in rows
        ]
        typer.echo(json.dumps(out, indent=2))
        return

    if not rows:
        typer.echo("(no runs)")
        return
    typer.echo(f"{'run_id':40}  {'run':10}  {'bundle':22}  {'integrity':10}  sample")
    for r in rows:
        typer.echo(
            f"{r.run_id:40}  {r.run_status:10}  {r.bundle_status:22}  "
            f"{r.integrity_status:10}  {r.sample_id or '-'}"
        )


@catalog_app.command("verify")
def catalog_verify(
    run_id: Annotated[
        str | None,
        typer.Argument(help="Specific run id to verify; omit to use --all."),
    ] = None,
    all_runs: Annotated[bool, typer.Option("--all", help="Verify every run.")] = False,
    runs_root: Annotated[Path | None, typer.Option("--runs-root")] = None,
) -> None:
    """Re-walk a bundle's artifacts and compare against ``manifest.sha256``."""
    configure_pre_run_logging()
    if run_id is None and not all_runs:
        typer.secho("verify: pass either RUN_ID or --all", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=64)

    root = resolve_runs_root(runs_root)
    exit_code = 0
    with RunCatalog(root) as cat:
        if run_id is not None:
            try:
                result = cat.verify_one(run_id)
            except CatalogError as exc:
                typer.secho(f"verify: {exc}", err=True, fg=typer.colors.RED)
                raise typer.Exit(code=2) from exc
            typer.echo(f"{run_id}: {result.status}")
            if result.mismatches:
                for m in result.mismatches:
                    typer.echo(f"  {m.kind}: {m.path}")
                exit_code = 3
        else:
            results = cat.verify_all()
            for rid, res in results:
                if isinstance(res, str):
                    typer.echo(f"{rid}: error: {res}")
                    exit_code = 3
                else:
                    typer.echo(f"{rid}: {res.status}")
                    if res.status != "ok":
                        exit_code = 3
    raise typer.Exit(code=exit_code)


@catalog_app.command("rebuild")
def catalog_rebuild(
    runs_root: Annotated[Path | None, typer.Option("--runs-root")] = None,
) -> None:
    """Re-scan ``runs/`` and rebuild ``runs.sqlite`` from each manifest."""
    configure_pre_run_logging()
    root = resolve_runs_root(runs_root)
    with RunCatalog(root) as cat:
        n = cat.rebuild_from_disk()
    typer.echo(f"rebuilt: {n} run(s) indexed at {root / 'runs.sqlite'}")
