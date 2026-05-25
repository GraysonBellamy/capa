"""``status.sqlite`` — periodic device-health snapshots.

Low-rate device-health rows (Watlow alarm bits, Alicat valve
drive, balance stable flag, comm latency, firmware version) live here, kept
separate from ``scalars.parquet`` so the engineering signal table doesn't get
polluted with diagnostic noise. Drop-oldest semantics are enforced by the
producer queue — this sink simply persists what arrives.

Schema is one row per :class:`~capa.devices.records.DeviceSnapshot`. The
free-form ``fields`` dict is JSON-encoded into ``fields_json``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from capa.core.errors import CapaError
from capa.devices.records import DeviceSnapshot

STATUS_FILENAME = "status.sqlite"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS status (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    adapter     TEXT    NOT NULL,
    device      TEXT    NOT NULL,
    t_mono_ns   INTEGER NOT NULL,
    t_utc       TEXT    NOT NULL,
    health      TEXT    NOT NULL,
    fields_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_status_device ON status (adapter, device, t_mono_ns);
"""


class StatusSinkError(CapaError):
    """Raised on writer-state errors."""


class StatusSink:
    """SQLite writer for ``status.sqlite``.

    Same ``WAL`` + ``NORMAL`` setup as :class:`~capa.storage.events_sink.EventsSink`.
    Status rows are not as durability-critical (latest value semantics, the
    producer drops old ones), but the same engine writes both, so we keep the
    connection setup symmetric.
    """

    __slots__ = ("_closed", "_conn", "_path")

    def __init__(self, bundle_root: Path) -> None:
        self._path = Path(bundle_root) / STATUS_FILENAME
        self._closed = False
        self._conn = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode = WAL;")
        self._conn.execute("PRAGMA synchronous = NORMAL;")
        self._conn.executescript(_SCHEMA)

    @property
    def path(self) -> Path:
        """Path to the bundle's ``status.sqlite`` database."""
        return self._path

    def write(self, snapshot: DeviceSnapshot) -> None:
        """Insert one :class:`DeviceSnapshot` row into ``status.sqlite``.

        Raises:
            StatusSinkError: ``write`` was called after :meth:`close`.
        """
        if self._closed:
            raise StatusSinkError("write() after close()")
        fields_json = json.dumps(dict(snapshot.fields)) if snapshot.fields else None
        self._conn.execute(
            """
            INSERT INTO status
            (adapter, device, t_mono_ns, t_utc, health, fields_json)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (
                snapshot.adapter,
                snapshot.device,
                int(snapshot.t_mono_ns),
                snapshot.t_utc.isoformat(),
                snapshot.health,
                fields_json,
            ),
        )

    def close(self) -> None:
        """Checkpoint the WAL and close the connection. Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        finally:
            self._conn.close()

    def __enter__(self) -> StatusSink:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = [
    "STATUS_FILENAME",
    "StatusSink",
    "StatusSinkError",
]
