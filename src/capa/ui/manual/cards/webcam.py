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

from typing import Final

import structlog
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

from capa.devices.camera.base import CameraCapability, CameraSpec
from capa.devices.camera.metadata import WebcamMetadata
from capa.runtime.dispatch import ManualClient
from capa.ui.async_util import schedule_bg
from capa.ui.manual.cards.base import CommandTarget, DeviceCard
from capa.ui.state import RunController, RunUiState
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
# The card calls :meth:`ManualClient.camera_metadata` on pool open; the
# returned :class:`WebcamMetadata.supported_resolutions` rewrites the
# combo from the real device list when the probe succeeded.
_FALLBACK_RESOLUTIONS: Final[tuple[tuple[int, int], ...]] = (
    (640, 480),
    (1280, 720),
    (1920, 1080),
)

# Same logic for framerates — the UVC negotiation will reject any fps the
# camera doesn't advertise for the chosen resolution.
COMMON_FRAMERATES: Final[tuple[float, ...]] = (15.0, 30.0, 60.0)


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
        # Live widget references and a latch so the probe-driven refresh
        # only runs once per card lifetime. Populated by section builders
        # and consumed by :meth:`_apply_metadata`.
        self._resolution_combo: QComboBox | None = None
        self._fps_spin: QDoubleSpinBox | None = None
        self._spinboxes: dict[str, QSpinBox] = {}
        self._controls_initialized: bool = False
        # Kept in sync from :meth:`_apply_metadata` so the
        # resolution-combo change handler can recompute the fps cap without
        # holding a reference to the WebcamAdapter.
        self._resolution_fps_caps: dict[tuple[int, int], float] = {}
        self._build_capability_sections()
        # Pool-change handler: kick the one-shot probe refresh when the
        # camera handle becomes available. The pool publishes itself via
        # pool_changed after open() resolves.
        self._controller.pool_changed.connect(self._on_pool_changed)

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
        """Return the :class:`WorkerPool`-owned webcam handle.

        Phase 4 / migration doc §6: webcams are constructed inside the
        pool's :class:`Worker` at :meth:`WorkerPool.open` time and
        wrapped in :class:`CameraDeviceAdapter`. The card consumes
        preview JPEGs via :attr:`RunController.preview_received` and
        probe metadata via :meth:`ManualClient.camera_metadata`; this
        helper is only used so the base-class
        :meth:`schedule_dispatch` has a non-``None`` target.
        """
        if self._adapter is not None:
            return self._adapter
        client = self._controller.manual_client
        if client is None:
            self._set_status("no config loaded — open a config first", level="warn")
            return None
        camera = client.camera(self._spec.name)
        if camera is None:
            self._set_status("camera not yet available (pool still opening?)", level="warn")
            return None
        self._adapter = camera
        return camera

    def _on_engine_state(self, state: object) -> None:
        """Phase 4: the pool owns the webcam handle across runs, so there
        is no per-run hand-off and no preview to tear down on PREPARING.
        :class:`WebcamAdapter` supports preview running concurrently with
        recording — the pre-run release dance the legacy card needed is
        gone."""
        super()._on_engine_state(state)
        if not isinstance(state, RunUiState):
            return
        # Card-side cleanup intentionally absent (see camera.py).

    def _on_pool_changed(self, pool: object) -> None:
        """Kick off the one-shot probe-driven control refresh when the pool
        becomes available. The pool publishes itself via
        :attr:`RunController.pool_changed` after :meth:`WorkerPool.open`
        resolves; before that, the camera handle is not yet open and the
        probe attributes are absent.

        The metadata read happens on the worker loop via
        :meth:`ManualClient.camera_metadata` — a typed snapshot DTO crosses
        loops, the live :class:`WebcamAdapter` handle never does. The
        async fetch is scheduled fire-and-forget; the apply step runs in
        :meth:`_apply_metadata` once the future resolves.
        """
        if pool is None or self._controls_initialized:
            return
        client = self._controller.manual_client
        if client is None:
            return
        schedule_bg(self._fetch_and_apply_metadata(client))

    async def _fetch_and_apply_metadata(self, client: ManualClient) -> None:
        """Probe the worker-resident camera and apply the snapshot.

        :class:`ManualClient.camera_metadata` returns ``None`` for
        non-webcam adapters (IR cameras, devices) and for cameras whose
        probe found nothing; either case leaves the card on its static
        widget defaults. We swallow unexpected exceptions because the
        card surface stays usable on the static fallback — a failed
        metadata fetch should not kill the whole card.
        """
        try:
            metadata = await client.camera_metadata(self._spec.name)
        except Exception as exc:
            _logger.warning(
                "webcam_card.metadata_fetch_failed",
                camera=self._spec.name,
                error=str(exc),
            )
            return
        if metadata is None:
            return
        self._apply_metadata(metadata)

    def _apply_metadata(self, metadata: WebcamMetadata) -> None:
        """Rewrite the resolution combo, fps cap, and spinbox ranges from
        the metadata snapshot.

        Called once after the adapter first opens. The resolution combo gets
        the dshow-enumerated list (or stays on the static fallback when the
        probe came up empty); each UVC spinbox picks up its true device-
        reported min/max/step and the value the camera currently has set;
        the framerate spinbox is capped to the camera-advertised fps for
        the currently-selected resolution.

        Signals are blocked across the rewrite so the dispatch handlers don't
        fire a flurry of stale set_* commands during widget rebuild.
        """
        self._resolution_fps_caps = dict(metadata.resolution_fps_caps)

        combo = self._resolution_combo
        if combo is not None and metadata.supported_resolutions:
            combo.blockSignals(True)
            try:
                combo.clear()
                hint = metadata.resolution_hint
                selected = -1
                for i, (w, h) in enumerate(metadata.supported_resolutions):
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

        for verb, rng in metadata.uvc_ranges.items():
            spin = self._spinboxes.get(verb)
            if spin is None:
                continue
            spin.blockSignals(True)
            try:
                spin.setRange(rng.minimum, rng.maximum)
                spin.setSingleStep(max(1, rng.step))
                spin.setValue(rng.current if rng.current is not None else rng.default)
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
