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
* ``TEMPERATURE_RANGE_SELECT``  — temperature range (forbidden mid-record)
* ``RADIOMETRIC_PARAMS``    — emissivity, atm temp, etc.
* ``REMOTE_PALETTE``        — camera-side display palette
* ``PALETTE``               — preview-side palette (UI dashboard)

Every field shows the camera's state, read via
:meth:`ManualClient.device_readback` (the adapter's
``read_state_snapshot``) once the pool is open and again after each
Apply: the temperature ranges and the active one, the auto-NUC interval,
the radiometric parameters, and both palettes with the choices the
camera offers. A field the operator has changed but not yet applied
keeps the change through a read-back.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

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

from capa.devices.camera.base import (
    Camera,
    CameraCapability,
    CameraSpec,
    CameraTemperatureRange,
    IrCameraStateSnapshot,
)
from capa.ui.async_util import schedule_bg
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
        self.set_subtitle(self._identity_line())
        self._temp_range_combo: QComboBox | None = None
        self._auto_nuc_spin: QSpinBox | None = None
        self._remote_palette_combo: QComboBox | None = None
        self._preview_palette_combo: QComboBox | None = None
        # Radiometric spinboxes by their IrRadiometricParams field.
        self._radiometric_spins: dict[str, QDoubleSpinBox] = {}
        # Fields changed since their last Apply, named as in the snapshot
        # (radiometric ones by their IrRadiometricParams field): a
        # read-back fills every other field.
        self._unapplied_edits: set[str] = set()
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
        spin.valueChanged.connect(lambda _value: self._unapplied_edits.add("auto_nuc_interval_s"))
        row.addWidget(spin)
        btn = QPushButton("Apply", self)
        btn.setObjectName("apply_auto_nuc_interval_s")
        btn.clicked.connect(
            lambda: self._apply_field(
                "auto_nuc_interval_s",
                kind="set_auto_nuc_interval",
                payload={"seconds": spin.value()},
            )
        )
        row.addWidget(btn)
        row.addStretch(1)
        body.addLayout(row)
        self.register_action_widget(spin)
        self.register_action_widget(btn)
        self._auto_nuc_spin = spin

    def _build_temp_range_section(self) -> None:
        body = self.add_section("Temperature range")
        row = QHBoxLayout()
        row.setSpacing(6)
        lbl = QLabel("Range:", self)
        lbl.setMinimumWidth(120)
        row.addWidget(lbl)
        combo = QComboBox(self)
        # Empty until the camera reports its ranges: a placeholder entry
        # would read as the active range.
        combo.setPlaceholderText("not read from camera yet")
        combo.setToolTip(
            "Camera-side temperature range; the list and the active range "
            "are read from the camera. Forbidden during recording "
            "(switching ranges typically forces a multi-second recalibration)."
        )
        # ``activated`` fires only for the operator's pick, not for a fill.
        combo.activated.connect(lambda _i: self._unapplied_edits.add("temperature_range_index"))
        row.addWidget(combo)
        btn = QPushButton("Apply", self)
        btn.clicked.connect(self._on_apply_temperature_range)
        row.addWidget(btn)
        row.addStretch(1)
        body.addLayout(row)
        self.register_action_widget(combo)
        self.register_action_widget(btn)
        self._temp_range_combo = combo

    def _on_apply_temperature_range(self) -> None:
        combo = self._temp_range_combo
        index = None if combo is None else combo.currentData()
        if combo is None or index is None:
            self._set_status("ranges not read from camera yet", level="warn")
            return
        self._apply_field(
            "temperature_range_index",
            kind="set_temperature_range",
            payload={"index": index},
            destructive=True,
            destructive_summary=(
                f"Switch camera temperature range to {combo.currentText()}. "
                "Triggers a multi-second recalibration."
            ),
        )

    def _build_radiometric_section(self) -> None:
        body = self.add_section("Radiometric (Atlas SDK kit)")
        # Emissivity: fraction 0.001–1.0
        self._add_double_row(
            body,
            label="Emissivity:",
            field="emissivity",
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
            field="atmospheric_temp_c",
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
            field="reflected_temp_c",
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
            field="distance_m",
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
            field="relative_humidity",
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
            field="atmospheric_transmission",
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
        self._remote_palette_combo = self._add_palette_row(
            body,
            field="remote_palette",
            kind="set_remote_palette",
            tooltip=(
                "Display palette on the camera's own screen; the choices and "
                "the active one are read from the camera. Distinct from the "
                "preview-side palette below."
            ),
        )

    def _build_preview_palette_section(self) -> None:
        body = self.add_section("UI preview palette")
        self._preview_palette_combo = self._add_palette_row(
            body,
            field="preview_palette",
            kind="set_preview_palette",
            tooltip=(
                "Preview palette in the dashboard; the choices and the active "
                "one are read from the camera adapter. UI-only — does not "
                "affect the recorded frames."
            ),
        )

    def _add_palette_row(
        self, body: QVBoxLayout, *, field: str, kind: str, tooltip: str
    ) -> QComboBox:
        """One palette row. ``field`` names the snapshot's active palette;
        the combo stays empty until a read-back lists the choices."""
        row = QHBoxLayout()
        row.setSpacing(6)
        lbl = QLabel("Palette:", self)
        lbl.setMinimumWidth(120)
        row.addWidget(lbl)
        combo = QComboBox(self)
        combo.setPlaceholderText("not read from camera yet")
        combo.setToolTip(tooltip)
        combo.activated.connect(lambda _i: self._unapplied_edits.add(field))
        row.addWidget(combo)
        btn = QPushButton("Apply", self)
        btn.setObjectName(f"apply_{field}")

        def _apply() -> None:
            if combo.currentIndex() < 0:
                self._set_status("palettes not read from camera yet", level="warn")
                return
            self._apply_field(field, kind=kind, payload={"palette": combo.currentText()})

        btn.clicked.connect(_apply)
        row.addWidget(btn)
        row.addStretch(1)
        body.addLayout(row)
        self.register_action_widget(combo)
        self.register_action_widget(btn)
        return combo

    def _add_double_row(
        self,
        body: QVBoxLayout,
        *,
        label: str,
        field: str,
        kind: str,
        payload_key: str,
        minimum: float,
        maximum: float,
        decimals: int,
        step: float,
        default: float,
        tooltip: str,
    ) -> None:
        """One radiometric row. ``field`` names the
        :class:`IrRadiometricParams` value a read-back fills it from;
        ``default`` shows until the first read-back."""
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
        spin.valueChanged.connect(lambda _value: self._unapplied_edits.add(field))
        row.addWidget(spin)
        self._radiometric_spins[field] = spin
        btn = QPushButton("Apply", self)
        btn.setObjectName(f"apply_{field}")
        btn.clicked.connect(
            lambda: self._apply_field(field, kind=kind, payload={payload_key: spin.value()})
        )
        row.addWidget(btn)
        row.addStretch(1)
        body.addLayout(row)
        self.register_action_widget(spin)
        self.register_action_widget(btn)

    # ------------------------------------------------------------------ dispatch

    def _apply_field(self, field: str, **dispatch: Any) -> None:
        """Send the command for ``field``, then re-read the camera."""
        if schedule_bg(self._apply_field_and_read_back(field, dispatch)) is None:
            self._set_status("no event loop — UI not running?", level="error")

    async def _apply_field_and_read_back(self, field: str, dispatch: dict[str, Any]) -> None:
        """Once the command has reached the camera, accepted or not, the
        field shows what the camera holds again. One that never went out —
        a declined confirmation, no operator id — keeps the operator's
        change."""
        if await self.dispatch(**dispatch) is not None:
            self._unapplied_edits.discard(field)
        await self.refresh_readback()

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

    # ------------------------------------------------------------------ live readback

    async def refresh_readback(self) -> None:
        """Fetch the camera's read-back and apply it.

        The dock calls this when the card is built and again once the pool
        has opened; every Apply calls it once the command lands.
        Best-effort: a failure leaves the card as it is. Skipped during a
        run, like every manual-card read.
        """
        if self._engine_blocks_writes():
            return
        client = self._controller.manual_client
        if client is None:
            return
        try:
            snapshot = await client.device_readback(self._spec.name)
        except Exception as exc:
            _logger.debug("manual.camera_readback_failed", device=self.device_name, error=str(exc))
            return
        if isinstance(snapshot, IrCameraStateSnapshot):
            self.apply_snapshot(snapshot)

    def apply_snapshot(self, snapshot: IrCameraStateSnapshot) -> None:
        """Show a read-back: every field takes the camera's value, each combo
        lists the camera's choices, and the subtitle names the active range.
        A field the operator has changed and not yet applied keeps the
        change."""
        radiometric = snapshot.radiometric
        if radiometric is not None:
            for field, spin in self._radiometric_spins.items():
                if field not in self._unapplied_edits:
                    _show_value(spin, getattr(radiometric, field))
        nuc_spin = self._auto_nuc_spin
        if (
            nuc_spin is not None
            and snapshot.auto_nuc_interval_s is not None
            and "auto_nuc_interval_s" not in self._unapplied_edits
        ):
            _show_value(nuc_spin, snapshot.auto_nuc_interval_s)
        self._fill_combo(
            "temperature_range_index",
            self._temp_range_combo,
            [_range_label(r) for r in snapshot.temperature_ranges],
            snapshot.temperature_range_index,
        )
        self._fill_combo(
            "remote_palette",
            self._remote_palette_combo,
            snapshot.remote_palettes,
            _position(snapshot.remote_palettes, snapshot.remote_palette),
        )
        self._fill_combo(
            "preview_palette",
            self._preview_palette_combo,
            snapshot.preview_palettes,
            _position(snapshot.preview_palettes, snapshot.preview_palette),
        )
        parts = [self._identity_line()]
        active_range = _active_range(snapshot)
        if active_range is not None:
            parts.append(f"Range: {_range_label(active_range)}")
        self.set_subtitle("   ".join(parts))

    def _fill_combo(
        self, field: str, combo: QComboBox | None, labels: Sequence[str], active: int | None
    ) -> None:
        """Replace ``combo``'s choices with ``labels``, each carrying its
        position as item data, and select the camera's ``active`` one. The
        operator's unapplied pick for ``field`` stays selected while the
        camera still offers it."""
        if combo is None:
            return
        pick = combo.currentText() if field in self._unapplied_edits else None
        combo.clear()
        for position, label in enumerate(labels):
            combo.addItem(label, position)
        # Adding items selects the first, which isn't the camera's choice.
        index = -1 if pick is None else combo.findText(pick)
        if index < 0:
            self._unapplied_edits.discard(field)
            index = active if active is not None and 0 <= active < combo.count() else -1
        combo.setCurrentIndex(index)

    def _identity_line(self) -> str:
        spec = self._spec
        return (
            f"Camera: {spec.name}   Adapter: {spec.adapter.rsplit('.', 1)[-1]}   Kind: {spec.kind}"
        )


def _show_value(spin: QSpinBox | QDoubleSpinBox, value: float) -> None:
    """Show a read-back in ``spin`` without counting it as an operator
    edit. The range widens to fit, since a spinbox would clamp a camera
    value beyond its limits and show a number the camera doesn't hold."""
    spin.blockSignals(True)
    try:
        if isinstance(spin, QSpinBox):
            whole = int(value)
            spin.setRange(min(spin.minimum(), whole), max(spin.maximum(), whole))
            spin.setValue(whole)
        else:
            spin.setRange(min(spin.minimum(), value), max(spin.maximum(), value))
            spin.setValue(value)
    finally:
        spin.blockSignals(False)


def _position(choices: Sequence[str], active: str | None) -> int | None:
    return choices.index(active) if active is not None and active in choices else None


def _active_range(snapshot: IrCameraStateSnapshot) -> CameraTemperatureRange | None:
    index = snapshot.temperature_range_index
    if index is None or not 0 <= index < len(snapshot.temperature_ranges):
        return None
    return snapshot.temperature_ranges[index]


def _range_label(temperature_range: CameraTemperatureRange) -> str:
    """``"-20 to 120 °C"`` — the range as the operator picks it."""
    return f"{_format_c(temperature_range.min_c)} to {_format_c(temperature_range.max_c)} °C"


def _format_c(value: float) -> str:
    # A Kelvin read-back converts to e.g. -19.999999999999972; round it
    # back, and fold a rounded -0.0 into 0.
    rounded = round(value, 1) + 0.0
    return f"{rounded:g}"


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
