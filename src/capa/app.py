"""``capa`` CLI — typer-based subcommand dispatcher.

Plan §14. The CLI is a first-class surface, not a debugging afterthought:
``capa validate``, ``capa run --headless``, ``capa finalize``,
``capa catalog list/verify/rebuild``, ``capa plugins list``.

Entry point: ``capa = "capa.app:main"`` in ``pyproject.toml``.

Each subcommand calls :func:`configure_pre_run_logging` first; the engine
later reconfigures so ``run --headless`` lines also tee into the bundle's
``run.log``.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated

import anyio
import typer

from capa import __version__ as capa_version
from capa.core.errors import CapaError
from capa.core.logging import configure_pre_run_logging
from capa.core.plugins_lock import PluginsLock
from capa.experiment.config import ExperimentConfig
from capa.experiment.engine import (
    ENGINE_VERSION,
    EngineResult,
    ExperimentEngine,
    _import_adapter_class,
    install_sigint_handler,
)
from capa.storage.catalog import CatalogError, RunCatalog
from capa.storage.finalize import FinalizeError, finalize_in_place
from capa.storage.manifest import BundleManifest

app = typer.Typer(
    name="capa",
    help=(
        "Control and DAQ for cone-calorimeter-class lab instruments.\n\n"
        f"capa {capa_version} (engine {ENGINE_VERSION})"
    ),
    no_args_is_help=True,
)
catalog_app = typer.Typer(name="catalog", help="Manage the run catalog.", no_args_is_help=True)
plugins_app = typer.Typer(name="plugins", help="Inspect plugin trust state.", no_args_is_help=True)
devices_app = typer.Typer(
    name="devices",
    help="Discover hardware visible on the local system.",
    no_args_is_help=True,
)
app.add_typer(catalog_app, name="catalog")
app.add_typer(plugins_app, name="plugins")
app.add_typer(devices_app, name="devices")


# ---------------------------------------------------------------------------
# Shared options
# ---------------------------------------------------------------------------


def _resolve_runs_root(runs_root: Path | None) -> Path:
    """Pick the runs root: explicit flag > ``$CAPA_RUNS_ROOT`` > ``./runs``."""
    if runs_root is not None:
        return runs_root.resolve()
    env = os.environ.get("CAPA_RUNS_ROOT")
    if env:
        return Path(env).resolve()
    return Path("runs").resolve()


def _resolve_repo_root() -> Path | None:
    """Walk upward from the cwd looking for ``.git``. Returns ``None`` if not
    inside a checkout. Used so manifests record an honest git sha."""
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _maybe_load_plugins_lock(path: Path | None) -> PluginsLock | None:
    if path is None or not path.is_file():
        return None
    return PluginsLock.load(path)


def _discover_plugins_lock_paths() -> tuple[Path, ...]:
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


def _resolve_plugins_lock_for_run(
    plugins_lock: Path | None, *, plugin_mode: str | None = None
) -> tuple[PluginsLock | None, Path | None]:
    """Production-aware plugins.lock resolution.

    * Explicit ``--plugins-lock`` always wins (must exist).
    * Otherwise in **dev** mode: silently no lock.
    * Otherwise in **production** mode: walk
      :func:`_discover_plugins_lock_paths`. First match wins; nothing
      found = hard error (exit 2).

    Returns ``(loaded_lock, resolved_path)`` so callers can surface the
    chosen file in the manifest.
    """
    from capa.core.plugins_runtime import resolve_mode  # noqa: PLC0415

    if plugins_lock is not None:
        # Honor the explicit flag verbatim — typer already validated the
        # path exists; load it eagerly so an unreadable lock fails fast.
        return PluginsLock.load(plugins_lock), plugins_lock

    mode = resolve_mode(plugin_mode)  # type: ignore[arg-type]
    if mode != "production":
        return None, None

    for candidate in _discover_plugins_lock_paths():
        if candidate.is_file():
            return PluginsLock.load(candidate), candidate

    typer.secho(
        "production plugin mode requires a plugins.lock; pass --plugins-lock or "
        "place one at " + " or ".join(str(p) for p in _discover_plugins_lock_paths()),
        err=True,
        fg=typer.colors.RED,
    )
    raise typer.Exit(code=2)


# ---------------------------------------------------------------------------
# capa validate
# ---------------------------------------------------------------------------


@app.command()
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
        # P0d: import each adapter module and, if it exposes a
        # ``handshake(params)`` async function, run a non-disruptive
        # read-only open + identify + close against the declared hardware.
        # Plan §14: ``validate --strict`` is a "non-disruptive read-only
        # handshake — no setpoint writes." Modules that do not expose
        # ``handshake`` (sim adapters, P2-and-later real adapters that
        # haven't grown the hook yet) fall back to the import-only check.
        import importlib  # noqa: PLC0415

        for dev in ec.hardware.devices:
            try:
                cls = _import_adapter_class(dev.adapter)
            except CapaError as exc:
                typer.secho(f"  strict: {dev.name}: {exc}", err=True, fg=typer.colors.RED)
                raise typer.Exit(code=2) from exc

            module = importlib.import_module(dev.adapter)
            handshake_fn = getattr(module, "handshake", None)
            if handshake_fn is None:
                typer.echo(
                    f"  strict: {dev.name} -> {cls.__module__}.{cls.__name__} "
                    f"(no handshake hook; import-only check)"
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


# ---------------------------------------------------------------------------
# capa run
# ---------------------------------------------------------------------------


@app.command()
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
            help="Optional plugins.lock to record into the manifest.",
        ),
    ] = None,
) -> None:
    """Run an experiment.

    ``--headless`` (default) writes a bundle without a GUI. The bundle's
    ``manifest.json`` records full software-environment provenance; the
    catalog row tracks ``run_status`` / ``bundle_status`` /
    ``integrity_status``.

    Exit codes: 0 = completed + sealed, 1 = aborted, 2 = crashed,
    3 = verification_failed, 4 = preflight refusal.
    """
    root = _resolve_runs_root(runs_root)
    root.mkdir(parents=True, exist_ok=True)

    lock, resolved_lock_path = _resolve_plugins_lock_for_run(plugins_lock)
    if resolved_lock_path is not None and plugins_lock is None:
        # Auto-discovery happened — surface the chosen path so operators
        # see exactly which lock the run was gated against.
        typer.echo(f"plugins.lock (auto-discovered): {resolved_lock_path}")
    repo_root = _resolve_repo_root()
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

    async def _go() -> EngineResult:
        with RunCatalog(root) as cat:
            cat.flip_orphans()
            engine = ExperimentEngine()
            return await engine.run(
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


# ---------------------------------------------------------------------------
# capa gui
# ---------------------------------------------------------------------------


@app.command()
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
            help="Optional plugins.lock to record into the manifest.",
        ),
    ] = None,
) -> None:
    """Launch the GUI. Loads the optional config if provided; otherwise
    opens empty and the operator picks a config via File > Open."""
    from capa.ui.app import run_gui  # noqa: PLC0415 — lazy PySide6 import

    root = _resolve_runs_root(runs_root)
    root.mkdir(parents=True, exist_ok=True)
    lock, resolved_lock_path = _resolve_plugins_lock_for_run(plugins_lock)
    if resolved_lock_path is not None and plugins_lock is None:
        typer.echo(f"plugins.lock (auto-discovered): {resolved_lock_path}")
    repo_root = _resolve_repo_root()
    lockfile_source = (repo_root / "uv.lock") if repo_root else None

    rc = run_gui(
        config_path=config,
        runs_root=root,
        plugins_lock=lock,
        repo_root=repo_root,
        lockfile_source=lockfile_source,
    )
    raise typer.Exit(code=rc)


# ---------------------------------------------------------------------------
# capa finalize
# ---------------------------------------------------------------------------


@app.command()
def finalize(
    run_id: Annotated[str, typer.Argument(help="Run id (the bundle directory name).")],
    runs_root: Annotated[Path | None, typer.Option("--runs-root")] = None,
) -> None:
    """Finalize an open or crashed bundle.

    Idempotent: rewrite in-flight Parquet, compute checksums, set
    ``ended_utc`` if absent, progress ``bundle_status`` to ``sealed``
    (or ``verification_failed``). Safe to run on already-sealed bundles.
    """
    configure_pre_run_logging()
    root = _resolve_runs_root(runs_root)
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


# ---------------------------------------------------------------------------
# capa catalog list / verify / rebuild
# ---------------------------------------------------------------------------


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
    root = _resolve_runs_root(runs_root)
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

    root = _resolve_runs_root(runs_root)
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
    root = _resolve_runs_root(runs_root)
    with RunCatalog(root) as cat:
        n = cat.rebuild_from_disk()
    typer.echo(f"rebuilt: {n} run(s) indexed at {root / 'runs.sqlite'}")


# ---------------------------------------------------------------------------
# capa plugins
# ---------------------------------------------------------------------------


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
        for candidate in _discover_plugins_lock_paths():
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

    Plan §17 #4 — the workflow owner is configurable via lab policy; this
    command is the technical primitive only.
    """
    from capa.core.plugins_lock import PluginEntry  # noqa: PLC0415
    from capa.core.plugins_runtime import discover_procedures  # noqa: PLC0415

    configure_pre_run_logging()
    if not reason.strip():
        typer.secho(
            "trust: --reason is required (recorded in audit journal)", err=True, fg=typer.colors.RED
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


# ---------------------------------------------------------------------------
# capa method / profile validate
# ---------------------------------------------------------------------------


method_app = typer.Typer(name="method", help="Method-file utilities.", no_args_is_help=True)
profile_app = typer.Typer(name="profile", help="Domain-profile utilities.", no_args_is_help=True)
app.add_typer(method_app, name="method")
app.add_typer(profile_app, name="profile")


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


# ---------------------------------------------------------------------------
# capa devices discover (plan §14)
# ---------------------------------------------------------------------------


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
    """Discover devices visible on the local system.

    Plan §14: ``capa devices discover``. Imports each registered real adapter
    and invokes its module-level ``discover()`` coroutine. Sim adapters are
    skipped — their "discovery" is a no-op. No bundle is created.

    Adapters that don't ship a ``discover()`` hook (or are not importable on
    this platform — e.g. ``nidaq`` without the NI runtime) are listed with
    ``(no discovery)`` rather than failing.
    """
    import importlib  # noqa: PLC0415

    from capa.devices import ADAPTER_REGISTRY, REAL_ADAPTERS  # noqa: PLC0415

    configure_pre_run_logging()

    targets: tuple[str, ...]
    if adapter is not None:
        if adapter not in REAL_ADAPTERS:
            typer.secho(
                f"discover: unknown adapter {adapter!r}; valid: {list(REAL_ADAPTERS)}",
                err=True,
                fg=typer.colors.RED,
            )
            raise typer.Exit(code=2)
        targets = (adapter,)
    else:
        targets = REAL_ADAPTERS

    rows: list[dict[str, object]] = []
    notes: list[str] = []

    for adapter_id in targets:
        module_path = ADAPTER_REGISTRY[adapter_id]
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            notes.append(f"{adapter_id}: not importable ({exc})")
            continue
        discover_fn = getattr(module, "discover", None)
        if discover_fn is None:
            notes.append(f"{adapter_id}: (no discovery hook)")
            continue
        try:
            results = anyio.run(discover_fn)
        except Exception as exc:
            notes.append(f"{adapter_id}: failed ({type(exc).__name__}: {exc})")
            continue
        for r in results:
            rows.append(r)
        if not results:
            notes.append(f"{adapter_id}: no devices found")

    if json_out:
        typer.echo(json.dumps({"devices": rows, "notes": notes}, indent=2, default=str))
        return

    if not rows:
        typer.echo("(no devices discovered)")
        for note in notes:
            typer.echo(f"  {note}")
        return

    # Render as a per-adapter group, since the keys differ.
    by_adapter: dict[str, list[dict[str, object]]] = {}
    for r in rows:
        by_adapter.setdefault(str(r.get("adapter", "?")), []).append(r)

    for adapter_id, group in by_adapter.items():
        typer.echo(f"\n[{adapter_id}]")
        # Print each row as one indented line of `key=value` pairs, omitting
        # the adapter key itself (already in the section header).
        for row in group:
            parts = [f"{k}={v!r}" for k, v in row.items() if k != "adapter"]
            typer.echo("  " + ", ".join(parts))

    if notes:
        typer.echo("\nNotes:")
        for note in notes:
            typer.echo(f"  {note}")


# ---------------------------------------------------------------------------
# capa version (small ergonomic helper, not in the plan §14 list)
# ---------------------------------------------------------------------------


@app.command()
def version() -> None:
    """Print the capa version + engine revision."""
    typer.echo(f"capa {capa_version} (engine {ENGINE_VERSION})")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    """Entry point referenced by ``[project.scripts]`` and tests.

    Tests can call ``main([...])`` to exercise the CLI in-process. Typer
    raises :class:`typer.Exit` for non-zero exit codes; we let that
    propagate so :class:`pytest.raises(SystemExit)` can capture the code.
    """
    app(argv)


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])


__all__ = ["app", "main"]
