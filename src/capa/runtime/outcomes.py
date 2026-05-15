"""Helpers for translating terminal runtime outcomes."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from capa.runtime.conductor import RunOutcome
from capa.storage.manifest import BundleManifest

RunStatus = Literal["completed", "aborted", "crashed"]


def run_status_for_outcome(outcome: RunOutcome) -> RunStatus:
    """Map a conductor outcome to the manifest/catalog ``run_status``."""

    match outcome:
        case RunOutcome.COMPLETED:
            return "completed"
        case RunOutcome.ABORTED:
            return "aborted"
        case RunOutcome.CRASHED | RunOutcome.CRASHED_BUT_SEALED:
            return "crashed"
    raise ValueError(f"unhandled run outcome: {outcome!r}")


def read_bundle_status(bundle_path: Path | None) -> tuple[str, str]:
    """Read ``bundle_status`` / ``integrity_status`` from a manifest.

    Returns ``("open", "unknown")`` when the bundle path is absent or the
    manifest cannot be read, matching the previous headless/UI fallback.
    """

    if bundle_path is None:
        return "open", "unknown"
    try:
        manifest = BundleManifest.read(bundle_path / "manifest.json")
        return str(manifest.bundle_status), str(manifest.integrity.status)
    except Exception:
        return "open", "unknown"


__all__ = ["RunStatus", "read_bundle_status", "run_status_for_outcome"]
