"""Per-camera frame-index sink.

Plan §12.3 / §12.5 / P4 Stage C. One sink instance per camera; writes
``video/<camera_name>.frames.in-flight.arrows`` (Arrow IPC stream — see
``arrow-ipc-streaming-plan.md``) while the run is active. The finalize stage
rewrites it to ``video/<camera_name>.frames.parquet`` with the same
large-row-group + sort-by-``t_mono_ns`` treatment the ``scalars.parquet``
rewrite uses.

Schema (locked at v1):

==================== ===================================== =============================
Column               Type                                  Notes
==================== ===================================== =============================
``frame_idx``        ``int64``                             Camera-assigned monotonic id.
``t_mono_ns``        ``int64``                             RunClock-derived.
``t_utc``            ``timestamp[us, tz=UTC]``             Wall-clock anchor.
``capture_latency_s``  ``float64``                         SDK→Python hand-off latency.
``camera``           dict<string>                          Stable camera name.
==================== ===================================== =============================

Camera output containers (``.mkv``, ``.csq``) are *not* managed by this sink
— each adapter owns its own container handle. The sink only owns the
frame-index parquet that lets analyzers correlate frame ids back to channel
samples without re-parsing the container.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pyarrow as pa

from capa.core.errors import CapaError
from capa.devices.camera.base import FrameReceipt
from capa.storage._ipc import DEFAULT_FLUSH_ROWS_FRAMES, IpcStreamSink

VIDEO_DIRNAME = "video"
"""Bundle-relative directory for camera artifacts (plan §8 layout)."""

INFLIGHT_SUFFIX = ".frames.in-flight.arrows"
FINAL_SUFFIX = ".frames.parquet"

INFLIGHT_FLUSH_ROWS = DEFAULT_FLUSH_ROWS_FRAMES
"""Frame-index rows are tiny — flush at 256 (about 8 s of 30-Hz video).
Module-level so tests can monkey-patch downward."""


def _arrow_schema() -> pa.Schema:
    return pa.schema(
        [
            pa.field("frame_idx", pa.int64(), nullable=False),
            pa.field("t_mono_ns", pa.int64(), nullable=False),
            pa.field("t_utc", pa.timestamp("us", tz="UTC"), nullable=False),
            pa.field("capture_latency_s", pa.float64(), nullable=False),
            pa.field("camera", pa.dictionary(pa.int32(), pa.string()), nullable=False),
        ]
    )


FRAMES_SCHEMA: pa.Schema = _arrow_schema()


def in_flight_path(bundle_root: Path, camera_name: str) -> Path:
    return Path(bundle_root) / VIDEO_DIRNAME / f"{camera_name}{INFLIGHT_SUFFIX}"


def final_path(bundle_root: Path, camera_name: str) -> Path:
    return Path(bundle_root) / VIDEO_DIRNAME / f"{camera_name}{FINAL_SUFFIX}"


class VideoSinkError(CapaError):
    """Raised on writer state errors."""


@dataclass(slots=True)
class _Buffer:
    frame_idx: list[int] = field(default_factory=list)
    t_mono_ns: list[int] = field(default_factory=list)
    t_utc: list[object] = field(default_factory=list)
    capture_latency_s: list[float] = field(default_factory=list)
    camera: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.frame_idx)

    def clear(self) -> None:
        self.frame_idx.clear()
        self.t_mono_ns.clear()
        self.t_utc.clear()
        self.capture_latency_s.clear()
        self.camera.clear()

    def to_table(self, schema: pa.Schema) -> pa.Table:
        return pa.table(
            {
                "frame_idx": pa.array(self.frame_idx, type=pa.int64()),
                "t_mono_ns": pa.array(self.t_mono_ns, type=pa.int64()),
                "t_utc": pa.array(self.t_utc, type=pa.timestamp("us", tz="UTC")),
                "capture_latency_s": pa.array(self.capture_latency_s, type=pa.float64()),
                "camera": pa.array(self.camera, type=pa.string()).dictionary_encode(),
            },
            schema=schema,
        )


class FramesSink:
    """Per-camera frame-index writer.

    Lifecycle: construct → ``write(receipt)`` 1..N → ``close``. The bundle
    writer maintains a dict ``{camera_name: FramesSink}`` and routes
    :class:`~capa.devices.camera.base.FrameReceipt` instances by ``name``.
    """

    __slots__ = ("_buf", "_camera", "_closed", "_flush_rows", "_path", "_writer")

    def __init__(
        self,
        bundle_root: Path,
        *,
        camera: str,
        flush_rows: int = INFLIGHT_FLUSH_ROWS,
    ) -> None:
        self._camera = camera
        self._path = in_flight_path(bundle_root, camera)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._buf = _Buffer()
        self._flush_rows = flush_rows
        self._closed = False
        self._writer = IpcStreamSink(self._path, schema=FRAMES_SCHEMA)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def camera(self) -> str:
        return self._camera

    def write(self, receipt: FrameReceipt) -> None:
        if self._closed:
            raise VideoSinkError("write() after close()")
        if receipt.name != self._camera:
            raise VideoSinkError(
                f"FramesSink for {self._camera!r} received receipt for {receipt.name!r}"
            )
        self._buf.frame_idx.append(receipt.frame_idx)
        self._buf.t_mono_ns.append(receipt.t_mono_ns)
        self._buf.t_utc.append(receipt.t_utc)
        self._buf.capture_latency_s.append(receipt.capture_latency_s)
        self._buf.camera.append(receipt.name)
        if len(self._buf) >= self._flush_rows:
            self.flush()

    def flush(self) -> None:
        if self._closed:
            raise VideoSinkError("flush() after close()")
        if not self._buf:
            return
        self._writer.write_table(self._buf.to_table(FRAMES_SCHEMA))
        self._buf.clear()

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self._buf:
                self.flush()
        finally:
            self._closed = True
            self._writer.close()

    def __enter__(self) -> FramesSink:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = [
    "FINAL_SUFFIX",
    "FRAMES_SCHEMA",
    "INFLIGHT_FLUSH_ROWS",
    "INFLIGHT_SUFFIX",
    "VIDEO_DIRNAME",
    "FramesSink",
    "VideoSinkError",
    "final_path",
    "in_flight_path",
]
