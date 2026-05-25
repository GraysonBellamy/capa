""":class:`FlirCard` — manual control card for IR cameras (FLIR + sim).

Cameras differ from devices in an important way: frame timestamps must
be anchored to the per-run :class:`~capa.core.clock.RunClock`, but the
manual panel needs to issue commands between runs (when no run clock
exists). Sharing a camera handle across panel-mode and run-mode would
mis-anchor the frame ``t_mono_ns`` column, so cameras are NOT routed
through the shared :class:`~capa.runtime.pool.WorkerPool`. The card
constructs its own camera instance, closes it before the run transitions
to ``PREPARING``, and reopens on return to ``IDLE`` if the operator uses
it again.

Gated on :class:`CameraCapability`:

* ``NUC_TRIGGER``           — one-shot flat-field correction
* ``AUTO_NUC_INTERVAL``     — scheduled auto-NUC interval
* ``TEMPERATURE_RANGE_SELECT``  — range index (forbidden mid-record)
* ``RADIOMETRIC_PARAMS``    — emissivity, atm temp, etc.
* ``REMOTE_PALETTE``        — camera-side display palette
* ``PALETTE``               — preview-side palette (UI dashboard)
"""

from __future__ import annotations

from typing import Final

import structlog
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from capa.devices.camera.base import Camera, CameraCapability, CameraSpec
from capa.ui.manual.cards.base import CommandTarget, DeviceCard
from capa.ui.state import RunController, RunUiState
from capa.ui.statusbar import OperatorIdProvider

_logger = structlog.get_logger("capa.ui.manual.camera")


# Capability flags for which the FlirCard exposes manual controls. A
# camera without any of these is a "dumb" visible adapter (webcam) — we
# skip rendering its card entirely.
RELEVANT_CAPABILITIES: Final[tuple[CameraCapability, ...]] = (
    CameraCapability.NUC_TRIGGER,
    CameraCapability.AUTO_NUC_INTERVAL,
    CameraCapability.TEMPERATURE_RANGE_SELECT,
    CameraCapability.RADIOMETRIC_PARAMS,
    CameraCapability.REMOTE_PALETTE,
    CameraCapability.PALETTE,
)


def camera_has_manual_controls(camera: Camera | None, spec: CameraSpec) -> bool:
    """``True`` if the camera (or its declared spec adapter) advertises any
    manual-control capability worth rendering a card for. Falls back to
    spec-string fingerprinting when the camera is not yet opened — only
    IR adapters declare control surfaces in this iteration."""
    if camera is not None:
        return any(f in camera.capabilities for f in RELEVANT_CAPABILITIES)
    # Heuristic: IR cameras (the `kind="ir"` cameras and the FLIR sim)
    # are the ones that ship control surfaces today.
    return spec.kind == "ir"


class FlirCard(DeviceCard):
    """Per-camera manual-control card.

    Lifecycle: opens on first action, auto-closes on engine PREPARING so
    the engine can construct its own (run-clock-anchored) handle. Reopens
    on return to IDLE if the operator clicks anything.
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
            title=f"Camera: {spec.name}",
            controller=controller,
            operator_provider=operator_provider,
            parent=parent,
        )
        self._spec: CameraSpec = spec
        # Optimistic capability set — render every section, let unsupported
        # verbs reject at command-time with a clear detail string. The real
        # camera will narrow this on first open.
        self._capabilities: frozenset[CameraCapability] = _default_ir_capabilities()
        self.set_subtitle(
            f"Camera: {spec.name}   Adapter: {spec.adapter.rsplit('.', 1)[-1]}   Kind: {spec.kind}"
        )
        self._temp_range_spin: QSpinBox | None = None
        self._build_capability_sections()

    # ------------------------------------------------------------------ build

    def _build_capability_sections(self) -> None:
        if CameraCapability.NUC_TRIGGER in self._capabilities:
            self._build_nuc_section()
        if CameraCapability.AUTO_NUC_INTERVAL in self._capabilities:
            self._build_auto_nuc_section()
        if CameraCapability.TEMPERATURE_RANGE_SELECT in self._capabilities:
            self._build_temp_range_section()
        if CameraCapability.RADIOMETRIC_PARAMS in self._capabilities:
            self._build_radiometric_section()
        if CameraCapability.REMOTE_PALETTE in self._capabilities:
            self._build_remote_palette_section()
        if CameraCapability.PALETTE in self._capabilities:
            self._build_preview_palette_section()

    def _build_nuc_section(self) -> None:
        body = self.add_section("NUC (flat-field correction)")
        row = QHBoxLayout()
        row.setSpacing(6)
        btn = QPushButton("Trigger NUC now", self)
        btn.setToolTip(
            "One-shot flat-field correction. Rejected during recording "
            "to avoid a calibration discontinuity in the frame stream."
        )
        btn.clicked.connect(lambda: self.schedule_dispatch(kind="trigger_nuc"))
        self.register_action_widget(btn)
        row.addWidget(btn)
        row.addStretch(1)
        body.addLayout(row)

    def _build_auto_nuc_section(self) -> None:
        body = self.add_section("Auto-NUC scheduler")
        row = QHBoxLayout()
        row.setSpacing(6)
        lbl = QLabel("Interval (s):", self)
        lbl.setMinimumWidth(120)
        row.addWidget(lbl)
        spin = QSpinBox(self)
        spin.setRange(0, 86400)
        spin.setSingleStep(1)
        spin.setToolTip(
            "Seconds between automatic NUC triggers. 0 disables. "
            "Common settings: 30–120 s for indoor lab work."
        )
        row.addWidget(spin)
        btn = QPushButton("Apply", self)
        btn.clicked.connect(
            lambda: self.schedule_dispatch(
                kind="set_auto_nuc_interval",
                payload={"seconds": spin.value()},
            )
        )
        row.addWidget(btn)
        row.addStretch(1)
        body.addLayout(row)
        self.register_action_widget(spin)
        self.register_action_widget(btn)

    def _build_temp_range_section(self) -> None:
        body = self.add_section("Temperature range")
        row = QHBoxLayout()
        row.setSpacing(6)
        lbl = QLabel("Range index:", self)
        lbl.setMinimumWidth(120)
        row.addWidget(lbl)
        spin = QSpinBox(self)
        spin.setRange(0, 8)
        spin.setToolTip(
            "Camera-side temperature range. Forbidden during recording "
            "(switching ranges typically forces a multi-second recalibration)."
        )
        row.addWidget(spin)
        btn = QPushButton("Apply", self)
        btn.clicked.connect(
            lambda: self.schedule_dispatch(
                kind="set_temperature_range",
                payload={"index": spin.value()},
                destructive=True,
                destructive_summary=(
                    f"Switch camera temperature range to index "
                    f"{spin.value()}. Triggers a multi-second recalibration."
                ),
            )
        )
        row.addWidget(btn)
        row.addStretch(1)
        body.addLayout(row)
        self.register_action_widget(spin)
        self.register_action_widget(btn)
        self._temp_range_spin = spin

    def _build_radiometric_section(self) -> None:
        body = self.add_section("Radiometric (Atlas SDK kit)")
        # Emissivity: fraction 0.001–1.0
        self._add_double_row(
            body,
            label="Emissivity:",
            kind="set_emissivity",
            payload_key="emissivity",
            minimum=0.001,
            maximum=1.0,
            decimals=3,
            step=0.01,
            default=0.95,
            tooltip="Surface emissivity (0.001 – 1.0). Default ~0.95 for matte black paints.",
        )
        self._add_double_row(
            body,
            label="Atm temp (°C):",
            kind="set_atmospheric_temp",
            payload_key="temperature_c",
            minimum=-50.0,
            maximum=200.0,
            decimals=1,
            step=1.0,
            default=22.0,
            tooltip="Ambient air temperature between camera and target.",
        )
        self._add_double_row(
            body,
            label="Reflected temp (°C):",
            kind="set_reflected_temp",
            payload_key="temperature_c",
            minimum=-50.0,
            maximum=500.0,
            decimals=1,
            step=1.0,
            default=22.0,
            tooltip="Apparent reflected temperature seen by the target.",
        )
        self._add_double_row(
            body,
            label="Distance (m):",
            kind="set_distance_m",
            payload_key="distance_m",
            minimum=0.01,
            maximum=1000.0,
            decimals=2,
            step=0.1,
            default=1.0,
            tooltip="Object distance in meters. Affects atm-attenuation model.",
        )
        self._add_double_row(
            body,
            label="Relative humidity:",
            kind="set_relative_humidity",
            payload_key="relative_humidity",
            minimum=0.0,
            maximum=1.0,
            decimals=2,
            step=0.05,
            default=0.5,
            tooltip=(
                "FRACTION 0.0–1.0 (not percent). "
                "SDK uses fraction; per-image API uses percent — don't confuse them."
            ),
        )
        self._add_double_row(
            body,
            label="Atm transmission:",
            kind="set_atmospheric_transmission",
            payload_key="transmission",
            minimum=0.0,
            maximum=1.0,
            decimals=2,
            step=0.05,
            default=1.0,
            tooltip="Atmospheric transmission coefficient (0.0–1.0). 1.0 = no attenuation.",
        )

    def _build_remote_palette_section(self) -> None:
        body = self.add_section("Camera-side palette")
        row = QHBoxLayout()
        row.setSpacing(6)
        lbl = QLabel("Palette:", self)
        lbl.setMinimumWidth(120)
        row.addWidget(lbl)
        combo = QComboBox(self)
        combo.addItems(["iron", "rainbow", "bw", "arctic", "lava"])
        combo.setToolTip(
            "Display palette on the camera's own screen. Distinct from "
            "the preview-side palette below."
        )
        row.addWidget(combo)
        btn = QPushButton("Apply", self)
        btn.clicked.connect(
            lambda: self.schedule_dispatch(
                kind="set_remote_palette",
                payload={"palette": combo.currentText()},
            )
        )
        row.addWidget(btn)
        row.addStretch(1)
        body.addLayout(row)
        self.register_action_widget(combo)
        self.register_action_widget(btn)

    def _build_preview_palette_section(self) -> None:
        body = self.add_section("UI preview palette")
        row = QHBoxLayout()
        row.setSpacing(6)
        lbl = QLabel("Palette:", self)
        lbl.setMinimumWidth(120)
        row.addWidget(lbl)
        combo = QComboBox(self)
        combo.addItems(["grayscale", "iron", "rainbow", "magma", "viridis"])
        combo.setToolTip(
            "Preview palette in the dashboard. UI-only — does not affect the recorded frames."
        )
        row.addWidget(combo)
        btn = QPushButton("Apply", self)
        btn.clicked.connect(
            lambda: self.schedule_dispatch(
                kind="set_preview_palette",
                payload={"palette": combo.currentText()},
            )
        )
        row.addWidget(btn)
        row.addStretch(1)
        body.addLayout(row)
        self.register_action_widget(combo)
        self.register_action_widget(btn)

    def _add_double_row(
        self,
        body: QVBoxLayout,
        *,
        label: str,
        kind: str,
        payload_key: str,
        minimum: float,
        maximum: float,
        decimals: int,
        step: float,
        default: float,
        tooltip: str,
    ) -> None:
        row = QHBoxLayout()
        row.setSpacing(6)
        lbl = QLabel(label, self)
        lbl.setMinimumWidth(120)
        row.addWidget(lbl)
        spin = QDoubleSpinBox(self)
        spin.setRange(minimum, maximum)
        spin.setDecimals(decimals)
        spin.setSingleStep(step)
        spin.setValue(default)
        spin.setToolTip(tooltip)
        row.addWidget(spin)
        btn = QPushButton("Apply", self)

        def _apply() -> None:
            self.schedule_dispatch(kind=kind, payload={payload_key: spin.value()})

        btn.clicked.connect(_apply)
        row.addWidget(btn)
        row.addStretch(1)
        body.addLayout(row)
        self.register_action_widget(spin)
        self.register_action_widget(btn)

    # ------------------------------------------------------------------ lifecycle

    async def _ensure_adapter(self) -> CommandTarget | None:
        """Return the :class:`WorkerPool`-owned camera handle.

        Cameras are constructed inside the pool's :class:`Worker` at
        :meth:`WorkerPool.open` time and wrapped in
        :class:`CameraDeviceAdapter`. Cards reach the
        underlying :class:`Camera` (for preview-stream subscription)
        through :meth:`ManualClient.camera`. The card never owns the
        camera's lifecycle — the pool does.
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
        """The worker owns the camera handle for the duration of the
        pool, so there is no per-run hand-off. The base class still
        handles the manual-write gate (cards refuse dispatch during a
        run); no camera-specific behavior is required here."""
        super()._on_engine_state(state)
        if not isinstance(state, RunUiState):
            return
        # Card-side cleanup intentionally absent: the pool owns the
        # camera across runs, so preview can keep running through
        # PREPARING and beyond.


async def _safe_close_camera(camera: Camera) -> None:
    try:
        await camera.close()
    except Exception as exc:
        _logger.warning(
            "manual.camera_close_failed",
            camera=getattr(getattr(camera, "spec", None), "name", "?"),
            error=str(exc),
        )


def _default_ir_capabilities() -> frozenset[CameraCapability]:
    """Optimistic default — show every section. Real capability set is
    narrowed on first open(). Same philosophy as the AlicatCard: a verb
    that doesn't apply rejects with a clear message at dispatch time."""
    return frozenset(
        {
            CameraCapability.NUC_TRIGGER,
            CameraCapability.AUTO_NUC_INTERVAL,
            CameraCapability.TEMPERATURE_RANGE_SELECT,
            CameraCapability.RADIOMETRIC_PARAMS,
            CameraCapability.REMOTE_PALETTE,
            CameraCapability.PALETTE,
        }
    )


__all__ = ["FlirCard", "camera_has_manual_controls"]
