"""Shared helpers for adapter tests.

All ``DeviceAdapter.start`` implementations take one
:class:`AdapterStartContext`. Most unit tests do not care about the
specific run id or bundle root, so this helper builds a minimal context
with sensible defaults.
"""

from __future__ import annotations

from pathlib import Path

from capa.core.clock import RunClock
from capa.devices.adapter import AdapterStartContext


def make_start_ctx(
    *,
    clock: RunClock | None = None,
    run_id: str = "test-run",
    bundle_root: Path | None = None,
) -> AdapterStartContext:
    """Build a minimal :class:`AdapterStartContext` for unit tests.

    Defaults pick a fresh :class:`RunClock` (anchored at "now") and a
    placeholder bundle root. Tests that exercise camera adapters or
    bundle-aware logic should pass explicit values.
    """
    return AdapterStartContext(
        clock=clock if clock is not None else RunClock.now(),
        run_id=run_id,
        bundle_root=bundle_root if bundle_root is not None else Path("/tmp/test-bundle"),
    )


__all__ = ["make_start_ctx"]
