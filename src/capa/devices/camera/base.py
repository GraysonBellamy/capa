""":class:`Camera` Protocol, :class:`CameraSpec` config, health/info/frame DTOs.

The Protocol is intentionally separate from
:class:`~capa.devices.adapter.DeviceAdapter`:

* lifecycle is ``open`` / ``close`` / ``start_recording(path)`` /
  ``stop_recording`` — *recording* is the explicit verb because cameras own
  their output container,
* emissions are :class:`FrameReceipt` records (one per frame, lightweight,
  posted from whichever thread the SDK callback runs on) and periodic
  :class:`CameraHealth` snapshots, *not* ``ChannelSample``,
* discovery returns :class:`CameraInfo` with model + serial + transport so the
  ``serial`` / ``model_hint`` selection rules can run without opening
  every camera.

Concrete adapters live under :mod:`capa.devices.camera` (visible) and in the
separate ``capa-flir`` package (IR). The sim fixture lives under
:mod:`capa.devices.sim.flir_ir_sim`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from enum import Flag, auto
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from pydantic import BaseModel, ConfigDict, Field, model_validator

from capa.devices.adapter import CommandResult, DeviceCommand


class CameraCapability(Flag):
    """Camera capability flags. Used by the UI to gate widgets and by the
    engine's preflight to validate against profile requirements.
    """

    NONE = 0
    RADIOMETRIC = auto()
    """Camera produces radiometric (per-pixel temperature) frames. IR-only."""
    PALETTE = auto()
    """Camera supports a configurable color palette (IR families). The
    preview-side palette — the live JPEG the dashboard shows. Distinct from
    REMOTE_PALETTE (camera onboard display)."""
    MEASUREMENT_SHAPES = auto()
    """Camera supports on-image measurement shapes (spotmeters, boxes, etc.)."""
    SUPPORTS_DISCOVERY = auto()
    """Adapter exposes ``discover()`` returning live :class:`CameraInfo` rows."""
    MODEL_HINT = auto()
    """Adapter honors :attr:`CameraSpec.model_hint` for selection."""
    SERIAL_SELECT = auto()
    """Adapter honors :attr:`CameraSpec.serial` for exact-match selection."""
    LIVE_PREVIEW = auto()
    """Adapter pumps preview frames onto :meth:`Camera.preview_stream`."""

    # ---- Control-surface flags ----
    NUC_TRIGGER = auto()
    """Adapter exposes a one-shot NUC / flat-field correction trigger
    (``ACS_Remote_Calibration_nuc_executeSync`` in the FLIR Atlas SDK)."""
    RADIOMETRIC_PARAMS = auto()
    """Adapter exposes the bundled radiometric kit — emissivity, atmospheric
    temperature/transmission, reflected temperature, object distance, relative
    humidity. The Atlas SDK ships these together; capa exposes them as one
    flag for the same reason."""
    TEMPERATURE_RANGE_SELECT = auto()
    """Adapter exposes camera temperature-range enumeration and selection
    (``ACS_Remote_TemperatureRange_*``). Switching ranges typically forces a
    multi-second recalibration; the adapter is responsible for refusing the
    operation while a recording is in progress."""
    AUTO_NUC_INTERVAL = auto()
    """Adapter exposes the camera's auto-NUC scheduler interval (seconds).
    ``0`` means disabled."""
    REMOTE_PALETTE = auto()
    """Adapter exposes camera-side display palette selection
    (``ACS_Remote_Palette_*``). Distinct from PALETTE, which means
    "preview-side palette is configurable"."""

    # ---- UVC control-surface flags (visible-camera control via duvc-ctl) ----
    EXPOSURE_CONTROL = auto()
    """Adapter exposes UVC exposure control: ``set_exposure`` (manual µs) and
    ``set_auto_exposure`` (auto/manual mode). UVC exposure is logged as
    2**(value) seconds; capa passes the raw int through to duvc-ctl."""
    FOCUS_CONTROL = auto()
    """Adapter exposes UVC focus control: ``set_focus`` (lens distance, raw
    units) and ``set_auto_focus`` (continuous-AF on/off). Cameras without a
    motorized lens (typical fixed-focus laptops) don't advertise this."""
    ZOOM_CONTROL = auto()
    """Adapter exposes UVC optical zoom (``set_zoom``) and/or digital zoom
    (``set_digital_zoom``). Distinct verbs because optical zoom rejects
    when the camera has none and silently falling back to digital would
    mislead the operator."""
    WB_CONTROL = auto()
    """Adapter exposes UVC white-balance control: ``set_white_balance``
    (color temperature K, raw int) and ``set_auto_white_balance`` (AWB
    on/off)."""
    PAN_TILT_CONTROL = auto()
    """Adapter exposes UVC pan / tilt control (PTZ cameras only):
    ``set_pan``, ``set_tilt``. Logitech PTZ Pro 2, BRIO 4K (limited),
    etc. Fixed cameras (C920/C930e) reject."""
    IMAGE_ADJUST = auto()
    """Adapter exposes UVC image-adjustment verbs: ``set_brightness``,
    ``set_contrast``, ``set_saturation``, ``set_sharpness``, ``set_gamma``,
    ``set_hue``, ``set_gain``, ``set_backlight_compensation``. Grouped
    under one flag because nearly every UVC device supports at least
    Brightness + Contrast; finer per-property gating is by probing each
    property's :class:`PropRange` at adapter ``open()``."""
    STREAM_FORMAT = auto()
    """Adapter exposes stream-format selection: ``set_resolution`` and
    ``set_framerate``. These are PyAV ``av.open()`` options (not UVC
    properties), so they require reopening the capture pipeline and are
    refused mid-recording."""


CameraTransport = Literal["usb", "ethernet", "file", "loopback"]
"""How the camera connects. ``loopback`` is the sim fixture's transport."""

CameraOnFailure = Literal["warn", "abort_run", "safe_shutdown"]
"""Policy for future camera recording-stall/error handling.

Current camera adapters surface events and health snapshots; safety-system
escalation based on this field is not wired yet.
"""


class CameraSpec(BaseModel):
    """Per-camera configuration entry inside
    :class:`~capa.experiment.config.HardwareProfile`.

    ``adapter`` is the importable adapter class — either a built-in
    (``capa.devices.camera.webcam``, ``capa.devices.sim.flir_ir_sim``) or a
    plugin id resolved through the ``capa.cameras`` entry-point group
    (``capa-flir``'s ``flir_ir``). The same dispatch rules as
    :class:`~capa.experiment.config.DeviceConfig` apply.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    """Camera-assigned name. Stable run-local id used as the parquet
    frame-index key (``<name>.frames.parquet``) and as the manifest entry id."""

    adapter: str
    """Module path or registered camera id (``"capa.devices.camera.webcam"``,
    ``"capa.devices.sim.flir_ir_sim"``, ``"flir_ir"`` once capa-flir is loaded)."""

    kind: Literal["visible", "ir"]
    """Coarse classification used to pick file extension (``.mkv`` vs. ``.csq``)
    and to drive UI grouping. Adapters declare their own kind via the Protocol;
    the spec value must agree."""

    model_hint: str | None = None
    """Preferred camera model (``"FLIR E85"``, ``"Logitech C920"``). Adapters
    that declare :attr:`CameraCapability.MODEL_HINT` filter discovery results
    by this — multiple matches log a warning."""

    serial: str | None = None
    """Exact-match selector. When set, :meth:`Camera.discover` must find a
    camera whose serial equals this string or :meth:`Camera.open` fails."""

    output_root: str | None = None
    """Optional override for the recorded file location. When
    set, the file lands at ``<output_root>/<run_id>/video/<name>.<ext>`` and
    the manifest records both the external path and a relative reference. When
    ``None`` (default), the file lives inside the bundle directory."""

    on_failure: CameraOnFailure = "warn"
    """Policy metadata for future camera safety escalation."""

    estimated_bps: int = Field(default=4_000_000, gt=0)
    """Bytes-per-second estimate used by the disk-space preflight.
    Defaults to ~4 MB/s — conservative for 30 fps H.264 webcam capture and
    realistic for E85 ``.csq`` at 30 Hz."""

    params: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    """Adapter-specific parameters: codec, palette, resolution overrides, etc.
    Validated at adapter construction, not here."""

    @model_validator(mode="after")
    def _kind_serial_pair(self) -> CameraSpec:
        """Allow ``kind="ir"`` without serial (model-hint selection is fine);
        the engine cross-checks against the adapter's ``kind`` at construction.
        """
        return self


class CameraInfo(BaseModel):
    """One row returned from :meth:`Camera.discover`.

    USB-discovery rules:

    * exact-match by ``serial`` wins over ``model_hint``,
    * ``model_hint`` match with multiple candidates logs a warning + picks
      the first deterministically,
    * unique camera with no selectors set is acceptable; multiple cameras
      with no selector is an error.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    adapter: str
    """The adapter id that produced this row (``"webcam"``,
    ``"flir_ir_sim"``, ``"flir_ir"``)."""
    name: str
    """Adapter-assigned device name (often the OS-level handle: ``"/dev/video0"``,
    ``"USB\\VID_..."``, etc.)."""
    model: str | None = None
    serial: str | None = None
    transport: CameraTransport = "usb"
    capabilities: tuple[str, ...] = Field(default_factory=tuple)
    """String-form :class:`CameraCapability` flags. Tuple-of-strings (rather
    than the Flag itself) keeps the serialization Pydantic-stable for the
    discover-output JSON."""
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class CameraHealth(BaseModel):
    """Periodic camera-health snapshot.

    Routed to ``status.sqlite`` via the existing snapshot
    sink path.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    t_mono_ns: int
    t_utc: datetime
    recording: bool
    """``True`` while a :meth:`Camera.start_recording` ↔ ``stop_recording``
    pair is active."""
    frame_count: int
    """Cumulative frames since the most recent ``start_recording``."""
    file_size_bytes: int = 0
    """Bytes written to the output container so far (best effort: vendor-
    managed writers may report stale values within a few hundred ms)."""
    last_frame_t_mono_ns: int | None = None
    """``t_mono_ns`` of the most recent frame, or ``None`` if no frame has
    been seen since recording started."""
    healthy: bool = True
    error: str | None = None
    dropped_frames: int = 0
    """Cumulative frames the encoder rejected (e.g. libx264 returned EINVAL
    on a malformed input) since ``start_recording``. The visible webcam
    path once tripped this at t≈23 s into a recipe and lost the run; the
    adapter now drops the bad frame, logs a ``pump_warning``
    event, and continues recording."""


class FrameReceipt(BaseModel):
    """One frame-arrival record. Posted from the SDK / capture thread onto a
    memory object stream; consumed on the engine event loop by the frame-index
    builder.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    """Camera name (matches :attr:`CameraSpec.name`)."""
    frame_idx: int
    t_mono_ns: int
    t_utc: datetime
    capture_latency_s: float = 0.0
    """Estimated SDK-to-Python hand-off latency for the frame. Webcam: PyAV's
    ``frame.time``-vs-now delta. IR: typically zero in capa-flir because the
    Atlas callback runs on its own thread and the timestamp is captured at
    callback entry."""
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class CameraEvent(BaseModel):
    """Discrete camera event: connect, disconnect, recording-start, stall,
    error. Distinct from :class:`FrameReceipt` because the cadence is
    different and the consumer (events.sqlite) is different.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    t_mono_ns: int
    t_utc: datetime
    kind: str
    message: str = ""
    severity: Literal["info", "warning", "error"] = "info"
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


@runtime_checkable
class Camera(Protocol):
    """Uniform camera surface.

    Concrete adapters back this with a vendor SDK or container library. The
    Protocol is *runtime_checkable* so the engine's adapter dispatch can
    isinstance-check loaded plugins without importing each one.

    Threading: an adapter MAY pump frames from its own SDK/native thread.
    The contract is that :attr:`frame_stream`, :attr:`preview_stream`, and
    :attr:`event_stream` are all consumable from the engine event loop —
    adapters bridge across threads via memory-object-streams (the typical
    pattern; see capa-flir's :mod:`_atlas._frame_pump`).
    """

    spec: CameraSpec
    capabilities: frozenset[CameraCapability]
    kind: Literal["visible", "ir"]
    resource_id: str

    async def discover(self) -> tuple[CameraInfo, ...]:
        """Enumerate connected cameras the adapter can drive.

        Adapters that don't support live discovery declare
        ``CameraCapability.SUPPORTS_DISCOVERY`` cleared and return an empty
        tuple; callers fall back to opening by ``adapter`` + ``serial`` /
        ``model_hint`` directly.
        """
        ...

    async def open(self) -> CameraInfo:
        """Establish the connection and return identifying info.

        Selection rules:

        * if ``self.spec.serial`` is set, require an exact match;
        * else if ``self.spec.model_hint`` is set, prefer a matching model
          and warn on multiple matches;
        * else accept the unique camera and fail clearly on multiple.

        Idempotent: a second call on an already-open camera returns the
        cached :class:`CameraInfo` without re-handshaking.
        """
        ...

    async def close(self) -> None:
        """Release the camera handle. Idempotent."""
        ...

    async def start_recording(self, output_path: Path) -> None:
        """Begin recording to ``output_path``.

        ``output_path`` is the *full* file path the adapter should write to,
        including the appropriate extension. The engine resolves the bundle
        directory + :attr:`CameraSpec.output_root` + extension before calling.
        Adapters MAY write a sidecar (e.g. ``ir_cam0.meta.json``) next to
        the main container.
        """
        ...

    async def stop_recording(self) -> None:
        """Flush and stop. Idempotent against a no-recording state."""
        ...

    async def snapshot(self) -> CameraHealth:
        """Build and return a fresh :class:`CameraHealth`."""
        ...

    def frame_stream(self) -> AsyncIterator[FrameReceipt]:
        """Async iterator of per-frame receipts. Drained by the frame-index
        sink. Bounded queue with ``BLOCK`` policy (frame index is durable;
        )."""
        ...

    def preview_stream(self) -> AsyncIterator[bytes]:
        """Optional preview frames (encoded as a small JPEG/PNG). Drained by
        the UI dock at ~2 Hz with ``DROP_OLDEST`` policy. Adapters that don't
        emit previews return an empty iterator and clear
        ``CameraCapability.LIVE_PREVIEW``."""
        ...

    def event_stream(self) -> AsyncIterator[CameraEvent]:
        """Async iterator of discrete camera events. Drained into
        ``events.sqlite``."""
        ...

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        """Issue a generic command. Same contract as
        :meth:`~capa.devices.adapter.DeviceAdapter.command`: the adapter
        applies the authorization gate, dispatches ``cmd.kind`` onto its
        typed methods, and returns a :class:`CommandResult` with
        ``accepted`` reflecting acceptance.

        Adapters that expose no control verbs (e.g. the visible-camera
        adapter) gate-and-reject — they must still implement the method so
        the Protocol surface stays uniform across visible and IR families.
        """
        ...


# ---------------------------------------------------------------------------
# Default async iterator implementations — adapters compose these with their
# own producer logic. Centralized so the four-stream wiring is consistent.
# ---------------------------------------------------------------------------


def make_stream_pair(
    buffer_size: int,
) -> tuple[MemoryObjectSendStream[Any], MemoryObjectReceiveStream[Any]]:
    """Build a bounded :func:`anyio.create_memory_object_stream` pair.

    Wrapped here so adapters import one symbol instead of grappling with
    AnyIO's typing dance.
    """
    return anyio.create_memory_object_stream(max_buffer_size=buffer_size)


__all__ = [
    "Camera",
    "CameraCapability",
    "CameraEvent",
    "CameraHealth",
    "CameraInfo",
    "CameraOnFailure",
    "CameraSpec",
    "CameraTransport",
    "FrameReceipt",
    "make_stream_pair",
]
