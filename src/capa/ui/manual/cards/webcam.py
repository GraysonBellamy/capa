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

Each UVC control's range, value and auto mode come from the camera's
read-back (:class:`WebcamStateSnapshot`): when the card is built, when the
pool opens, after each Apply and after an experiment's device settings
are applied. Rows for controls the camera lacks are greyed out. Until the
first read-back the spinboxes take a permissive range and the adapter
rejects what the camera can't take.

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

from capa.devices.camera.base import CameraCapability, CameraSpec, WebcamStateSnapshot
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

    Every section renders from the static base set. Once the camera has
    been read, rows for controls it lacks are greyed out rather than
    hidden, so operators can still ask "wait, my C920 had pan/tilt, why
    is it off?" instead of being confused.
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
        self.set_subtitle(self._identity_line())
        # Stream-format widgets and a latch so the metadata refresh only
        # runs once per card lifetime; consumed by :meth:`_apply_metadata`.
        self._resolution_combo: QComboBox | None = None
        self._fps_spin: QDoubleSpinBox | None = None
        self._controls_initialized: bool = False
        # UVC rows, filled by :meth:`apply_snapshot`. Value spinboxes are
        # keyed by setting name ("zoom"), auto checkboxes by their auto
        # field ("auto_exposure"), and every row widget by the control it
        # sets, so a control the camera lacks can be greyed out as one.
        self._value_spins: dict[str, QSpinBox] = {}
        self._auto_checks: dict[str, QCheckBox] = {}
        self._control_widgets: dict[str, list[QWidget]] = {}
        self._unsupported: frozenset[str] = frozenset()
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
                control="exposure",
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
                control="exposure",
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
                control="focus",
                tooltip="Toggle continuous AF. When off, focus value below is used.",
            )
        )
        body.addLayout(
            self._int_value_row(
                label="Focus value:",
                control="focus",
                tooltip="Manual focus position. Units are device-specific.",
            )
        )

    def _build_zoom_section(self) -> None:
        body = self.add_section("Zoom")
        body.addLayout(
            self._int_value_row(
                label="Optical zoom:",
                control="zoom",
                tooltip="Zoom position (UVC Zoom). Units and range depend on the camera.",
            )
        )
        body.addLayout(
            self._int_value_row(
                label="Digital zoom:",
                control="digital_zoom",
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
                control="white_balance",
                tooltip="Toggle camera-driven auto white-balance.",
            )
        )
        body.addLayout(
            self._int_value_row(
                label="WB temperature (K):",
                control="white_balance",
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
                control="pan",
                tooltip="PTZ pan position, in arc-seconds on most cameras. 0 is centered.",
            )
        )
        body.addLayout(
            self._int_value_row(
                label="Tilt:",
                control="tilt",
                tooltip="PTZ tilt position, in arc-seconds on most cameras. 0 is centered.",
            )
        )

    def _build_image_adjust_section(self) -> None:
        body = self.add_section("Image adjust")
        for label, control, tooltip in (
            ("Brightness:", "brightness", "Image brightness offset."),
            ("Contrast:", "contrast", "Image contrast."),
            ("Saturation:", "saturation", "Color saturation. 0 = grayscale."),
            ("Sharpness:", "sharpness", "In-camera sharpening intensity."),
            ("Gamma:", "gamma", "Gamma correction. 100 = linear."),
            ("Hue:", "hue", "Color hue rotation. Rarely useful for lab imaging."),
            ("Gain:", "gain", "Sensor gain. High gain raises noise."),
            (
                "Backlight comp:",
                "backlight_compensation",
                "Compensate for bright backlight. 0 = off.",
            ),
        ):
            body.addLayout(
                self._int_value_row(
                    label=label,
                    control=control,
                    tooltip=tooltip,
                )
            )

    # ------------------------------------------------------------------ row helpers

    def _int_value_row(
        self,
        *,
        label: str,
        control: str,
        tooltip: str,
    ) -> QHBoxLayout:
        """One `label / QSpinBox / Apply` row for the ``set_<control>``
        verb's ``{"value": int}``.

        The bounds start wide (16-bit signed range); the camera's own range
        lands with the first read-back (:meth:`apply_snapshot`). Until then
        the spinbox accepts any plausible value rather than clipping to a
        guessed range.
        """
        row = QHBoxLayout()
        row.setSpacing(6)
        lbl = QLabel(label, self)
        lbl.setMinimumWidth(120)
        row.addWidget(lbl)
        spin = QSpinBox(self)
        spin.setRange(-32768, 32767)
        spin.setToolTip(tooltip)
        spin.valueChanged.connect(lambda _value: self._unapplied_edits.add(control))
        self._value_spins[control] = spin
        row.addWidget(spin)
        btn = QPushButton("Apply", self)
        btn.setObjectName(f"apply_{control}")
        btn.clicked.connect(
            lambda: self._apply_field(
                control, kind=f"set_{control}", payload={"value": int(spin.value())}
            )
        )
        row.addWidget(btn)
        row.addStretch(1)
        for w in (spin, btn):
            self.register_action_widget(w)
        self._control_widgets.setdefault(control, []).extend((spin, btn))
        return row

    def _auto_toggle_row(
        self,
        *,
        label: str,
        control: str,
        tooltip: str,
    ) -> QHBoxLayout:
        """One `label / QCheckBox / Apply` row for the ``set_auto_<control>``
        verb. Checked until the first read-back says otherwise."""
        field = f"auto_{control}"
        row = QHBoxLayout()
        row.setSpacing(6)
        lbl = QLabel(label, self)
        lbl.setMinimumWidth(120)
        row.addWidget(lbl)
        check = QCheckBox("enable", self)
        check.setToolTip(tooltip)
        check.setChecked(True)
        check.toggled.connect(lambda _checked: self._unapplied_edits.add(field))
        self._auto_checks[field] = check
        row.addWidget(check)
        btn = QPushButton("Apply", self)
        btn.setObjectName(f"apply_{field}")
        btn.clicked.connect(
            lambda: self._apply_field(
                field, kind=f"set_{field}", payload={"enable": check.isChecked()}
            )
        )
        row.addWidget(btn)
        row.addStretch(1)
        for w in (check, btn):
            self.register_action_widget(w)
        self._control_widgets.setdefault(control, []).extend((check, btn))
        return row

    # ------------------------------------------------------------------ lifecycle

    async def _ensure_adapter(self) -> CommandTarget | None:
        """Return the :class:`WorkerPool`-owned webcam handle.

        Webcams are constructed inside the pool's :class:`Worker` at
        :meth:`WorkerPool.open` time and wrapped in
        :class:`CameraDeviceAdapter`. The card consumes
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
        """The pool owns the webcam handle across runs, so there
        is no per-run hand-off and no preview to tear down on PREPARING.
        :class:`WebcamAdapter` runs preview concurrently with recording.
        """
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
        """Rewrite the resolution combo and fps cap from the metadata
        snapshot.

        Called once after the adapter first opens. The resolution combo gets
        the dshow-enumerated list (or stays on the static fallback when the
        probe came up empty); the framerate spinbox is capped to the
        camera-advertised fps for the currently-selected resolution. UVC
        controls come from :meth:`refresh_readback` instead.

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
        self._controls_initialized = True

    # ------------------------------------------------------------------ live readback

    async def refresh_readback(self) -> None:
        """Read the camera's UVC controls and show them.

        The dock calls this when the card is built, once the pool has
        opened, and after an experiment's device settings were applied;
        every Apply calls it once the command lands. Best-effort: a failure
        leaves the card as it is. Skipped during a run, like every
        manual-card read.
        """
        if self._engine_blocks_writes():
            return
        client = self._controller.manual_client
        if client is None:
            return
        try:
            snapshot = await client.device_readback(self._spec.name)
        except Exception as exc:
            _logger.debug("manual.webcam_readback_failed", device=self.device_name, error=str(exc))
            return
        if isinstance(snapshot, WebcamStateSnapshot):
            self.apply_snapshot(snapshot)

    def apply_snapshot(self, snapshot: WebcamStateSnapshot) -> None:
        """Show a read-back: each spinbox takes the camera's range, step and
        value, each auto checkbox its mode, and rows for controls the camera
        lacks are greyed out. A field the operator has changed and not yet
        applied keeps the change."""
        for control, spin in self._value_spins.items():
            state = snapshot.controls.get(control)
            if state is None:
                continue
            spin.blockSignals(True)
            try:
                if state.minimum is not None and state.maximum is not None:
                    spin.setRange(state.minimum, state.maximum)
                if state.step is not None:
                    spin.setSingleStep(max(1, state.step))
                if state.value is not None and control not in self._unapplied_edits:
                    # Widen rather than clamp: show what the camera holds.
                    spin.setRange(
                        min(spin.minimum(), state.value), max(spin.maximum(), state.value)
                    )
                    spin.setValue(state.value)
            finally:
                spin.blockSignals(False)
        for field, check in self._auto_checks.items():
            state = snapshot.controls.get(field.removeprefix("auto_"))
            if state is None or state.auto is None or field in self._unapplied_edits:
                continue
            check.blockSignals(True)
            try:
                check.setChecked(state.auto)
            finally:
                check.blockSignals(False)
        self._unsupported = frozenset(self._control_widgets) - snapshot.controls.keys()
        line = self._identity_line()
        if snapshot.unavailable is not None:
            line += f"   Controls: {snapshot.unavailable}"
        self.set_subtitle(line)
        self._sync_action_widgets()

    def _sync_action_widgets(self, state: RunUiState | None = None) -> None:
        """The base enables every row together; rows for controls the
        camera lacks stay greyed out."""
        super()._sync_action_widgets(state)
        for control in self._unsupported:
            for widget in self._control_widgets[control]:
                widget.setEnabled(False)

    def _identity_line(self) -> str:
        spec = self._spec
        return (
            f"Camera: {spec.name}   Adapter: {spec.adapter.rsplit('.', 1)[-1]}   Kind: {spec.kind}"
        )

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
