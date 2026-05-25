"""Recent-configs persistence.

Stores up to :data:`MAX_RECENTS` paths the operator has recently opened
in ``~/.capa/recents.json``. The welcome hero and the Setup tab's
``Open ▾`` overflow both read this list.

The format is a JSON array of ``{"path": str, "opened_at": str}``
records, sorted most-recent first. Records whose path no longer exists
on disk are filtered out at read time — the file is not eagerly
rewritten on read, only on the next mutation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import structlog

RECENTS_PATH: Final[Path] = Path.home() / ".capa" / "recents.json"
MAX_RECENTS: Final[int] = 10

_logger = structlog.get_logger("capa.ui.recents")


@dataclass(frozen=True, slots=True)
class RecentEntry:
    """One row in the Welcome screen's "Recents" list — a config path and its last-opened timestamp."""

    path: Path
    opened_at: datetime


def load_recents() -> list[RecentEntry]:
    """Return existing recents, most recent first.

    Missing file → empty list. Entries whose path no longer exists are
    dropped silently. Malformed records are skipped, not raised — the
    file may be hand-edited and we'd rather show what we can than block
    the welcome screen on a typo.
    """
    if not RECENTS_PATH.exists():
        return []
    try:
        raw = json.loads(RECENTS_PATH.read_text())
    except (OSError, ValueError) as exc:
        _logger.warning("ui.recents.read_failed", error=str(exc))
        return []
    if not isinstance(raw, list):
        return []
    out: list[RecentEntry] = []
    for record in raw:
        if not isinstance(record, dict):
            continue
        path_str = record.get("path")
        opened_at = record.get("opened_at")
        if not isinstance(path_str, str) or not isinstance(opened_at, str):
            continue
        path = Path(path_str)
        if not path.exists():
            continue
        try:
            ts = datetime.fromisoformat(opened_at)
        except ValueError:
            continue
        out.append(RecentEntry(path=path, opened_at=ts))
    return out


def record_open(path: Path) -> None:
    """Push ``path`` to the front of the recents list, persist atomically.

    Existing entries for the same resolved path are removed so the list
    never grows duplicates. Truncates to :data:`MAX_RECENTS`. Best-effort
    — IO errors are logged and swallowed so a permissions hiccup never
    breaks Open.
    """
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    existing = load_recents()
    filtered = [entry for entry in existing if _resolve_or_self(entry.path) != resolved]
    filtered.insert(0, RecentEntry(path=resolved, opened_at=datetime.now(UTC)))
    filtered = filtered[:MAX_RECENTS]
    payload = [
        {"path": str(entry.path), "opened_at": entry.opened_at.isoformat()} for entry in filtered
    ]
    try:
        RECENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        RECENTS_PATH.write_text(json.dumps(payload, indent=2))
    except OSError as exc:
        _logger.warning("ui.recents.write_failed", error=str(exc))


def _resolve_or_self(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


__all__ = ["MAX_RECENTS", "RECENTS_PATH", "RecentEntry", "load_recents", "record_open"]
