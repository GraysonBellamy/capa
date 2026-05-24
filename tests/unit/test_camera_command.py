"""``Camera.command`` Protocol surface — webcam rejection + FLIR sim verbs.

The :class:`Camera` Protocol carries a generic ``command(cmd) -> CommandResult``
method. Webcam declares no control verbs and gate-rejects everything; FlirIrSim
mirrors :class:`capa_flir.flir_ir.FlirIrAdapter`'s dispatch table so recipes
targeting the real camera validate identically against sim.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from capa.core.clock import RunClock
from capa.devices.adapter import DeviceCommand
from capa.devices.camera.base import CameraCapability, CameraSpec
from capa.devices.camera.webcam import WebcamAdapter
from capa.devices.sim.flir_ir_sim import FlirIrSim

pytestmark = pytest.mark.anyio


def _ir_spec(name: str = "ir_cam0", **overrides: object) -> CameraSpec:
    base: dict[str, object] = {
        "name": name,
        "adapter": "capa.devices.sim.flir_ir_sim",
        "kind": "ir",
    }
    base.update(overrides)
    return CameraSpec.model_validate(base)


def _visible_spec(name: str = "visible_cam0") -> CameraSpec:
    return CameraSpec.model_validate(
        {
            "name": name,
            "adapter": "capa.devices.camera.webcam",
            "kind": "visible",
        }
    )


def _authorized(kind: str, **payload: object) -> DeviceCommand:
    """Build a command with the authorization fields populated."""
    return DeviceCommand(
        kind=kind,
        payload=payload,
        issued_by="op-1",
        authorization_id="auth-test",
        confirmed_by="op-1",
    )


def _unauthorized(kind: str, **payload: object) -> DeviceCommand:
    """Build a command missing both authorization_id and confirmed_by."""
    return DeviceCommand(
        kind=kind,
        payload=payload,
        issued_by="op-1",
    )


# ---------------------------------------------------------------------------
# Webcam: no control surface, but gate ordering must match the IR family.
# ---------------------------------------------------------------------------


class TestWebcamCommand:
    """Visible-camera adapter dispatch. Gate ordering: auth → not-open →
    capability/recording-state → verb-table. A misrouted command surfaces
    the most informative rejection first.
    """

    def _make(self) -> WebcamAdapter:
        return WebcamAdapter(
            spec=_visible_spec(),
            clock=RunClock.now(),
            fps=30.0,
            width=64,
            height=48,
            codec="mpeg4",
        )

    async def test_unauthorized_rejected_before_open_check(self) -> None:
        cam = self._make()
        # Not opened — but the auth gate runs first, so we should see the
        # auth refusal, not the not-open refusal.
        result = await cam.command(_unauthorized("anything"))
        assert result.accepted is False
        assert "unauthorized" in result.detail.lower() or "refuses" in result.detail.lower()

    async def test_not_open_rejected(self) -> None:
        cam = self._make()
        result = await cam.command(_authorized("anything"))
        assert result.accepted is False
        assert "not open" in result.detail

    async def test_unknown_verb_rejected_when_open(self) -> None:
        cam = self._make()
        await cam.open()
        try:
            result = await cam.command(_authorized("set_warp_drive", level=11))
            assert result.accepted is False
            assert "unknown verb" in result.detail
            assert "set_warp_drive" in result.detail
        finally:
            await cam.close()

    async def test_stream_format_set_resolution_accepted(self) -> None:
        cam = self._make()
        await cam.open()
        try:
            result = await cam.command(_authorized("set_resolution", width=1920, height=1080))
            assert result.accepted is True
            assert cam._width == 1920
            assert cam._height == 1080
        finally:
            await cam.close()

    async def test_stream_format_set_framerate_accepted(self) -> None:
        cam = self._make()
        await cam.open()
        try:
            result = await cam.command(_authorized("set_framerate", fps=60.0))
            assert result.accepted is True
            assert cam._fps == 60.0
        finally:
            await cam.close()

    async def test_stream_format_rejects_zero(self) -> None:
        cam = self._make()
        await cam.open()
        try:
            result = await cam.command(_authorized("set_framerate", fps=0.0))
            assert result.accepted is False
            assert "fps must be > 0" in result.detail
        finally:
            await cam.close()

    async def test_uvc_verb_without_controller_rejects(self) -> None:
        """No real duvc-ctl device match in a test env → set_exposure rejects
        cleanly with "UVC controls unavailable" rather than crashing."""
        cam = self._make()
        await cam.open()
        try:
            # Force the unattached state for determinism — open() may or
            # may not have matched a device depending on test host.
            cam._uvc = None
            result = await cam.command(_authorized("set_exposure", value=-6))
            assert result.accepted is False
            assert "UVC controls unavailable" in result.detail
        finally:
            await cam.close()


class TestWebcamCapabilityBaseline:
    """The static base capability set the WebcamAdapter advertises before
    duvc-ctl probing. Hardware tests cover the dynamic augmentation path."""

    async def test_baseline_includes_stream_format(self) -> None:
        cam = WebcamAdapter(
            spec=_visible_spec(),
            clock=RunClock.now(),
            fps=30.0,
            width=64,
            height=48,
            codec="mpeg4",
        )
        assert CameraCapability.STREAM_FORMAT in cam.capabilities
        assert CameraCapability.LIVE_PREVIEW in cam.capabilities
        assert CameraCapability.SUPPORTS_DISCOVERY in cam.capabilities


# ---------------------------------------------------------------------------
# FLIR sim: full dispatch table parity with capa-flir's _dispatch_command.
# ---------------------------------------------------------------------------


def _make_sim() -> FlirIrSim:
    return FlirIrSim(spec=_ir_spec(), clock=RunClock.now())


class TestFlirIrSimCapabilities:
    def test_advertises_all_control_flags(self) -> None:
        for flag in (
            CameraCapability.NUC_TRIGGER,
            CameraCapability.RADIOMETRIC_PARAMS,
            CameraCapability.TEMPERATURE_RANGE_SELECT,
            CameraCapability.AUTO_NUC_INTERVAL,
            CameraCapability.REMOTE_PALETTE,
        ):
            assert flag in FlirIrSim.capabilities, flag.name


class TestFlirIrSimGateOrdering:
    async def test_unauthorized_rejected(self) -> None:
        sim = _make_sim()
        await sim.open()
        try:
            result = await sim.command(_unauthorized("trigger_nuc"))
            assert result.accepted is False
            assert "refuses" in result.detail.lower() or "unauthorized" in result.detail.lower()
            assert sim._nuc_count == 0  # state untouched
        finally:
            await sim.close()

    async def test_not_open_rejected(self) -> None:
        sim = _make_sim()
        result = await sim.command(_authorized("trigger_nuc"))
        assert result.accepted is False
        assert "not open" in result.detail

    async def test_unknown_verb_rejected(self) -> None:
        sim = _make_sim()
        await sim.open()
        try:
            result = await sim.command(_authorized("set_warp_drive", level=11))
            assert result.accepted is False
            assert "unknown camera command kind" in result.detail
        finally:
            await sim.close()


class TestFlirIrSimRadiometricVerbs:
    async def test_set_emissivity(self) -> None:
        sim = _make_sim()
        await sim.open()
        try:
            result = await sim.command(_authorized("set_emissivity", emissivity=0.62))
            assert result.accepted is True
            assert sim._emissivity == 0.62
        finally:
            await sim.close()

    @pytest.mark.parametrize("bad", [0.0, -0.1, 1.5])
    async def test_set_emissivity_out_of_range(self, bad: float) -> None:
        sim = _make_sim()
        await sim.open()
        try:
            result = await sim.command(_authorized("set_emissivity", emissivity=bad))
            assert result.accepted is False
            assert "emissivity must be in" in result.detail
        finally:
            await sim.close()

    async def test_set_atmospheric_temp(self) -> None:
        sim = _make_sim()
        await sim.open()
        try:
            result = await sim.command(_authorized("set_atmospheric_temp", temperature_c=12.5))
            assert result.accepted is True
            assert sim._atmospheric_temp_c == 12.5
        finally:
            await sim.close()

    async def test_set_reflected_temp(self) -> None:
        sim = _make_sim()
        await sim.open()
        try:
            result = await sim.command(_authorized("set_reflected_temp", temperature_c=22.0))
            assert result.accepted is True
            assert sim._reflected_temp_c == 22.0
        finally:
            await sim.close()

    async def test_set_distance_m(self) -> None:
        sim = _make_sim()
        await sim.open()
        try:
            result = await sim.command(_authorized("set_distance_m", distance_m=2.5))
            assert result.accepted is True
            assert sim._distance_m == 2.5
        finally:
            await sim.close()

    async def test_set_distance_zero_rejected(self) -> None:
        sim = _make_sim()
        await sim.open()
        try:
            result = await sim.command(_authorized("set_distance_m", distance_m=0.0))
            assert result.accepted is False
            assert "must be > 0 meters" in result.detail
        finally:
            await sim.close()

    async def test_set_relative_humidity(self) -> None:
        sim = _make_sim()
        await sim.open()
        try:
            result = await sim.command(_authorized("set_relative_humidity", relative_humidity=0.42))
            assert result.accepted is True
            assert sim._relative_humidity == 0.42
        finally:
            await sim.close()

    async def test_set_relative_humidity_percent_misuse(self) -> None:
        """Catch the classic 'sent percent, SDK wanted fraction' mistake.
        The error message must mention the fraction-vs-percent distinction
        so an operator can self-diagnose without reading source."""
        sim = _make_sim()
        await sim.open()
        try:
            result = await sim.command(_authorized("set_relative_humidity", relative_humidity=42.0))
            assert result.accepted is False
            assert "fraction" in result.detail and "percent" in result.detail
        finally:
            await sim.close()

    async def test_set_atmospheric_transmission(self) -> None:
        sim = _make_sim()
        await sim.open()
        try:
            result = await sim.command(
                _authorized("set_atmospheric_transmission", transmission=0.98)
            )
            assert result.accepted is True
            assert sim._atmospheric_transmission == 0.98
        finally:
            await sim.close()


class TestFlirIrSimNucVerbs:
    async def test_trigger_nuc_increments_counter(self) -> None:
        sim = _make_sim()
        await sim.open()
        try:
            assert sim._nuc_count == 0
            result = await sim.command(_authorized("trigger_nuc"))
            assert result.accepted is True
            assert sim._nuc_count == 1
            await sim.command(_authorized("trigger_nuc"))
            assert sim._nuc_count == 2
        finally:
            await sim.close()

    async def test_trigger_nuc_forbidden_during_recording(self, tmp_path: Path) -> None:
        sim = _make_sim()
        await sim.open()
        try:
            await sim.start_recording(tmp_path / "ir.csq")
            result = await sim.command(_authorized("trigger_nuc"))
            assert result.accepted is False
            assert "forbidden during recording" in result.detail
            assert sim._nuc_count == 0  # never incremented
        finally:
            await sim.close()

    async def test_set_auto_nuc_interval(self) -> None:
        sim = _make_sim()
        await sim.open()
        try:
            result = await sim.command(_authorized("set_auto_nuc_interval", seconds=30))
            assert result.accepted is True
            assert sim._auto_nuc_interval_s == 30
        finally:
            await sim.close()

    async def test_set_auto_nuc_interval_zero_disables(self) -> None:
        sim = _make_sim()
        await sim.open()
        try:
            await sim.command(_authorized("set_auto_nuc_interval", seconds=0))
            assert sim._auto_nuc_interval_s == 0
        finally:
            await sim.close()

    async def test_set_auto_nuc_interval_negative_rejected(self) -> None:
        sim = _make_sim()
        await sim.open()
        try:
            result = await sim.command(_authorized("set_auto_nuc_interval", seconds=-1))
            assert result.accepted is False
            assert "must be >= 0" in result.detail
        finally:
            await sim.close()


class TestFlirIrSimTemperatureRange:
    async def test_set_temperature_range_valid_index(self) -> None:
        sim = _make_sim()
        await sim.open()
        try:
            result = await sim.command(_authorized("set_temperature_range", index=1))
            assert result.accepted is True
            assert sim._temperature_range_index == 1
        finally:
            await sim.close()

    async def test_set_temperature_range_out_of_bounds(self) -> None:
        sim = _make_sim()
        await sim.open()
        try:
            result = await sim.command(_authorized("set_temperature_range", index=99))
            assert result.accepted is False
            assert "out of bounds" in result.detail
        finally:
            await sim.close()

    async def test_set_temperature_range_forbidden_during_recording(self, tmp_path: Path) -> None:
        sim = _make_sim()
        await sim.open()
        try:
            await sim.start_recording(tmp_path / "ir.csq")
            result = await sim.command(_authorized("set_temperature_range", index=1))
            assert result.accepted is False
            assert "forbidden during recording" in result.detail
            assert sim._temperature_range_index == 0  # state untouched
        finally:
            await sim.close()


class TestFlirIrSimPaletteVerbs:
    async def test_set_remote_palette(self) -> None:
        sim = _make_sim()
        await sim.open()
        try:
            result = await sim.command(_authorized("set_remote_palette", palette="rainbow"))
            assert result.accepted is True
            assert sim._remote_palette == "rainbow"
        finally:
            await sim.close()

    async def test_set_remote_palette_unknown_rejected(self) -> None:
        sim = _make_sim()
        await sim.open()
        try:
            result = await sim.command(_authorized("set_remote_palette", palette="puce"))
            assert result.accepted is False
            assert "unknown remote palette" in result.detail
        finally:
            await sim.close()

    async def test_set_preview_palette(self) -> None:
        sim = _make_sim()
        await sim.open()
        try:
            result = await sim.command(_authorized("set_preview_palette", palette="lava"))
            assert result.accepted is True
            assert sim._preview_palette == "lava"
        finally:
            await sim.close()

    async def test_set_preview_palette_unknown_rejected(self) -> None:
        sim = _make_sim()
        await sim.open()
        try:
            result = await sim.command(_authorized("set_preview_palette", palette="puce"))
            assert result.accepted is False
            assert "unknown preview palette" in result.detail
        finally:
            await sim.close()
