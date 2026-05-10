""":mod:`capa.devices.camera._uvc` — duvc-ctl wrapper.

The real duvc-ctl is Windows-only and requires a connected UVC camera;
these tests build a fake module surface that mirrors the Result-Based API
so the wrapper's mapping logic (capability flags ↔ supported properties,
verb table ↔ duvc enum, Result unwrap ↔ AdapterError) is exercised on
any platform.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from typing import Any

import pytest

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Fake duvc-ctl module + helpers.
#
# Pinned to the exact surface :mod:`capa.devices.camera._uvc` calls into:
# list_devices(), open_camera(device), get_device_capabilities(device),
# CamProp.*, VidProp.*, CamMode.Auto/Manual, PropSetting, and Camera with
# .get / .get_video_property / .get_range / .set / .device.
# ---------------------------------------------------------------------------


@dataclass
class _FakeError:
    message: str

    def description(self) -> str:
        return self.message


class _FakeResult:
    """Minimal Result type — is_ok / value / error."""

    def __init__(self, value: Any = None, error: _FakeError | None = None) -> None:
        self._value = value
        self._error = error

    def is_ok(self) -> bool:
        return self._error is None

    def value(self) -> Any:
        return self._value

    def error(self) -> _FakeError:
        assert self._error is not None
        return self._error


class _FakeDevice:
    def __init__(self, name: str, path: str) -> None:
        self.name = name
        self.path = path


class _FakePropSetting:
    def __init__(self, value: int, mode: Any) -> None:
        self.value = value
        self.mode = mode


class _FakePropRange:
    def __init__(self, mn: int, mx: int, step: int, default_val: int, default_mode: Any) -> None:
        self.min = mn
        self.max = mx
        self.step = step
        self.default_val = default_val
        self.default_mode = default_mode


class _FakePropertyCapability:
    def __init__(
        self,
        supported: bool,
        range: _FakePropRange | None = None,
        current: _FakePropSetting | None = None,
    ) -> None:
        self.supported = supported
        self.range = range
        self.current = current


class _FakeCameraEnum:
    """Mimics the duvc-ctl pybind11 enum surface — name + value
    attributes per member, member access via getattr."""

    Pan = "Pan"
    Tilt = "Tilt"
    Zoom = "Zoom"
    DigitalZoom = "DigitalZoom"
    Exposure = "Exposure"
    Focus = "Focus"


class _FakeVideoEnum:
    Brightness = "Brightness"
    Contrast = "Contrast"
    Saturation = "Saturation"
    Sharpness = "Sharpness"
    Gamma = "Gamma"
    Hue = "Hue"
    Gain = "Gain"
    WhiteBalance = "WhiteBalance"
    BacklightCompensation = "BacklightCompensation"


class _FakeCamMode:
    Auto = "Auto"
    Manual = "Manual"


class _FakeDeviceCapabilities:
    def __init__(
        self,
        camera_supported: list[str],
        video_supported: list[str],
        video_ranges: dict[str, _FakePropRange] | None = None,
        camera_ranges: dict[str, _FakePropRange] | None = None,
        camera_currents: dict[str, int] | None = None,
        video_currents: dict[str, int] | None = None,
    ) -> None:
        # supported_camera_properties / supported_video_properties return
        # "duvc enum values" — we use strings since the wrapper only reads
        # .name off them via _FakeProp shim.
        self._camera_supported = [_FakeProp(n) for n in camera_supported]
        self._video_supported = [_FakeProp(n) for n in video_supported]
        self._camera_ranges = camera_ranges or {}
        self._video_ranges = video_ranges or {}
        self._camera_currents = camera_currents or {}
        self._video_currents = video_currents or {}

    def supported_camera_properties(self) -> list[Any]:
        return self._camera_supported

    def supported_video_properties(self) -> list[Any]:
        return self._video_supported

    @staticmethod
    def _prop_key(prop_enum: Any) -> str:
        """The real duvc-ctl method takes a CamProp / VidProp enum; the
        wrapper's new probe loop passes those enums through unchanged. The
        existing _get_property_range path passes a string from
        ``getattr(CamProp, name)``. Normalize both to the string name."""
        return prop_enum.name if hasattr(prop_enum, "name") else prop_enum

    def get_camera_capability(self, prop_enum: Any) -> _FakePropertyCapability | None:
        key = self._prop_key(prop_enum)
        rng = self._camera_ranges.get(key)
        current_val = self._camera_currents.get(key)
        current = (
            _FakePropSetting(current_val, _FakeCamMode.Manual)
            if current_val is not None
            else (_FakePropSetting(0, _FakeCamMode.Manual) if rng else None)
        )
        return _FakePropertyCapability(
            supported=rng is not None,
            range=rng,
            current=current,
        )

    def get_video_capability(self, prop_enum: Any) -> _FakePropertyCapability | None:
        key = self._prop_key(prop_enum)
        rng = self._video_ranges.get(key)
        current_val = self._video_currents.get(key)
        current = (
            _FakePropSetting(current_val, _FakeCamMode.Manual)
            if current_val is not None
            else (_FakePropSetting(0, _FakeCamMode.Manual) if rng else None)
        )
        return _FakePropertyCapability(
            supported=rng is not None,
            range=rng,
            current=current,
        )


class _FakeProp:
    """Stand-in for a duvc-ctl ``CamProp`` / ``VidProp`` enum value.

    The wrapper reads ``.name`` to bucket properties; equality and
    hash off ``.name`` so they round-trip through frozensets.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _FakeProp) and self.name == other.name

    def __hash__(self) -> int:
        return hash(self.name)


class _FakeCamera:
    """Stand-in for a duvc-ctl ``Camera`` handle."""

    def __init__(
        self,
        device: _FakeDevice,
        camera_props: dict[str, _FakePropSetting] | None = None,
        video_props: dict[str, _FakePropSetting] | None = None,
        ranges: dict[str, _FakePropRange] | None = None,
        set_fails: set[str] | None = None,
    ) -> None:
        self.device = device
        self._cam_props = camera_props or {}
        self._vid_props = video_props or {}
        self._ranges = ranges or {}
        self._set_fails = set_fails or set()
        self.set_calls: list[tuple[str, int, Any]] = []

    def get(self, prop_enum: str) -> _FakeResult:
        setting = self._cam_props.get(prop_enum)
        if setting is None:
            return _FakeResult(error=_FakeError(f"unsupported {prop_enum}"))
        return _FakeResult(value=setting)

    def get_video_property(self, prop_enum: str) -> _FakeResult:
        setting = self._vid_props.get(prop_enum)
        if setting is None:
            return _FakeResult(error=_FakeError(f"unsupported video {prop_enum}"))
        return _FakeResult(value=setting)

    def get_range(self, prop_enum: str) -> _FakeResult:
        r = self._ranges.get(prop_enum)
        if r is None:
            return _FakeResult(error=_FakeError("no range"))
        return _FakeResult(value=r)

    def set(self, prop_enum: str, setting: _FakePropSetting) -> _FakeResult:
        self.set_calls.append((prop_enum, setting.value, setting.mode))
        if prop_enum in self._set_fails:
            return _FakeResult(error=_FakeError(f"refused {prop_enum}"))
        self._cam_props[prop_enum] = setting
        return _FakeResult(value=None)


@pytest.fixture
def fake_duvc(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a fake ``duvc_ctl`` module on ``sys.modules`` so
    :mod:`capa.devices.camera._uvc`'s soft import grabs it on the next
    reimport. Also forces ``sys.platform == 'win32'`` so the platform
    guard accepts the module on Linux/macOS test runners."""
    mod: Any = types.ModuleType("duvc_ctl")
    mod.list_devices = lambda: []  # default; tests override
    mod.open_camera = lambda dev: _FakeResult(error=_FakeError("override me"))
    mod.get_device_capabilities = lambda dev: _FakeResult(error=_FakeError("override me"))
    mod.set_video_property = lambda dev, prop, setting: _FakeResult(value=None)
    mod.CamProp = _FakeCameraEnum
    mod.VidProp = _FakeVideoEnum
    mod.CamMode = _FakeCamMode
    mod.PropSetting = _FakePropSetting
    monkeypatch.setitem(sys.modules, "duvc_ctl", mod)
    monkeypatch.setattr(sys, "platform", "win32")
    # Force a fresh import of the wrapper so the new fake is bound to
    # ``_uvc._duvc``. Reimport order matters — the wrapper module caches
    # the duvc_ctl reference at module top.
    monkeypatch.delitem(sys.modules, "capa.devices.camera._uvc", raising=False)
    return mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCapabilityMapping:
    async def test_logitech_c930e_profile(self, fake_duvc: Any) -> None:
        """A device that advertises Pan/Tilt/Zoom/Exposure/Focus on the
        camera side and Brightness/Contrast/WB on the video side should
        produce: EXPOSURE_CONTROL, FOCUS_CONTROL, ZOOM_CONTROL,
        PAN_TILT_CONTROL, WB_CONTROL, IMAGE_ADJUST."""
        from capa.devices.camera._uvc import UvcController
        from capa.devices.camera.base import CameraCapability

        device = _FakeDevice(
            name="Logitech Webcam C930e",
            path=r"\\?\usb#vid_046d&pid_0843&mi_00",
        )
        fake_duvc.list_devices = lambda: [device]
        fake_duvc.open_camera = lambda d: _FakeResult(value=_FakeCamera(d))
        fake_duvc.get_device_capabilities = lambda d: _FakeResult(
            value=_FakeDeviceCapabilities(
                camera_supported=["Pan", "Tilt", "Zoom", "Exposure", "Focus"],
                video_supported=["Brightness", "Contrast", "WhiteBalance"],
            )
        )

        ctrl = await UvcController.find(model_hint="C930e", serial=None)
        assert ctrl is not None
        caps = await ctrl.probe_capabilities()
        assert CameraCapability.EXPOSURE_CONTROL in caps
        assert CameraCapability.FOCUS_CONTROL in caps
        assert CameraCapability.ZOOM_CONTROL in caps
        assert CameraCapability.PAN_TILT_CONTROL in caps
        assert CameraCapability.WB_CONTROL in caps
        assert CameraCapability.IMAGE_ADJUST in caps

    async def test_fixed_focus_no_zoom_webcam(self, fake_duvc: Any) -> None:
        """A built-in laptop cam with only Brightness/Contrast should NOT
        get FOCUS/ZOOM/PAN_TILT/EXPOSURE flags — only IMAGE_ADJUST."""
        from capa.devices.camera._uvc import UvcController
        from capa.devices.camera.base import CameraCapability

        device = _FakeDevice(name="Integrated Camera", path=r"\\?\some-path")
        fake_duvc.list_devices = lambda: [device]
        fake_duvc.open_camera = lambda d: _FakeResult(value=_FakeCamera(d))
        fake_duvc.get_device_capabilities = lambda d: _FakeResult(
            value=_FakeDeviceCapabilities(
                camera_supported=[],
                video_supported=["Brightness", "Contrast"],
            )
        )

        ctrl = await UvcController.find(model_hint=None, serial=None)
        assert ctrl is not None
        caps = await ctrl.probe_capabilities()
        assert CameraCapability.IMAGE_ADJUST in caps
        for flag in (
            CameraCapability.EXPOSURE_CONTROL,
            CameraCapability.FOCUS_CONTROL,
            CameraCapability.ZOOM_CONTROL,
            CameraCapability.PAN_TILT_CONTROL,
            CameraCapability.WB_CONTROL,
        ):
            assert flag not in caps, flag.name


class TestDeviceSelection:
    async def test_serial_substring_match(self, fake_duvc: Any) -> None:
        from capa.devices.camera._uvc import UvcController

        a = _FakeDevice(name="Cam A", path=r"\\?\usb#vid_046d&pid_0843#SERIAL_A123")
        b = _FakeDevice(name="Cam B", path=r"\\?\usb#vid_046d&pid_0843#SERIAL_B456")
        fake_duvc.list_devices = lambda: [a, b]
        fake_duvc.open_camera = lambda d: _FakeResult(value=_FakeCamera(d))

        ctrl = await UvcController.find(model_hint=None, serial="b456")
        assert ctrl is not None
        assert ctrl.device_name == "Cam B"

    async def test_model_hint_match(self, fake_duvc: Any) -> None:
        from capa.devices.camera._uvc import UvcController

        a = _FakeDevice(name="Logitech C920", path="p1")
        b = _FakeDevice(name="Logitech C930e", path="p2")
        fake_duvc.list_devices = lambda: [a, b]
        fake_duvc.open_camera = lambda d: _FakeResult(value=_FakeCamera(d))

        ctrl = await UvcController.find(model_hint="C930e", serial=None)
        assert ctrl is not None
        assert ctrl.device_name == "Logitech C930e"

    async def test_no_selector_multiple_cameras_returns_none(self, fake_duvc: Any) -> None:
        """Multiple cameras + no selector is ambiguous — adapter must not
        silently grab the first one."""
        from capa.devices.camera._uvc import UvcController

        fake_duvc.list_devices = lambda: [
            _FakeDevice(name="A", path="p1"),
            _FakeDevice(name="B", path="p2"),
        ]
        ctrl = await UvcController.find(model_hint=None, serial=None)
        assert ctrl is None

    async def test_no_selector_single_camera_succeeds(self, fake_duvc: Any) -> None:
        from capa.devices.camera._uvc import UvcController

        fake_duvc.list_devices = lambda: [_FakeDevice(name="Only", path="p")]
        fake_duvc.open_camera = lambda d: _FakeResult(value=_FakeCamera(d))
        ctrl = await UvcController.find(model_hint=None, serial=None)
        assert ctrl is not None
        assert ctrl.device_name == "Only"


class TestPropertySetSucceeds:
    async def test_set_camera_property_passes_manual_mode(self, fake_duvc: Any) -> None:
        from capa.devices.camera._uvc import UvcController, UvcGroup, UvcProperty

        device = _FakeDevice(name="X", path="p")
        cam = _FakeCamera(device)
        fake_duvc.list_devices = lambda: [device]
        fake_duvc.open_camera = lambda d: _FakeResult(value=cam)
        fake_duvc.get_device_capabilities = lambda d: _FakeResult(
            value=_FakeDeviceCapabilities(camera_supported=["Focus"], video_supported=[])
        )

        ctrl = await UvcController.find(model_hint=None, serial=None)
        assert ctrl is not None
        await ctrl.probe_capabilities()
        await ctrl.set_value(UvcProperty("Focus", UvcGroup.CAMERA), 250)
        assert cam.set_calls == [("Focus", 250, _FakeCamMode.Manual)]


class TestPropertySetFails:
    async def test_set_failure_raises_adapter_error(self, fake_duvc: Any) -> None:
        from capa.core.errors import AdapterError
        from capa.devices.camera._uvc import UvcController, UvcGroup, UvcProperty

        device = _FakeDevice(name="X", path="p")
        cam = _FakeCamera(device, set_fails={"Focus"})
        fake_duvc.list_devices = lambda: [device]
        fake_duvc.open_camera = lambda d: _FakeResult(value=cam)
        fake_duvc.get_device_capabilities = lambda d: _FakeResult(
            value=_FakeDeviceCapabilities(camera_supported=["Focus"], video_supported=[])
        )

        ctrl = await UvcController.find(model_hint=None, serial=None)
        assert ctrl is not None
        await ctrl.probe_capabilities()
        with pytest.raises(AdapterError, match="refused"):
            await ctrl.set_value(UvcProperty("Focus", UvcGroup.CAMERA), 250)


class TestVerbTables:
    def test_property_by_verb_covers_all_groups(self) -> None:
        from capa.devices.camera._uvc import PROPERTY_BY_VERB, UvcGroup

        camera_verbs = {v for v, p in PROPERTY_BY_VERB.items() if p.group is UvcGroup.CAMERA}
        video_verbs = {v for v, p in PROPERTY_BY_VERB.items() if p.group is UvcGroup.VIDEO}
        # Sanity: representative verbs land in the right group so the
        # adapter dispatch routes correctly.
        assert "set_exposure" in camera_verbs
        assert "set_focus" in camera_verbs
        assert "set_pan" in camera_verbs
        assert "set_brightness" in video_verbs
        assert "set_white_balance" in video_verbs
        assert "set_gain" in video_verbs

    def test_auto_verbs_subset_of_property_table(self) -> None:
        """Every auto-mode verb must reference a property that also has a
        numeric-set counterpart, so toggling auto off doesn't strand the
        operator without a way to pin a manual value."""
        from capa.devices.camera._uvc import (
            AUTO_VERB_TO_PROPERTY,
            PROPERTY_BY_VERB,
        )

        auto_props = {p.name for p in AUTO_VERB_TO_PROPERTY.values()}
        numeric_props = {p.name for p in PROPERTY_BY_VERB.values()}
        assert auto_props.issubset(numeric_props), (
            f"auto verbs reference properties with no numeric setter: {auto_props - numeric_props}"
        )


class TestCachedRangesAndCurrents:
    """``probe_capabilities`` populates per-property range + current caches
    off the same ``DeviceCapabilities`` snapshot so the UI can build spinbox
    bounds and seed initial values without paying a per-property DirectShow
    round-trip on every refresh."""

    async def test_camera_property_range_and_current_cached(self, fake_duvc: Any) -> None:
        from capa.devices.camera._uvc import UvcController, UvcGroup, UvcProperty

        device = _FakeDevice(name="X", path="p")
        fake_duvc.list_devices = lambda: [device]
        fake_duvc.open_camera = lambda d: _FakeResult(value=_FakeCamera(d))
        fake_duvc.get_device_capabilities = lambda d: _FakeResult(
            value=_FakeDeviceCapabilities(
                camera_supported=["Focus"],
                video_supported=[],
                camera_ranges={
                    "Focus": _FakePropRange(0, 250, 5, 100, _FakeCamMode.Manual),
                },
                camera_currents={"Focus": 175},
            )
        )

        ctrl = await UvcController.find(model_hint=None, serial=None)
        assert ctrl is not None
        await ctrl.probe_capabilities()

        rng = ctrl.get_cached_range(UvcProperty("Focus", UvcGroup.CAMERA))
        assert rng is not None
        assert rng.minimum == 0
        assert rng.maximum == 250
        assert rng.step == 5
        assert rng.default == 100
        assert ctrl.get_cached_current(UvcProperty("Focus", UvcGroup.CAMERA)) == 175

    async def test_video_property_range_cached(self, fake_duvc: Any) -> None:
        from capa.devices.camera._uvc import UvcController, UvcGroup, UvcProperty

        device = _FakeDevice(name="X", path="p")
        fake_duvc.list_devices = lambda: [device]
        fake_duvc.open_camera = lambda d: _FakeResult(value=_FakeCamera(d))
        fake_duvc.get_device_capabilities = lambda d: _FakeResult(
            value=_FakeDeviceCapabilities(
                camera_supported=[],
                video_supported=["Brightness"],
                video_ranges={
                    "Brightness": _FakePropRange(0, 255, 1, 128, _FakeCamMode.Manual),
                },
                video_currents={"Brightness": 200},
            )
        )

        ctrl = await UvcController.find(model_hint=None, serial=None)
        assert ctrl is not None
        await ctrl.probe_capabilities()

        rng = ctrl.get_cached_range(UvcProperty("Brightness", UvcGroup.VIDEO))
        assert rng is not None
        assert (rng.minimum, rng.maximum, rng.step, rng.default) == (0, 255, 1, 128)
        assert ctrl.get_cached_current(UvcProperty("Brightness", UvcGroup.VIDEO)) == 200

    async def test_unprobed_property_returns_none(self, fake_duvc: Any) -> None:
        from capa.devices.camera._uvc import UvcController, UvcGroup, UvcProperty

        device = _FakeDevice(name="X", path="p")
        fake_duvc.list_devices = lambda: [device]
        fake_duvc.open_camera = lambda d: _FakeResult(value=_FakeCamera(d))
        fake_duvc.get_device_capabilities = lambda d: _FakeResult(
            value=_FakeDeviceCapabilities(camera_supported=[], video_supported=[])
        )

        ctrl = await UvcController.find(model_hint=None, serial=None)
        assert ctrl is not None
        await ctrl.probe_capabilities()

        assert ctrl.get_cached_range(UvcProperty("Zoom", UvcGroup.CAMERA)) is None
        assert ctrl.get_cached_current(UvcProperty("Zoom", UvcGroup.CAMERA)) is None
