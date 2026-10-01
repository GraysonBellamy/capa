"""``device_records/fuji.parquet`` for a run that begins during an outage.

A failed poll's row has every column of a good one, with the readings empty.
:class:`DeviceRecordsSink` takes each column's type from the rows of its first
flush (1,024 rows, or the whole of a shorter run), and types a column that is
empty in all of them as text. These tests pin what that means for the
analyzer's records: an outage at the start costs nothing while one good poll
is in the first flush, and when none is, the readings that follow are kept as
text rather than refused.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.ipc as pa_ipc
from fujilib import FujiConnectionError, Sample, sample_to_row

from capa.devices.fuji import ADAPTER_ID
from capa.devices.records import SourceRecord
from capa.storage.device_records_sink import INFLIGHT_SUFFIX, DeviceRecordsSink
from tests.unit.test_fuji_adapter import _error_sample, _sample


def _record(seq: int, sample: Sample) -> SourceRecord:
    return SourceRecord(
        record_id=f"{ADAPTER_ID}:analyzer:{seq}",
        adapter=ADAPTER_ID,
        device="analyzer",
        shape="wide_row",
        t_mono_ns=seq * 1_000_000_000,
        t_utc=sample.t_utc,
        row=sample_to_row(sample),
        metadata={"address": sample.address},
    )


def _outage() -> Sample:
    return _error_sample(FujiConnectionError("the connection to COM8 failed"))


def _write(tmp_path: Path, samples: list[Sample], *, flush_rows: int) -> pa.Table:
    sink = DeviceRecordsSink(tmp_path, flush_rows=flush_rows)
    for seq, sample in enumerate(samples, start=1):
        sink.write(_record(seq, sample))
    sink.close()
    path = sink.directory / f"{ADAPTER_ID}{INFLIGHT_SUFFIX}"
    with pa_ipc.open_stream(path) as reader:
        return reader.read_all()


def test_error_rows_first_keep_typed_columns_once_a_good_poll_is_in_the_first_flush(
    tmp_path: Path,
) -> None:
    table = _write(tmp_path, [_outage(), _outage(), _sample(), _sample()], flush_rows=1024)
    schema = table.schema
    assert schema.field("ch3_value").type == pa.float64()
    assert schema.field("ch3_valid").type == pa.bool_()
    assert schema.field("ch3_raw").type == pa.int64()
    assert schema.field("ch3_state").type == pa.string()
    rows = table.to_pylist()
    assert [row["ch3_value"] is None for row in rows] == [True, True, False, False]
    assert rows[0]["error_type"] == "fujilib.errors.FujiConnectionError"
    assert rows[2]["error_type"] is None
    assert isinstance(rows[2]["ch3_value"], float)


def test_a_first_flush_of_error_rows_only_keeps_later_readings_as_text(tmp_path: Path) -> None:
    good = _sample()
    table = _write(tmp_path, [_outage(), _outage(), good], flush_rows=2)
    # Nothing is refused and nothing is lost, but the columns that were empty
    # throughout the first flush are text from then on.
    assert table.schema.field("ch3_value").type == pa.string()
    rows = table.to_pylist()
    assert len(rows) == 3
    assert rows[0]["ch3_value"] is None
    assert float(rows[2]["ch3_value"]) == sample_to_row(good)["ch3_value"]
    assert rows[2]["ch3_state"] == "ok"
