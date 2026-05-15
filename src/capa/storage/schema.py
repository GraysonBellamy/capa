"""Bundle schema version and migration registry.

Plan §8.3. ``manifest.json`` carries a ``bundle_schema_version`` integer.
Bumping the layout bumps the version; this module's :data:`MIGRATIONS` maps
``old_version -> migrate(dict) -> dict`` so old bundles remain first-class.

v1 → v2 (Arrow IPC streaming for in-flight files): in-flight transit format
changed from ``*.in-flight.parquet`` to ``*.in-flight.arrows``. Final parquet
artifacts are unchanged. v1 has no migration registered — capa hadn't shipped
when v1 existed, so any stray v1 manifest is a developer-machine artifact and
should reject loudly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from capa.core.errors import CapaError

BUNDLE_SCHEMA_VERSION: int = 2
"""Current bundle layout version. Every bundle written by this version of
capa carries this in ``manifest.json``."""


class BundleSchemaError(CapaError):
    """Raised when a bundle's recorded schema version is unknown or its
    migration chain cannot be completed."""


Migration = Callable[[dict[str, Any]], dict[str, Any]]
"""``migrate(manifest_dict) -> manifest_dict``. Pure dict-in/dict-out so the
registry can compose without instantiating Pydantic models mid-chain."""


MIGRATIONS: dict[int, Migration] = {}
"""Maps ``from_version`` to the migration that produces ``from_version + 1``.

Empty by default. To add a v1 → v2 migration, register
``MIGRATIONS[1] = _migrate_1_to_2``.
"""


def current_version() -> int:
    """Return :data:`BUNDLE_SCHEMA_VERSION`. Use this from the manifest writer
    rather than importing the constant directly so test code can monkey-patch
    if needed."""
    return BUNDLE_SCHEMA_VERSION


def migrate(manifest_dict: dict[str, Any]) -> dict[str, Any]:
    """Walk ``manifest_dict`` from its recorded version up to the current
    one, applying each registered migration in order.

    Returns the upgraded dict (same object if already current). Raises
    :class:`BundleSchemaError` if any required step is missing.
    """
    recorded = manifest_dict.get("bundle_schema_version")
    if not isinstance(recorded, int):
        raise BundleSchemaError(
            f"manifest is missing a numeric bundle_schema_version (got {recorded!r})"
        )
    if recorded > BUNDLE_SCHEMA_VERSION:
        raise BundleSchemaError(
            f"manifest schema v{recorded} is newer than this capa "
            f"(supports v{BUNDLE_SCHEMA_VERSION}); upgrade capa to read it"
        )
    current = manifest_dict
    while recorded < BUNDLE_SCHEMA_VERSION:
        step = MIGRATIONS.get(recorded)
        if step is None:
            raise BundleSchemaError(
                f"no migration registered from bundle schema v{recorded} to v{recorded + 1}"
            )
        current = step(current)
        current["bundle_schema_version"] = recorded + 1
        recorded += 1
    return current


__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "MIGRATIONS",
    "BundleSchemaError",
    "Migration",
    "current_version",
    "migrate",
]
