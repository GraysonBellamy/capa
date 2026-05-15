"""Webcam adapter constants and platform-default helpers.

Module-level defaults (codec, pixel format, retry schedule, preview
cadence) live here so submodules and tests can import them without
pulling in the whole adapter class.
"""

from __future__ import annotations

import sys

from capa.devices.camera.base import CameraCapability

_BASE_CAPABILITIES: frozenset[CameraCapability] = frozenset(
    {
        CameraCapability.SUPPORTS_DISCOVERY,
        CameraCapability.SERIAL_SELECT,
        CameraCapability.MODEL_HINT,
        CameraCapability.LIVE_PREVIEW,
        # STREAM_FORMAT is always supported — PyAV reopens the input with
        # the new resolution/framerate on the next start_recording().
        # UVC control flags are added at open() time after duvc-ctl probes
        # the device; off Windows / on cameras with no controllable UVC
        # properties they stay absent.
        CameraCapability.STREAM_FORMAT,
    }
)

DEFAULT_CODEC = "libx264"
DEFAULT_PIX_FMT = "yuv420p"
DEFAULT_FPS = 30

OPEN_RETRY_DELAYS_S: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 2.0, 2.0)
"""Backoff schedule for transient ``av.open`` failures (Windows DirectShow
hold-time after ``cam.close()``). Cumulative ≈ 7.75 s, which covers the
worst-case observed C930e release latency on Windows 11. POSIX paths
normally open first try, so retries are dormant on Linux/macOS."""

OPEN_RETRY_DEADLINE_S: float = 8.0
"""Hard ceiling on retries; if we haven't opened by then the underlying
problem is not transient and we surface the original error."""

PREVIEW_INTERVAL_NS = 500_000_000
"""2 Hz preview cadence (plan §10.2; Camera Protocol docstring). At 30 fps the
adapter encodes one preview every 15 frames; the encode itself runs in the
worker thread already used for the H.264 pipeline."""

PREVIEW_MAX_WIDTH = 320
"""Preview thumbnails are width-capped, aspect preserved. 320 px keeps the
JPEG payload well under 30 kB at quality=70 even for high-detail frames."""

PREVIEW_JPEG_QUALITY = 70
"""Visually lossless enough for a thumbnail; cheap to encode at 2 Hz."""

_PLATFORM_DEFAULTS: dict[str, tuple[str, str]] = {
    "linux": ("v4l2", "/dev/video0"),
    "darwin": ("avfoundation", "default"),
    "win32": ("dshow", "video=0"),
}


def _platform_default_format() -> tuple[str, str]:
    """Return ``(input_format, default_input_url)`` for the current OS.

    Used when :attr:`CameraSpec.params` does not override. Only consulted by
    :meth:`WebcamAdapter.run_pump`; push-mode callers never hit this.
    """
    return _PLATFORM_DEFAULTS.get(sys.platform, ("v4l2", "/dev/video0"))
