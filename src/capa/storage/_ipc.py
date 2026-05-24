"""Arrow IPC streaming helpers — the in-flight transit format.

Replaces ``pyarrow.parquet.ParquetWriter`` for the three telemetry sinks. The
canonical ``[schema][batch][batch]...`` framing means a SIGKILL or power-loss
mid-write tears at most the trailing message; everything before the tear is
recoverable. See ``arrow-ipc-streaming-plan.md`` for context.

Implementation note — file backend (the OSFile + shadow-fd pattern)
-------------------------------------------------------------------

The plan called for ``pa.OSFile`` (pyarrow's C++ fast-path) plus
``os.fsync(file.fileno())``. On pyarrow 24.0.0 (and earlier) ``OSFile.fileno()``
is broken in write mode — see ``io.pxi`` ``OSFile``: it accesses ``self.handle``,
which is only populated on the readable code path. The fix landed on upstream
``main`` in https://github.com/apache/arrow/pull/49750 (merged 2026-04-22),
one day after the 24.0.0 release. It will first ship in pyarrow 25.0.0.

Until we bump ``pyarrow >= 25``, we work around it: open the writer through
``pa.OSFile`` (so pyarrow keeps its native syscall path) and open a *shadow*
read-only fd via ``os.open(path, os.O_RDONLY)`` purely to call ``os.fsync``
on. POSIX fsync flushes dirty pages at the inode level, so any fd to the
file works.

When pyarrow exposes ``fileno()`` on this stream, drop the shadow fd and call
``os.fsync(self._arrow_file.fileno())`` directly.

Windows portability of this approach: pyarrow's ``FileOutputStream`` opens
with ``FILE_SHARE_READ | FILE_SHARE_WRITE`` (arrow C++ ``io_util.cc``
``FileOpenWritable``), so the shadow ``os.open(O_RDONLY)`` is granted by
Windows. If pyarrow tightens that share mode in a future release, the
shadow fd would fail with ``PermissionError`` — easy to detect.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.ipc

from capa.core.errors import CapaError

INFLIGHT_EXTENSION = ".arrows"
"""File extension for in-flight Arrow IPC streams (replaces ``.parquet``)."""

INFLIGHT_COMPRESSION = "zstd"
"""IPC body compression. IPC has no level knob — defaults to zstd:1."""

DEFAULT_FLUSH_ROWS_BULK = 1024
"""Scalars / device-records flush cadence."""

DEFAULT_FLUSH_ROWS_FRAMES = 256
"""Video frame-index flush cadence (~8 s of 30 Hz)."""


class IpcStreamSinkError(CapaError):
    """Raised on writer-state errors (write after close, schema drift)."""


def make_write_options() -> pa.ipc.IpcWriteOptions:  # type: ignore[name-defined]
    """Build the IpcWriteOptions used by every in-flight stream."""
    return pa.ipc.IpcWriteOptions(compression=INFLIGHT_COMPRESSION)  # type: ignore[attr-defined]


class IpcStreamSink:
    """Thin RAII wrapper around a single Arrow IPC stream file.

    Lazy-open: the file is created on first :meth:`write_table`, not in
    ``__init__``. This matches the device-records sink, where the schema is
    only known after the first batch arrives.

    Lifecycle: construct → ``write_table`` 1..N → ``close``. Every successful
    ``write_table`` ends with ``os.fsync`` so a crash leaves a parseable
    prefix on disk. ``close`` is idempotent; safe in a ``finally`` block.
    Not thread-safe.
    """

    __slots__ = ("_arrow_file", "_closed", "_path", "_schema", "_sync_fd", "_writer")

    def __init__(self, path: Path, schema: pa.Schema | None = None) -> None:
        self._path = path
        self._schema = schema
        self._arrow_file: pa.OSFile | None = None
        self._sync_fd: int | None = None
        self._writer: pa.RecordBatchStreamWriter | None = None
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def has_data(self) -> bool:
        """True once the stream has been opened (i.e. at least one batch
        has crossed a write boundary)."""
        return self._writer is not None

    def _ensure_open(self, schema: pa.Schema) -> None:
        if self._writer is not None:
            return
        if self._schema is None:
            self._schema = schema
        elif not self._schema.equals(schema):
            raise IpcStreamSinkError(
                f"{self._path.name}: schema drift on first write — opened with "
                f"{self._schema}, got {schema}"
            )
        # OSFile is pyarrow's C++ fast path — writes go straight to the OS
        # via FileOutputStream's syscall path, no Python-side buffering.
        self._arrow_file = pa.OSFile(str(self._path), "wb")
        # Shadow fd for fsync; see module docstring. Drop this once
        # self._arrow_file.fileno() is available.
        try:
            self._sync_fd = os.open(str(self._path), os.O_RDONLY)
        except OSError:
            self._arrow_file.close()
            self._arrow_file = None
            raise
        try:
            self._writer = pa.ipc.new_stream(
                self._arrow_file, self._schema, options=make_write_options()
            )
        except Exception:
            os.close(self._sync_fd)
            self._sync_fd = None
            self._arrow_file.close()
            self._arrow_file = None
            raise

    def write_table(self, table: pa.Table) -> None:
        """Append ``table`` as one record batch, then fsync."""
        if self._closed:
            raise IpcStreamSinkError(f"{self._path.name}: write_table() after close()")
        self._ensure_open(table.schema)
        if self._schema is not None and not self._schema.equals(table.schema):
            raise IpcStreamSinkError(
                f"{self._path.name}: schema drift mid-stream — locked "
                f"{self._schema}, got {table.schema}"
            )
        assert self._writer is not None
        self._writer.write_table(table)
        self.flush()

    def flush(self) -> None:
        """Push pending OS-cache bytes to the disk via fsync.

        No-op if the stream hasn't been opened yet. ``pa.OSFile`` writes
        already hit the OS file cache directly (no Python-side buffer to
        drain), so fsync on the shadow fd is the only step needed for
        crash durability.
        """
        if self._sync_fd is None:
            return
        with contextlib.suppress(OSError):
            os.fsync(self._sync_fd)

    def close(self) -> None:
        """Close writer + file. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._writer is not None:
            try:
                self._writer.close()
            finally:
                self._writer = None
        if self._arrow_file is not None:
            try:
                self._arrow_file.close()
            finally:
                self._arrow_file = None
        if self._sync_fd is not None:
            try:
                os.close(self._sync_fd)
            finally:
                self._sync_fd = None

    def __enter__(self) -> IpcStreamSink:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def read_recoverable(path: Path) -> pa.Table | None:
    """Open an Arrow IPC stream and read every intact batch.

    Returns:
        - A :class:`pa.Table` of every readable batch when the file is intact
          *or* truncated mid-batch (returns the prefix in the latter case).
        - An empty-but-typed table when the schema message decoded but no
          batches were ever written.
        - ``None`` when the schema message itself is unreadable (the file was
          torn before the first flush boundary, or the file is missing /
          empty / unreadable).

    Never raises on truncation. Catches :class:`pyarrow.lib.ArrowInvalid`
    and :class:`OSError` raised mid-read; pyarrow 24 surfaces a torn message
    body as OSError ("Expected to read N bytes, got M"), so both must be
    handled.
    """
    batches: list[pa.RecordBatch] = []
    schema: pa.Schema | None = None
    try:
        with pa.OSFile(str(path), "rb") as fd:
            try:
                reader = pa.ipc.open_stream(fd)
            except (pa.lib.ArrowInvalid, OSError):
                return None
            schema = reader.schema
            try:
                for batch in reader:
                    batches.append(batch)
            except (pa.lib.ArrowInvalid, OSError):
                # Trailing message torn — return the prefix we already read.
                pass
    except OSError:
        return None
    if schema is None:
        return None
    if batches:
        return pa.Table.from_batches(batches, schema=schema)
    return pa.table({name: [] for name in schema.names}, schema=schema)


__all__ = [
    "DEFAULT_FLUSH_ROWS_BULK",
    "DEFAULT_FLUSH_ROWS_FRAMES",
    "INFLIGHT_COMPRESSION",
    "INFLIGHT_EXTENSION",
    "IpcStreamSink",
    "IpcStreamSinkError",
    "make_write_options",
    "read_recoverable",
]
