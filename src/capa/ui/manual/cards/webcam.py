""":class:`WebcamCard` — manual control card for visible-light UVC cameras.

Mirrors :class:`~capa.ui.manual.cards.camera.FlirCard` in shape but talks
to :class:`~capa.devices.camera.webcam.WebcamAdapter`'s UVC verbs (set via
the duvc-ctl wrapper). Section gating is on the granular
:class:`CameraCapability` flags the adapter probes at ``open()``:

* ``STREAM_FORMAT``       — resolution / framerate (applies on next start_recording)
* ``EXPOSURE_CONTROL``    — manual µs + auto-exposure toggle
* ``FOCUS_CONTROL``       — manual focus + AF toggle
* ``ZOOM_CONTROL``        — optical / digital zoom sliders
* ``WB_CONTROL``          — white-balance temperature + AWB toggle
* ``PAN_TILT_CONTROL``    — pan / tilt sliders (PTZ cameras only)
* ``IMAGE_ADJUST``        — brightness / contrast / saturation / sharpness / gamma / hue / gain / backlight

UVC properties have device-specific value ranges; we fetch them on first
open via :class:`UvcPropertyRange` and use them to bound the spinboxes.
When the live range isn't available (UVC controls absent on this device,
or the adapter isn't open yet) the spinbox falls back to a permissive
default range and lets the adapter reject out-of-range at command-time.

Same lifecycle as FlirCard: open lazily on first action, auto-close on
engine PREPARING so the engine can acquire the camera with its own
run-clock anchor.
"""

from __future__ import annotations

import asyncio
from typing import Final

import structlog
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QWidget,
)

from capa.core.clock import RunClock
from capa.devices.camera._uvc import PROPERTY_BY_VERB
from capa.devices.camera.base import Camera, CameraCapability, CameraSpec
from capa.devices.camera.webcam import WebcamAdapter
from capa.devices.registry import _SingleCameraConfig
from capa.experiment.cameras import construct_cameras
from capa.experiment.engine import EngineState
from capa.ui.async_util import schedule_bg
from capa.ui.manual.cards.base import CommandTarget, DeviceCard
from capa.ui.manual.cards.camera import _safe_close_camera
from capa.ui.state import RunController
from capa.ui.statusbar import OperatorIdProvider

_logger = structlog.get_logger("capa.ui.manual.webcam")


# Capability flags the WebcamCard renders a section for. A visible camera
# advertising none of these (e.g. duvc-ctl unavailable + STREAM_FORMAT
# alone) still gets a card so the operator can change resolution /
# framerate between runs.
RELEVANT_CAPABILITIES: Final[tuple[CameraCapability, ...]] = (
    CameraCapability.STREAM_FORMAT,
    CameraCapability.EXPOSURE_CONTROL,
    CameraCapability.FOCUS_CONTROL,
    CameraCapability.ZOOM_CONTROL,
    CameraCapability.WB_CONTROL,
    CameraCapability.PAN_TILT_CONTROL,
    CameraCapability.IMAGE_ADJUST,
)


# Fallback resolution set used only when the dshow ``list_options`` probe
# could not enumerate real device formats (non-Windows, the camera was
# constructed without being opened, the probe parse turned up empty).
# :meth:`WebcamAdapter.open` populates :attr:`WebcamAdapter.supported_resolutions`
# from the real device when it can, and :meth:`WebcamCard._refresh_controls_from_probe`
# rewrites the combo from those values on first open.
_FALLBACK_RESOLUTIONS: Final[tuple[tuple[int, int], ...]] = (
    (640, 480),
    (1280, 720),
    (1920, 1080),
)

# Same logic for framerates — the UVC negotiation will reject any fps the
# camera doesn't advertise for the chosen resolution.
COMMON_FRAMERATES: Final[tuple[float, ...]] = (15.0, 30.0, 60.0)


PREVIEW_TILE_WIDTH: Final[int] = 320
"""Width of the embedded preview tile in pixels. Matches the adapter's
:data:`PREVIEW_MAX_WIDTH` so JPEGs from :meth:`WebcamAdapter.preview_stream`
scale 1:1 — no extra resizing on the UI thread."""

PREVIEW_TILE_HEIGHT: Final[int] = 180
"""16:9 height for the preview tile. Frames with other aspect ratios are
centered in the tile rather than stretched."""


def is_webcam_camera(spec: CameraSpec) -> bool:
    """``True`` if this camera spec should render a :class:`WebcamCard`."""
    return spec.kind == "visible"


class WebcamCard(DeviceCard):
    """Per-webcam manual-control card.

    Capabilities are populated optimistically from the static base set;
    when the camera is opened, the live capability set narrows the
    sections to those duvc-ctl confirmed against the device. Sections
    that fall away after probing simply reject their dispatch with a
    clear "device does not support …" message — preferred over silently
    hiding the buttons because operators can then ask "wait, my C920 had
    pan/tilt, why is it gone?" instead of being confused.
    """

    def __init__(
        self,
        *,
        spec: CameraSpec,
        controller: RunController,
        operator_provider: OperatorIdProvider,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            name=spec.name,
            title=f"Webcam: {spec.name}",
            controller=controller,
            operator_provider=operator_provider,
            parent=parent,
        )
        self._spec: CameraSpec = spec
        self._capabilities: frozenset[CameraCapability] = _default_webcam_capabilities()
        self.set_subtitle(
            f"Camera: {spec.name}   Adapter: {spec.adapter.rsplit('.', 1)[-1]}   Kind: {spec.kind}"
        )
        # Preview tile lifecycle. The two background tasks run on the
        # qasync loop: ``_preview_pump_task`` drives the adapter's input
        # container; ``_preview_consumer_task`` drains preview_stream() and
        # paints into ``_preview_label``. Both cancel when the card leaves
        # IDLE (engine.PREPARING) so the engine can claim the camera.
        self._preview_label: QLabel = self._build_preview_tile()
        self._preview_pump_task: asyncio.Task[object] | None = None
        self._preview_consumer_task: asyncio.Task[object] | None = None
        # Live widget references and a latch so the probe-driven refresh
        # only runs once per card lifetime. Populated by section builders
        # and consumed by :meth:`_refresh_controls_from_probe`.
        self._resolution_combo: QComboBox | None = None
        self._fps_spin: QDoubleSpinBox | None = None
        self._spinboxes: dict[str, QSpinBox] = {}
        self._controls_initialized: bool = False
        # Kept in sync from :meth:`_refresh_controls_from_probe` so the
        # resolution-combo change handler can recompute the fps cap without
        # holding a reference to the WebcamAdapter.
        self._resolution_fps_caps: dict[tuple[int, int], float] = {}
        self._build_capability_sections()
        # Kick off the preview right after construction *when* an event loop
        # is running (production: qasync loop is up by the time MainWindow
        # builds the dock). In tests that build a card without spinning up
        # the loop, skip the kickoff — the coroutine would never be awaited
        # and Python would emit a RuntimeWarning. Production cards always
        # see a running loop here.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            schedule_bg(self._start_preview_session())

    # ------------------------------------------------------------------ build

    def _build_capability_sections(self) -> None:
        if CameraCapability.STREAM_FORMAT in self._capabilities:
            self._build_stream_format_section()
        if CameraCapability.EXPOSURE_CONTROL in self._capabilities:
            self._build_exposure_section()
        if CameraCapability.FOCUS_CONTROL in self._capabilities:
            self._build_focus_section()
        if CameraCapability.ZOOM_CONTROL in self._capabilities:
            self._build_zoom_section()
        if CameraCapability.WB_CONTROL in self._capabilities:
            self._build_wb_section()
        if CameraCapability.PAN_TILT_CONTROL in self._capabilities:
            self._build_pan_tilt_section()
        if CameraCapability.IMAGE_ADJUST in self._capabilities:
            self._build_image_adjust_section()

    def _build_stream_format_section(self) -> None:
        body = self.add_section("Stream format (next recording)")
        # Resolution
        row = QHBoxLayout()
        row.setSpacing(6)
        lbl = QLabel("Resolution:", self)
        lbl.setMinimumWidth(120)
        row.addWidget(lbl)
        combo = QComboBox(self)
        self._resolution_combo = combo
        for w, h in _FALLBACK_RESOLUTIONS:
            combo.addItem(f"{w}×{h}", userData=(w, h))
        combo.setToolTip(
            "Frame size for the next start_recording. UVC negotiates at "
            "encoder open — unsupported combos are rejected by the camera."
        )
        row.addWidget(combo)
        btn = QPushButton("Apply", self)

        def _apply_res() -> None:
            wh = combo.currentData()
            if not isinstance(wh, tuple) or len(wh) != 2:
                return
            self.schedule_dispatch(
                kind="set_resolution",
                payload={"width": int(wh[0]), "height": int(wh[1])},
            )

        btn.clicked.connect(_apply_res)
        row.addWidget(btn)
        row.addStretch(1)
        body.addLayout(row)
        for widget in (combo, btn):
            self.register_action_widget(widget)

        # Framerate
        row = QHBoxLayout()
        row.setSpacing(6)
        lbl = QLabel("Framerate (fps):", self)
        lbl.setMinimumWidth(120)
        row.addWidget(lbl)
        fps_spin = QDoubleSpinBox(self)
        fps_spin.setRange(1.0, 240.0)
        fps_spin.setDecimals(1)
        fps_spin.setSingleStep(1.0)
        fps_spin.setValue(30.0)
        fps_spin.setToolTip(
            "Target frames per second. Maximum is set from the camera's "
            "advertised cap for the selected resolution; changing resolution "
            "updates the cap."
        )
        self._fps_spin = fps_spin
        # Recompute fps cap whenever the resolution combo changes so the
        # max reflects the per-resolution rate the device advertised.
        combo.currentIndexChanged.connect(self._apply_fps_cap_for_current_resolution)
        row.addWidget(fps_spin)
        btn_fps = QPushButton("Apply", self)
        btn_fps.clicked.connect(
            lambda: self.schedule_dispatch(kind="set_framerate", payload={"fps": fps_spin.value()})
        )
        row.addWidget(btn_fps)
        row.addStretch(1)
        body.addLayout(row)
        for fps_widget in (fps_spin, btn_fps):
            self.register_action_widget(fps_widget)

    def _build_exposure_section(self) -> None:
        body = self.add_section("Exposure")
        # Auto toggle
        body.addLayout(
            self._auto_toggle_row(
                label="Auto exposure:",
                kind="set_auto_exposure",
                tooltip=(
                    "Toggle camera-driven auto-exposure. When off, exposure "
                    "value below is used. UVC exposure is a log2(seconds) int."
                ),
            )
        )
        # Manual value
        body.addLayout(
            self._int_value_row(
                label="Exposure value:",
                kind="set_exposure",
                tooltip=(
                    "Manual exposure value (UVC encoding: 2^value seconds). "
                    "Range varies per camera; device rejects out-of-range."
                ),
            )
        )

    def _build_focus_section(self) -> None:
        body = self.add_section("Focus")
        body.addLayout(
            self._auto_toggle_row(
                label="Auto focus:",
                kind="set_auto_focus",
                tooltip="Toggle continuous AF. When off, focus value below is used.",
            )
        )
        body.addLayout(
            self._int_value_row(
                label="Focus value:",
                kind="set_focus",
                tooltip="Manual focus position. Units are device-specific.",
            )
        )

    def _build_zoom_section(self) -> None:
        body = self.add_section("Zoom")
        body.addLayout(
            self._int_value_row(
                label="Optical zoom:",
                kind="set_zoom",
                tooltip=(
                    "Optical zoom level. Cameras without an optical zoom "
                    "(C920 / C930e) reject — use Digital zoom instead."
                ),
            )
        )
        body.addLayout(
            self._int_value_row(
                label="Digital zoom:",
                kind="set_digital_zoom",
                tooltip=(
                    "Digital zoom (crop + upscale). Software effect inside "
                    "the camera; quality degrades at high values."
                ),
            )
        )

    def _build_wb_section(self) -> None:
        body = self.add_section("White balance")
        body.addLayout(
            self._auto_toggle_row(
                label="Auto WB:",
                kind="set_auto_white_balance",
                tooltip="Toggle camera-driven auto white-balance.",
            )
        )
        body.addLayout(
            self._int_value_row(
                label="WB temperature (K):",
                kind="set_white_balance",
                tooltip=(
                    "Color temperature in Kelvin (typical UVC range "
                    "2800 – 6500). Manual WB only takes effect after Auto "
                    "WB is disabled."
                ),
            )
        )

    def _build_pan_tilt_section(self) -> None:
        body = self.add_section("Pan / tilt")
        body.addLayout(
            self._int_value_row(
                label="Pan:",
                kind="set_pan",
                tooltip="PTZ pan position. 0 is centered for most cameras.",
            )
        )
        body.addLayout(
            self._int_value_row(
                label="Tilt:",
                kind="set_tilt",
                tooltip="PTZ tilt position. 0 is centered for most cameras.",
            )
        )

    def _build_image_adjust_section(self) -> None:
        body = self.add_section("Image adjust")
        for label, kind, tooltip in (
            ("Brightness:", "set_brightness", "Image brightness offset."),
            ("Contrast:", "set_contrast", "Image contrast."),
            ("Saturation:", "set_saturation", "Color saturation. 0 = grayscale."),
            ("Sharpness:", "set_sharpness", "In-camera sharpening intensity."),
            ("Gamma:", "set_gamma", "Gamma correction. 100 = linear."),
            ("Hue:", "set_hue", "Color hue rotation. Rarely useful for lab imaging."),
            ("Gain:", "set_gain", "Sensor gain. High gain raises noise."),
            (
                "Backlight comp:",
                "set_backlight_compensation",
                "Compensate for bright backlight. 0 = off.",
            ),
        ):
            body.addLayout(
                self._int_value_row(
                    label=label,
                    kind=kind,
                    tooltip=tooltip,
                )
            )

    # ------------------------------------------------------------------ row helpers

    def _int_value_row(
        self,
        *,
        label: str,
        kind: str,
        tooltip: str,
        minimum: int = -32768,
        maximum: int = 32767,
    ) -> QHBoxLayout:
        """One `label / QSpinBox / Apply` row for a `{"value": int}` verb.

        Default bounds are intentionally wide (16-bit signed range); the
        real per-property min/max land via
        :meth:`_refresh_controls_from_probe` after the device is opened.
        Pre-probe the spinbox accepts any plausible value rather than
        clipping to a guessed range.
        """
        row = QHBoxLayout()
        row.setSpacing(6)
        lbl = QLabel(label, self)
        lbl.setMinimumWidth(120)
        row.addWidget(lbl)
        spin = QSpinBox(self)
        spin.setRange(minimum, maximum)
        spin.setToolTip(tooltip)
        self._spinboxes[kind] = spin
        row.addWidget(spin)
        btn = QPushButton("Apply", self)
        btn.clicked.connect(
            lambda: self.schedule_dispatch(kind=kind, payload={"value": int(spin.value())})
        )
        row.addWidget(btn)
        row.addStretch(1)
        for w in (spin, btn):
            self.register_action_widget(w)
        return row

    def _auto_toggle_row(
        self,
        *,
        label: str,
        kind: str,
        tooltip: str,
    ) -> QHBoxLayout:
        """One `label / QCheckBox / Apply` row for an auto-mode toggle verb."""
        row = QHBoxLayout()
        row.setSpacing(6)
        lbl = QLabel(label, self)
        lbl.setMinimumWidth(120)
        row.addWidget(lbl)
        check = QCheckBox("enable", self)
        check.setToolTip(tooltip)
        check.setChecked(True)
        row.addWidget(check)
        btn = QPushButton("Apply", self)
        btn.clicked.connect(
            lambda: self.schedule_dispatch(kind=kind, payload={"enable": check.isChecked()})
        )
        row.addWidget(btn)
        row.addStretch(1)
        for w in (check, btn):
            self.register_action_widget(w)
        return row

    # ------------------------------------------------------------------ lifecycle

    async def _ensure_adapter(self) -> CommandTarget | None:
        """Construct + open the webcam on first action. Same rationale as
        :meth:`FlirCard._ensure_adapter`: cameras are not registry-shared
        because frame timestamps must anchor to the run clock, and the
        panel uses an idle :class:`RunClock` since it never records."""
        if self._adapter is not None:
            return self._adapter
        try:
            cameras = construct_cameras(
                _SingleCameraConfig(self._spec),  # type: ignore[arg-type]
                clock=RunClock.now(),
            )
            if not cameras:
                self._set_status("camera construction returned no instance", level="error")
                return None
            camera = cameras[0]
            await camera.open()
        except Exception as exc:
            self._set_status(f"open failed: {exc}", level="error")
            _logger.warning(
                "manual.webcam_open_failed",
                camera=self._spec.name,
                error=str(exc),
            )
            return None
        self._adapter = camera
        return camera

    def _on_engine_state(self, state: object) -> None:
        """Release our handle on PREPARING so the engine can acquire the
        camera with a fresh run-clock anchor. Same dance as FlirCard, with
        the addition that we cancel the preview tasks first — the engine's
        ``camera_task`` opens its own input container, which would collide
        with ours if it's still pumping."""
        super()._on_engine_state(state)
        if not isinstance(state, EngineState):
            return
        if state is EngineState.PREPARING and isinstance(self._adapter, Camera):
            camera = self._adapter
            self._adapter = None
            schedule_bg(self._stop_preview_session_and_close(camera))
        elif state is EngineState.IDLE and self._adapter is None:
            # Returning to idle after a run: re-acquire and restart preview.
            schedule_bg(self._start_preview_session())

    # ----------------------------------------------------------- preview tile

    def _build_preview_tile(self) -> QLabel:
        """Fixed-size :class:`QLabel` that hosts the live preview pixmap.

        Inserted as the first row of the card body via :meth:`add_section`
        so it sits above every settings section but below the subtitle.
        Until a frame arrives the label shows an "(no preview)" placeholder
        in the idle-text color.
        """
        body = self.add_section("Live preview")
        label = QLabel(self)
        label.setFixedSize(PREVIEW_TILE_WIDTH, PREVIEW_TILE_HEIGHT)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet(
            "QLabel { background-color: #111; color: #888; border: 1px solid #333; }"
        )
        label.setText("(no preview)")
        body.addWidget(label)
        return label

    async def _start_preview_session(self) -> None:
        """Acquire the adapter, start preview, spawn pump + consumer tasks.

        Safe to call repeatedly: returns early if either task is already
        running. Failures (adapter open failed, camera held by another
        process) are surfaced in the inline status label and leave the
        preview tile in its placeholder state. The card stays interactive
        — operators can still change settings even if no preview is
        available, although the response to those changes obviously won't
        be visible until a future preview attempt succeeds.
        """
        if self._preview_pump_task is not None or self._preview_consumer_task is not None:
            return
        adapter = await self._ensure_adapter()
        if adapter is None:
            return
        webcam = self._as_webcam(adapter)
        if webcam is None:
            return
        if not self._controls_initialized:
            self._refresh_controls_from_probe(webcam)
        try:
            await webcam.start_preview()
        except Exception as exc:
            _logger.warning(
                "manual.webcam_preview_start_failed",
                camera=self._spec.name,
                error=str(exc),
            )
            return
        self._preview_pump_task = schedule_bg(_run_preview_pump_safe(webcam))
        self._preview_consumer_task = schedule_bg(self._consume_preview_stream(webcam))

    async def _stop_preview_session_and_close(self, camera: Camera) -> None:
        """Cancel both preview tasks, stop preview on the adapter, then
        close. Sequence matters: the consumer must drain (or be cancelled)
        before close, otherwise its ``async for`` raises a noisy
        :class:`anyio.ClosedResourceError`."""
        for task in (self._preview_consumer_task, self._preview_pump_task):
            if task is not None and not task.done():
                task.cancel()
        self._preview_consumer_task = None
        self._preview_pump_task = None
        webcam = self._as_webcam(camera)
        if webcam is not None:
            try:
                await webcam.stop_preview()
            except Exception as exc:  # pragma: no cover — defensive
                _logger.debug(
                    "manual.webcam_stop_preview_failed",
                    camera=self._spec.name,
                    error=str(exc),
                )
        await _safe_close_camera(camera)
        # Clear the tile so a stale frame doesn't suggest the preview is
        # still live during the run.
        self._preview_label.clear()
        self._preview_label.setText("(preview paused — run active)")

    async def _consume_preview_stream(self, webcam: WebcamAdapter) -> None:
        """Drain ``preview_stream()`` and paint each JPEG into the tile.

        Runs on the qasync loop so ``setPixmap`` is safe without a signal
        marshal. Exits cleanly on ``BrokenResourceError`` (adapter closed)
        or ``CancelledError`` (engine PREPARING cancelled the task)."""
        try:
            async for jpeg in webcam.preview_stream():
                pix = QPixmap()
                if not pix.loadFromData(jpeg):
                    continue
                scaled = pix.scaled(
                    PREVIEW_TILE_WIDTH,
                    PREVIEW_TILE_HEIGHT,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self._preview_label.setPixmap(scaled)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover — defensive
            _logger.debug(
                "manual.webcam_preview_consumer_exit",
                camera=self._spec.name,
                error=str(exc),
            )

    def _refresh_controls_from_probe(self, webcam: WebcamAdapter) -> None:
        """Rewrite the resolution combo, fps cap, and spinbox ranges from
        the device probe.

        Called once after the adapter first opens. The resolution combo gets
        the dshow-enumerated list (or stays on the static fallback when the
        probe came up empty); each UVC spinbox picks up its true device-
        reported min/max/step and the value the camera currently has set;
        the framerate spinbox is capped to the camera-advertised fps for
        the currently-selected resolution.

        Signals are blocked across the rewrite so the dispatch handlers don't
        fire a flurry of stale set_* commands during widget rebuild.
        """
        # Snapshot the fps caps off the adapter so the combo's change
        # handler doesn't need to keep a webcam reference.
        self._resolution_fps_caps = {
            wh: webcam.max_fps_for_resolution(*wh) or 0.0
            for wh in webcam.supported_resolutions
            if webcam.max_fps_for_resolution(*wh) is not None
        }

        combo = self._resolution_combo
        if combo is not None:
            resolutions = webcam.supported_resolutions
            if resolutions:
                combo.blockSignals(True)
                try:
                    combo.clear()
                    hint = webcam.resolution_hint
                    selected = -1
                    for i, (w, h) in enumerate(resolutions):
                        combo.addItem(f"{w}×{h}", userData=(w, h))
                        if (w, h) == hint:
                            selected = i
                    if selected >= 0:
                        combo.setCurrentIndex(selected)
                finally:
                    combo.blockSignals(False)

        # Apply fps cap for whatever resolution the combo now shows. Done
        # after the combo refresh so the cap matches the displayed entry.
        self._apply_fps_cap_for_current_resolution()

        uvc = webcam._uvc
        if uvc is not None:
            for verb, prop in PROPERTY_BY_VERB.items():
                spin = self._spinboxes.get(verb)
                if spin is None:
                    continue
                rng = uvc.get_cached_range(prop)
                if rng is None:
                    continue
                spin.blockSignals(True)
                try:
                    spin.setRange(rng.minimum, rng.maximum)
                    spin.setSingleStep(max(1, rng.step))
                    current = uvc.get_cached_current(prop)
                    spin.setValue(current if current is not None else rng.default)
                finally:
                    spin.blockSignals(False)

        self._controls_initialized = True

    def _apply_fps_cap_for_current_resolution(self) -> None:
        """Cap the framerate spinbox to the dshow-reported max fps for the
        currently-selected resolution.

        Connected to the resolution combo's ``currentIndexChanged`` signal,
        so switching from 640×480 (30 fps) to 1920×1080 (30 fps on the
        C930e) updates the spinbox cap in step. No-op when probe data is
        absent or the combo is missing — the wide 1–240 default survives,
        and the camera still rejects unsupported rates at negotiation.
        """
        combo = self._resolution_combo
        spin = self._fps_spin
        if combo is None or spin is None or not self._resolution_fps_caps:
            return
        wh = combo.currentData()
        if not isinstance(wh, tuple) or len(wh) != 2:
            return
        cap = self._resolution_fps_caps.get((int(wh[0]), int(wh[1])))
        if cap is None or cap <= 0:
            return
        spin.blockSignals(True)
        try:
            spin.setRange(1.0, float(cap))
            if spin.value() > cap:
                spin.setValue(float(cap))
        finally:
            spin.blockSignals(False)

    @staticmethod
    def _as_webcam(adapter: CommandTarget | Camera) -> WebcamAdapter | None:
        """Narrow ``adapter`` to :class:`WebcamAdapter` for type-checked
        access to the preview surface. Returns ``None`` if the adapter
        isn't a webcam (defensive; should never happen since this card
        is only constructed for visible cameras)."""
        return adapter if isinstance(adapter, WebcamAdapter) else None


async def _run_preview_pump_safe(webcam: WebcamAdapter) -> None:
    """Run the preview pump, swallowing the AdapterError that arises when
    the camera is already held by another process. The pump task's
    failure shouldn't propagate out of :func:`schedule_bg` as an
    "exception was never retrieved" warning."""
    try:
        await webcam.run_preview_pump()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _logger.info(
            "manual.webcam_preview_pump_exit",
            camera=getattr(getattr(webcam, "spec", None), "name", "?"),
            error=str(exc),
        )


def _default_webcam_capabilities() -> frozenset[CameraCapability]:
    """Optimistic default — render every section. Real device support is
    confirmed when the adapter opens; verbs against unsupported properties
    reject at dispatch-time with a clear "device does not support …"
    message. Same philosophy as :func:`FlirCard._default_ir_capabilities`."""
    return frozenset(
        {
            CameraCapability.STREAM_FORMAT,
            CameraCapability.EXPOSURE_CONTROL,
            CameraCapability.FOCUS_CONTROL,
            CameraCapability.ZOOM_CONTROL,
            CameraCapability.WB_CONTROL,
            CameraCapability.PAN_TILT_CONTROL,
            CameraCapability.IMAGE_ADJUST,
        }
    )


__all__ = ["WebcamCard", "is_webcam_camera"]
