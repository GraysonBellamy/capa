"""``events.sqlite`` — operator notes, segment transitions, alarms, errors,
device events.

Plan §8 / §13.1. SQLite is transactional and crash-safe — events written are
not lost even on abnormal exit. Used by the engine task group and the
operator-events dock; any caller can write without dragging in the engine.

Two write paths:

* :meth:`EventsSink.write_device_event` — typed
  :class:`~capa.devices.records.DeviceEvent` from an adapter.
* :meth:`EventsSink.write` — generic ``(kind, severity, source, message,
  metadata)`` record from the procedure / operator / safety layers.

Schema is intentionally narrow — every column maps to a question someone will
ask when reading the bundle, and ``metadata_json`` carries the open set.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from capa.core.errors import CapaError
from capa.devices.records import DeviceEvent

EVENTS_FILENAME = "events.sqlite"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    t_mono_ns    INTEGER NOT NULL,
    t_utc        TEXT    NOT NULL,
    kind         TEXT    NOT NULL,
    severity     TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    message      TEXT    NOT NULL,
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_t_mono_ns ON events (t_mono_ns);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events (kind);
"""

# Severity values mirror DeviceEvent + add procedure/operator levels.
ALLOWED_SEVERITIES = frozenset({"info", "warning", "error"})


class EventsSinkError(CapaError):
    """Raised on writer-state errors."""


class EventsSink:
    """SQLite writer for ``events.sqlite``.

    One connection, one writer thread (assumed external). Commits after every
    write — losing events to a crash is the worst possible outcome. Throughput
    is fine for capa's 3–60 Hz envelope; if a procedure ever needs bulk events
    we can add a batched API later.

    The connection uses ``journal_mode=WAL`` and ``synchronous=NORMAL``: WAL
    keeps writes durable across crashes, and NORMAL gives us the fsync-per-
    commit that capa's bundle promise requires without the extra fsync per
    page from FULL.
    """

    __slots__ = ("_closed", "_conn", "_path")

    def __init__(self, bundle_root: Path) -> None:
        self._path = Path(bundle_root) / EVENTS_FILENAME
        self._closed = False
        # check_same_thread=False so a future engine that uses a writer task
        # can hand the connection between coroutine threads. The flag is harmless.
        self._conn = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode = WAL;")
        self._conn.execute("PRAGMA synchronous = NORMAL;")
        self._conn.executescript(_SCHEMA)

    @property
    def path(self) -> Path:
        return self._path

    def write(
        self,
        *,
        kind: str,
        message: str,
        severity: str = "info",
        source: str = "engine",
        t_mono_ns: int,
        t_utc: datetime,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Insert one event row.

        ``severity`` must be one of :data:`ALLOWED_SEVERITIES`. ``source``
        is free-form (``"watlow:heater"``, ``"procedure:capa.recipe_runner"``,
        ``"operator"``, ``"safety"``). ``metadata`` is JSON-serialized into
        the ``metadata_json`` column.
        """
        if self._closed:
            raise EventsSinkError("write() after close()")
        if severity not in ALLOWED_SEVERITIES:
            raise EventsSinkError(
                f"severity must be one of {sorted(ALLOWED_SEVERITIES)}, got {severity!r}"
            )
        meta_json = json.dumps(metadata) if metadata else None
        self._conn.execute(
            """
            INSERT INTO events
            (t_mono_ns, t_utc, kind, severity, source, message, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (
                int(t_mono_ns),
                t_utc.isoformat(),
                kind,
                severity,
                source,
                message,
                meta_json,
            ),
        )

    def write_device_event(self, event: DeviceEvent) -> None:
        """Convenience wrapper around :meth:`write` for adapter events."""
        self.write(
            kind=event.kind,
            message=event.message,
            severity=event.severity,
            source=f"{event.adapter}:{event.device}",
            t_mono_ns=event.t_mono_ns,
            t_utc=event.t_utc,
            metadata=event.metadata or None,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            # Final WAL checkpoint so a fresh-open reader sees everything.
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        finally:
            self._conn.close()

    def __enter__(self) -> EventsSink:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = [
    "ALLOWED_SEVERITIES",
    "EVENTS_FILENAME",
    "EventsSink",
    "EventsSinkError",
]
