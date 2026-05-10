"""Thin async wrapper around ``duvc-ctl`` for UVC webcam control.

duvc-ctl exposes a Result-Based API (every operation returns a ``*Result``
with ``is_ok()`` / ``value()`` / ``error()``) over a Windows DirectShow
``IAMCameraControl`` / ``IAMVideoProcAmp`` shim. This module:

* normalizes the two property namespaces (``CamProp`` for camera controls
  like Pan/Zoom/Exposure/Focus, ``VidProp`` for image-adjust controls like
  Brightness/Contrast/WhiteBalance/Gain) under a single :class:`UvcProperty`
  enum so the adapter's command dispatch can route by string name;
* maps duvc-ctl's per-device capability probe onto capa's granular
  :class:`~capa.devices.camera.base.CameraCapability` flags;
* wraps every blocking duvc-ctl call in :func:`anyio.to_thread.run_sync` so
  the asyncio loop stays free (duvc-ctl is a synchronous DirectShow shim);
* exposes a small typed surface (``UvcController``) the
  :class:`~capa.devices.camera.webcam.WebcamAdapter` consumes — adapter
  code never imports ``duvc_ctl`` directly.

Windows-only. The adapter's import path is guarded by ``sys.platform`` so
Linux / macOS builds don't fail on the missing module.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any

import anyio

from capa.core.errors import AdapterError
from capa.devices.camera.base import CameraCapability

# Soft-import: on Linux/macOS the dep is excluded by pyproject's
# ``; sys_platform == 'win32'`` marker, and even on Windows a fresh
# checkout may not yet have the vendored wheel installed (CI without
# vendor/, contributors who skipped the install step). Treat both as
# "no UVC controls", not as an ImportError on adapter load.
#
# Annotated as ``Any | None`` so mypy doesn't narrow to ``None`` on the
# non-Windows branch (which would make every property-access line below
# "unreachable" from its perspective). The runtime ``_duvc is None``
# guards still gate every duvc-ctl call.
_duvc: Any
if sys.platform == "win32":
    try:
        import duvc_ctl as _duvc_module

        _duvc = _duvc_module
    except ImportError:  # pragma: no cover — Windows w/o the wheel installed
        _duvc = None
else:  # pragma: no cover — non-Windows
    _duvc = None


class UvcGroup(Enum):
    """duvc-ctl splits properties across two DirectShow interfaces.

    ``CAMERA`` → ``IAMCameraControl`` (mechanical: pan/tilt/zoom/exposure/focus).
    ``VIDEO``  → ``IAMVideoProcAmp`` (image-adjust: brightness/contrast/WB/gain).

    Same property name (``BacklightCompensation``) appears in both namespaces
    on some devices; the adapter's verb table is explicit about which group
    each verb targets, so we never have to guess.
    """

    CAMERA = "camera"
    VIDEO = "video"


@dataclass(frozen=True, slots=True)
class UvcProperty:
    """One controllable UVC property.

    ``name`` is the duvc-ctl enum attribute name (e.g. ``"Exposure"``,
    ``"WhiteBalance"``). ``group`` selects ``CamProp`` vs ``VidProp``.
    """

    name: str
    group: UvcGroup


# ---- Verb → property mapping ----------------------------------------------
#
# Used by both the adapter's command() dispatch and the capability probe.
# The verb name is what the manual-control card / procedure step sends; the
# UvcProperty pins down which duvc-ctl enum to look up at runtime. Keep this
# table small and explicit — the duvc-ctl property set is fixed at the UVC
# spec level, so there's no churn here.

PROPERTY_BY_VERB: dict[str, UvcProperty] = {
    # CamProp (mechanical)
    "set_exposure": UvcProperty("Exposure", UvcGroup.CAMERA),
    "set_focus": UvcProperty("Focus", UvcGroup.CAMERA),
    "set_zoom": UvcProperty("Zoom", UvcGroup.CAMERA),
    "set_digital_zoom": UvcProperty("DigitalZoom", UvcGroup.CAMERA),
    "set_pan": UvcProperty("Pan", UvcGroup.CAMERA),
    "set_tilt": UvcProperty("Tilt", UvcGroup.CAMERA),
    # VidProp (image-adjust)
    "set_brightness": UvcProperty("Brightness", UvcGroup.VIDEO),
    "set_contrast": UvcProperty("Contrast", UvcGroup.VIDEO),
    "set_saturation": UvcProperty("Saturation", UvcGroup.VIDEO),
    "set_sharpness": UvcProperty("Sharpness", UvcGroup.VIDEO),
    "set_gamma": UvcProperty("Gamma", UvcGroup.VIDEO),
    "set_hue": UvcProperty("Hue", UvcGroup.VIDEO),
    "set_gain": UvcProperty("Gain", UvcGroup.VIDEO),
    "set_white_balance": UvcProperty("WhiteBalance", UvcGroup.VIDEO),
    "set_backlight_compensation": UvcProperty("BacklightCompensation", UvcGroup.VIDEO),
}

# Auto-mode verbs: a separate verb table because the property is the same
# but the dispatch sets the CamMode rather than a numeric value. Distinct
# from PROPERTY_BY_VERB so the adapter doesn't have to special-case payload
# shape per verb.
AUTO_VERB_TO_PROPERTY: dict[str, UvcProperty] = {
    "set_auto_exposure": UvcProperty("Exposure", UvcGroup.CAMERA),
    "set_auto_focus": UvcProperty("Focus", UvcGroup.CAMERA),
    "set_auto_white_balance": UvcProperty("WhiteBalance", UvcGroup.VIDEO),
}


# ---- Capability mapping ---------------------------------------------------
#
# Each capa CameraCapability flag is satisfied by the camera supporting at
# least one of the named UVC properties (per duvc-ctl's
# ``supported_camera_properties()`` / ``supported_video_properties()``
# probe). The capability flag means "the UI should show this section"; the
# verb-level rejection still happens at command() if a specific property
# within the group isn't supported on this device.

CAPABILITY_REQUIREMENTS: dict[CameraCapability, tuple[UvcProperty, ...]] = {
    CameraCapability.EXPOSURE_CONTROL: (UvcProperty("Exposure", UvcGroup.CAMERA),),
    CameraCapability.FOCUS_CONTROL: (UvcProperty("Focus", UvcGroup.CAMERA),),
    CameraCapability.ZOOM_CONTROL: (
        UvcProperty("Zoom", UvcGroup.CAMERA),
        UvcProperty("DigitalZoom", UvcGroup.CAMERA),
    ),
    CameraCapability.WB_CONTROL: (UvcProperty("WhiteBalance", UvcGroup.VIDEO),),
    CameraCapability.PAN_TILT_CONTROL: (
        UvcProperty("Pan", UvcGroup.CAMERA),
        UvcProperty("Tilt", UvcGroup.CAMERA),
    ),
    CameraCapability.IMAGE_ADJUST: (
        UvcProperty("Brightness", UvcGroup.VIDEO),
        UvcProperty("Contrast", UvcGroup.VIDEO),
        UvcProperty("Saturation", UvcGroup.VIDEO),
        UvcProperty("Sharpness", UvcGroup.VIDEO),
        UvcProperty("Gamma", UvcGroup.VIDEO),
        UvcProperty("Hue", UvcGroup.VIDEO),
        UvcProperty("Gain", UvcGroup.VIDEO),
        UvcProperty("BacklightCompensation", UvcGroup.VIDEO),
    ),
}


# ---- Range / current readback ---------------------------------------------


@dataclass(frozen=True, slots=True)
class UvcPropertyRange:
    """Subset of duvc-ctl's :class:`PropRange` capa cares about.

    Frozen because the UI caches ranges from the open() probe and re-uses
    them across slider rebuilds. duvc-ctl's PropRange is mutable.
    """

    minimum: int
    maximum: int
    step: int
    default: int
    """Factory default (PropRange.default_val)."""


@dataclass(frozen=True, slots=True)
class UvcPropertyState:
    """Current value and mode of one UVC property."""

    value: int
    auto: bool
    """``True`` when duvc-ctl reports CamMode.Auto; ``False`` for Manual."""


# ---- Controller -----------------------------------------------------------


class UvcController:
    """Owns a duvc-ctl Device handle + camera Result for one UVC camera.

    Lifecycle: cheap to construct (just stores the matched Device); the
    Camera handle is acquired lazily on the first property access so a
    camera that doesn't expose any UVC controls doesn't pay the
    ``open_camera`` cost. ``close()`` drops the handle (duvc-ctl Camera is
    RAII — no explicit close method).

    Thread model: every blocking call into duvc-ctl is wrapped in
    :func:`anyio.to_thread.run_sync` so the asyncio loop is free. duvc-ctl
    holds the IDirectShow filter graph for ~100 ms during a get/set, which
    is plenty to starve a 30 fps frame pump.
    """

    __slots__ = ("_camera", "_currents", "_device", "_device_name", "_ranges", "_supported")

    def __init__(self, device: Any, device_name: str) -> None:
        self._device = device
        self._device_name = device_name
        self._camera: Any | None = None
        # Frozen at probe time so adapter dispatch can refuse unsupported
        # verbs without round-tripping into duvc-ctl every call.
        self._supported: frozenset[tuple[str, UvcGroup]] = frozenset()
        # Populated by probe_capabilities() so the UI can build spinbox
        # bounds + initial values from real device data without paying a
        # per-property DirectShow round-trip on every refresh.
        self._ranges: dict[tuple[str, UvcGroup], UvcPropertyRange] = {}
        self._currents: dict[tuple[str, UvcGroup], int] = {}

    @property
    def device_name(self) -> str:
        return self._device_name

    @classmethod
    async def find(
        cls,
        *,
        model_hint: str | None,
        serial: str | None,
    ) -> UvcController | None:
        """Match one of the connected UVC devices against ``model_hint`` /
        ``serial`` and return a controller, or ``None`` if no match.

        Selection rules mirror :class:`~capa.devices.camera.base.CameraSpec`
        (plan §12.1):

        * exact ``serial`` substring match in the Device's path wins,
        * else ``model_hint`` substring against the Device's friendly name,
        * else: the unique device, or ``None`` when multiple cameras are
          connected without a selector.

        Returns ``None`` (not raises) when duvc-ctl is unavailable (non-
        Windows builds) so the adapter can degrade to no-controls mode
        without a try/except dance.
        """
        if _duvc is None:
            return None
        try:
            devices = await anyio.to_thread.run_sync(_duvc.list_devices)
        except Exception:
            return None
        if not devices:
            return None

        def _match(dev: Any) -> bool:
            if serial is not None and serial.lower() in dev.path.lower():
                return True
            return model_hint is not None and model_hint.lower() in dev.name.lower()

        if serial is not None or model_hint is not None:
            matches = [d for d in devices if _match(d)]
            if not matches:
                return None
            chosen = matches[0]
        elif len(devices) == 1:
            chosen = devices[0]
        else:
            # Multiple cameras + no selector: the adapter's open() will
            # surface this; we just decline to guess.
            return None
        return cls(chosen, chosen.name)

    async def probe_capabilities(self) -> frozenset[CameraCapability]:
        """Open + introspect the device and return the set of capa
        :class:`CameraCapability` flags this camera actually supports.

        Calls ``duvc_ctl.get_device_capabilities`` and maps the returned
        ``supported_camera_properties()`` / ``supported_video_properties()``
        onto capa's granular flag set via :data:`CAPABILITY_REQUIREMENTS`.

        Side effect: populates the internal ``_supported`` set used by
        :meth:`get`, :meth:`set_value`, :meth:`set_auto` for fast support
        checks. Idempotent — calling twice re-probes (so a device that
        gained / lost a property between probes reflects reality).
        """
        if _duvc is None:
            return frozenset()
        caps = await anyio.to_thread.run_sync(_get_device_capabilities, self._device)
        if caps is None:
            self._supported = frozenset()
            self._ranges = {}
            self._currents = {}
            return frozenset()
        supported_pairs: set[tuple[str, UvcGroup]] = set()
        for prop in caps.supported_camera_properties():
            supported_pairs.add((prop.name, UvcGroup.CAMERA))
        for prop in caps.supported_video_properties():
            supported_pairs.add((prop.name, UvcGroup.VIDEO))
        self._supported = frozenset(supported_pairs)

        # Cache per-property range + current value off the same capabilities
        # snapshot so the UI can populate spinbox bounds and initial values
        # without paying a per-property DirectShow round-trip. PropertyCapability
        # is returned by value from the duvc-ctl Python binding (the docs'
        # Result-wrapped pattern is for free functions, not the
        # DeviceCapabilities methods).
        self._ranges = {}
        self._currents = {}
        for cam_prop in caps.supported_camera_properties():
            cap = caps.get_camera_capability(cam_prop)
            self._record_capability(cap, cam_prop.name, UvcGroup.CAMERA)
        for vid_prop in caps.supported_video_properties():
            cap = caps.get_video_capability(vid_prop)
            self._record_capability(cap, vid_prop.name, UvcGroup.VIDEO)

        flags: set[CameraCapability] = set()
        for flag, requirements in CAPABILITY_REQUIREMENTS.items():
            if any((req.name, req.group) in self._supported for req in requirements):
                flags.add(flag)
        return frozenset(flags)

    def _record_capability(self, cap: Any, name: str, group: UvcGroup) -> None:
        """Extract range + current value from a ``PropertyCapability`` and
        stash them in the per-controller caches. ``None`` (or a defensive
        ``AttributeError`` from a binding that diverges) leaves the entry
        absent — callers fall back to safe widget defaults."""
        if cap is None:
            return
        try:
            rng = cap.range
        except AttributeError:
            rng = None
        if rng is not None:
            self._ranges[(name, group)] = UvcPropertyRange(
                minimum=int(rng.min),
                maximum=int(rng.max),
                step=int(rng.step) if int(rng.step) > 0 else 1,
                default=int(rng.default_val),
            )
        try:
            current = cap.current
        except AttributeError:
            current = None
        if current is not None:
            self._currents[(name, group)] = int(current.value)

    def get_cached_range(self, prop: UvcProperty) -> UvcPropertyRange | None:
        """Return the probed range for ``prop`` or ``None`` if the probe
        never recorded one (property unsupported, capabilities snapshot
        unavailable, …). Pure cache hit — never touches DirectShow."""
        return self._ranges.get((prop.name, prop.group))

    def get_cached_current(self, prop: UvcProperty) -> int | None:
        """Return the device's current value for ``prop`` as captured at
        the last :meth:`probe_capabilities` call, or ``None`` if not
        recorded. Used by the manual-control card to seed spinboxes with
        the value the camera actually reports rather than the factory
        default — keeps the UI honest after the operator's prior session
        nudged settings."""
        return self._currents.get((prop.name, prop.group))

    def supports(self, prop: UvcProperty) -> bool:
        """``True`` when this device declared ``prop`` in its capability
        probe. Cheap pure-Python check — adapter dispatch calls this before
        every set/get so a verb on an unsupported property rejects with a
        useful message instead of bubbling a duvc-ctl error."""
        return (prop.name, prop.group) in self._supported

    async def get(self, prop: UvcProperty) -> UvcPropertyState | None:
        """Read current value + mode. ``None`` on read failure (device
        disconnected mid-session, etc.) — distinct from "unsupported",
        which :meth:`supports` answers."""
        cam = await self._ensure_camera()
        if cam is None:
            return None
        return await anyio.to_thread.run_sync(_get_property_state, cam, prop)

    async def get_range(self, prop: UvcProperty) -> UvcPropertyRange | None:
        """Read the property's allowed range. ``None`` if the property is
        unsupported or the device returned no range (rare; usually means
        the property is read-only or auto-only).
        """
        cam = await self._ensure_camera()
        if cam is None:
            return None
        return await anyio.to_thread.run_sync(_get_property_range, cam, prop)

    async def set_value(self, prop: UvcProperty, value: int) -> None:
        """Set ``prop`` to ``value`` in manual mode. Raises
        :class:`AdapterError` if duvc-ctl rejects (out-of-range, unsupported,
        device disconnected) — the adapter's command() catches and converts
        to a :class:`CommandResult` with ``accepted=False``.
        """
        cam = await self._ensure_camera()
        if cam is None:
            raise AdapterError(
                f"duvc-ctl: cannot open camera {self._device_name!r}",
                device=self._device_name,
            )
        await anyio.to_thread.run_sync(_set_property_value, cam, prop, value)

    async def set_auto(self, prop: UvcProperty, enable: bool) -> None:
        """Set ``prop`` to ``CamMode.Auto`` (``enable=True``) or
        ``CamMode.Manual`` (``enable=False``). When toggling back to manual
        without a new value, the device retains its last manual value —
        operators should follow up with :meth:`set_value` to pin one."""
        cam = await self._ensure_camera()
        if cam is None:
            raise AdapterError(
                f"duvc-ctl: cannot open camera {self._device_name!r}",
                device=self._device_name,
            )
        await anyio.to_thread.run_sync(_set_property_auto, cam, prop, enable)

    def close(self) -> None:
        """Drop the cached Camera handle. duvc-ctl uses RAII — the
        ``IBaseFilter`` is released when the Python ref drops. Idempotent.
        """
        self._camera = None

    # ------------------------------------------------------------------ internals

    async def _ensure_camera(self) -> Any | None:
        if self._camera is not None:
            return self._camera
        if _duvc is None:
            return None
        cam = await anyio.to_thread.run_sync(_open_camera, self._device)
        self._camera = cam
        return cam


# ---- Sync helpers (run inside anyio.to_thread.run_sync) -------------------
#
# Each helper isolates one synchronous duvc-ctl block so the adapter only
# pays the worker-thread hop once per UI action, and so the helper-level
# Result unwrap is centralized in one place per operation.


def _open_camera(device: Any) -> Any | None:
    """Open ``device`` and return the duvc-ctl ``Camera`` handle or
    ``None`` on failure. Runs on a worker thread.
    """
    if _duvc is None:
        return None
    result = _duvc.open_camera(device)
    if not result.is_ok():
        return None
    return result.value()


def _get_device_capabilities(device: Any) -> Any | None:
    if _duvc is None:
        return None
    result = _duvc.get_device_capabilities(device)
    if not result.is_ok():
        return None
    return result.value()


def _resolve_prop_enum(prop: UvcProperty) -> Any | None:
    """Map a :class:`UvcProperty` onto the duvc-ctl ``CamProp`` /
    ``VidProp`` enum value. Returns ``None`` if the duvc-ctl module
    doesn't define a matching member (forward-compat: a capa verb table
    that names a property duvc-ctl removes won't crash).
    """
    if _duvc is None:
        return None
    cls = _duvc.CamProp if prop.group is UvcGroup.CAMERA else _duvc.VidProp
    return getattr(cls, prop.name, None)


def _get_property_state(cam: Any, prop: UvcProperty) -> UvcPropertyState | None:
    if _duvc is None:
        return None
    enum_val = _resolve_prop_enum(prop)
    if enum_val is None:
        return None
    result = (
        cam.get(enum_val) if prop.group is UvcGroup.CAMERA else cam.get_video_property(enum_val)
    )
    if not result.is_ok():
        return None
    setting = result.value()
    return UvcPropertyState(
        value=int(setting.value),
        auto=(setting.mode == _duvc.CamMode.Auto),
    )


def _get_property_range(cam: Any, prop: UvcProperty) -> UvcPropertyRange | None:
    if _duvc is None:
        return None
    enum_val = _resolve_prop_enum(prop)
    if enum_val is None:
        return None
    # duvc-ctl exposes ``get_range`` for CamProp; range for VidProp comes via
    # the device-capability snapshot (PropertyCapability.range). We fall back
    # through the capabilities path for VidProp.
    if prop.group is UvcGroup.CAMERA:
        result = cam.get_range(enum_val)
        if not result.is_ok():
            return None
        pr = result.value()
    else:
        caps_result = _duvc.get_device_capabilities(cam.device)
        if not caps_result.is_ok():
            return None
        prop_cap = caps_result.value().get_video_capability(enum_val)
        if prop_cap is None or prop_cap.range is None:
            return None
        pr = prop_cap.range
    return UvcPropertyRange(
        minimum=int(pr.min),
        maximum=int(pr.max),
        step=int(pr.step),
        default=int(pr.default_val),
    )


def _set_property_value(cam: Any, prop: UvcProperty, value: int) -> None:
    if _duvc is None:
        raise AdapterError("duvc-ctl not available on this platform")
    enum_val = _resolve_prop_enum(prop)
    if enum_val is None:
        raise AdapterError(f"duvc-ctl: unknown property {prop.name!r} (group {prop.group.value})")
    setting = _duvc.PropSetting(int(value), _duvc.CamMode.Manual)
    if prop.group is UvcGroup.CAMERA:
        result = cam.set(enum_val, setting)
    else:
        # The Camera class only exposes ``set`` for CamProp; VidProp set goes
        # through the module-level ``set_video_property``.
        result = _duvc.set_video_property(cam.device, enum_val, setting)
    if not result.is_ok():
        err = result.error()
        raise AdapterError(f"duvc-ctl: set {prop.name} = {value} failed: {err.description()}")


def _set_property_auto(cam: Any, prop: UvcProperty, enable: bool) -> None:
    if _duvc is None:
        raise AdapterError("duvc-ctl not available on this platform")
    enum_val = _resolve_prop_enum(prop)
    if enum_val is None:
        raise AdapterError(f"duvc-ctl: unknown property {prop.name!r} (group {prop.group.value})")
    mode = _duvc.CamMode.Auto if enable else _duvc.CamMode.Manual
    # Reading the current value first preserves it under the new mode —
    # toggling Auto on/off without a value erases the manual setpoint on
    # some webcams (observed: Logitech C930e exposure resets to 0 on
    # Auto→Manual round-trip without an intervening value).
    current = _get_property_state(cam, prop)
    value = current.value if current is not None else 0
    setting = _duvc.PropSetting(int(value), mode)
    if prop.group is UvcGroup.CAMERA:
        result = cam.set(enum_val, setting)
    else:
        result = _duvc.set_video_property(cam.device, enum_val, setting)
    if not result.is_ok():
        err = result.error()
        raise AdapterError(f"duvc-ctl: set {prop.name} auto={enable} failed: {err.description()}")


__all__ = [
    "AUTO_VERB_TO_PROPERTY",
    "CAPABILITY_REQUIREMENTS",
    "PROPERTY_BY_VERB",
    "UvcController",
    "UvcGroup",
    "UvcProperty",
    "UvcPropertyRange",
    "UvcPropertyState",
]
