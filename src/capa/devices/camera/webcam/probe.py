"""Camera discovery + handshake (sysfs / V4L2 / DirectShow).

Both the Setup editor's DiscoveryDialog and Layer 5 of the validation
pipeline reach for these without ever constructing an adapter. They
must be passive (no recording, no RunClock) and platform-tolerant —
a missing OS API returns an empty list, not an exception.
"""

from __future__ import annotations

import contextlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import av

from capa.core.errors import AdapterError


@dataclass(frozen=True, slots=True)
class V4L2Probe:
    """Identity fields extracted from sysfs for a V4L2 device path.

    Each field is ``None`` when sysfs didn't expose it (non-Linux,
    non-V4L2 path, missing parent USB descriptor, …) — the adapter
    falls back to whatever was in :class:`CameraSpec` for those.
    """

    card_name: str | None
    serial: str | None
    bus_info: str | None


def _is_linux_platform() -> bool:
    return sys.platform.startswith("linux")


def _probe_v4l2_info(device_path: str) -> V4L2Probe:
    """Read sysfs metadata for ``/dev/videoN`` (Linux only).

    Returns a fully-``None`` :class:`V4L2Probe` on every error path so
    :meth:`WebcamAdapter.open` doesn't have to special-case missing files,
    non-Linux platforms, or non-USB cameras (built-in MIPI sensors don't
    expose a USB ``serial``). Layout queried:

    * ``/sys/class/video4linux/<node>/name`` — card name (e.g. card_type
      from the V4L2 driver, ``"Logitech Webcam C930e"``).
    * ``/sys/class/video4linux/<node>/device`` — symlink to the parent
      USB *interface*; one level up is the USB *device* whose ``serial``,
      ``idVendor``, ``idProduct`` files identify the unit.
    """
    empty = V4L2Probe(card_name=None, serial=None, bus_info=None)
    if not _is_linux_platform():
        return empty
    if not device_path.startswith("/dev/video"):
        return empty
    node = device_path.rsplit("/", 1)[-1]  # "video4"
    sysfs_root = Path("/sys/class/video4linux") / node
    if not sysfs_root.exists():
        return empty

    card_name: str | None = None
    name_file = sysfs_root / "name"
    try:
        if name_file.exists():
            card_name = name_file.read_text().strip() or None
    except OSError:
        pass

    serial: str | None = None
    bus_info: str | None = None
    device_link = sysfs_root / "device"
    try:
        if device_link.exists():
            interface_dir = device_link.resolve()
            usb_device_dir = interface_dir.parent
            serial_file = usb_device_dir / "serial"
            if serial_file.exists():
                serial = serial_file.read_text().strip() or None
            bus_info = usb_device_dir.name or None
    except OSError:
        pass

    return V4L2Probe(card_name=card_name, serial=serial, bus_info=bus_info)


_DSHOW_MAX_FORMAT_RE: re.Pattern[str] = re.compile(
    r"\bmax s=(\d+)x(\d+)\s+fps=([\d.]+)", re.IGNORECASE
)
_DSHOW_MIN_FORMAT_RE: re.Pattern[str] = re.compile(
    r"\bmin s=(\d+)x(\d+)\s+fps=([\d.]+)", re.IGNORECASE
)
_DSHOW_MAX_SIZE_RE: re.Pattern[str] = re.compile(r"\bmax s=(\d+)x(\d+)\b", re.IGNORECASE)
_DSHOW_MIN_SIZE_RE: re.Pattern[str] = re.compile(r"\bmin s=(\d+)x(\d+)\b", re.IGNORECASE)


def _probe_dshow_format_info_sync(
    input_url: str,
) -> tuple[list[tuple[int, int]], dict[tuple[int, int], float]]:
    """Enumerate ``(width, height)`` pairs and per-resolution max fps caps.

    FFmpeg's dshow demuxer prints the device's pin formats when opened with
    ``options={"list_options": "true"}`` — the call always fails with the
    expected ``Immediate exit requested``, but the format dump lands on the
    libav log channel first. We capture those lines via
    :func:`av.logging.Capture` and parse the ``max s=WxH fps=NN.NNN`` tail
    of each ``pixel_format=…`` line. Multiple pixel formats per resolution
    collapse to the highest reported fps for that size.

    Uses ``Capture(local=False)`` because this helper is invoked through
    :func:`anyio.to_thread.run_sync`; the libav log callback fires from the
    worker thread, and ``local=True`` would only route logs back to the
    constructing thread's id. Restores the prior log level on exit so the
    rest of capa's PyAV usage stays silent.

    Returns ``([], {})`` on any failure (PyAV missing, non-Windows path,
    parse mismatch). Callers fall back to a static resolution set and an
    uncapped fps spinbox when nothing was probed.
    """
    old_level = av.logging.get_level()
    av.logging.set_level(av.logging.VERBOSE)
    try:
        with av.logging.Capture(local=False) as logs, contextlib.suppress(Exception):
            container = av.open(input_url, format="dshow", options={"list_options": "true"})
            container.close()
    finally:
        av.logging.set_level(old_level)

    seen: set[tuple[int, int]] = set()
    resolutions: list[tuple[int, int]] = []
    fps_caps: dict[tuple[int, int], float] = {}
    for entry in logs:
        message = entry[2] if len(entry) >= 3 else ""
        size_w: int | None = None
        size_h: int | None = None
        fps_value: float | None = None
        fmt_match = _DSHOW_MAX_FORMAT_RE.search(message) or _DSHOW_MIN_FORMAT_RE.search(message)
        if fmt_match is not None:
            size_w = int(fmt_match.group(1))
            size_h = int(fmt_match.group(2))
            with contextlib.suppress(ValueError):
                fps_value = float(fmt_match.group(3))
        else:
            size_match = _DSHOW_MAX_SIZE_RE.search(message) or _DSHOW_MIN_SIZE_RE.search(message)
            if size_match is not None:
                size_w = int(size_match.group(1))
                size_h = int(size_match.group(2))
        if size_w is None or size_h is None:
            continue
        wh = (size_w, size_h)
        if wh not in seen:
            seen.add(wh)
            resolutions.append(wh)
        if fps_value is not None and fps_value > 0:
            existing = fps_caps.get(wh)
            if existing is None or fps_value > existing:
                fps_caps[wh] = fps_value
    resolutions.sort(key=lambda wh: (wh[0] * wh[1], wh[0]))
    return resolutions, fps_caps


async def discover_cameras() -> list[dict[str, Any]]:
    """Walk the local OS camera enumeration APIs and return a row per
    visible visible-light camera.

    Returns dicts shaped like the other adapters' ``discover()`` output
    so the CLI can render them uniformly::

        {
            "adapter": "capa.devices.camera.webcam",
            "selector": "/dev/video0" | "video=Logitech C920" | "0",
            "model":    "Logitech C920",
            "serial":   "ABC123" | None,
            "transport": "usb",
        }

    Platform paths:

    * **Linux** — walks ``/sys/class/video4linux/video*`` and reuses the
      existing :func:`_probe_v4l2_info` helper so card-name / USB serial
      come from sysfs without opening the device.
    * **Windows** — uses ``duvc_ctl.list_devices()`` when the wheel is
      installed. Returns one row per visible DirectShow camera.
    * **macOS / unsupported** — returns ``[]``. AVFoundation
      enumeration can be added later; for now operators add macOS cameras
      by hand.
    """
    platform = sys.platform
    if platform.startswith("linux"):
        return await anyio.to_thread.run_sync(_enumerate_v4l2_sync)
    if platform == "win32":
        return await _enumerate_directshow()
    return []


def _enumerate_v4l2_sync() -> list[dict[str, Any]]:
    """List visible-light V4L2 capture nodes via sysfs (Linux only)."""
    root = Path("/sys/class/video4linux")
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    seen_devices: set[str] = set()
    for node_dir in sorted(root.iterdir()):
        node = node_dir.name
        if not re.fullmatch(r"video\d+", node):
            continue
        device_path = f"/dev/{node}"
        probed = _probe_v4l2_info(device_path)
        # bus_info is the USB device id; collapse the multiple
        # /dev/videoN nodes one webcam exposes (capture + metadata) to
        # a single row keyed on the bus.
        bus = probed.bus_info or device_path
        if bus in seen_devices:
            continue
        seen_devices.add(bus)
        rows.append(
            {
                "adapter": "capa.devices.camera.webcam",
                "selector": device_path,
                "model": probed.card_name,
                "serial": probed.serial,
                "transport": "usb",
            }
        )
    return rows


async def _enumerate_directshow() -> list[dict[str, Any]]:
    """List DirectShow cameras via duvc-ctl (Windows only).

    Falls back to an empty list when the duvc-ctl wheel is missing —
    operators on a stripped-down Windows install simply see no camera
    rows rather than a crash.
    """
    try:
        from capa.devices.camera._uvc import _duvc  # noqa: PLC0415
    except ImportError:
        return []
    if _duvc is None:
        return []
    try:
        devices = await anyio.to_thread.run_sync(_duvc.list_devices)
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for dev in devices or ():
        name = getattr(dev, "name", None)
        path = getattr(dev, "path", None)
        rows.append(
            {
                "adapter": "capa.devices.camera.webcam",
                "selector": f"video={name}" if name else (path or ""),
                "model": name,
                "serial": path,  # duvc path is the DirectShow moniker
                "transport": "directshow",
            }
        )
    return rows


def _match_camera_row(
    rows: list[dict[str, Any]],
    *,
    model_hint: str | None,
    serial: str | None,
) -> dict[str, Any] | None:
    """Apply the selector rules to a discover result list.

    Returns the chosen row or ``None`` when no unique match exists.
    """
    if serial is not None:
        for row in rows:
            row_serial = row.get("serial")
            if isinstance(row_serial, str) and serial.lower() in row_serial.lower():
                return row
        return None
    if model_hint is not None:
        matches = [
            row
            for row in rows
            if isinstance(row.get("model"), str) and model_hint.lower() in row["model"].lower()
        ]
        if not matches:
            return None
        return matches[0]
    if len(rows) == 1:
        return rows[0]
    return None


async def handshake(cam_spec: dict[str, Any]) -> str:
    """Layer-5 read-only verification for a configured visible camera.

    Unlike device handshakes (which open + identify + close a serial
    port), a real DirectShow / V4L2 open holds the capture pin for
    100s of ms and competes with whatever else might be watching the
    camera. We use the cheaper "the camera shows up in discovery"
    check instead — sufficient to catch the common wiring failure
    (cable yanked, device path renumbered) without paying the
    capture-pin cost. item 1.
    """
    rows = await discover_cameras()
    if not rows:
        raise AdapterError(
            "no visible cameras enumerated on this host (sysfs/duvc-ctl returned no devices)"
        )
    model_hint = cam_spec.get("model_hint")
    serial = cam_spec.get("serial")
    chosen = _match_camera_row(
        rows,
        model_hint=model_hint if isinstance(model_hint, str) else None,
        serial=serial if isinstance(serial, str) else None,
    )
    if chosen is None:
        wanted = (
            f"serial={serial!r}"
            if serial is not None
            else f"model_hint={model_hint!r}"
            if model_hint is not None
            else "no selector (and >1 camera present)"
        )
        raise AdapterError(f"no unique camera match for {wanted}; saw {len(rows)} devices")
    model = chosen.get("model") or "?"
    serial_seen = chosen.get("serial") or "?"
    selector = chosen.get("selector") or "?"
    return f"webcam model={model!r} serial={serial_seen!r} selector={selector!r}"
