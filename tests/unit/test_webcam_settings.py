"""Webcam declarative settings (:mod:`capa.devices.camera.webcam.settings`)
and the adapter read-back they are planned against."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from capa.config import ConfigDocument, ConfigProblem, validate
from capa.core.clock import RunClock
from capa.devices.adapter import DeviceCommand
from capa.devices.camera import _uvc
from capa.devices.camera._uvc import (
    AUTO_VERB_TO_PROPERTY,
    PROPERTY_BY_VERB,
    UvcProperty,
    UvcPropertyRange,
    UvcPropertyState,
)
from capa.devices.camera.base import (
    CameraCapability,
    CameraSpec,
    WebcamControlState,
    WebcamStateSnapshot,
)
from capa.devices.camera.webcam import adapter as webcam_adapter
from capa.devices.camera.webcam.adapter import WebcamAdapter
from capa.devices.camera.webcam.settings import AUTO, WEBCAM_SETTINGS, WebcamSettings
from capa.devices.registry import _import_builtins
from capa.devices.settings import capture_settings, plan_settings

pytestmark = pytest.mark.anyio

_AUTO_CONTROLS = ("focus", "exposure", "white_balance")


def _control(value: int | None, low: int, high: int, step: int = 1, **kw: Any) -> Any:
    return WebcamControlState(value=value, minimum=low, maximum=high, step=step, **kw)


def _snapshot(**overrides: WebcamControlState) -> WebcamStateSnapshot:
    controls = {
        "zoom": _control(100, 100, 500),
        "pan": _control(0, -36000, 36000, 3600),
        "tilt": _control(0, -36000, 36000, 3600),
        "focus": _control(0, 0, 250, 5, auto=False),
        "exposure": _control(-5, -11, -2, auto=True),
        "white_balance": _control(4000, 2000, 6500, 10, auto=False),
        "gain": _control(0, 0, 255),
    }
    controls.update(overrides)
    return WebcamStateSnapshot(controls=controls)


def _plan(snapshot: WebcamStateSnapshot | None = None, **desired: Any) -> Any:
    return plan_settings(
        WEBCAM_SETTINGS, WebcamSettings(**desired), snapshot if snapshot else _snapshot()
    )


# ---------------------------------------------------------------------------
# The spec
# ---------------------------------------------------------------------------


def test_every_adapter_control_is_declarable() -> None:
    names = {verb.removeprefix("set_") for verb in [*PROPERTY_BY_VERB, *AUTO_VERB_TO_PROPERTY]}
    assert {f.name for f in WEBCAM_SETTINGS.fields} == names
    assert [f.name for f in WEBCAM_SETTINGS.fields] == list(WebcamSettings.model_fields)


def test_zoom_is_applied_before_pan_and_tilt_and_auto_before_its_value() -> None:
    order = [f.name for f in WEBCAM_SETTINGS.fields]
    assert order.index("zoom") < order.index("pan") < order.index("tilt")
    for control in _AUTO_CONTROLS:
        assert order.index(f"auto_{control}") == order.index(control) - 1


@pytest.mark.parametrize("control", _AUTO_CONTROLS)
def test_auto_on_with_a_value_is_rejected(control: str) -> None:
    with pytest.raises(ValidationError, match=f"auto_{control} is true but {control} is set"):
        WebcamSettings.model_validate({f"auto_{control}": True, control: 10})
    # Auto off with a value, or a value alone, is fine.
    WebcamSettings.model_validate({f"auto_{control}": False, control: 10})
    WebcamSettings.model_validate({control: 10})


@pytest.mark.parametrize("value", [True, -6.5, "auto"])
def test_values_are_strict_ints(value: object) -> None:
    with pytest.raises(ValidationError):
        WebcamSettings.model_validate({"exposure": value})


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def test_matching_values_need_no_change() -> None:
    assert _plan(zoom=100, tilt=0, auto_exposure=True, white_balance=4000) == ((), ())


def test_changes_carry_the_command_payloads() -> None:
    changes, issues = _plan(zoom=265, tilt=3600, auto_focus=True, gain=10, white_balance=4500)
    assert issues == ()
    assert [(c.kind, dict(c.payload)) for c in changes] == [
        ("set_zoom", {"value": 265}),
        ("set_tilt", {"value": 3600}),
        ("set_auto_focus", {"enable": True}),
        ("set_white_balance", {"value": 4500}),
        ("set_gain", {"value": 10}),
    ]
    assert [(c.current, c.desired) for c in changes][2:4] == [("off", "on"), ("4000 K", "4500 K")]


def test_a_control_in_auto_reads_as_auto() -> None:
    (change,), _ = _plan(exposure=-6)
    assert (change.current, change.desired) == (AUTO, "-6")
    assert change.kind == "set_exposure"


def test_an_out_of_range_value_is_an_issue() -> None:
    changes, issues = _plan(zoom=600)
    assert changes == ()
    assert [(i.field, i.message) for i in issues] == [
        ("zoom", "600 is outside the camera's range, 100 to 500")
    ]


def test_an_off_step_value_is_an_issue_naming_the_nearest() -> None:
    _, issues = _plan(tilt=5)
    assert issues[0].message == (
        "the camera takes -36000 to 36000 in steps of 3600; nearest is 0 or 3600"
    )
    _, issues = _plan(tilt=35000)
    assert issues[0].message.endswith("nearest is 32400 or 36000")


def test_a_control_the_camera_lacks_is_an_issue() -> None:
    changes, issues = _plan(digital_zoom=2, auto_white_balance=True)
    assert [c.field for c in changes] == ["auto_white_balance"]
    assert [(i.field, i.message) for i in issues] == [
        ("digital_zoom", "the camera doesn't have this control")
    ]


def test_unavailable_controls_make_every_declared_setting_an_issue() -> None:
    snapshot = WebcamStateSnapshot(unavailable="camera controls are only available on Windows")
    changes, issues = _plan(snapshot, zoom=265, auto_exposure=False)
    assert changes == ()
    assert {i.message for i in issues} == {"camera controls are only available on Windows"}
    assert len(issues) == 2


def test_a_failed_read_counts_as_a_change() -> None:
    (change,), _ = _plan(_snapshot(zoom=_control(None, 100, 500)), zoom=265)
    assert change.current is None


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def test_capture_leaves_out_values_in_auto_and_round_trips() -> None:
    captured = capture_settings(WEBCAM_SETTINGS, _snapshot())
    assert captured == {
        "zoom": 100,
        "pan": 0,
        "tilt": 0,
        "auto_focus": False,
        "focus": 0,
        "auto_exposure": True,
        "auto_white_balance": False,
        "white_balance": 4000,
        "gain": 0,
    }
    desired = WebcamSettings.model_validate(captured)
    assert plan_settings(WEBCAM_SETTINGS, desired, _snapshot()) == ((), ())


def test_capture_of_unavailable_controls_is_empty() -> None:
    snapshot = WebcamStateSnapshot(unavailable="duvc-ctl isn't installed")
    assert capture_settings(WEBCAM_SETTINGS, snapshot) == {}


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def _settings_problems(configs_dir: Path, entry: object) -> list[ConfigProblem]:
    _import_builtins()
    doc = ConfigDocument.load(configs_dir / "experiments" / "sim_capa_pyrolysis.yaml")
    doc.hardware_payload["cameras"].append(
        {"name": "visible_cam0", "adapter": "capa.devices.camera.webcam", "kind": "visible"}
    )
    doc.experiment_payload["device_settings"]["visible_cam0"] = entry
    return [p for p in validate(doc) if p.section == "device_settings"]


def test_a_webcam_entry_validates(configs_dir: Path) -> None:
    entry = {"zoom": 265, "tilt": 3600, "auto_exposure": False, "exposure": -6}
    assert _settings_problems(configs_dir, entry) == []


def test_a_webcam_entry_with_auto_and_value_is_an_error(configs_dir: Path) -> None:
    (problem,) = _settings_problems(configs_dir, {"auto_focus": True, "focus": 50})
    assert problem.code == "device_settings.value_error"
    assert problem.path == ("device_settings", "visible_cam0")


def test_a_webcam_entry_rejects_stream_format(configs_dir: Path) -> None:
    (problem,) = _settings_problems(configs_dir, {"resolution": [1920, 1080]})
    assert problem.code == "device_settings.extra_forbidden"


# ---------------------------------------------------------------------------
# The adapter's read-back, against a fake duvc-ctl controller
# ---------------------------------------------------------------------------


class _FakeUvc:
    """Stands in for :class:`UvcController`: holds each property's state
    and changes it the way duvc-ctl does (a value means manual mode)."""

    device_name = "Logitech Webcam C930e"

    def __init__(
        self,
        states: dict[str, UvcPropertyState],
        ranges: dict[str, UvcPropertyRange],
        unreadable: frozenset[str] = frozenset(),
    ) -> None:
        self.states = states
        self.ranges = ranges
        self.unreadable = unreadable
        self.calls: list[tuple[str, object]] = []

    @classmethod
    async def find(cls, **_selectors: object) -> _FakeUvc | None:
        return _found

    async def probe_capabilities(self) -> frozenset[CameraCapability]:
        return frozenset({CameraCapability.ZOOM_CONTROL}) if self.states else frozenset()

    def supports(self, prop: UvcProperty) -> bool:
        return prop.name in self.states

    def get_cached_range(self, prop: UvcProperty) -> UvcPropertyRange | None:
        return self.ranges.get(prop.name)

    async def get(self, prop: UvcProperty) -> UvcPropertyState | None:
        if prop.name in self.unreadable:
            return None
        return self.states[prop.name]

    async def set_value(self, prop: UvcProperty, value: int) -> None:
        self.calls.append((prop.name, value))
        self.states[prop.name] = UvcPropertyState(value=value, auto=False)

    async def set_auto(self, prop: UvcProperty, enable: bool) -> None:
        self.calls.append((f"auto {prop.name}", enable))
        self.states[prop.name] = UvcPropertyState(value=self.states[prop.name].value, auto=enable)

    def close(self) -> None:
        pass


_found: _FakeUvc | None = None


def _c930e(**kw: Any) -> _FakeUvc:
    def rng(low: int, high: int, step: int, default: int) -> UvcPropertyRange:
        return UvcPropertyRange(minimum=low, maximum=high, step=step, default=default)

    return _FakeUvc(
        states={
            "Zoom": UvcPropertyState(value=100, auto=False),
            "Pan": UvcPropertyState(value=0, auto=False),
            "Tilt": UvcPropertyState(value=0, auto=False),
            "Exposure": UvcPropertyState(value=-5, auto=True),
            "Focus": UvcPropertyState(value=0, auto=True),
            "Brightness": UvcPropertyState(value=128, auto=False),
        },
        ranges={
            "Zoom": rng(100, 500, 1, 100),
            "Pan": rng(-36000, 36000, 3600, 0),
            "Tilt": rng(-36000, 36000, 3600, 0),
            "Exposure": rng(-11, -2, 1, -5),
            "Focus": rng(0, 250, 5, 0),
            "Brightness": rng(0, 255, 1, 128),
        },
        **kw,
    )


def _webcam(**selectors: str) -> WebcamAdapter:
    spec = CameraSpec.model_validate(
        {"name": "visible_cam0", "adapter": "capa.devices.camera.webcam", "kind": "visible"}
        | selectors
    )
    # ``v4l2`` keeps open() off the dshow format probe, whatever the host.
    return WebcamAdapter(
        spec=spec,
        clock=RunClock.now(),
        codec="mpeg4",
        width=64,
        height=48,
        input_format="v4l2",
        input_url="/dev/null",
    )


@pytest.fixture
def on_windows(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """open() as on Windows with duvc-ctl, finding whatever ``_found`` holds."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(webcam_adapter, "uvc_backend_available", lambda: True)
    monkeypatch.setattr(webcam_adapter, "UvcController", _FakeUvc)
    return monkeypatch


def _find(monkeypatch: pytest.MonkeyPatch, found: _FakeUvc | None) -> None:
    monkeypatch.setattr(sys.modules[__name__], "_found", found)


async def test_no_read_back_before_open() -> None:
    assert await _webcam().read_state_snapshot() is None


async def test_the_read_back_reports_supported_controls_live(
    on_windows: pytest.MonkeyPatch,
) -> None:
    _find(on_windows, _c930e(unreadable=frozenset({"Brightness"})))
    cam = _webcam()
    await cam.open()
    try:
        snapshot = await cam.read_state_snapshot()
    finally:
        await cam.close()
    assert snapshot is not None
    assert snapshot.unavailable is None
    assert set(snapshot.controls) == {"zoom", "pan", "tilt", "exposure", "focus", "brightness"}
    assert snapshot.controls["tilt"] == WebcamControlState(
        value=0, auto=None, minimum=-36000, maximum=36000, step=3600
    )
    assert snapshot.controls["exposure"].auto is True  # only the three have a mode
    assert snapshot.controls["zoom"].auto is None
    assert snapshot.controls["brightness"].value is None  # the live read failed


async def test_off_windows_the_read_back_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    cam = _webcam()
    await cam.open()
    try:
        snapshot = await cam.read_state_snapshot()
        refused = await cam.command(
            DeviceCommand(
                kind="set_zoom", payload={"value": 265}, issued_by="op", confirmed_by="op"
            )
        )
    finally:
        await cam.close()
    assert snapshot == WebcamStateSnapshot(
        unavailable="camera controls are only available on Windows"
    )
    assert not refused.accepted
    assert "UVC controls unavailable (camera controls are only available on Windows)" in (
        refused.detail
    )


@pytest.mark.parametrize(
    ("selectors", "reason"),
    [
        ({"model_hint": "C930e"}, "duvc-ctl found no camera matching model_hint 'C930e'"),
        ({"serial": "ABC"}, "duvc-ctl found no camera with serial 'ABC'"),
        ({}, "duvc-ctl found no camera, or several and no model_hint or serial"),
    ],
)
async def test_an_unmatched_camera_says_which_selector_missed(
    on_windows: pytest.MonkeyPatch, selectors: dict[str, str], reason: str
) -> None:
    _find(on_windows, None)
    cam = _webcam(**selectors)
    await cam.open()
    try:
        snapshot = await cam.read_state_snapshot()
    finally:
        await cam.close()
    assert snapshot is not None
    assert snapshot.unavailable is not None
    assert snapshot.unavailable.startswith(reason)


async def test_a_camera_without_controls_says_so(on_windows: pytest.MonkeyPatch) -> None:
    _find(on_windows, _FakeUvc(states={}, ranges={}))
    cam = _webcam()
    await cam.open()
    try:
        snapshot = await cam.read_state_snapshot()
    finally:
        await cam.close()
    assert snapshot is not None
    assert snapshot.unavailable == "Logitech Webcam C930e reports no adjustable controls"


async def test_declared_settings_apply_to_the_camera(on_windows: pytest.MonkeyPatch) -> None:
    """Plan → dispatch → read back through the adapter: nothing left."""
    uvc = _c930e()
    _find(on_windows, uvc)
    cam = _webcam()
    await cam.open()
    try:
        desired = WebcamSettings(
            zoom=265, tilt=3600, auto_exposure=False, exposure=-6, auto_focus=False
        )
        before = await cam.read_state_snapshot()
        changes, issues = plan_settings(WEBCAM_SETTINGS, desired, before)
        assert issues == ()
        for change in changes:
            result = await cam.command(
                DeviceCommand(
                    kind=change.kind,
                    payload=dict(change.payload),
                    issued_by="op",
                    confirmed_by="op",
                )
            )
            assert result.accepted, result.detail
        after = await cam.read_state_snapshot()
        assert plan_settings(WEBCAM_SETTINGS, desired, after) == ((), ())
    finally:
        await cam.close()
    assert uvc.calls == [
        ("Zoom", 265),
        ("Tilt", 3600),
        ("auto Focus", False),
        ("auto Exposure", False),
        ("Exposure", -6),
    ]


def test_the_uvc_backend_flag_reflects_the_import() -> None:
    assert _uvc.uvc_backend_available() is (_uvc._duvc is not None)
