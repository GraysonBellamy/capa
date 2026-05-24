"""Normalized channel-samples sink — ``scalars.in-flight.arrows``.

Streams :class:`~capa.devices.records.ChannelSample` rows
to an in-flight Arrow IPC stream (one record batch per flush, fsync between)
for crash safety — see ``arrow-ipc-streaming-plan.md``. The finalize stage
rewrites the IPC stream into a large-row-group Parquet sorted by
``t_mono_ns``.

Schema (locked at v1; bumping requires a ``bundle_schema_version`` bump and
a migration registered in :mod:`capa.storage.schema`):

==================== ==================================== =====================
Column               Type                                  Notes
==================== ==================================== =====================
``t_mono_ns``        ``int64``                             Canonical join key.
``t_mono_s``         ``float64``                           Derived; redundant.
``channel``          dict<string>                          Encoded as Arrow
                                                          dictionary for size.
``value``            ``float64``                           Always populated.
``value_kind``       dict<string>                          ``"float"|"int"|"bool"``
                                                          to round-trip type.
``raw_value``        ``float64`` nullable                  Numeric raw.
``raw_text``         string nullable                       String raw.
``raw_kind``         dict<string> nullable                 ``"float"|"int"|"bool"|"str"``
                                                          or null when raw is None.
``unit``             dict<string>                          Output unit.
``uncertainty``      ``float64`` nullable
``status``           dict<string>                          ``"ok"`` and friends.
``source_record_id`` string nullable                       Back-pointer.
``source_field``     string nullable
==================== ==================================== =====================

``value`` is float64 even for bool/int channels (cast back via ``value_kind``).
This keeps the column rectangular for fast cross-channel queries while
preserving fidelity for the rare bool/int channel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pyarrow as pa

from capa.core.errors import CapaError
from capa.devices.records import ChannelSample
from capa.storage._ipc import DEFAULT_FLUSH_ROWS_BULK, IpcStreamSink

INFLIGHT_FILENAME = "scalars.in-flight.arrows"
FINAL_FILENAME = "scalars.parquet"

INFLIGHT_FLUSH_ROWS = DEFAULT_FLUSH_ROWS_BULK
"""Number of buffered rows that triggers an automatic flush. Module-level
so tests can monkey-patch downward to exercise the path on synthetic runs
that emit a few rows."""


def _arrow_schema() -> pa.Schema:
    """Locked schema for ``scalars.parquet``. Constructed once and reused."""
    return pa.schema(
        [
            pa.field("t_mono_ns", pa.int64(), nullable=False),
            pa.field("t_mono_s", pa.float64(), nullable=False),
            pa.field("channel", pa.dictionary(pa.int32(), pa.string()), nullable=False),
            pa.field("value", pa.float64(), nullable=False),
            pa.field(
                "value_kind",
                pa.dictionary(pa.int32(), pa.string()),
                nullable=False,
            ),
            pa.field("raw_value", pa.float64(), nullable=True),
            pa.field("raw_text", pa.string(), nullable=True),
            pa.field("raw_kind", pa.dictionary(pa.int32(), pa.string()), nullable=True),
            pa.field("unit", pa.dictionary(pa.int32(), pa.string()), nullable=False),
            pa.field("uncertainty", pa.float64(), nullable=True),
            pa.field("status", pa.dictionary(pa.int32(), pa.string()), nullable=False),
            pa.field("source_record_id", pa.string(), nullable=True),
            pa.field("source_field", pa.string(), nullable=True),
        ]
    )


CHANNEL_SAMPLES_SCHEMA: pa.Schema = _arrow_schema()


def _value_kind(value: float | int | bool) -> str:
    # bool subclasses int — check it first.
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    return "float"


def _split_raw(
    raw: float | int | bool | str | None,
) -> tuple[float | None, str | None, str | None]:
    """Translate a polymorphic ``raw`` into three storage columns.

    Returns ``(raw_value, raw_text, raw_kind)``. Exactly the columns whose
    type matches will be populated.
    """
    if raw is None:
        return None, None, None
    if isinstance(raw, bool):
        return float(raw), None, "bool"
    if isinstance(raw, int):
        return float(raw), None, "int"
    if isinstance(raw, float):
        return raw, None, "float"
    # string fallthrough
    return None, raw, "str"


# ---------------------------------------------------------------------------
# ChannelSamplesSink
# ---------------------------------------------------------------------------


class ChannelSamplesSinkError(CapaError):
    """Raised on writer state errors (write after close, etc.)."""


@dataclass(slots=True)
class _Buffer:
    """Per-column staging arrays. Flushed to a row group via ``write_table``."""

    t_mono_ns: list[int] = field(default_factory=list)
    t_mono_s: list[float] = field(default_factory=list)
    channel: list[str] = field(default_factory=list)
    value: list[float] = field(default_factory=list)
    value_kind: list[str] = field(default_factory=list)
    raw_value: list[float | None] = field(default_factory=list)
    raw_text: list[str | None] = field(default_factory=list)
    raw_kind: list[str | None] = field(default_factory=list)
    unit: list[str] = field(default_factory=list)
    uncertainty: list[float | None] = field(default_factory=list)
    status: list[str] = field(default_factory=list)
    source_record_id: list[str | None] = field(default_factory=list)
    source_field: list[str | None] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.t_mono_ns)

    def clear(self) -> None:
        self.t_mono_ns.clear()
        self.t_mono_s.clear()
        self.channel.clear()
        self.value.clear()
        self.value_kind.clear()
        self.raw_value.clear()
        self.raw_text.clear()
        self.raw_kind.clear()
        self.unit.clear()
        self.uncertainty.clear()
        self.status.clear()
        self.source_record_id.clear()
        self.source_field.clear()

    def to_table(self, schema: pa.Schema) -> pa.Table:
        return pa.table(
            {
                "t_mono_ns": pa.array(self.t_mono_ns, type=pa.int64()),
                "t_mono_s": pa.array(self.t_mono_s, type=pa.float64()),
                "channel": pa.array(self.channel, type=pa.string()).dictionary_encode(),
                "value": pa.array(self.value, type=pa.float64()),
                "value_kind": pa.array(self.value_kind, type=pa.string()).dictionary_encode(),
                "raw_value": pa.array(self.raw_value, type=pa.float64()),
                "raw_text": pa.array(self.raw_text, type=pa.string()),
                "raw_kind": pa.array(self.raw_kind, type=pa.string()).dictionary_encode(),
                "unit": pa.array(self.unit, type=pa.string()).dictionary_encode(),
                "uncertainty": pa.array(self.uncertainty, type=pa.float64()),
                "status": pa.array(self.status, type=pa.string()).dictionary_encode(),
                "source_record_id": pa.array(self.source_record_id, type=pa.string()),
                "source_field": pa.array(self.source_field, type=pa.string()),
            },
            schema=schema,
        )


class ChannelSamplesSink:
    """Streaming writer for ``scalars.in-flight.arrows``.

    Lifecycle: construct → ``write`` 1..N → ``close``. ``close`` flushes any
    buffered rows and closes the underlying IPC stream. Idempotent on repeated
    close. Not thread-safe; the bundle writer drives a single producer.
    """

    __slots__ = (
        "_buf",
        "_closed",
        "_flush_rows",
        "_path",
        "_writer",
    )

    def __init__(
        self,
        bundle_root: Path,
        *,
        flush_rows: int = INFLIGHT_FLUSH_ROWS,
    ) -> None:
        self._path = Path(bundle_root) / INFLIGHT_FILENAME
        self._buf = _Buffer()
        self._flush_rows = flush_rows
        self._closed = False
        # IpcStreamSink lazy-opens on first write; fine here because the
        # schema is locked at module load.
        self._writer = IpcStreamSink(self._path, schema=CHANNEL_SAMPLES_SCHEMA)

    @property
    def path(self) -> Path:
        """Absolute path of the in-flight file."""
        return self._path

    def write(self, sample: ChannelSample) -> None:
        """Append one sample to the buffer; auto-flush if full."""
        if self._closed:
            raise ChannelSamplesSinkError("write() after close()")
        raw_value, raw_text, raw_kind = _split_raw(sample.raw)
        self._buf.t_mono_ns.append(sample.t_mono_ns)
        self._buf.t_mono_s.append(sample.t_mono_s)
        self._buf.channel.append(sample.channel)
        self._buf.value.append(float(sample.value))
        self._buf.value_kind.append(_value_kind(sample.value))
        self._buf.raw_value.append(raw_value)
        self._buf.raw_text.append(raw_text)
        self._buf.raw_kind.append(raw_kind)
        self._buf.unit.append(sample.unit)
        self._buf.uncertainty.append(sample.uncertainty)
        self._buf.status.append(sample.status)
        self._buf.source_record_id.append(sample.source_record_id)
        self._buf.source_field.append(sample.source_field)
        if len(self._buf) >= self._flush_rows:
            self.flush()

    def flush(self) -> None:
        """Emit a record batch with whatever's buffered.

        No-op when the buffer is empty. The underlying :class:`IpcStreamSink`
        fsyncs after every write so a power-loss after this call leaves the
        last batch on disk.
        """
        if self._closed:
            raise ChannelSamplesSinkError("flush() after close()")
        if not self._buf:
            return
        table = self._buf.to_table(CHANNEL_SAMPLES_SCHEMA)
        self._writer.write_table(table)
        self._buf.clear()

    def close(self) -> None:
        """Flush any remaining rows and close the IPC stream.

        Idempotent. Safe to call from a ``finally`` block.
        """
        if self._closed:
            return
        try:
            if self._buf:
                self.flush()
        finally:
            self._closed = True
            self._writer.close()

    def __enter__(self) -> ChannelSamplesSink:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = [
    "CHANNEL_SAMPLES_SCHEMA",
    "FINAL_FILENAME",
    "INFLIGHT_FILENAME",
    "INFLIGHT_FLUSH_ROWS",
    "ChannelSamplesSink",
    "ChannelSamplesSinkError",
]
