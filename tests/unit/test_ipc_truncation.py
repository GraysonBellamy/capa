"""Property-style tests for :mod:`capa.storage._ipc`.

The big one is :func:`test_truncation_at_every_byte_offset` — it writes a
known stream, then for every byte-offset truncates a copy and asserts
:func:`read_recoverable` either returns ``None`` or a clean prefix. Catches
edges in pyarrow's stream reader more thoroughly than a single SIGKILL.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from capa.storage._ipc import (
    IpcStreamSink,
    IpcStreamSinkError,
    read_recoverable,
)

_SCHEMA = pa.schema([pa.field("value", pa.int64(), nullable=False)])


def _one_row_table(value: int) -> pa.Table:
    return pa.table({"value": pa.array([value], type=pa.int64())}, schema=_SCHEMA)


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "good.arrows"
    sink = IpcStreamSink(path)
    for i in range(5):
        sink.write_table(_one_row_table(i))
    sink.close()

    result = read_recoverable(path)
    assert result is not None
    assert result.column("value").to_pylist() == [0, 1, 2, 3, 4]


def test_lazy_open_no_writes(tmp_path: Path) -> None:
    """Constructing a sink without writing must not create the file."""
    path = tmp_path / "absent.arrows"
    sink = IpcStreamSink(path)
    assert not path.exists()
    assert not sink.has_data
    sink.close()
    assert not path.exists()


def test_close_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "idem.arrows"
    sink = IpcStreamSink(path)
    sink.write_table(_one_row_table(7))
    sink.close()
    sink.close()  # must not raise


def test_write_after_close_raises(tmp_path: Path) -> None:
    path = tmp_path / "after_close.arrows"
    sink = IpcStreamSink(path)
    sink.write_table(_one_row_table(1))
    sink.close()
    with pytest.raises(IpcStreamSinkError, match="after close"):
        sink.write_table(_one_row_table(2))


def test_schema_drift_mid_stream_raises(tmp_path: Path) -> None:
    """Once a schema is locked, a different schema on a later write fails."""
    path = tmp_path / "drift.arrows"
    sink = IpcStreamSink(path)
    sink.write_table(_one_row_table(0))
    other = pa.table({"value": pa.array([1.5], type=pa.float64())})
    with pytest.raises(IpcStreamSinkError, match="drift"):
        sink.write_table(other)
    sink.close()


def test_explicit_schema_mismatch_at_first_write_raises(tmp_path: Path) -> None:
    """If a sink is constructed with an explicit schema and the first write
    differs, fail before opening the file."""
    path = tmp_path / "first-write-drift.arrows"
    declared = pa.schema([pa.field("value", pa.float64())])
    sink = IpcStreamSink(path, schema=declared)
    with pytest.raises(IpcStreamSinkError, match="drift"):
        sink.write_table(_one_row_table(0))
    assert not path.exists()  # never opened the file
    sink.close()


def test_read_recoverable_missing_file(tmp_path: Path) -> None:
    assert read_recoverable(tmp_path / "missing.arrows") is None


def test_read_recoverable_empty_file(tmp_path: Path) -> None:
    """A zero-byte file (sink opened but flushed nothing) → None."""
    path = tmp_path / "empty.arrows"
    path.write_bytes(b"")
    assert read_recoverable(path) is None


def test_truncation_at_every_byte_offset(tmp_path: Path) -> None:
    """For every byte offset in a known-good stream, truncate a copy and
    confirm :func:`read_recoverable` returns either ``None`` or a clean
    prefix of the original rows. Never raises."""
    src = tmp_path / "good.arrows"
    sink = IpcStreamSink(src)
    n = 10
    for i in range(n):
        sink.write_table(_one_row_table(i))
    sink.close()
    full = src.read_bytes()
    assert len(full) > 0

    seen_prefix_lengths: set[int] = set()
    saw_none = False
    for k in range(len(full) + 1):
        torn = tmp_path / f"torn_{k:04d}.arrows"
        torn.write_bytes(full[:k])
        result = read_recoverable(torn)
        if result is None:
            saw_none = True
            continue
        rows = result.column("value").to_pylist()
        # Whatever rows are present must be a strict prefix of [0..n).
        assert rows == list(range(len(rows))), f"k={k}: expected prefix of [0..{n}), got {rows}"
        seen_prefix_lengths.add(len(rows))

    # The healthy file must always come back fully — sanity check on n.
    assert n in seen_prefix_lengths, "full file should round-trip without loss"
    # Truncations near the start should produce None at least once.
    assert saw_none, "no schema-torn cases observed — test is too small?"


def test_recoverable_after_partial_writes(tmp_path: Path) -> None:
    """Simulate a crash by truncating mid-batch and confirm the prefix that
    *was* fsync'd is still readable. (Different from the property test in
    that we test the public API surface explicitly.)"""
    path = tmp_path / "partial.arrows"
    sink = IpcStreamSink(path)
    sink.write_table(_one_row_table(0))
    sink.write_table(_one_row_table(1))
    sink.write_table(_one_row_table(2))
    # Don't close — copy the bytes mid-stream and truncate the tail.
    raw = path.read_bytes()
    sink.close()
    torn = tmp_path / "torn.arrows"
    torn.write_bytes(raw[: len(raw) - 5])
    result = read_recoverable(torn)
    assert result is not None
    rows = result.column("value").to_pylist()
    assert rows == [0, 1]  # last batch's tail was lopped off
