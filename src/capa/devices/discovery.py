"""Shared discovery routing for adapter descriptors.

Discovery is descriptor-driven: callers pick an
:class:`~capa.devices.registry.AdapterDescriptor`, and this module handles
the boring-but-important details of importing the adapter module, selecting
the right hook, normalising the result, and preserving a useful error string.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

from capa.devices.registry import ADAPTERS, AdapterDescriptor, ensure_adapters_loaded


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """Result of one adapter discovery probe."""

    descriptor: AdapterDescriptor
    rows: list[dict[str, Any]]
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the discovery probe completed successfully."""
        return self.error is None


def discoverable_descriptors(
    *,
    adapter: str | None = None,
    include_cameras: bool = True,
) -> list[AdapterDescriptor]:
    """Return discoverable descriptors, optionally filtered by id or family.

    ``adapter`` accepts either a full descriptor id
    (``"capa.devices.alicat"``) or a family (``"alicat"``).
    """

    ensure_adapters_loaded()
    descriptors = [
        d
        for d in ADAPTERS.values()
        if d.discoverable and (include_cameras or not d.family.startswith("camera_"))
    ]
    if adapter is None:
        return descriptors
    return [d for d in descriptors if d.id == adapter or d.family == adapter]


async def discover_descriptor(descriptor: AdapterDescriptor) -> DiscoveryResult:
    """Run one descriptor's discovery hook."""

    try:
        module = importlib.import_module(descriptor.id)
    except ImportError as exc:
        return DiscoveryResult(descriptor=descriptor, rows=[], error=f"not importable ({exc})")

    hook = getattr(module, "discover_cameras", None) or getattr(module, "discover", None)
    if hook is None:
        return DiscoveryResult(
            descriptor=descriptor,
            rows=[],
            error="no discover_cameras()/discover() hook",
        )

    try:
        rows = await hook()
    except Exception as exc:
        return DiscoveryResult(
            descriptor=descriptor,
            rows=[],
            error=f"failed ({type(exc).__name__}: {exc})",
        )
    if not isinstance(rows, list):
        return DiscoveryResult(
            descriptor=descriptor,
            rows=[],
            error=f"returned {type(rows).__name__}, expected list",
        )

    out: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            out.append(row)
    return DiscoveryResult(descriptor=descriptor, rows=out)


__all__ = [
    "DiscoveryResult",
    "discover_descriptor",
    "discoverable_descriptors",
]
