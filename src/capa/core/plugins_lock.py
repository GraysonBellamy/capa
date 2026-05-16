"""``plugins.lock`` parser and drift detector.

production mode uses a ``plugins.lock`` file containing plugin id,
package name, version, entry point, and distribution hash. Startup refuses an
installed plugin whose hash/version differs from the lock unless the operator
explicitly runs ``capa plugins trust ...``. The lock snapshot is copied into
every bundle and mirrored into ``manifest.json.plugins``.

Ships:

* the lock file format (TOML),
* parse + validate via Pydantic,
* :func:`detect_drift` against an "installed" set (pure-data — no
  ``importlib.metadata`` discovery; the discovery step is in the
  procedure runtime).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PluginEntry(BaseModel):
    """One entry in ``plugins.lock``.

    The four fields ``(id, package, version, distribution_hash)`` together
    constitute the trust assertion: "this id was installed from this package
    at this version with this hash, and it is allowed to run real hardware."
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    """Plugin id as declared on the entry point (e.g.
    ``"capa.builtin.recipe_runner"`` or ``"lab.heatflux.calibration"``)."""

    package: str
    """Distribution name (``"capa"``, ``"lab-heatflux"``)."""

    version: str
    """PEP 440 version string. Compared exactly against the installed
    distribution's version; range matching is not supported here — pin to
    a specific version in the lock."""

    entry_point: str
    """``"module.path:Class"`` form, used to import and instantiate."""

    distribution_hash: str
    """``"sha256:..."`` (or ``"sha512:..."``) of the installed wheel/sdist.
    Computed at install time; recomputed at startup; mismatch implies a
    modified install."""

    @field_validator("entry_point")
    @classmethod
    def _check_entry_point(cls, value: str) -> str:
        if ":" not in value:
            raise ValueError(f"entry_point must be 'module.path:Class', got {value!r}")
        return value

    @field_validator("distribution_hash")
    @classmethod
    def _check_hash(cls, value: str) -> str:
        if ":" not in value:
            raise ValueError(
                f"distribution_hash must be 'sha256:HEX' or 'sha512:HEX', got {value!r}"
            )
        algo, _, digest = value.partition(":")
        if algo not in {"sha256", "sha512"}:
            raise ValueError(f"distribution_hash algorithm must be sha256 or sha512, got {algo!r}")
        if not digest:
            raise ValueError("distribution_hash digest is empty")
        return value


class PluginsLock(BaseModel):
    """Top-level ``plugins.lock`` model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = 1
    """Lock-file schema version. Bumped on incompatible format changes."""

    plugins: tuple[PluginEntry, ...] = Field(default_factory=tuple)

    @field_validator("plugins")
    @classmethod
    def _check_unique_ids(cls, value: tuple[PluginEntry, ...]) -> tuple[PluginEntry, ...]:
        ids = [p.id for p in value]
        if len(ids) != len(set(ids)):
            raise ValueError("plugins.lock contains duplicate ids")
        return value

    @classmethod
    def load(cls, path: str | Path) -> PluginsLock:
        """Parse a ``plugins.lock`` TOML file."""
        with open(path, "rb") as fp:
            data = tomllib.load(fp)
        return cls.model_validate(data)

    def get(self, plugin_id: str) -> PluginEntry | None:
        for entry in self.plugins:
            if entry.id == plugin_id:
                return entry
        return None


# ---------------------------------------------------------------------------
# Drift detection — what the production trust check asks at startup.
# ---------------------------------------------------------------------------


class DriftKind(Enum):
    """Discrete drift categories.

    Recorded against each id where the installed view differs from the lock.
    """

    MISSING_FROM_INSTALL = "missing_from_install"
    """Lock entry exists; install does not."""

    MISSING_FROM_LOCK = "missing_from_lock"
    """Install exists; lock entry does not. Production refuses these unless
    the operator explicitly trusts via ``capa plugins trust``."""

    VERSION_MISMATCH = "version_mismatch"

    HASH_MISMATCH = "hash_mismatch"

    ENTRY_POINT_MISMATCH = "entry_point_mismatch"


@dataclass(frozen=True, slots=True)
class Drift:
    """One detected drift between lock and installed view."""

    plugin_id: str
    kind: DriftKind
    expected: str | None
    actual: str | None


@dataclass(frozen=True, slots=True)
class InstalledPlugin:
    """Pure-data view of an installed plugin.

    Production code builds these from ``importlib.metadata``; tests build
    them directly. The discovery step is deliberately kept out of the parser
    so the schema and drift logic can be exercised without a real install.
    """

    id: str
    package: str
    version: str
    entry_point: str
    distribution_hash: str


def detect_drift(
    lock: PluginsLock,
    installed: list[InstalledPlugin],
) -> list[Drift]:
    """Compare ``lock`` to ``installed`` and return every drift.

    Order: every lock entry is examined first (any of MISSING_FROM_INSTALL /
    VERSION_MISMATCH / HASH_MISMATCH / ENTRY_POINT_MISMATCH that applies);
    then every install entry not in the lock is reported as
    MISSING_FROM_LOCK.
    """
    drifts: list[Drift] = []
    installed_by_id = {p.id: p for p in installed}

    for entry in lock.plugins:
        inst = installed_by_id.get(entry.id)
        if inst is None:
            drifts.append(
                Drift(
                    plugin_id=entry.id,
                    kind=DriftKind.MISSING_FROM_INSTALL,
                    expected=f"{entry.package}=={entry.version}",
                    actual=None,
                )
            )
            continue
        if inst.version != entry.version:
            drifts.append(
                Drift(
                    plugin_id=entry.id,
                    kind=DriftKind.VERSION_MISMATCH,
                    expected=entry.version,
                    actual=inst.version,
                )
            )
        if inst.distribution_hash != entry.distribution_hash:
            drifts.append(
                Drift(
                    plugin_id=entry.id,
                    kind=DriftKind.HASH_MISMATCH,
                    expected=entry.distribution_hash,
                    actual=inst.distribution_hash,
                )
            )
        if inst.entry_point != entry.entry_point:
            drifts.append(
                Drift(
                    plugin_id=entry.id,
                    kind=DriftKind.ENTRY_POINT_MISMATCH,
                    expected=entry.entry_point,
                    actual=inst.entry_point,
                )
            )

    locked_ids = {p.id for p in lock.plugins}
    for inst in installed:
        if inst.id not in locked_ids:
            drifts.append(
                Drift(
                    plugin_id=inst.id,
                    kind=DriftKind.MISSING_FROM_LOCK,
                    expected=None,
                    actual=f"{inst.package}=={inst.version}",
                )
            )

    return drifts


__all__ = [
    "Drift",
    "DriftKind",
    "InstalledPlugin",
    "PluginEntry",
    "PluginsLock",
    "detect_drift",
]
