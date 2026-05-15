"""Shared CLI helpers used by more than one command module.

Resolution helpers (``_resolve_runs_root``, ``_resolve_repo_root``,
``_resolve_plugins_lock_for_run``) and the problem/discovery row
renderers live here so each command module stays focused on its own
flag surface.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import typer

from capa.core.plugins_lock import PluginsLock
from capa.devices.discovery import discover_descriptor
from capa.devices.registry import AdapterDescriptor


def resolve_runs_root(runs_root: Path | None) -> Path:
    """Pick the runs root: explicit flag > ``$CAPA_RUNS_ROOT`` > ``./runs``."""
    if runs_root is not None:
        return runs_root.resolve()
    env = os.environ.get("CAPA_RUNS_ROOT")
    if env:
        return Path(env).resolve()
    return Path("runs").resolve()


def resolve_repo_root() -> Path | None:
    """Walk upward from the cwd looking for ``.git``. Returns ``None`` if not
    inside a checkout. Used so manifests record an honest git sha."""
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def discover_plugins_lock_paths() -> tuple[Path, ...]:
    """Default lookup order for ``plugins.lock`` when ``--plugins-lock`` is unset.

    Per-project (``./plugins.lock``) wins over user-global
    (``$XDG_CONFIG_HOME/capa/plugins.lock``, falling back to
    ``$HOME/.config/capa/plugins.lock``) — matches how most ecosystem
    tools resolve lockfiles.
    """
    candidates: list[Path] = [Path.cwd() / "plugins.lock"]
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        candidates.append(Path(xdg) / "capa" / "plugins.lock")
    else:
        home = os.environ.get("HOME")
        if home:
            candidates.append(Path(home) / ".config" / "capa" / "plugins.lock")
    return tuple(candidates)


def resolve_plugins_lock_for_run(
    plugins_lock: Path | None, *, plugin_mode: str | None = None
) -> tuple[PluginsLock | None, Path | None]:
    """Production-aware plugins.lock resolution.

    * Explicit ``--plugins-lock`` always wins (must exist).
    * Otherwise in **dev** mode: silently no lock.
    * Otherwise in **production** mode: walk
      :func:`discover_plugins_lock_paths`. First match wins; nothing
      found = hard error (exit 2).

    Returns ``(loaded_lock, resolved_path)`` so callers can surface the
    chosen file in the manifest.
    """
    from capa.core.plugins_runtime import resolve_mode  # noqa: PLC0415

    if plugins_lock is not None:
        return PluginsLock.load(plugins_lock), plugins_lock

    mode = resolve_mode(plugin_mode)  # type: ignore[arg-type]
    if mode != "production":
        return None, None

    for candidate in discover_plugins_lock_paths():
        if candidate.is_file():
            return PluginsLock.load(candidate), candidate

    typer.secho(
        "production plugin mode requires a plugins.lock; pass --plugins-lock or "
        "place one at " + " or ".join(str(p) for p in discover_plugins_lock_paths()),
        err=True,
        fg=typer.colors.RED,
    )
    raise typer.Exit(code=2)


def render_problems(
    problems: list[Any], *, source: Path | str | None = None
) -> tuple[int, int, int]:
    """Print ``ConfigProblem`` rows uniformly; return (errors, warnings, info)."""
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
        target = source if source is not None else "(unknown)"
        typer.secho(f"OK: {target}  ({summary})", fg=typer.colors.GREEN)
    else:
        typer.echo(summary)
    return error_count, warn_count, info_count


async def collect_discovery_rows(
    descriptors: list[AdapterDescriptor],
) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    notes: list[str] = []
    for descriptor in descriptors:
        result = await discover_descriptor(descriptor)
        if result.error is not None:
            notes.append(f"{descriptor.family}: {result.error}")
            continue
        rows.extend(result.rows)
        if not result.rows:
            notes.append(f"{descriptor.family}: no devices found")
    return rows, notes


def emit_discovery_rows(
    rows: list[dict[str, Any]],
    notes: list[str],
    *,
    json_out: bool,
) -> None:
    if json_out:
        typer.echo(json.dumps({"devices": rows, "notes": notes}, indent=2, default=str))
        return

    if not rows:
        typer.echo("(no devices discovered)")
        for note in notes:
            typer.echo(f"  {note}")
        return

    by_adapter: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_adapter.setdefault(str(row.get("adapter", "?")), []).append(row)
    for adapter_id, group in by_adapter.items():
        typer.echo(f"\n[{adapter_id}]")
        for row in group:
            parts = [f"{k}={v!r}" for k, v in row.items() if k != "adapter"]
            typer.echo("  " + ", ".join(parts))
    if notes:
        typer.echo("\nNotes:")
        for note in notes:
            typer.echo(f"  {note}")
