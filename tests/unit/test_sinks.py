"""Per-sink unit tests. Integration tests live under ``tests/integration``."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from capa.devices.records import (
    ChannelSample,
    DeviceEvent,
    DeviceSnapshot,
    SourceRecord,
)
from capa.storage._ipc import read_recoverable
from capa.storage.channel_samples_sink import (
    CHANNEL_SAMPLES_SCHEMA,
    INFLIGHT_FILENAME,
    ChannelSamplesSink,
    ChannelSamplesSinkError,
)
from capa.storage.device_records_sink import (
    DEVICE_RECORDS_DIRNAME,
    INFLIGHT_SUFFIX,
    DeviceRecordsSink,
    SchemaDriftError,
)
from capa.storage.events_sink import EventsSink, EventsSinkError
from capa.storage.log_sink import LogSink
from capa.storage.status_sink import StatusSink

WALL = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)


def _sample(
    channel: str = "heater.pv",
    *,
    t_mono_ns: int = 0,
    value: float | int | bool = 1.0,
    raw: float | int | bool | str | None = None,
    unit: str = "degC",
    status: str = "ok",
) -> ChannelSample:
    return ChannelSample(
        channel=channel,
        t_mono_ns=t_mono_ns,
        t_mono_s=t_mono_ns / 1e9,
        value=value,
        raw=raw,
        unit=unit,
        status=status,
    )


class TestChannelSamplesSink:
    def test_write_and_read_back(self, tmp_path: Path) -> None:
        sink = ChannelSamplesSink(tmp_path, flush_rows=4)
        for i in range(10):
            sink.write(_sample(t_mono_ns=i * 1_000_000, value=float(i)))
        sink.close()
        # Idempotent close
        sink.close()

        path = tmp_path / INFLIGHT_FILENAME
        assert path.is_file()
        table = read_recoverable(path)
        assert table is not None
        assert table.num_rows == 10
        assert table.schema.equals(CHANNEL_SAMPLES_SCHEMA, check_metadata=False)
        assert table.column("t_mono_ns").to_pylist() == [i * 1_000_000 for i in range(10)]
        assert table.column("value").to_pylist() == [float(i) for i in range(10)]
        assert all(k == "float" for k in table.column("value_kind").to_pylist())

    def test_value_kind_round_trips_bool_int_float(self, tmp_path: Path) -> None:
        sink = ChannelSamplesSink(tmp_path)
        sink.write(_sample(value=True))
        sink.write(_sample(value=42))
        sink.write(_sample(value=3.14))
        sink.close()

        table = read_recoverable(tmp_path / INFLIGHT_FILENAME)
        assert table is not None
        kinds = table.column("value_kind").to_pylist()
        # bool comes first in our isinstance order
        assert kinds == ["bool", "int", "float"]

    def test_raw_columns_split_correctly(self, tmp_path: Path) -> None:
        sink = ChannelSamplesSink(tmp_path)
        sink.write(_sample(raw=None))
        sink.write(_sample(raw=1.5))
        sink.write(_sample(raw="overload"))
        sink.write(_sample(raw=True))
        sink.close()

        table = read_recoverable(tmp_path / INFLIGHT_FILENAME)
        assert table is not None
        rows = table.to_pylist()
        assert rows[0]["raw_kind"] is None
        assert rows[0]["raw_value"] is None
        assert rows[0]["raw_text"] is None
        assert rows[1]["raw_kind"] == "float"
        assert rows[1]["raw_value"] == 1.5
        assert rows[2]["raw_kind"] == "str"
        assert rows[2]["raw_text"] == "overload"
        assert rows[3]["raw_kind"] == "bool"
        assert rows[3]["raw_value"] == 1.0

    def test_write_after_close_raises(self, tmp_path: Path) -> None:
        sink = ChannelSamplesSink(tmp_path)
        sink.close()
        with pytest.raises(ChannelSamplesSinkError):
            sink.write(_sample())


# ---------------------------------------------------------------------------
# DeviceRecordsSink
# ---------------------------------------------------------------------------


def _record(
    *,
    adapter: str = "watlow",
    shape: str = "long_row",
    t_mono_ns: int = 0,
    row: dict[str, object] | None = None,
    metadata: dict[str, object] | None = None,
) -> SourceRecord:
    return SourceRecord(
        record_id=f"{adapter}:dev:{t_mono_ns}",
        adapter=adapter,
        device="dev",
        shape=shape,
        t_mono_ns=t_mono_ns,
        t_utc=WALL,
        row=row or {},
        metadata=metadata or {},
    )


class TestDeviceRecordsSink:
    def test_routes_by_adapter(self, tmp_path: Path) -> None:
        sink = DeviceRecordsSink(tmp_path)
        sink.write(
            _record(
                adapter="watlow",
                row={"parameter": "process_value", "value": 1.0, "instance": 1},
            )
        )
        sink.write(
            _record(
                adapter="alicat",
                shape="wide_row",
                row={"Mass_Flow": 0.5, "Abs_Press": 101.3, "unit_id": "A"},
            )
        )
        sink.close()
        dr = tmp_path / DEVICE_RECORDS_DIRNAME
        assert (dr / f"watlow{INFLIGHT_SUFFIX}").is_file()
        assert (dr / f"alicat{INFLIGHT_SUFFIX}").is_file()

    def test_skips_block_records(self, tmp_path: Path) -> None:
        sink = DeviceRecordsSink(tmp_path)
        sink.write(
            SourceRecord(
                record_id="nidaq_block:dev:0",
                adapter="nidaq_block",
                device="dev",
                shape="block",
                t_mono_ns=0,
                t_utc=WALL,
                row={},
                block_ref="memory:dev:0",
            )
        )
        sink.close()
        # No file written; counter increments.
        assert sink.skipped_blocks == {"nidaq_block": 1}

    def test_schema_lock_rejects_new_columns(self, tmp_path: Path) -> None:
        sink = DeviceRecordsSink(tmp_path, flush_rows=1)
        sink.write(
            _record(
                adapter="watlow",
                row={"value": 1.0, "parameter": "pv"},
            )
        )
        # First write flushed and locked schema.
        with pytest.raises(SchemaDriftError):
            sink.write(
                _record(
                    adapter="watlow",
                    row={"value": 1.0, "parameter": "pv", "extra_col": 9},
                )
            )

    def test_shape_change_rejected(self, tmp_path: Path) -> None:
        sink = DeviceRecordsSink(tmp_path)
        sink.write(_record(adapter="x", shape="long_row", row={"v": 1}))
        with pytest.raises(SchemaDriftError):
            sink.write(_record(adapter="x", shape="wide_row", row={"v": 1}))

    def test_round_trip_columns(self, tmp_path: Path) -> None:
        sink = DeviceRecordsSink(tmp_path, flush_rows=2)
        sink.write(
            _record(
                adapter="watlow",
                t_mono_ns=10,
                row={"parameter": "pv", "value": 1.0, "instance": 1},
            )
        )
        sink.write(
            _record(
                adapter="watlow",
                t_mono_ns=20,
                row={"parameter": "sp", "value": 5.0, "instance": 1},
            )
        )
        sink.close()
        table = read_recoverable(tmp_path / DEVICE_RECORDS_DIRNAME / f"watlow{INFLIGHT_SUFFIX}")
        assert table is not None
        # Header columns are present and rectangular.
        assert "record_id" in table.column_names
        assert "t_mono_ns" in table.column_names
        assert "t_utc" in table.column_names
        assert "parameter" in table.column_names
        assert table.num_rows == 2
        assert table.column("t_mono_ns").to_pylist() == [10, 20]


# ---------------------------------------------------------------------------
# Events / Status
# ---------------------------------------------------------------------------


class TestEventsSink:
    def test_write_and_read_back(self, tmp_path: Path) -> None:
        import sqlite3

        sink = EventsSink(tmp_path)
        sink.write(
            kind="run_start",
            message="started",
            t_mono_ns=10,
            t_utc=WALL,
        )
        sink.write_device_event(
            DeviceEvent(
                adapter="watlow",
                device="heater",
                t_mono_ns=20,
                t_utc=WALL,
                kind="comm_error",
                message="timeout",
                severity="warning",
                metadata={"port": "/dev/ttyUSB0"},
            )
        )
        sink.close()

        conn = sqlite3.connect(tmp_path / "events.sqlite")
        rows = list(conn.execute("SELECT kind, severity, source, message FROM events ORDER BY id;"))
        assert rows[0] == ("run_start", "info", "engine", "started")
        assert rows[1] == ("comm_error", "warning", "watlow:heater", "timeout")
        conn.close()

    def test_severity_validated(self, tmp_path: Path) -> None:
        sink = EventsSink(tmp_path)
        try:
            with pytest.raises(EventsSinkError):
                sink.write(
                    kind="x",
                    message="m",
                    severity="catastrophic",
                    t_mono_ns=0,
                    t_utc=WALL,
                )
        finally:
            sink.close()


class TestStatusSink:
    def test_round_trip(self, tmp_path: Path) -> None:
        import sqlite3

        sink = StatusSink(tmp_path)
        sink.write(
            DeviceSnapshot(
                adapter="watlow",
                device="heater",
                t_mono_ns=10,
                t_utc=WALL,
                healthy=True,
                fields={"address": 1, "state": "running"},
            )
        )
        sink.close()
        conn = sqlite3.connect(tmp_path / "status.sqlite")
        rows = list(conn.execute("SELECT adapter, device, healthy FROM status;"))
        assert rows == [("watlow", "heater", 1)]
        conn.close()


class TestLogSink:
    def test_write_event_appends_jsonl(self, tmp_path: Path) -> None:
        sink = LogSink(tmp_path)
        sink.write_event({"event": "start", "run_id": "x"})
        sink.write_event({"event": "stop", "run_id": "x"})
        sink.close()
        text = (tmp_path / "run.log").read_text(encoding="utf-8")
        assert text.count("\n") == 2
        assert text.startswith('{"event":"start"')

    def test_write_after_close_is_silent_noop(self, tmp_path: Path) -> None:
        # Engine shutdown writes a final "engine.run.end" log line *after*
        # close_sinks() runs. Surfacing that path as an exception only adds
        # noise — the LogSink's contract is "best-effort during shutdown".
        sink = LogSink(tmp_path)
        sink.close()
        sink.write_event({"event": "x"})  # no raise
        sink.write_line('{"event":"y"}')  # no raise
