"""In-process IR-camera sim fixture (plan §12.1, P4 entry).

Writes a small, deterministic "fake-csq" file at the path the engine asks for.
The format is **not** a real FLIR FFF/`.csq` — it is a capa-private layout
with a distinct magic so it cannot be confused with a vendor file:

::

    +0   12 bytes  magic = b"CAPA-IR-SIM\\n"
    +12  4  bytes  little-endian uint32 frame count (filled in at stop)
    +16  4  bytes  little-endian uint32 frame width
    +20  4  bytes  little-endian uint32 frame height
    +24  4  bytes  little-endian uint32 fps
    +28  ...       repeated frame records:
                     uint32 frame_idx
                     int64  t_mono_ns
                     uint32 payload_size
                     bytes  payload (deterministic gradient)

The sim's job is twofold:

1. Prove the engine's camera-task wiring end-to-end without an SDK install:
   files appear in the bundle, ``manifest.json.cameras`` populates,
   ``manifest.sha256`` covers the file, the frame-index parquet round-trips.
2. Give downstream tools a header-only parser target so the
   "post-finalize frame index extractor" path (plan §12.1) is testable from
   capa core without importing ``capa-flir``.

Because the file is capa-private, capa core ships its own
:func:`extract_frame_index` for sim files; ``capa-flir`` ships the Atlas-
backed extractor. The two never overlap (different magic).
"""

from __future__ import annotations

import contextlib
import json
import struct
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from capa.core.clock import RunClock
from capa.core.errors import AdapterError
from capa.devices.camera.base import (
    CameraCapability,
    CameraEvent,
    CameraHealth,
    CameraInfo,
    CameraSpec,
    FrameReceipt,
    make_stream_pair,
)

SIM_MAGIC = b"CAPA-IR-SIM\n"
"""Distinct from real FFF (``b"FFF\\0..."``)."""

HEADER_SIZE = 28
"""Bytes consumed by ``magic + frame_count + width + height + fps``."""

_FRAME_HEADER = struct.Struct("<IqI")
"""Little-endian ``(frame_idx: uint32, t_mono_ns: int64, payload_size: uint32)``."""


class FlirIrSim:
    """Sim camera that mirrors the :class:`~capa.devices.camera.base.Camera`
    Protocol exactly.

    Constructed via :meth:`from_params` so a hardware TOML can declare it the
    same way it declares any other adapter::

        [[hardware.cameras]]
        name = "ir_cam0"
        adapter = "capa.devices.sim.flir_ir_sim"
        kind = "ir"
        params = { fps = 30, width = 384, height = 288, frame_payload_bytes = 1024 }

    The frame pump is driven by an internal AnyIO task that wakes on the
    sim cadence and pushes :class:`FrameReceipt`\\ s onto the frame stream
    while writing payload bytes to disk. This mirrors how the real
    :class:`capa_flir.FlirIrAdapter` pumps Atlas's ``OnImageReceived``
    callbacks (plan §12.1) — only the source of frames differs.
    """

    __slots__ = (
        "_clock",
        "_event_recv",
        "_event_send",
        "_file",
        "_file_size",
        "_fps",
        "_frame_count",
        "_frame_payload_bytes",
        "_frame_recv",
        "_frame_send",
        "_height",
        "_info",
        "_last_frame_t_mono_ns",
        "_meta_path",
        "_open",
        "_output_path",
        "_preview_recv",
        "_preview_send",
        "_recording",
        "_serial",
        "_spec",
        "_started_t_mono_ns",
        "_stream_anchor_mono_ns",
        "_width",
    )

    capabilities: frozenset[CameraCapability] = frozenset(
        {
            CameraCapability.RADIOMETRIC,
            CameraCapability.PALETTE,
            CameraCapability.SUPPORTS_DISCOVERY,
            CameraCapability.MODEL_HINT,
            CameraCapability.SERIAL_SELECT,
        }
    )
    kind: Literal["ir"] = "ir"

    def __init__(
        self,
        spec: CameraSpec,
        *,
        clock: RunClock,
        fps: float = 30.0,
        width: int = 384,
        height: int = 288,
        frame_payload_bytes: int = 1024,
        serial: str = "SIM-IR-0001",
    ) -> None:
        if spec.kind != "ir":
            raise AdapterError(f"FlirIrSim requires CameraSpec.kind == 'ir', got {spec.kind!r}")
        if fps <= 0:
            raise AdapterError(f"FlirIrSim fps must be > 0; got {fps}")
        self._spec = spec
        self._clock = clock
        self._fps = float(fps)
        self._width = int(width)
        self._height = int(height)
        self._frame_payload_bytes = int(frame_payload_bytes)
        self._serial = serial
        self._info = CameraInfo(
            adapter="flir_ir_sim",
            name=spec.name,
            model="FLIR-SIM",
            serial=serial,
            transport="loopback",
            capabilities=tuple(c.name for c in self.capabilities if c.name is not None),
        )
        self._open = False
        self._recording = False
        self._frame_count = 0
        self._file_size = 0
        self._last_frame_t_mono_ns: int | None = None
        self._started_t_mono_ns: int | None = None
        self._stream_anchor_mono_ns: int | None = None
        self._output_path: Path | None = None
        self._meta_path: Path | None = None
        self._file: Any = None  # binary file handle

        self._frame_send: MemoryObjectSendStream[FrameReceipt]
        self._frame_recv: MemoryObjectReceiveStream[FrameReceipt]
        self._frame_send, self._frame_recv = make_stream_pair(64)
        self._preview_send: MemoryObjectSendStream[bytes]
        self._preview_recv: MemoryObjectReceiveStream[bytes]
        self._preview_send, self._preview_recv = make_stream_pair(2)
        self._event_send: MemoryObjectSendStream[CameraEvent]
        self._event_recv: MemoryObjectReceiveStream[CameraEvent]
        self._event_send, self._event_recv = make_stream_pair(16)

    # --------------------------------------------------------------- builders

    @classmethod
    def from_params(
        cls,
        *,
        spec: CameraSpec,
        clock: RunClock,
        fps: float = 30.0,
        width: int = 384,
        height: int = 288,
        frame_payload_bytes: int = 1024,
        serial: str = "SIM-IR-0001",
    ) -> FlirIrSim:
        """TOML-friendly constructor (plan §16 P3 ``from_params`` convention)."""
        return cls(
            spec=spec,
            clock=clock,
            fps=fps,
            width=width,
            height=height,
            frame_payload_bytes=frame_payload_bytes,
            serial=serial,
        )

    # -------------------------------------------------------------- properties

    @property
    def spec(self) -> CameraSpec:
        return self._spec

    @property
    def info(self) -> CameraInfo:
        return self._info

    # ----------------------------------------------------------- protocol API

    async def discover(self) -> tuple[CameraInfo, ...]:
        return (self._info,)

    async def open(self) -> CameraInfo:
        if self._open:
            return self._info
        self._select_camera()
        self._open = True
        return self._info

    async def close(self) -> None:
        if self._recording:
            await self.stop_recording()
        # Close send sides so consumers iterating frame/preview/event streams
        # see EndOfStream and exit cleanly. Receive sides stay alive so
        # generators draining them can still observe the closure.
        await self._frame_send.aclose()
        await self._preview_send.aclose()
        await self._event_send.aclose()
        self._open = False

    async def start_recording(self, output_path: Path) -> None:
        if not self._open:
            raise AdapterError("FlirIrSim.start_recording requires open()")
        if self._recording:
            return
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._output_path = output_path
        self._meta_path = output_path.with_suffix(output_path.suffix + ".meta.json")
        self._file = open(output_path, "wb")  # noqa: SIM115
        self._file.write(self._build_header())
        self._file.flush()
        self._file_size = HEADER_SIZE
        self._frame_count = 0
        self._last_frame_t_mono_ns = None
        self._started_t_mono_ns = self._clock.t_mono_ns()
        self._stream_anchor_mono_ns = self._started_t_mono_ns
        self._write_meta_sidecar()
        self._recording = True
        await self._emit_event(
            kind="recording_started",
            message=f"path={output_path}",
            severity="info",
        )

    async def stop_recording(self) -> None:
        if not self._recording:
            return
        self._recording = False
        try:
            assert self._file is not None
            # Patch in the final frame count at offset +12.
            self._file.flush()
            self._file.seek(12)
            self._file.write(struct.pack("<I", self._frame_count))
            self._file.flush()
            self._file.close()
        finally:
            self._file = None
        self._write_meta_sidecar(final=True)
        await self._emit_event(
            kind="recording_stopped",
            message=f"frames={self._frame_count}",
            severity="info",
        )

    async def snapshot(self) -> CameraHealth:
        return CameraHealth(
            name=self._spec.name,
            t_mono_ns=self._clock.t_mono_ns(),
            t_utc=datetime.now(UTC),
            recording=self._recording,
            frame_count=self._frame_count,
            file_size_bytes=self._file_size,
            last_frame_t_mono_ns=self._last_frame_t_mono_ns,
            healthy=True,
        )

    def frame_stream(self) -> AsyncIterator[FrameReceipt]:
        return _drain_stream(self._frame_recv)

    def preview_stream(self) -> AsyncIterator[bytes]:
        return _drain_stream(self._preview_recv)

    def event_stream(self) -> AsyncIterator[CameraEvent]:
        return _drain_stream(self._event_recv)

    # ----------------------------------------------------------- pump driver

    async def pump_one_frame(self) -> FrameReceipt:
        """Synthesize one frame, write it to disk, push it onto the streams.

        Exposed so tests can drive the sim deterministically without spinning
        up a background pump task. Production-style usage runs
        :meth:`run_pump` inside the engine task group instead.
        """
        if not self._recording or self._file is None:
            raise AdapterError("pump_one_frame requires start_recording()")
        idx = self._frame_count
        anchor = self._stream_anchor_mono_ns or 0
        # Frame timestamps relative to start of recording, evenly spaced.
        t_mono_ns = anchor + int(idx * (1_000_000_000 / self._fps))
        payload = self._frame_payload(idx)
        self._file.write(_FRAME_HEADER.pack(idx, t_mono_ns, len(payload)))
        self._file.write(payload)
        self._file.flush()
        self._file_size += _FRAME_HEADER.size + len(payload)
        self._frame_count = idx + 1
        self._last_frame_t_mono_ns = t_mono_ns
        receipt = FrameReceipt(
            name=self._spec.name,
            frame_idx=idx,
            t_mono_ns=t_mono_ns,
            t_utc=self._clock.to_wall_ns(t_mono_ns),
        )
        await self._frame_send.send(receipt)
        # Best-effort preview drop (DROP_OLDEST semantics, matching plan §7.1).
        with contextlib.suppress(anyio.WouldBlock):
            self._preview_send.send_nowait(payload[: min(64, len(payload))])
        return receipt

    async def run_pump(self) -> None:
        """Run the frame pump until cancellation or :meth:`stop_recording`.

        Wakes at ``1/fps`` intervals, synthesizes one frame, posts it. The
        engine wraps this in its task group so cancellation propagates.
        """
        if not self._recording:
            raise AdapterError("run_pump requires start_recording()")
        period = 1.0 / self._fps
        while self._recording:
            await self.pump_one_frame()
            await anyio.sleep(period)

    # -------------------------------------------------------------- internals

    def _select_camera(self) -> None:
        """Apply the §12.1 selection rules. Sim publishes exactly one camera,
        so the rules collapse to:

        * ``spec.serial`` set + matches → ok
        * ``spec.serial`` set + mismatch → AdapterError
        * ``spec.model_hint`` set + matches "FLIR-SIM" → ok
        * neither set → ok (unique camera)
        """
        if self._spec.serial is not None and self._spec.serial != self._serial:
            raise AdapterError(
                f"FlirIrSim: requested serial {self._spec.serial!r} but only "
                f"{self._serial!r} is available"
            )
        if self._spec.model_hint is not None and self._spec.model_hint != "FLIR-SIM":
            raise AdapterError(
                f"FlirIrSim: model_hint {self._spec.model_hint!r} does not match 'FLIR-SIM'"
            )

    def _build_header(self) -> bytes:
        return (
            SIM_MAGIC
            + struct.pack("<I", 0)  # frame count placeholder
            + struct.pack("<III", self._width, self._height, int(self._fps))
        )

    def _frame_payload(self, idx: int) -> bytes:
        """Deterministic byte payload — a tiny gradient seeded by frame index.
        Tests assert equality against this so we can detect silent corruption.
        """
        size = self._frame_payload_bytes
        seed = idx & 0xFF
        return bytes((seed + i) & 0xFF for i in range(size))

    def _write_meta_sidecar(self, *, final: bool = False) -> None:
        """Write ``ir_cam0.csq.meta.json`` next to the .csq.

        The meta sidecar carries the run-start anchor (plan §12.1: "captures
        one ``t_mono_s`` anchor at ``start_recording()``"), the SDK config,
        and (when ``final``) the final file size.
        """
        if self._meta_path is None or self._output_path is None:
            return

        anchor_ns = self._started_t_mono_ns or 0
        body = {
            "name": self._spec.name,
            "adapter": "flir_ir_sim",
            "model": self._info.model,
            "serial": self._serial,
            "fps": self._fps,
            "width": self._width,
            "height": self._height,
            "frame_payload_bytes": self._frame_payload_bytes,
            "started_mono_ns_offset": anchor_ns,
            "started_utc": self._clock.to_wall_ns(anchor_ns).isoformat(),
            "output_path": str(self._output_path.name),
            "final": final,
            "frame_count": self._frame_count,
            "file_size_bytes": self._file_size,
        }
        self._meta_path.write_text(
            json.dumps(body, indent=2, sort_keys=False) + "\n", encoding="utf-8"
        )

    async def _emit_event(self, *, kind: str, message: str, severity: str) -> None:
        with contextlib.suppress(anyio.BrokenResourceError):
            await self._event_send.send(
                CameraEvent(
                    name=self._spec.name,
                    t_mono_ns=self._clock.t_mono_ns(),
                    t_utc=datetime.now(UTC),
                    kind=kind,
                    message=message,
                    severity=severity,
                )
            )


# ---------------------------------------------------------------------------
# Header-only frame-index extractor (post-finalize). capa-flir ships a
# separate Atlas-backed implementation for real ``.csq`` files.
# ---------------------------------------------------------------------------


def extract_frame_index(path: Path) -> list[tuple[int, int]]:
    """Parse a sim ``.csq`` and return ``[(frame_idx, t_mono_ns), ...]``.

    Pure-Python; reads the whole file but only header bytes per frame.
    Raises :class:`AdapterError` on a non-sim file (wrong magic).
    """
    with open(path, "rb") as fp:
        magic = fp.read(len(SIM_MAGIC))
        if magic != SIM_MAGIC:
            raise AdapterError(f"{path}: not a capa IR sim file (magic={magic!r})")
        frame_count = struct.unpack("<I", fp.read(4))[0]
        # Skip width/height/fps (12 bytes).
        fp.seek(HEADER_SIZE)
        out: list[tuple[int, int]] = []
        for _ in range(frame_count):
            header = fp.read(_FRAME_HEADER.size)
            if len(header) != _FRAME_HEADER.size:
                raise AdapterError(f"{path}: truncated frame header at offset {fp.tell()}")
            idx, t_mono_ns, payload_size = _FRAME_HEADER.unpack(header)
            out.append((idx, t_mono_ns))
            fp.seek(payload_size, 1)
        return out


async def _drain_stream[T](recv: MemoryObjectReceiveStream[T]) -> AsyncIterator[T]:
    """Yield items from a memory object stream until the send end is closed.

    Does not close the receive side — the camera owns the stream lifecycle
    and closes both ends in :meth:`FlirIrSim.close`. This makes the iterator
    safe to abandon mid-iteration (e.g., when the engine task group is
    cancelled) without prematurely tearing down the channel.
    """
    async for item in recv:
        yield item


__all__ = [
    "HEADER_SIZE",
    "SIM_MAGIC",
    "FlirIrSim",
    "extract_frame_index",
]
