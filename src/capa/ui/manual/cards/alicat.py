""":class:`AlicatCard` — manual control card for Alicat MFCs / pressure devices.

Gated on the adapter's :class:`Capability` flagset:

* setpoint                — ``HAS_SETPOINT`` (controllers only)
* gas / fluid selection   — ``HAS_GAS_SELECT``
* tares                   — ``HAS_TARE``
* valve hold              — ``HAS_VALVE_HOLD`` (one destructive verb inside)
* totalizer               — ``HAS_TOTALIZER`` (destructive reset)
* display lock / blink    — ``HAS_DISPLAY_CONTROL``

The setpoint widget is the only one that carries a payload value. Everything
else is a button or a small combo. We don't auto-generate the form from
Pydantic — the verb table is stable and small, and hand-laying the controls
keeps the labels precise.
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
    QWidget,
)

from capa.devices.adapter import Capability
from capa.experiment.config import DeviceConfig
from capa.ui.manual.cards.base import DeviceCard
from capa.ui.state import RunController
from capa.ui.statusbar import OperatorIdProvider

_logger = structlog.get_logger("capa.ui.manual.alicat")


SETPOINT_UNITS: Final[tuple[str, ...]] = (
    "SCCM",
    "SLPM",
    "CCS",
    "Pa",
    "kPa",
    "psia",
    "psig",
)

RELEVANT_CAPABILITIES: Final[tuple[Capability, ...]] = (
    Capability.HAS_SETPOINT,
    Capability.HAS_GAS_SELECT,
    Capability.HAS_TARE,
    Capability.HAS_VALVE_HOLD,
    Capability.HAS_TOTALIZER,
    Capability.HAS_DISPLAY_CONTROL,
    Capability.HAS_PARAMETER_CONFIG,
)


def is_alicat_device(spec: DeviceConfig) -> bool:
    """Filter predicate: ``True`` if ``device`` is an Alicat MFC/MFM."""
    return "alicat" in spec.adapter.lower()


class AlicatCard(DeviceCard):
    """Per-Alicat manual-control card.

    Capabilities are populated from the live adapter when it's already
    open; otherwise we render *all* possible sections and let the adapter
    reject unsupported verbs at command-time. The reject lands in the
    status label, so the operator gets a clear "this device is a meter,
    not a controller" message instead of a silently broken button.
    """

    def __init__(
        self,
        *,
        spec: DeviceConfig,
        controller: RunController,
        operator_provider: OperatorIdProvider,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            name=spec.name,
            title=f"Alicat: {spec.name}",
            controller=controller,
            operator_provider=operator_provider,
            parent=parent,
        )
        self._spec: DeviceConfig = spec
        # Pool-hosted adapter feeds the real capability set; fall back
        # to the Alicat default flags when the pool hasn't finished
        # opening.
        caps: frozenset[Capability] = frozenset()
        pool = controller.worker_pool
        if pool is not None:
            try:
                worker = pool.worker_for(spec.name)
                opened = worker.adapters.get(spec.name)
            except Exception:
                opened = None
            if opened is not None:
                caps = getattr(opened, "capabilities", frozenset())
        if not caps:
            caps = _default_alicat_capabilities()
        self._capabilities: frozenset[Capability] = caps
        self.set_subtitle(f"Device: {spec.name}   Adapter: {spec.adapter.rsplit('.', 1)[-1]}")
        self._gas_combo: QComboBox | None = None
        self._build_capability_sections()

    # ------------------------------------------------------------------ build

    def _build_capability_sections(self) -> None:
        if Capability.HAS_SETPOINT in self._capabilities:
            self._build_setpoint_section()
        if Capability.HAS_GAS_SELECT in self._capabilities:
            self._build_gas_section()
        if Capability.HAS_TARE in self._capabilities:
            self._build_tare_section()
        if Capability.HAS_VALVE_HOLD in self._capabilities:
            self._build_valve_section()
        if Capability.HAS_TOTALIZER in self._capabilities:
            self._build_totalizer_section()
        if Capability.HAS_DISPLAY_CONTROL in self._capabilities:
            self._build_display_section()

    def _build_setpoint_section(self) -> None:
        body = self.add_section("Setpoint")
        row = QHBoxLayout()
        row.setSpacing(6)
        value_label = QLabel("Value:", self)
        value_label.setMinimumWidth(80)
        row.addWidget(value_label)
        spin = QDoubleSpinBox(self)
        spin.setRange(-1_000_000.0, 1_000_000.0)
        spin.setDecimals(3)
        spin.setSingleStep(1.0)
        spin.setToolTip(
            "Setpoint value in the selected unit. Engineering-unit "
            "conversion is the adapter's responsibility."
        )
        row.addWidget(spin)
        unit_combo = QComboBox(self)
        unit_combo.addItems(list(SETPOINT_UNITS))
        unit_combo.setToolTip(
            "Engineering unit — passed through to alicatlib without "
            "client-side conversion. The device must accept the unit "
            "or the command rejects."
        )
        row.addWidget(unit_combo)
        btn = QPushButton("Set", self)

        def _apply() -> None:
            self.schedule_dispatch(
                kind="set_setpoint",
                payload={"value": spin.value(), "unit": unit_combo.currentText()},
            )

        btn.clicked.connect(_apply)
        row.addWidget(btn)
        row.addStretch(1)
        body.addLayout(row)
        for w in (spin, unit_combo, btn):
            self.register_action_widget(w)

    def _build_gas_section(self) -> None:
        body = self.add_section("Gas / fluid")
        row = QHBoxLayout()
        row.setSpacing(6)
        lbl = QLabel("Gas:", self)
        lbl.setMinimumWidth(80)
        row.addWidget(lbl)
        combo = QComboBox(self)
        combo.setEditable(True)
        combo.setToolTip(
            "Wire code or fluid name. The list is populated from the "
            "device on first acquire — until then, type a known name "
            "(e.g. 'N2', 'Air', 'CO2')."
        )
        # Populate with common defaults so the operator has something to
        # click on before the live read_gas_list() returns.
        combo.addItems(["N2", "Air", "Ar", "He", "CO2", "O2", "H2"])
        row.addWidget(combo)
        self._gas_combo = combo
        btn_set = QPushButton("Set (session)", self)
        btn_set_persist = QPushButton("Set + save (EEPROM)", self)

        def _apply(*, save: bool) -> None:
            self.schedule_dispatch(
                kind="set_gas",
                payload={"gas": combo.currentText(), "save": save},
                destructive=save,
                destructive_summary=(
                    f"Set gas to {combo.currentText()!r} AND persist to "
                    "EEPROM. Wears flash — only do this when the device "
                    "should boot with this gas after power-cycle."
                )
                if save
                else None,
            )

        btn_set.clicked.connect(lambda: _apply(save=False))
        btn_set_persist.clicked.connect(lambda: _apply(save=True))
        row.addWidget(btn_set)
        row.addWidget(btn_set_persist)
        row.addStretch(1)
        body.addLayout(row)
        for w in (combo, btn_set, btn_set_persist):
            self.register_action_widget(w)

    def _build_tare_section(self) -> None:
        body = self.add_section("Tare")
        row = QHBoxLayout()
        row.setSpacing(6)
        btn_flow = QPushButton("Tare flow", self)
        btn_flow.setToolTip(
            "Re-zero the flow reading at the current zero-flow condition. Block the line first."
        )
        btn_flow.clicked.connect(lambda: self.schedule_dispatch(kind="tare_flow"))
        row.addWidget(btn_flow)
        btn_abs = QPushButton("Tare ΔP (abs)", self)
        btn_abs.setToolTip("Re-zero absolute-pressure reading.")
        btn_abs.clicked.connect(lambda: self.schedule_dispatch(kind="tare_absolute_pressure"))
        row.addWidget(btn_abs)
        btn_gauge = QPushButton("Tare ΔP (gauge)", self)
        btn_gauge.setToolTip("Re-zero gauge-pressure reading.")
        btn_gauge.clicked.connect(lambda: self.schedule_dispatch(kind="tare_gauge_pressure"))
        row.addWidget(btn_gauge)
        row.addStretch(1)
        body.addLayout(row)
        for w in (btn_flow, btn_abs, btn_gauge):
            self.register_action_widget(w)

    def _build_valve_section(self) -> None:
        body = self.add_section("Valves")
        row = QHBoxLayout()
        row.setSpacing(6)
        btn_hold = QPushButton("Hold at current drive", self)
        btn_hold.setToolTip(
            "Freeze the valve drive at its current value. Reversible — "
            "Cancel hold returns to setpoint tracking."
        )
        btn_hold.clicked.connect(lambda: self.schedule_dispatch(kind="hold_valves"))
        row.addWidget(btn_hold)
        btn_closed = QPushButton("Hold closed!", self)
        btn_closed.setToolTip(
            "Force the valves fully closed. DESTRUCTIVE: kills downstream "
            "flow; only use if you intend to isolate the line."
        )
        btn_closed.clicked.connect(
            lambda: self.schedule_dispatch(
                kind="hold_valves_closed",
                destructive=True,
                destructive_summary=(
                    "Force the controller valves fully closed. This stops "
                    "flow immediately and overrides any setpoint until "
                    "Cancel hold is issued."
                ),
            )
        )
        row.addWidget(btn_closed)
        btn_cancel = QPushButton("Cancel hold", self)
        btn_cancel.setToolTip("Return to setpoint tracking.")
        btn_cancel.clicked.connect(lambda: self.schedule_dispatch(kind="cancel_valve_hold"))
        row.addWidget(btn_cancel)
        row.addStretch(1)
        body.addLayout(row)
        for w in (btn_hold, btn_closed, btn_cancel):
            self.register_action_widget(w)

    def _build_totalizer_section(self) -> None:
        body = self.add_section("Totalizer")
        row = QHBoxLayout()
        row.setSpacing(6)
        btn_reset = QPushButton("Reset total", self)
        btn_reset.setToolTip(
            "Zero the cumulative-flow counter. DESTRUCTIVE: discards accumulated volume history."
        )
        btn_reset.clicked.connect(
            lambda: self.schedule_dispatch(
                kind="totalizer_reset",
                payload={"totalizer": 1},
                destructive=True,
                destructive_summary=(
                    "Reset totalizer #1 to zero. Cumulative-flow history "
                    "since the last reset is discarded."
                ),
            )
        )
        row.addWidget(btn_reset)
        btn_reset_peak = QPushButton("Reset peak", self)
        btn_reset_peak.setToolTip("Zero the peak-flow watermark.")
        btn_reset_peak.clicked.connect(
            lambda: self.schedule_dispatch(
                kind="totalizer_reset_peak",
                payload={"totalizer": 1},
                destructive=True,
                destructive_summary="Reset totalizer #1 peak-flow watermark.",
            )
        )
        row.addWidget(btn_reset_peak)
        row.addStretch(1)
        body.addLayout(row)
        for w in (btn_reset, btn_reset_peak):
            self.register_action_widget(w)

    def _build_display_section(self) -> None:
        body = self.add_section("Display")
        row = QHBoxLayout()
        row.setSpacing(6)
        btn_blink = QPushButton("Blink 3s", self)
        btn_blink.setToolTip(
            "Flash the front-panel display so the operator can identify "
            "the physical device this card controls."
        )
        btn_blink.clicked.connect(
            lambda: self.schedule_dispatch(kind="blink_display", payload={"duration_s": 3})
        )
        row.addWidget(btn_blink)
        btn_lock = QPushButton("Lock", self)
        btn_lock.setToolTip("Lock the front-panel buttons.")
        btn_lock.clicked.connect(lambda: self.schedule_dispatch(kind="lock_display"))
        row.addWidget(btn_lock)
        btn_unlock = QPushButton("Unlock", self)
        btn_unlock.setToolTip(
            "Unlock the front-panel buttons. Always callable, even on "
            "devices that don't advertise HAS_DISPLAY_CONTROL — safety "
            "escape per the adapter docstring."
        )
        btn_unlock.clicked.connect(lambda: self.schedule_dispatch(kind="unlock_display"))
        row.addWidget(btn_unlock)
        row.addStretch(1)
        body.addLayout(row)
        for w in (btn_blink, btn_lock, btn_unlock):
            self.register_action_widget(w)

    # ------------------------------------------------------------------ live readback

    async def refresh_readback(self) -> None:
        """Refresh the gas combo from ``read_gas_list``. Skipped while a
        run is active (the adapter is busy streaming)."""
        if self._engine_blocks_writes() or self._gas_combo is None:
            return
        adapter = await self._ensure_adapter()
        if adapter is None:
            return
        reader = getattr(adapter, "read_gas_list", None)
        if not callable(reader):
            return
        try:
            gases = await reader()
        except Exception as exc:
            _logger.debug(
                "manual.alicat_readback_failed",
                device=self.device_name,
                error=str(exc),
            )
            return
        if not gases:
            return
        # Replace the combo's items, preserving the current text.
        current = self._gas_combo.currentText()
        self._gas_combo.clear()
        for _, label in sorted(gases.items()):
            self._gas_combo.addItem(label)
        # Restore selection if the operator's current pick is in the list.
        idx = self._gas_combo.findText(current)
        if idx >= 0:
            self._gas_combo.setCurrentIndex(idx)
        else:
            self._gas_combo.setEditText(current)


def _default_alicat_capabilities() -> frozenset[Capability]:
    """Optimistic default — render every section. Verbs that don't apply
    to this particular device get rejected at dispatch time with a clear
    detail string, which is more useful than silently hiding the button.
    See [src/capa/devices/alicat.py:226](../../../devices/alicat.py#L226)
    for the canonical flag list."""
    return frozenset(
        {
            Capability.HAS_TARE,
            Capability.HAS_GAS_SELECT,
            Capability.HAS_PARAMETER_CONFIG,
            Capability.HAS_DISPLAY_CONTROL,
            Capability.HAS_TOTALIZER,
            Capability.HAS_SETPOINT,
            Capability.HAS_VALVE_HOLD,
        }
    )


__all__ = ["AlicatCard", "is_alicat_device"]
