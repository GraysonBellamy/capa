"""Library-native device-record sidecars — ``device_records/<adapter>.parquet``.

The device libraries already made careful choices about
their emitted row/block shapes; capa preserves those next to the normalized
``scalars.parquet``. One file per adapter family:

==================== ============================ ====================================
Adapter id           Native shape                  path
==================== ============================ ====================================
``alicat``           wide_row                      ``device_records/alicat.parquet``
``watlow``           long_row                      ``device_records/watlow.parquet``
``sartorius``        single_value_row              ``device_records/sartorius.parquet``
``nidaq_polled``     wide_row                      ``device_records/nidaq_polled.parquet``
``nidaq_block``      block (TDMS / sidecar later)  deferred
==================== ============================ ====================================

This sink is a multiplexer: it owns one ``_PerFamilyWriter`` per ``adapter``
key seen on incoming :class:`SourceRecord`\\ s. Each per-family writer locks
its column types after its first flush; subsequent rows that contradict the
locked schema raise :class:`SchemaDriftError`.

``shape="block"`` records are silently skipped (logged in metadata via
:meth:`DeviceRecordsSink.skipped_blocks`); the block sidecar / TDMS landing
path is deferred per"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa

from capa.core.errors import CapaError
from capa.devices.records import RecordShape, SourceRecord
from capa.storage._ipc import DEFAULT_FLUSH_ROWS_BULK, IpcStreamSink

INFLIGHT_FLUSH_ROWS = DEFAULT_FLUSH_ROWS_BULK
DEVICE_RECORDS_DIRNAME = "device_records"
INFLIGHT_SUFFIX = ".in-flight.arrows"
FINAL_SUFFIX = ".parquet"


class DeviceRecordsSinkError(CapaError):
    """Raised on writer-state errors (write after close, no rows seen)."""


class SchemaDriftError(DeviceRecordsSinkError):
    """Raised when a per-family writer sees a row whose column schema is
    incompatible with the schema locked at first flush.

    schema-stability across runs is unit-tested. Real adapters
    that change shape mid-run (firmware reconfiguration) would surface here
    as a hard error rather than corrupting the device-records file.
    """


# ---------------------------------------------------------------------------
# Type inference for wide / long / single-value rows
# ---------------------------------------------------------------------------


def _classify_value(v: Any) -> str:
    """Tag a single non-None Python value with its narrowest type marker.

    The order matters: bool is an int subclass, so it must be checked before
    int. ``bytes`` becomes ``"bytes"`` so the column is stored as binary;
    library row-emitters generally avoid bytes (raw payloads stay in
    :class:`SourceRecord.metadata` instead), but we don't refuse them here.
    """
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, bytes):
        return "bytes"
    if isinstance(v, datetime):
        return "datetime"
    return "str"  # fallback: stringify on cast


_PROMOTION_ORDER = {
    "bool": 0,
    "int": 1,
    "float": 2,
    "str": 3,
    "bytes": 4,
    "datetime": 5,
}


def _promote(a: str, b: str) -> str:
    """Pick the wider of two type tags.

    Numeric promotions: bool < int < float. Anything mixed with string
    promotes to string (the row dict's emitting library lost type info
    somewhere — preserve fidelity by storing as text). Bytes and datetime
    don't promote into anything else; mixing them with another type is a
    drift error.
    """
    if a == b:
        return a
    pair = frozenset({a, b})
    if pair == frozenset({"bool", "int"}):
        return "int"
    if pair <= {"bool", "int", "float"}:
        return "float"
    if "str" in pair and pair <= {"bool", "int", "float", "str"}:
        return "str"
    raise SchemaDriftError(f"incompatible value types: {a!r} and {b!r}")


def _tag_to_arrow(tag: str) -> pa.DataType:
    return {
        "bool": pa.bool_(),
        "int": pa.int64(),
        "float": pa.float64(),
        "str": pa.string(),
        "bytes": pa.binary(),
        "datetime": pa.timestamp("us", tz="UTC"),
    }[tag]


def _coerce(value: Any, tag: str) -> Any:
    """Cast ``value`` to the runtime representation Arrow expects for ``tag``."""
    if value is None:
        return None
    if tag == "str" and not isinstance(value, str):
        return str(value)
    if tag == "float" and not isinstance(value, float):
        # bool/int widen to float
        return float(value)
    if tag == "int" and isinstance(value, bool):
        return int(value)
    if tag == "datetime" and isinstance(value, str):
        # Library rows produce ISO-8601 timestamps; tolerate them when the
        # column was inferred as datetime from another row.
        return datetime.fromisoformat(value)
    return value


def _infer_schema(rows: Iterable[dict[str, Any]]) -> pa.Schema:
    """Walk a buffer of rows and pick a column type per key.

    Every key seen in any row appears in the resulting schema, even when its
    value is ``None`` in every observed row — otherwise the next flush would
    raise :class:`SchemaDriftError` for a column that's been there all along.

    Columns whose only observed value is ``None`` are typed as nullable
    string. That's the safest default: subsequent rows that fill the column
    with a string land cleanly, and rows that fill it with a number are
    coerced to text without losing fidelity.
    """
    # tag is None until we've seen a real value; that distinction matters
    # because a "str"-tagged column that's never seen a real string should
    # be overwritten by the first real type, while a real-string column
    # should *promote* mixed numerics to string.
    column_tags: dict[str, str | None] = {}
    for row in rows:
        for key, value in row.items():
            if value is None:
                column_tags.setdefault(key, None)
                continue
            tag = _classify_value(value)
            existing = column_tags.get(key)
            if existing is None:
                column_tags[key] = tag
            else:
                column_tags[key] = _promote(existing, tag)
    fields: list[pa.Field] = [
        pa.field(key, _tag_to_arrow(tag or "str"), nullable=True)
        for key, tag in column_tags.items()
    ]
    return pa.schema(fields)


# ---------------------------------------------------------------------------
# Header columns added by capa to every device_records row
# ---------------------------------------------------------------------------

_HEADER_FIELDS = [
    pa.field("record_id", pa.string(), nullable=False),
    pa.field("t_mono_ns", pa.int64(), nullable=False),
    pa.field("t_utc", pa.timestamp("us", tz="UTC"), nullable=False),
]
_HEADER_KEYS = tuple(f.name for f in _HEADER_FIELDS)


def _record_to_row(record: SourceRecord) -> dict[str, Any]:
    """Combine capa header columns with the library-native row.

    Header columns shadow any same-named keys in ``record.row`` — adapters
    that put their own ``t_mono_ns`` in the row are still preserved as a
    second column under the library's name (it's part of the long/wide
    schema), but the canonical ``t_mono_ns`` column comes from the
    SourceRecord proper.
    """
    out: dict[str, Any] = dict(record.row)
    out["record_id"] = record.record_id
    out["t_mono_ns"] = record.t_mono_ns
    out["t_utc"] = record.t_utc
    return out


# ---------------------------------------------------------------------------
# Per-adapter writer
# ---------------------------------------------------------------------------


class _PerFamilyWriter:
    """One in-flight Arrow IPC writer per adapter family.

    Buffers rows in Python until the first flush (so the schema can be
    inferred from a representative batch). After that, the schema is locked
    and every subsequent row is cast against it.
    """

    __slots__ = (
        "_buf",
        "_closed",
        "_flush_rows",
        "_layout",
        "_path",
        "_schema",
        "_writer",
    )

    def __init__(
        self,
        path: Path,
        layout: RecordShape,
        *,
        flush_rows: int = INFLIGHT_FLUSH_ROWS,
    ) -> None:
        self._path = path
        self._layout = layout
        self._buf: list[dict[str, Any]] = []
        self._flush_rows = flush_rows
        self._closed = False
        self._schema: pa.Schema | None = None
        self._writer: IpcStreamSink | None = None

    @property
    def path(self) -> Path:
        """Path to this writer's in-flight Arrow IPC stream."""
        return self._path

    @property
    def layout(self) -> RecordShape:
        """The :class:`RecordShape` this writer locked on first flush."""
        return self._layout

    @property
    def has_data(self) -> bool:
        """``True`` if the writer has either flushed rows or buffered rows pending flush."""
        return self._writer is not None or bool(self._buf)

    def write(self, record: SourceRecord) -> None:
        """Append a :class:`SourceRecord` to the buffer; auto-flush at ``flush_rows``.

        Raises:
            DeviceRecordsSinkError: ``write`` was called after :meth:`close`.
        """
        if self._closed:
            raise DeviceRecordsSinkError("write() after close()")
        self._buf.append(_record_to_row(record))
        if len(self._buf) >= self._flush_rows:
            self.flush()

    def flush(self) -> None:
        """Materialize the buffer to the Arrow IPC stream; no-op when empty.

        Raises:
            DeviceRecordsSinkError: ``flush`` was called after :meth:`close`.
        """
        if self._closed:
            raise DeviceRecordsSinkError("flush() after close()")
        if not self._buf:
            return
        # First flush establishes the schema; the writer is opened against
        # that schema.
        if self._schema is None:
            inferred = _infer_schema(self._buf)
            # Canonicalize header column placement: capa header first, then
            # library row keys in the order they appear in the inferred schema.
            header = [f for f in _HEADER_FIELDS]
            header_names = {f.name for f in header}
            tail = [
                inferred.field(i)
                for i in range(len(inferred))
                if inferred.field(i).name not in header_names
            ]
            # Header types from inference may have been set by their literal
            # values. Force the canonical types from _HEADER_FIELDS to keep
            # them stable across adapters / runs.
            self._schema = pa.schema(header + tail)
            self._writer = IpcStreamSink(self._path, schema=self._schema)
        assert self._schema is not None
        assert self._writer is not None
        table = self._build_table(self._buf, self._schema)
        self._writer.write_table(table)
        self._buf.clear()

    def close(self) -> None:
        """Flush any pending rows and close the writer. Idempotent."""
        if self._closed:
            return
        try:
            if self._buf:
                self.flush()
        finally:
            self._closed = True
            if self._writer is not None:
                self._writer.close()
                self._writer = None

    # ------------------------------------------------------------------ helpers

    def _build_table(self, rows: list[dict[str, Any]], schema: pa.Schema) -> pa.Table:
        """Build a Table whose columns match ``schema``, casting each value.

        Missing keys → null. Extra keys (i.e. drift; column appeared after
        schema lock) → :class:`SchemaDriftError`.
        """
        column_names = set(schema.names)
        for row in rows:
            extras = set(row.keys()) - column_names
            if extras:
                raise SchemaDriftError(
                    f"{self._path.name}: row introduced new columns after schema "
                    f"lock: {sorted(extras)}"
                )
        columns: dict[str, list[Any]] = {name: [] for name in schema.names}
        type_tags: dict[str, str] = {
            name: _arrow_to_tag(schema.field(name).type) for name in schema.names
        }
        for row in rows:
            for name in schema.names:
                value = row.get(name)
                if value is None:
                    columns[name].append(None)
                    continue
                try:
                    columns[name].append(_coerce(value, type_tags[name]))
                except (TypeError, ValueError) as exc:
                    raise SchemaDriftError(
                        f"{self._path.name}: column {name!r} value {value!r} cannot "
                        f"be cast to locked type {schema.field(name).type}"
                    ) from exc
        return pa.table(
            {name: pa.array(columns[name], type=schema.field(name).type) for name in schema.names},
            schema=schema,
        )


def _arrow_to_tag(t: pa.DataType) -> str:
    """Inverse of :func:`_tag_to_arrow` — used by :meth:`_PerFamilyWriter._build_table`."""
    if pa.types.is_boolean(t):
        return "bool"
    if pa.types.is_integer(t):
        return "int"
    if pa.types.is_floating(t):
        return "float"
    if pa.types.is_string(t) or pa.types.is_large_string(t):
        return "str"
    if pa.types.is_binary(t):
        return "bytes"
    if pa.types.is_timestamp(t):
        return "datetime"
    return "str"


# ---------------------------------------------------------------------------
# DeviceRecordsSink — multiplexes by adapter family
# ---------------------------------------------------------------------------


class DeviceRecordsSink:
    """Multiplexing sink for :class:`SourceRecord`\\ s.

    Routes each record by ``adapter`` to a per-family Parquet writer under
    ``<bundle_root>/device_records/<adapter>.in-flight.arrows``.
    ``shape="block"`` records are skipped (block sidecar landing is deferred
    to TDMS; see ) but counted via :attr:`skipped_blocks`.
    """

    __slots__ = ("_closed", "_dirpath", "_flush_rows", "_skipped_blocks", "_writers")

    def __init__(
        self,
        bundle_root: Path,
        *,
        flush_rows: int = INFLIGHT_FLUSH_ROWS,
    ) -> None:
        self._dirpath = Path(bundle_root) / DEVICE_RECORDS_DIRNAME
        self._dirpath.mkdir(parents=True, exist_ok=True)
        self._writers: dict[str, _PerFamilyWriter] = {}
        self._flush_rows = flush_rows
        self._skipped_blocks: dict[str, int] = {}
        self._closed = False

    @property
    def directory(self) -> Path:
        """Path to ``<bundle_root>/device_records/`` — where per-adapter sidecars land."""
        return self._dirpath

    @property
    def adapters(self) -> tuple[str, ...]:
        """Adapters that have actually written rows. Used by the manifest's
        ``data_shape.device_records`` block."""
        return tuple(sorted(name for name, w in self._writers.items() if w.has_data))

    def layout_for(self, adapter: str) -> RecordShape:
        """Layout tag for a given adapter — :class:`SourceRecord.shape`."""
        return self._writers[adapter].layout

    @property
    def skipped_blocks(self) -> dict[str, int]:
        """Per-adapter count of ``shape="block"`` records this sink declined
        to write (because block sidecars land via TDMS, not here)."""
        return dict(self._skipped_blocks)

    def write(self, record: SourceRecord) -> None:
        """Route ``record`` to its per-family writer.

        Records with ``shape="block"`` are counted but not written — block
        sidecars are landed via TDMS, not through this sink.

        Raises:
            DeviceRecordsSinkError: The sink has already been closed.
            SchemaDriftError: An adapter changed its record shape mid-run.
        """
        if self._closed:
            raise DeviceRecordsSinkError("write() after close()")
        if record.shape == "block":
            self._skipped_blocks[record.adapter] = self._skipped_blocks.get(record.adapter, 0) + 1
            return
        writer = self._writers.get(record.adapter)
        if writer is None:
            writer = _PerFamilyWriter(
                self._dirpath / f"{record.adapter}{INFLIGHT_SUFFIX}",
                layout=record.shape,
                flush_rows=self._flush_rows,
            )
            self._writers[record.adapter] = writer
        elif writer.layout != record.shape:
            raise SchemaDriftError(
                f"adapter {record.adapter!r}: shape changed mid-run "
                f"({writer.layout!r} -> {record.shape!r})"
            )
        writer.write(record)

    def flush(self) -> None:
        """Flush every per-family writer.

        Raises:
            DeviceRecordsSinkError: The sink has already been closed.
        """
        if self._closed:
            raise DeviceRecordsSinkError("flush() after close()")
        for writer in self._writers.values():
            writer.flush()

    def close(self) -> None:
        """Close every per-family writer. Idempotent."""
        if self._closed:
            return
        try:
            for writer in self._writers.values():
                writer.close()
        finally:
            self._closed = True

    def __enter__(self) -> DeviceRecordsSink:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = [
    "DEVICE_RECORDS_DIRNAME",
    "FINAL_SUFFIX",
    "INFLIGHT_SUFFIX",
    "DeviceRecordsSink",
    "DeviceRecordsSinkError",
    "SchemaDriftError",
]
