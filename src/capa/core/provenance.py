"""Software-environment provenance.

Plan §8.1. ``manifest.json`` carries:

* capa version + git sha + git_dirty,
* Python version + implementation + executable,
* platform os/machine/node,
* a lockfile pointer (``env/uv.lock``) plus its sha256,
* the resolved plugin list (matched against ``plugins.lock``).

Plus the bundle writes ``env/uv.lock`` and ``env/packages.json`` next to it
so re-deriving values five years later does not depend on what tooling
happens to be installed today.

This module is pure data gathering. The bundle writer (P0b) calls
:func:`gather_provenance` at open and uses the result to populate
``manifest.json`` plus copy the ``env/`` directory contents into place.

Everything degrades gracefully: missing ``git``, missing lockfile, missing
``importlib.metadata`` distribution — each turns into a recorded ``None``
rather than a crash. The bundle is always honest about what was unknown.
"""

from __future__ import annotations

import importlib.metadata as _ilm
import json
import platform as _platform
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from capa.core.plugins_lock import PluginsLock
from capa.storage.manifest import (
    CapaBlock,
    LockfileBlock,
    PlatformBlock,
    PluginEntryBlock,
    PythonBlock,
)


@dataclass(frozen=True, slots=True)
class Provenance:
    """Bundle-ready provenance snapshot.

    The four ``*_block`` fields go straight into ``manifest.json``. The two
    ``*_bytes`` fields (when present) are copied into the bundle's ``env/``
    subdirectory by the bundle writer.
    """

    capa: CapaBlock
    python: PythonBlock
    platform: PlatformBlock
    lockfile: LockfileBlock
    plugins: tuple[PluginEntryBlock, ...]

    lockfile_bytes: bytes | None
    """Contents of the source lockfile, ready to write to ``env/uv.lock``.
    ``None`` when no lockfile was located at gather time."""

    packages_json_bytes: bytes
    """``json.dumps`` of installed-distribution name/version pairs, ready to
    write to ``env/packages.json``."""


# ---------------------------------------------------------------------------
# Individual gatherers — each isolated so unit tests can probe one at a time.
# ---------------------------------------------------------------------------


def _capa_version() -> str:
    try:
        return _ilm.version("capa")
    except _ilm.PackageNotFoundError:
        return "0.0.0"


def _git_metadata(repo_root: Path | None) -> tuple[str | None, bool | None]:
    """Return ``(sha, dirty)``. Both ``None`` outside a git checkout or when
    the ``git`` CLI is unavailable.

    Uses subprocess rather than a Python git library to keep the dependency
    surface small — git is already required for development and CI.
    """
    if repo_root is None:
        return None, None
    if not (repo_root / ".git").exists() and not (repo_root / ".git").is_file():
        return None, None
    try:
        sha_out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        dirty_out = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None, None
    return sha_out.stdout.strip(), bool(dirty_out.stdout.strip())


def gather_capa(
    *,
    repo_root: Path | None = None,
    engine_version: str | None = None,
) -> CapaBlock:
    """Build the ``capa`` block of ``manifest.json``.

    ``repo_root`` is the directory to ask git about. Pass ``None`` (default)
    to skip the git probe — useful when capa is installed from a wheel and
    the running tree is not a checkout. ``engine_version`` is the engine
    code revision marker (plan §13.1); the engine plumbs this in at
    construction time.
    """
    sha, dirty = _git_metadata(repo_root)
    return CapaBlock(
        version=_capa_version(),
        git_sha=sha,
        git_dirty=dirty,
        build_time=None,
        engine_version=engine_version,
    )


def gather_python() -> PythonBlock:
    return PythonBlock(
        version=_platform.python_version(),
        implementation=_platform.python_implementation(),
        executable=sys.executable,
    )


def gather_platform() -> PlatformBlock:
    return PlatformBlock(
        os=_platform.platform(aliased=False, terse=False),
        machine=_platform.machine() or "unknown",
        node=_platform.node() or "unknown",
    )


def gather_lockfile(source: Path | None) -> tuple[LockfileBlock, bytes | None]:
    """Read the lockfile at ``source`` (typically ``<repo>/uv.lock``).

    Returns the manifest block plus the file's contents (so the caller can
    copy them into ``env/uv.lock``). When ``source`` is ``None`` or does not
    exist, the block records ``path=None`` and ``sha256=None`` and the bytes
    are ``None``.
    """
    if source is None or not source.is_file():
        return LockfileBlock(path=None, sha256=None), None
    data = source.read_bytes()
    digest = sha256(data).hexdigest()
    # Path is recorded relative to the bundle: the writer always lands the
    # lockfile at env/uv.lock, regardless of where it came from.
    return LockfileBlock(path="env/uv.lock", sha256=digest), data


def gather_plugins(
    lock: PluginsLock | None = None,
) -> tuple[PluginEntryBlock, ...]:
    """Mirror ``plugins.lock`` entries into manifest blocks.

    P0b records what the lock claims; P3 layers in actual install discovery
    and drift detection (already implemented in :mod:`capa.core.plugins_lock`).
    For now the bundle records the trust assertion verbatim.
    """
    if lock is None:
        return ()
    return tuple(
        PluginEntryBlock(
            id=entry.id,
            version=entry.version,
            package=entry.package,
            entry_point=entry.entry_point,
            distribution_hash=entry.distribution_hash,
        )
        for entry in lock.plugins
    )


def gather_packages_json() -> bytes:
    """JSON-encoded snapshot of installed distributions: ``[{name, version}, ...]``.

    Mirrors ``python -m pip list --format=json`` without spawning pip.
    Stable: sorted by name (case-insensitive). Always returns at least an
    empty list if discovery fails entirely.
    """
    try:
        dists = list(_ilm.distributions())
    except Exception:  # pragma: no cover - defensive; ilm rarely raises here
        dists = []
    rows: list[dict[str, str]] = []
    for dist in dists:
        meta = dist.metadata
        name = (meta.get("Name") if meta is not None else None) or ""
        if not name:
            continue
        rows.append({"name": name, "version": dist.version or ""})
    rows.sort(key=lambda row: row["name"].lower())
    return (json.dumps(rows, indent=2) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def gather_provenance(
    *,
    repo_root: Path | None = None,
    lockfile_source: Path | None = None,
    plugins_lock: PluginsLock | None = None,
    engine_version: str | None = None,
) -> Provenance:
    """One-shot provenance snapshot.

    Args:
        repo_root: directory to query with ``git`` for sha/dirty. ``None``
            skips the probe.
        lockfile_source: path to a ``uv.lock`` to copy into the bundle. The
            sha256 is recorded in the manifest. ``None`` is honest about the
            lockfile's absence.
        plugins_lock: parsed ``plugins.lock`` (see
            :mod:`capa.core.plugins_lock`). Mirrored verbatim into the
            manifest's ``plugins`` block.
        engine_version: engine task-group revision marker recorded into
            :attr:`CapaBlock.engine_version`. Plan §13.1.
    """
    lockfile_block, lockfile_bytes = gather_lockfile(lockfile_source)
    return Provenance(
        capa=gather_capa(repo_root=repo_root, engine_version=engine_version),
        python=gather_python(),
        platform=gather_platform(),
        lockfile=lockfile_block,
        plugins=gather_plugins(plugins_lock),
        lockfile_bytes=lockfile_bytes,
        packages_json_bytes=gather_packages_json(),
    )


# Helpers used by tests and the bundle writer — keep public so refactors
# don't break test fixtures.

__all__ = [
    "Provenance",
    "gather_capa",
    "gather_lockfile",
    "gather_packages_json",
    "gather_platform",
    "gather_plugins",
    "gather_provenance",
    "gather_python",
]


# ``datetime`` is currently unused in the public surface but kept for forward
# compatibility — capa version tags will eventually carry build time.
_ = datetime
