"""Plugin discovery + runtime trust enforcement.

The lock parser + drift detector live in
:mod:`capa.core.plugins_lock`. This module is the runtime side:

1. **Discovery.** Walk ``importlib.metadata.entry_points(group="capa.procedures")``
   and (when dev mode is enabled) any ``Procedure`` subclasses under a local
   ``plugins/`` folder.
2. **Contract enforcement at load time.** Every loaded plugin is checked
   against the :class:`~capa.experiment.procedures.base.Procedure` Protocol:
   ``id`` / ``name`` / ``version`` / ``config_model`` are all present, the
   class implements ``preflight`` and ``run``, and ``config_model`` is a
   Pydantic ``BaseModel`` subclass.
3. **Trust enforcement.** In production mode (``mode="production"``) any
   discovered plugin not present in ``plugins.lock`` (or whose distribution
   hash drifts from the lock) is excluded from the registry and recorded as
   a :class:`~capa.core.errors.PluginTrustError` with the offending drift.

Builtin procedures (``capa.builtin.free_run``, ``recipe_runner``, ``batch``)
are *always* trusted — they ship with the engine and live in the same
distribution; their hash is computed at startup and matched against the
running engine's own distribution. They never need an entry in
``plugins.lock``, but they may be listed there for completeness.

The runtime is intentionally permissive in dev mode (``mode="dev"``): it
loads everything that passes the contract check, ignoring trust drift. The
engine selects the mode based on ``CAPA_PLUGIN_MODE`` env var (default
``"dev"`` for now; ``capa run --plugin-mode production`` switches).
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import inspect
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel

from capa.core.errors import PluginTrustError
from capa.core.plugins_lock import (
    Drift,
    DriftKind,
    InstalledPlugin,
    PluginsLock,
    detect_drift,
)
from capa.experiment.procedures.base import Procedure

ENTRY_POINT_GROUP = "capa.procedures"
"""PEP 621 entry-point group name. Plugins register their procedure class
under this group in their own ``pyproject.toml``."""

PluginMode = Literal["dev", "production"]
"""Two-state mode. ``dev`` ignores trust drift; ``production`` refuses any
plugin missing from ``plugins.lock`` or whose hash differs."""


def resolve_mode(override: PluginMode | None = None) -> PluginMode:
    """Resolve the active plugin mode.

    Order: explicit ``override`` > ``CAPA_PLUGIN_MODE`` env var > ``"dev"``.
    """
    if override is not None:
        return override
    env = os.environ.get("CAPA_PLUGIN_MODE", "").strip().lower()
    if env == "production":
        return "production"
    return "dev"


# ---------------------------------------------------------------------------
# Discovery + load.
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class LoadedProcedure:
    """A successfully loaded procedure plugin.

    Returned by :func:`discover_procedures`. The class can be instantiated
    via ``cls.from_config(...)`` (every builtin defines this; plugin authors
    are expected to as well, though the engine falls back to ``cls()`` when
    not present).
    """

    id: str
    name: str
    version: str
    cls: type[Procedure]
    package: str
    distribution_hash: str
    entry_point: str
    """``"module.path:Class"``."""


@dataclass(slots=True)
class DiscoveryReport:
    """What :func:`discover_procedures` returns.

    Keeps loaded plugins separate from rejection reasons so the CLI can
    print both.
    """

    mode: PluginMode
    loaded: list[LoadedProcedure] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    """(``id_or_entry_point``, ``reason``) per rejection."""

    drifts: list[Drift] = field(default_factory=list)


def discover_procedures(
    *,
    plugins_lock: PluginsLock | None = None,
    mode: PluginMode | None = None,
    enable_local_plugins_dir: bool = False,
    local_plugins_dir: Path | None = None,
) -> DiscoveryReport:
    """Discover and load every available procedure plugin.

    Args:
        plugins_lock: Required in production mode. Pass the parsed
            :class:`PluginsLock`; ``None`` is allowed in dev mode (every
            discovered plugin loads, drift is recorded for inspection but
            does not gate loading).
        mode: ``"dev"`` or ``"production"``. Default resolves via
            :func:`resolve_mode`.
        enable_local_plugins_dir: If ``True``, also scan ``local_plugins_dir``
            for ``Procedure`` subclasses. Production mode rejects this — local
            hot-loaded plugins do not have a distribution hash.
        local_plugins_dir: Override the default ``./plugins`` location.

    Returns:
        :class:`DiscoveryReport` with loaded and rejected lists.
    """
    mode = resolve_mode(mode)
    if mode == "production" and enable_local_plugins_dir:
        raise PluginTrustError("local_plugins_dir is not allowed in production mode")

    report = DiscoveryReport(mode=mode)
    seen_ids: set[str] = set()
    installed: list[InstalledPlugin] = []

    # 1. Entry points -----------------------------------------------------
    for ep in _iter_entry_points():
        package, version, dist_hash = _resolve_distribution(ep)
        installed.append(
            InstalledPlugin(
                id=_entry_point_id_guess(ep),  # provisional; refined post-load
                package=package,
                version=version,
                entry_point=f"{ep.module}:{ep.attr}",
                distribution_hash=dist_hash,
            )
        )

    # 2. Trust check (production) ----------------------------------------
    if mode == "production" and plugins_lock is not None:
        report.drifts = detect_drift(plugins_lock, installed)

    drifted_ids: set[str] = set()
    if mode == "production":
        for d in report.drifts:
            # MISSING_FROM_LOCK / HASH_MISMATCH / VERSION_MISMATCH /
            # ENTRY_POINT_MISMATCH all block load. MISSING_FROM_INSTALL is
            # informational here (we don't have the install to load).
            if d.kind in (
                DriftKind.MISSING_FROM_LOCK,
                DriftKind.VERSION_MISMATCH,
                DriftKind.HASH_MISMATCH,
                DriftKind.ENTRY_POINT_MISMATCH,
            ):
                drifted_ids.add(d.plugin_id)

    # 3. Load each entry point, run the contract check, gate on trust ----
    for ep in _iter_entry_points():
        provisional_id = _entry_point_id_guess(ep)
        try:
            cls = ep.load()
        except Exception as exc:
            report.rejected.append((f"{ep.module}:{ep.attr}", f"import failed: {exc}"))
            continue

        try:
            check_procedure_class(cls)
        except PluginTrustError as exc:
            report.rejected.append((provisional_id, f"contract check: {exc}"))
            continue

        cls = cast(type[Procedure], cls)

        actual_id = getattr(cls, "id", provisional_id)
        if actual_id in seen_ids:
            report.rejected.append(
                (actual_id, f"duplicate plugin id (entry point {ep.module}:{ep.attr})")
            )
            continue
        seen_ids.add(actual_id)

        if mode == "production" and actual_id in drifted_ids:
            report.rejected.append((actual_id, "plugins.lock drift; trust check failed"))
            continue

        package, version, dist_hash = _resolve_distribution(ep)
        report.loaded.append(
            LoadedProcedure(
                id=actual_id,
                name=getattr(cls, "name", actual_id),
                # Use the *distribution* version, not the class attribute.
                # ``capa plugins trust`` writes ``LoadedProcedure.version`` into
                # the lock; ``detect_drift`` compares against ``dist.version``.
                # Reading the class attribute here caused the two sides to
                # diverge in editable installs (class attr stays "0.1.0" while
                # dist.version is "0.0.1.dev1+gXXXX"), making the trust check
                # always fail. The plugin author's
                # declared version is still available via ``cls.version``.
                version=version,
                cls=cls,
                package=package,
                distribution_hash=dist_hash,
                entry_point=f"{ep.module}:{ep.attr}",
            )
        )

    # 4. Local plugins (dev only, opt-in) --------------------------------
    if enable_local_plugins_dir and mode == "dev":
        for cls in _iter_local_plugin_classes(local_plugins_dir):
            try:
                check_procedure_class(cls)
            except PluginTrustError as exc:
                report.rejected.append(
                    (cls.__module__ + ":" + cls.__name__, f"contract check: {exc}")
                )
                continue
            actual_id = getattr(cls, "id", cls.__name__)
            if actual_id in seen_ids:
                report.rejected.append(
                    (actual_id, f"duplicate plugin id (local: {cls.__module__})")
                )
                continue
            seen_ids.add(actual_id)
            report.loaded.append(
                LoadedProcedure(
                    id=actual_id,
                    name=getattr(cls, "name", actual_id),
                    version=getattr(cls, "version", "0.0.0+local"),
                    cls=cls,
                    package="<local>",
                    distribution_hash="sha256:local",
                    entry_point=f"{cls.__module__}:{cls.__name__}",
                )
            )

    return report


# ---------------------------------------------------------------------------
# Contract check.
# ---------------------------------------------------------------------------


def check_procedure_class(cls: type[object]) -> None:
    """Raise :class:`PluginTrustError` if ``cls`` does not satisfy the
    :class:`Procedure` Protocol.

    This is the load-time contract enforcement –957
    requires. Failures here keep the plugin out of the registry — it never
    appears in the procedure picker."""
    if not isinstance(cls, type):
        raise PluginTrustError(
            f"plugin entry point did not load to a class (got {type(cls).__name__})"
        )

    for attr in ("id", "name", "version", "config_model"):
        if not hasattr(cls, attr):
            raise PluginTrustError(f"class {cls.__name__} missing attribute {attr!r}")

    config_model = getattr(cls, "config_model", None)
    if not (isinstance(config_model, type) and issubclass(config_model, BaseModel)):
        raise PluginTrustError(
            f"class {cls.__name__}.config_model must be a Pydantic BaseModel subclass"
        )

    for method in ("preflight", "run"):
        fn = getattr(cls, method, None)
        if fn is None:
            raise PluginTrustError(f"class {cls.__name__} missing async method {method!r}")
        if not inspect.iscoroutinefunction(fn):
            raise PluginTrustError(
                f"class {cls.__name__}.{method} must be a coroutine function (async def)"
            )

    # ``Procedure`` is a Protocol with non-method members; ``issubclass`` is
    # not supported on it (TypeError). Fall back to attribute presence — the
    # required attributes were already verified above; this last check
    # catches plugins that monkey-patch a ``preflight`` onto an unrelated
    # class.
    if not _structurally_procedure(cls):
        raise PluginTrustError(f"class {cls.__name__} does not structurally implement Procedure")


def _structurally_procedure(cls: type) -> bool:
    # The ``Procedure`` Protocol is runtime_checkable but uses dataclass
    # field defaults that defeat ``isinstance`` for *classes* (vs instances).
    # Fall back to attribute presence — we already verified the methods are
    # coroutines, so the remaining check is the typed attributes are there.
    needed = {"id", "name", "version", "config_model", "required_capabilities"}
    return needed.issubset(set(dir(cls)))


# ---------------------------------------------------------------------------
# Helpers — entry points + dist hashes.
# ---------------------------------------------------------------------------


def _iter_entry_points() -> list[importlib.metadata.EntryPoint]:
    eps = importlib.metadata.entry_points()
    if hasattr(eps, "select"):
        return list(eps.select(group=ENTRY_POINT_GROUP))
    # Pre-3.10 selection API; not expected on Python 3.13 but keep the
    # branch tiny.
    return [ep for ep in eps.get(ENTRY_POINT_GROUP, [])]  # type: ignore[attr-defined]


def _entry_point_id_guess(ep: importlib.metadata.EntryPoint) -> str:
    """Use the entry-point name as the provisional id.

    The class's ``id`` attribute (read post-load) is canonical; this guess
    only matters if the class fails to load."""
    return ep.name


def _resolve_distribution(ep: importlib.metadata.EntryPoint) -> tuple[str, str, str]:
    """Resolve ``(package, version, distribution_hash)`` for an entry point.

    The hash is computed over the distribution's ``RECORD``-listed files by
    default — sufficient to detect tampering with the installed package
    contents. We hash the joined contents of the dist's ``RECORD`` /
    ``METADATA`` files which is fast and stable."""
    dist = ep.dist
    if dist is None:
        return ("<unknown>", "0.0.0", "sha256:unknown")
    package = dist.metadata.get("Name", "<unknown>")
    version = dist.version
    dist_hash = _hash_distribution(dist)
    return (package, version, dist_hash)


def _hash_distribution(dist: importlib.metadata.Distribution) -> str:
    """Compute a stable hash over the distribution's METADATA + RECORD.

    Not a full content hash (would re-walk every installed file on every
    startup) — sufficient for detecting "the wheel I installed has been
    swapped" since both files change when a new version is installed.

    **Operator-facing trust scope.** This hash detects *wheel swaps*: a
    different version of the package installed over the previous one, or
    a tampered ``RECORD`` file. It does **not** detect:

    * source-file edits in an editable install (``pip install -e .``) —
      METADATA + RECORD don't change when ``my_plugin/recipe.py`` changes;
    * runtime monkey-patching of the loaded class.

    If your trust model requires "any code change invalidates trust,"
    install plugins as wheels (not editable) and treat each wheel build
    as requiring a fresh ``capa plugins trust``. The bundled commit hash
    in dev-mode ``dist.version`` (``0.0.1.dev1+gXXXX.dYYYY``) does change
    on every commit — so on editable installs, every ``git commit``
    invalidates the lock and operators must re-trust before the next
    production run."""
    h = hashlib.sha256()
    for fname in ("METADATA", "RECORD"):
        try:
            data = dist.read_text(fname)
        except (FileNotFoundError, KeyError):
            data = None
        if data is None:
            continue
        h.update(fname.encode("utf-8"))
        h.update(b"\0")
        h.update(data.encode("utf-8", errors="replace"))
        h.update(b"\0")
    return f"sha256:{h.hexdigest()}"


def _iter_local_plugin_classes(folder: Path | None) -> Iterable[type[Procedure]]:
    """Walk a folder for ``Procedure`` subclasses. Used by dev-mode
    hot-loading."""
    folder = folder or Path("plugins")
    if not folder.is_dir():
        return []
    out: list[type[Procedure]] = []
    for path in folder.glob("*.py"):
        spec = importlib.util.spec_from_file_location(f"capa_local.{path.stem}", path)
        if spec is None or spec.loader is None:
            continue
        try:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:
            continue
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ != module.__name__:
                continue
            if _structurally_procedure(obj):
                out.append(cast(type[Procedure], obj))
    return out


# ---------------------------------------------------------------------------
# Registry — keyed by id, populated from a DiscoveryReport.
# ---------------------------------------------------------------------------


class ProcedureRegistry:
    """Resolved procedure registry.

    The engine builds one of these per-process at startup (or per-test) and
    queries by ``ProcedureRef.id``. Construction is cheap; tests can build
    a registry without going through entry-points by passing a hand-built
    list of :class:`LoadedProcedure`.
    """

    __slots__ = ("_by_id", "_report")

    def __init__(self, loaded: list[LoadedProcedure], report: DiscoveryReport | None = None):
        self._by_id: dict[str, LoadedProcedure] = {p.id: p for p in loaded}
        self._report = report

    @classmethod
    def discover(cls, **kwargs: Any) -> ProcedureRegistry:
        report = discover_procedures(**kwargs)
        return cls(report.loaded, report=report)

    @property
    def report(self) -> DiscoveryReport | None:
        return self._report

    def __contains__(self, plugin_id: object) -> bool:
        return isinstance(plugin_id, str) and plugin_id in self._by_id

    def get(self, plugin_id: str) -> LoadedProcedure | None:
        return self._by_id.get(plugin_id)

    def ids(self) -> tuple[str, ...]:
        return tuple(self._by_id.keys())

    def instantiate(self, plugin_id: str, raw_config: dict[str, Any] | None) -> Procedure:
        """Construct a procedure instance from its raw config dict.

        Resolution order:

        1. Look up by id in the registry.
        2. If the class defines a ``from_config`` classmethod, call it
           (every builtin does; it validates against ``config_model`` and
           returns a constructed instance).
        3. Otherwise validate ``raw_config`` against ``config_model`` and
           pass the dumped fields as kwargs to the class.
        """
        loaded = self._by_id.get(plugin_id)
        if loaded is None:
            raise PluginTrustError(f"procedure {plugin_id!r} is not in the trusted registry")
        cls = loaded.cls
        from_config = getattr(cls, "from_config", None)
        if callable(from_config):
            return cast(Procedure, from_config(raw_config))
        config_model = cls.config_model
        validated = config_model.model_validate(raw_config or {})
        return cls(**validated.model_dump())


__all__ = [
    "ENTRY_POINT_GROUP",
    "DiscoveryReport",
    "LoadedProcedure",
    "PluginMode",
    "ProcedureRegistry",
    "check_procedure_class",
    "discover_procedures",
    "resolve_mode",
]
