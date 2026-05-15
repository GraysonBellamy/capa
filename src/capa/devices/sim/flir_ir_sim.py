"""In-process IR-camera sim fixture (plan §12.1).

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
import io
import json
import struct
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from PIL import Image

from capa.core.clock import RunClock
from capa.core.errors import AdapterError
from capa.devices._helpers import (
    make_accepted_result,
    make_not_open_result,
    reject_unless_authorized,
)
from capa.devices.adapter import CommandResult, DeviceCommand
from capa.devices.camera.base import (
    CameraCapability,
    CameraEvent,
    CameraHealth,
    CameraInfo,
    CameraSpec,
    FrameReceipt,
    make_stream_pair,
)

if TYPE_CHECKING:
    from capa.devices.registry import AdapterDescriptor

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
        "_atmospheric_temp_c",
        "_atmospheric_transmission",
        "_auto_nuc_interval_s",
        "_clock",
        "_distance_m",
        "_emissivity",
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
        "_nuc_count",
        "_open",
        "_output_path",
        "_preview_palette",
        "_preview_recv",
        "_preview_send",
        "_recording",
        "_reflected_temp_c",
        "_relative_humidity",
        "_remote_palette",
        "_serial",
        "_spec",
        "_started_t_mono_ns",
        "_stream_anchor_mono_ns",
        "_temperature_range_index",
        "_width",
    )

    capabilities: frozenset[CameraCapability] = frozenset(
        {
            CameraCapability.RADIOMETRIC,
            CameraCapability.PALETTE,
            CameraCapability.SUPPORTS_DISCOVERY,
            CameraCapability.MODEL_HINT,
            CameraCapability.SERIAL_SELECT,
            # Sim emits JPEG-encoded preview frames during recording so the
            # preview integration tests can decode pixels via QImage. The
            # real FlirIrAdapter declares this when capa-flir wires the
            # live preview pump.
            CameraCapability.LIVE_PREVIEW,
            # Control-surface flags — sim mirrors the real FlirIrAdapter so
            # recipes / UI panels can be developed against the sim without an
            # SDK install. Verb table mirrors capa-flir's _dispatch_command.
            CameraCapability.NUC_TRIGGER,
            CameraCapability.RADIOMETRIC_PARAMS,
            CameraCapability.TEMPERATURE_RANGE_SELECT,
            CameraCapability.AUTO_NUC_INTERVAL,
            CameraCapability.REMOTE_PALETTE,
        }
    )
    kind: Literal["ir"] = "ir"

    TEMPERATURE_RANGES: tuple[str, ...] = ("low", "high")
    """Sim camera reports two ranges. ``set_temperature_range`` validates
    the index against this list."""

    REMOTE_PALETTES: tuple[str, ...] = ("iron", "rainbow", "bw", "arctic", "lava")
    """Sim camera-side palette names. Distinct from the preview-side preset
    list — :attr:`PREVIEW_PALETTE_PRESETS` mirrors capa-flir's
    ``PALETTE_PRESET_NAMES``."""

    PREVIEW_PALETTE_PRESETS: frozenset[str] = frozenset(
        {
            "arctic",
            "blackhot",
            "bw",
            "coldest",
            "color_wheel_redhot",
            "color_wheel_12",
            "color_wheel_6",
            "double_rainbow_2",
            "hottest",
            "iron",
            "lava",
            "rainbow",
            "rain_hc",
            "whitehot",
        }
    )
    """Mirrors capa-flir's ``_atlas._cdef.PALETTE_PRESET_NAMES`` keys so a
    recipe targeting the real adapter validates the same way against sim."""

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

        # Radiometric / control-surface state. Defaults match the typical
        # "human room" values a fresh FLIR camera reports out of the box;
        # recipes override per-experiment via DeviceCommand verbs.
        self._emissivity: float = 0.95
        self._atmospheric_temp_c: float = 20.0
        self._atmospheric_transmission: float = 1.0
        self._reflected_temp_c: float = 20.0
        self._distance_m: float = 1.0
        self._relative_humidity: float = 0.5
        self._temperature_range_index: int = 0
        self._auto_nuc_interval_s: int = 0
        self._remote_palette: str = "iron"
        self._preview_palette: str = "iron"
        self._nuc_count: int = 0

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
        """TOML-friendly constructor (plan §16 ``from_params`` convention)."""
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

    @property
    def resource_id(self) -> str:
        """Per-resource worker key (``docs/per-resource-worker-migration.md`` §4.10).

        Sim cameras share the ``sim:`` scheme with sim device adapters so
        ``build_workers`` validation treats them uniformly. The body is the
        configured spec name — distinct sims emit distinct workers.
        """
        return f"sim:{self._spec.name}"

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

    # ----------------------------------------------------------- command surface

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        """§9 authorization-gated dispatcher onto the typed sim state.

        Verb table is kept in lockstep with
        :class:`capa_flir.flir_ir.FlirIrAdapter._dispatch_command` so a
        recipe targeting the real camera validates identically against the
        sim. Discrepancies between the two surfaces should be considered
        bugs, not divergence: the deferred-control-surface handoff calls
        out sim parity as required for recipe portability.
        """
        clock = self._clock
        rejection = reject_unless_authorized(
            cmd, adapter_id="flir_ir_sim", device_name=self._spec.name, clock=clock
        )
        if rejection is not None:
            return rejection
        if not self._open:
            return make_not_open_result(
                adapter_id="flir_ir_sim", device_name=self._spec.name, clock=clock
            )
        try:
            detail = await self._dispatch_command(cmd)
        except (AdapterError, ValueError, KeyError) as exc:
            return CommandResult(
                accepted=False,
                detail=f"{cmd.kind!r}: {exc}",
                t_mono_ns=clock.t_mono_ns(),
                t_utc=datetime.now(UTC),
            )
        return make_accepted_result(detail=detail, clock=clock)

    async def _dispatch_command(self, cmd: DeviceCommand) -> str:
        """Map ``cmd.kind`` → sim state mutation. Raises on unknown verbs and
        on payload validation failures so :meth:`command` wraps the error
        into a rejected :class:`CommandResult`."""
        kind = cmd.kind
        payload = cmd.payload
        if kind == "trigger_nuc":
            if self._recording:
                raise AdapterError(
                    "trigger_nuc forbidden during recording (calibration discontinuity)"
                )
            self._nuc_count += 1
            await self._emit_event(kind="nuc_triggered", message="", severity="info")
            return "trigger_nuc"
        if kind == "set_emissivity":
            value = float(payload["emissivity"])
            if not (0.001 <= value <= 1.0):
                raise ValueError(f"emissivity must be in [0.001, 1.0]; got {value}")
            self._emissivity = value
            return f"set_emissivity emissivity={value}"
        if kind == "set_temperature_range":
            if self._recording:
                raise AdapterError("set_temperature_range forbidden during recording")
            index = int(payload["index"])
            if index < 0:
                raise ValueError(f"temperature-range index must be >= 0; got {index}")
            if index >= len(self.TEMPERATURE_RANGES):
                raise ValueError(
                    f"temperature-range index {index} out of bounds; sim reports "
                    f"{len(self.TEMPERATURE_RANGES)} range(s)"
                )
            self._temperature_range_index = index
            return f"set_temperature_range index={index}"
        if kind == "set_atmospheric_temp":
            celsius = float(payload["temperature_c"])
            self._atmospheric_temp_c = celsius
            return f"set_atmospheric_temp temperature_c={celsius}"
        if kind == "set_reflected_temp":
            celsius = float(payload["temperature_c"])
            self._reflected_temp_c = celsius
            return f"set_reflected_temp temperature_c={celsius}"
        if kind == "set_distance_m":
            distance = float(payload["distance_m"])
            if distance <= 0:
                raise ValueError(f"object distance must be > 0 meters; got {distance}")
            self._distance_m = distance
            return f"set_distance_m distance_m={distance}"
        if kind == "set_relative_humidity":
            fraction = float(payload["relative_humidity"])
            if not (0.0 <= fraction <= 1.0):
                # SDK uses fraction; per-image API uses percent — mirror the
                # real adapter's wording so the same misuse looks the same here.
                raise ValueError(
                    f"relative humidity must be a fraction in [0, 1]; got {fraction} "
                    "(SDK uses fraction; per-image API uses percent — don't confuse them)"
                )
            self._relative_humidity = fraction
            return f"set_relative_humidity relative_humidity={fraction}"
        if kind == "set_atmospheric_transmission":
            value = float(payload["transmission"])
            if not (0.0 <= value <= 1.0):
                raise ValueError(f"atmospheric transmission must be in [0, 1]; got {value}")
            self._atmospheric_transmission = value
            return f"set_atmospheric_transmission transmission={value}"
        if kind == "set_auto_nuc_interval":
            seconds = int(payload["seconds"])
            if seconds < 0:
                raise ValueError(f"auto-NUC interval must be >= 0; got {seconds}")
            self._auto_nuc_interval_s = seconds
            return f"set_auto_nuc_interval seconds={seconds}"
        if kind == "set_remote_palette":
            name = str(payload["palette"])
            if name not in self.REMOTE_PALETTES:
                raise ValueError(
                    f"unknown remote palette {name!r}; expected one of "
                    f"{sorted(self.REMOTE_PALETTES)}"
                )
            self._remote_palette = name
            return f"set_remote_palette palette={name!r}"
        if kind == "set_preview_palette":
            name = str(payload["palette"])
            if name not in self.PREVIEW_PALETTE_PRESETS:
                raise ValueError(
                    f"unknown preview palette {name!r}; expected one of "
                    f"{sorted(self.PREVIEW_PALETTE_PRESETS)}"
                )
            self._preview_palette = name
            return f"set_preview_palette palette={name!r}"
        raise AdapterError(f"unknown camera command kind={kind!r}")

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
        # Encode a tiny grayscale-gradient JPEG so the preview dock /
        # webcam card can actually paint pixels. The sim doesn't have a
        # real radiometric frame to render, so we synthesize a 64×48
        # gradient seeded by ``idx`` to give the operator a visible
        # cadence indicator. Best-effort drop (DROP_OLDEST semantics,
        # matching plan §7.1).
        jpeg = self._encode_preview_jpeg(idx)
        with contextlib.suppress(anyio.WouldBlock):
            self._preview_send.send_nowait(jpeg)
        return receipt

    @staticmethod
    def _encode_preview_jpeg(idx: int) -> bytes:
        """Encode a deterministic 64×48 grayscale gradient as JPEG.

        Seeded by ``idx`` so successive frames differ; the operator sees
        the tile shift each frame instead of a frozen static image.
        Pillow is a transitive dep; the encode is ~200 µs and the
        result is well under 1 KB.
        """
        offset = idx & 0xFF
        # Row-wise gradient that scrolls with idx — cheap to compute and
        # gives the preview tile a visible "movie" feel during recording.
        pixels = bytes(((row * 4 + offset) & 0xFF) for row in range(48) for _ in range(64))
        img = Image.frombytes("L", (64, 48), pixels)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        return buf.getvalue()

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
    "DESCRIPTOR",
    "HEADER_SIZE",
    "SIM_MAGIC",
    "FlirIrSim",
    "FlirIrSimParams",
    "discover_cameras",
    "extract_frame_index",
    "handshake",
]


# ---------------------------------------------------------------------------
# Module-level discovery + handshake (plan §7.2 item 1).
# ---------------------------------------------------------------------------


_SIM_DEFAULT_SERIAL = "SIM-IR-0001"
"""Stable serial advertised by the sim regardless of params. Operators
can override via ``cameras[*].serial`` in the config; the matcher below
treats the spec serial as authoritative when set."""


async def discover_cameras() -> list[dict[str, Any]]:
    """Return the single sim IR camera as a discover row.

    The simulator has no real device behind it, so "discovery" is a
    fixed advertisement — useful for the CLI's ``capa hardware
    discover`` and for the wizard's "CAPA pyrolysis simulated"
    starting point.
    """
    return [
        {
            "adapter": "capa.devices.sim.flir_ir_sim",
            "selector": _SIM_DEFAULT_SERIAL,
            "model": "FLIR IR sim",
            "serial": _SIM_DEFAULT_SERIAL,
            "transport": "sim",
        }
    ]


async def handshake(cam_spec: dict[str, Any]) -> str:
    """Layer-5 read-only verification for the IR sim camera.

    The sim always succeeds: there's nothing to wire up. We surface the
    selector / serial in the summary so the Problems panel reads
    consistently with the real camera adapter.
    """
    serial = cam_spec.get("serial") or _SIM_DEFAULT_SERIAL
    return f"flir_ir_sim model={'FLIR IR sim'!r} serial={serial!r}"


# ---------------------------------------------------------------------------
# Setup-editor descriptor (plan §5.7).
# ---------------------------------------------------------------------------


from pydantic import BaseModel, ConfigDict, Field  # noqa: E402


class FlirIrSimParams(BaseModel):
    """View model for :class:`FlirIrSim` params (plan §4.9.3)."""

    model_config = ConfigDict(extra="ignore")

    fps: float = Field(default=30.0, gt=0)
    width: int = Field(default=384, gt=0)
    height: int = Field(default=288, gt=0)
    frame_payload_bytes: int = Field(default=1024, gt=0)
    serial: str = "SIM-IR-0001"


def _build_descriptor() -> AdapterDescriptor:
    from capa.devices.registry import AdapterDescriptor  # noqa: PLC0415

    return AdapterDescriptor(
        id="capa.devices.sim.flir_ir_sim",
        label="FLIR IR camera (simulator)",
        family="camera_ir",
        adapter_factory=None,
        params_model=FlirIrSimParams,
        supported_binding_sources=(),
        default_params={
            "fps": 30.0,
            "width": 384,
            "height": 288,
            "frame_payload_bytes": 1024,
            "serial": "SIM-IR-0001",
        },
        channel_templates=(),
        discoverable=True,
        handshake_available=True,
    )


DESCRIPTOR = _build_descriptor()

from capa.devices.registry import register as _register  # noqa: E402

_register(DESCRIPTOR)
