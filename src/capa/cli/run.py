"""``capa run`` / ``capa gui`` / ``capa finalize`` — run-lifecycle commands."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import anyio
import typer

from capa.cli._helpers import (
    resolve_plugins_lock_for_run,
    resolve_repo_root,
    resolve_runs_root,
)
from capa.core.errors import CapaError
from capa.core.logging import configure_pre_run_logging
from capa.experiment.config import ExperimentConfig
from capa.runtime import install_sigint_handler
from capa.runtime.headless import HeadlessResult, run_headless
from capa.storage.catalog import RunCatalog
from capa.storage.finalize import FinalizeError, finalize_in_place
from capa.storage.manifest import BundleManifest


def run(
    config: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    headless: Annotated[
        bool,
        typer.Option("--headless/--gui", help="Headless mode (no GUI)."),
    ] = True,
    runs_root: Annotated[
        Path | None,
        typer.Option(
            "--runs-root",
            help="Where to write the bundle. Default: $CAPA_RUNS_ROOT or ./runs.",
        ),
    ] = None,
    plugins_lock: Annotated[
        Path | None,
        typer.Option(
            "--plugins-lock",
            help="plugins.lock for procedure trust; mirrored into the manifest.",
        ),
    ] = None,
) -> None:
    """Run an experiment.

    ``--headless`` (default) writes a bundle without a GUI. The bundle's
    ``manifest.json`` records full software-environment provenance; the
    catalog row tracks ``run_status`` / ``bundle_status`` /
    ``integrity_status``.

    Exit codes: 0 = completed + sealed, 1 = aborted (includes preflight
    refusal — the preflight path emits ``run_status="aborted"``), 2 = crashed
    (or config load failed before the engine started), 3 = verification_failed.
    """
    root = resolve_runs_root(runs_root)
    root.mkdir(parents=True, exist_ok=True)

    lock, resolved_lock_path = resolve_plugins_lock_for_run(plugins_lock)
    if resolved_lock_path is not None and plugins_lock is None:
        typer.echo(f"plugins.lock (auto-discovered): {resolved_lock_path}")
    repo_root = resolve_repo_root()
    lockfile_source = (repo_root / "uv.lock") if repo_root else None

    if not headless:
        # GUI dispatch — the qasync bootstrap owns its own event loop and
        # catalog; the config path is forwarded so the operator opens with
        # something already loaded. Lazy-imported so headless paths
        # (validate, catalog list) don't pay the PySide6 startup cost.
        from capa.ui.app import run_gui  # noqa: PLC0415 — intentionally lazy

        rc = run_gui(
            config_path=config,
            runs_root=root,
            plugins_lock=lock,
            repo_root=repo_root,
            lockfile_source=lockfile_source,
        )
        raise typer.Exit(code=rc)

    configure_pre_run_logging()
    try:
        ec = ExperimentConfig.load(config)
    except CapaError as exc:
        typer.secho(f"run: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc

    stop_event = anyio.Event()
    install_sigint_handler(stop_event)

    async def _go() -> HeadlessResult:
        with RunCatalog(root) as cat:
            cat.flip_orphans()
            return await run_headless(
                ec,
                runs_root=root,
                plugins_lock=lock,
                repo_root=repo_root,
                lockfile_source=lockfile_source,
                external_stop=stop_event,
                catalog=cat,
            )

    result = anyio.run(_go)
    typer.echo(f"run_id:           {result.run_id}")
    typer.echo(f"bundle:           {result.bundle_path}")
    typer.echo(f"run_status:       {result.run_status}")
    typer.echo(f"bundle_status:    {result.bundle_status}")
    typer.echo(f"integrity_status: {result.integrity_status}")
    if result.exit_reason:
        typer.echo(f"exit_reason:      {result.exit_reason}")
    raise typer.Exit(code=result.exit_code())


def gui(
    config: Annotated[
        Path | None,
        typer.Argument(
            exists=True,
            dir_okay=False,
            readable=True,
            help="Optional experiment config to preload. Omit to launch empty and pick one from File > Open.",
        ),
    ] = None,
    runs_root: Annotated[
        Path | None,
        typer.Option(
            "--runs-root",
            help="Where to write bundles. Default: $CAPA_RUNS_ROOT or ./runs.",
        ),
    ] = None,
    plugins_lock: Annotated[
        Path | None,
        typer.Option(
            "--plugins-lock",
            help="plugins.lock for procedure trust; mirrored into the manifest.",
        ),
    ] = None,
) -> None:
    """Launch the GUI. Loads the optional config if provided; otherwise
    opens empty and the operator picks a config via File > Open."""
    from capa.ui.app import run_gui  # noqa: PLC0415 — lazy PySide6 import

    root = resolve_runs_root(runs_root)
    root.mkdir(parents=True, exist_ok=True)
    lock, resolved_lock_path = resolve_plugins_lock_for_run(plugins_lock)
    if resolved_lock_path is not None and plugins_lock is None:
        typer.echo(f"plugins.lock (auto-discovered): {resolved_lock_path}")
    repo_root = resolve_repo_root()
    lockfile_source = (repo_root / "uv.lock") if repo_root else None

    rc = run_gui(
        config_path=config,
        runs_root=root,
        plugins_lock=lock,
        repo_root=repo_root,
        lockfile_source=lockfile_source,
    )
    raise typer.Exit(code=rc)


def finalize(
    run_id: Annotated[str, typer.Argument(help="Run id (the bundle directory name).")],
    runs_root: Annotated[Path | None, typer.Option("--runs-root")] = None,
) -> None:
    """Finalize an open or crashed bundle.

    Idempotent: rewrite in-flight Arrow IPC streams, compute checksums, set
    ``ended_utc`` if absent, progress ``bundle_status`` to ``sealed``
    (or ``verification_failed``). Safe to run on already-sealed bundles.
    """
    configure_pre_run_logging()
    root = resolve_runs_root(runs_root)
    bundle_path = root / run_id
    if not bundle_path.is_dir():
        typer.secho(
            f"finalize: bundle directory not found: {bundle_path}",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)

    manifest_path = bundle_path / "manifest.json"
    try:
        manifest = BundleManifest.read(manifest_path)
    except Exception as exc:
        typer.secho(f"finalize: malformed manifest: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc

    inferred = manifest.ended_utc is None
    target_status = (
        "completed"
        if manifest.run_status == "completed"
        else "crashed"
        if manifest.run_status in ("running", "crashed")
        else manifest.run_status
    )
    try:
        result = finalize_in_place(
            bundle_path,
            run_status=target_status,
            inferred_ended_utc=inferred,
        )
    except FinalizeError as exc:
        typer.secho(f"finalize: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=3) from exc

    with RunCatalog(root) as cat:
        try:
            updated = BundleManifest.read(manifest_path)
            cat.upsert_operator(updated.operator.id, updated.operator.display_name)
            cat.insert_run_at_open(updated, bundle_path=bundle_path)
            cat.update_at_finalize(updated, bundle_path=bundle_path)
        except Exception as exc:
            typer.secho(
                f"finalize: catalog update failed (bundle still sealed): {exc}",
                err=True,
                fg=typer.colors.YELLOW,
            )

    typer.echo(f"finalized: {run_id}")
    typer.echo(f"  rewrote:  {len(result.rewrote)} file(s)")
    typer.echo(f"  skipped:  {len(result.skipped_already_final)} already-final file(s)")
    typer.echo(f"  integrity: {result.integrity.status}")
