"""``run.log`` — JSON-lines append handle for run-correlated structured logs.

Plan §13.1. The structlog config (root logger, processors, run_id binder)
lands in P0c; P0b just owns the bundle-side file. A line-buffered text handle
is enough — structlog's JSONRenderer produces one self-contained line per
event, and the engine task group will plumb the run-id context binder around
the writes.

This sink intentionally accepts pre-rendered JSON strings rather than dicts:
a future structlog handler can stream straight in without the line-by-line
re-encoding overhead, and tests can write deterministic byte sequences.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TextIO

from capa.core.errors import CapaError

LOG_FILENAME = "run.log"


class LogSinkError(CapaError):
    """Raised on writer-state errors."""


class LogSink:
    """Append-only JSON-lines writer.

    File is opened with line-buffering and ``newline=""`` so writes commit
    promptly without per-write fsync overhead. The bundle writer flushes /
    closes at finalize so the on-disk file is complete.

    Line atomicity: each write must already be a single JSON line ending in
    ``\\n``. ``write_line`` enforces this; ``write_event`` formats a dict
    safely.
    """

    __slots__ = ("_closed", "_handle", "_path")

    def __init__(self, bundle_root: Path) -> None:
        self._path = Path(bundle_root) / LOG_FILENAME
        # The handle's lifetime is the LogSink's lifetime; close()/exit close
        # it. Open without a context manager because we hold it across
        # arbitrary write_line() calls — that's the whole purpose of a sink.
        self._handle: TextIO | None = open(  # noqa: SIM115
            self._path, "a", encoding="utf-8", buffering=1, newline=""
        )
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    def write_line(self, line: str) -> None:
        """Append a JSON-encoded line. The caller guarantees the line is
        valid JSON; we add a trailing newline if absent.

        Writes after :meth:`close` are silent no-ops: the structlog stdlib
        handler may emit a final shutdown-phase line after the bundle's
        sinks have closed (engine finalize → close_sinks → engine.run.end
        log line). Surfacing that as an exception just creates noise.
        """
        if self._closed or self._handle is None:
            return
        if not line.endswith("\n"):
            line = line + "\n"
        self._handle.write(line)

    def write_event(self, event: dict[str, Any]) -> None:
        """Convenience wrapper that JSON-encodes ``event`` and appends a line.

        Like :meth:`write_line`, post-close writes are silent no-ops.
        """
        if self._closed or self._handle is None:
            return
        line = json.dumps(event, separators=(",", ":")) + "\n"
        self._handle.write(line)

    def flush(self) -> None:
        if self._closed or self._handle is None:
            return
        self._handle.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._handle is not None:
            try:
                self._handle.flush()
            finally:
                self._handle.close()
                self._handle = None

    def __enter__(self) -> LogSink:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = [
    "LOG_FILENAME",
    "LogSink",
    "LogSinkError",
]
