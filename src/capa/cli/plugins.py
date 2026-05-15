"""``capa plugins {list,trust}`` — plugin trust commands."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from capa.cli._helpers import discover_plugins_lock_paths
from capa.core.logging import configure_pre_run_logging
from capa.core.plugins_lock import PluginsLock

plugins_app = typer.Typer(name="plugins", help="Inspect plugin trust state.", no_args_is_help=True)


@plugins_app.command("list")
def plugins_list(
    plugins_lock: Annotated[
        Path | None,
        typer.Option("--plugins-lock", help="Path to plugins.lock; default ./plugins.lock"),
    ] = None,
    plugin_mode: Annotated[
        str | None,
        typer.Option("--plugin-mode", help="dev|production; overrides $CAPA_PLUGIN_MODE."),
    ] = None,
) -> None:
    """List discovered procedure plugins.

    Walks the ``capa.procedures`` entry-point group, runs the load-time
    contract check, and (in production mode) gates each plugin against
    ``plugins.lock``. Rejected plugins are listed with the reason.
    """
    from capa.core.plugins_runtime import discover_procedures, resolve_mode  # noqa: PLC0415

    configure_pre_run_logging()
    if plugins_lock is not None:
        lock_path: Path | None = plugins_lock
        lock = PluginsLock.load(plugins_lock) if plugins_lock.is_file() else None
    else:
        lock = None
        lock_path = None
        for candidate in discover_plugins_lock_paths():
            if candidate.is_file():
                lock = PluginsLock.load(candidate)
                lock_path = candidate
                break
    mode = resolve_mode(plugin_mode)  # type: ignore[arg-type]
    report = discover_procedures(plugins_lock=lock, mode=mode)

    typer.echo(f"plugin mode: {report.mode}")
    if lock is not None:
        typer.echo(f"plugins.lock: {lock_path} (schema v{lock.version})")
    else:
        typer.echo("plugins.lock: (none — production mode requires one to gate trust)")

    if not report.loaded:
        typer.echo("(no plugins loaded)")
    else:
        typer.echo(f"\n{'id':40}  {'package':20}  {'version':10}  hash")
        for p in report.loaded:
            algo, _, digest = p.distribution_hash.partition(":")
            short = f"{algo}:{digest[:12]}…"
            typer.echo(f"{p.id:40}  {p.package:20}  {p.version:10}  {short}")

    if report.rejected:
        typer.echo("\nRejected:")
        for plugin_id, reason in report.rejected:
            typer.echo(f"  {plugin_id}: {reason}")

    if report.drifts:
        typer.echo("\nDrift vs plugins.lock:")
        for d in report.drifts:
            typer.echo(
                f"  {d.plugin_id}: {d.kind.value} (expected={d.expected!r}, actual={d.actual!r})"
            )


@plugins_app.command("trust")
def plugins_trust(
    plugin_id: Annotated[str, typer.Argument(help="Plugin id to add to plugins.lock")],
    plugins_lock: Annotated[
        Path | None,
        typer.Option("--plugins-lock", help="Path to plugins.lock; default ./plugins.lock"),
    ] = None,
    reason: Annotated[
        str,
        typer.Option(
            "--reason",
            help="Audit text recorded alongside the trust grant. Required.",
        ),
    ] = "",
) -> None:
    """Add (or refresh) a discovered plugin in ``plugins.lock``.

    Reads the live entry-point discovery, finds the plugin by id, and writes
    a matching :class:`PluginEntry` into the lockfile. If the plugin is
    already present its hash/version are refreshed. Production mode then
    treats the plugin as trusted on the next run.

    The workflow owner is configurable via lab policy; this command is
    the technical primitive only.
    """
    from capa.core.plugins_lock import PluginEntry  # noqa: PLC0415
    from capa.core.plugins_runtime import discover_procedures  # noqa: PLC0415

    configure_pre_run_logging()
    if not reason.strip():
        typer.secho(
            "trust: --reason is required (recorded in audit journal)",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)

    report = discover_procedures(mode="dev")  # dev so contract-pass plugins surface
    match = next((p for p in report.loaded if p.id == plugin_id), None)
    if match is None:
        typer.secho(
            f"trust: plugin {plugin_id!r} not found among installed plugins. "
            f"Available: {', '.join(p.id for p in report.loaded) or '<none>'}",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)

    lock_path = plugins_lock or Path("plugins.lock")
    if lock_path.is_file():
        lock = PluginsLock.load(lock_path)
    else:
        lock = PluginsLock(version=1, plugins=())

    new_entry = PluginEntry(
        id=match.id,
        package=match.package,
        version=match.version,
        entry_point=match.entry_point,
        distribution_hash=match.distribution_hash,
    )
    others = tuple(e for e in lock.plugins if e.id != match.id)
    new_lock = PluginsLock(version=lock.version, plugins=(*others, new_entry))

    import tomli_w  # noqa: PLC0415

    payload = {
        "version": new_lock.version,
        "plugins": [e.model_dump() for e in new_lock.plugins],
    }
    with open(lock_path, "wb") as fp:
        tomli_w.dump(payload, fp)

    journal_path = lock_path.with_name(lock_path.name + ".journal")
    with open(journal_path, "a", encoding="utf-8") as jp:
        jp.write(
            f"{datetime.now().isoformat()}\ttrust\t{match.id}\t{match.version}\t"
            f"{match.distribution_hash}\t{reason}\n"
        )

    typer.echo(f"trusted: {match.id} {match.version} ({match.distribution_hash[:30]}…)")
    typer.echo(f"  lock:    {lock_path}")
    typer.echo(f"  journal: {journal_path}")
