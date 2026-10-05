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

The active gas, the gas list and the setpoint are read from the device via
:meth:`ManualClient.device_readback` (the adapter's ``read_state_snapshot``)
once the pool is open, and again after each gas / setpoint command.
"""

from __future__ import annotations

from typing import Any, Final

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
from capa.devices.alicat import AlicatStateSnapshot
from capa.experiment.config import DeviceConfig
from capa.ui.manual.cards.base import DeviceCard, select_or_add
from capa.ui.state import RunController
from capa.ui.statusbar import OperatorIdProvider

_logger = structlog.get_logger("capa.ui.manual.alicat")


_UNKNOWN_SETPOINT_UNIT: Final[str] = "device units"

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

    Capabilities come from the live adapter once the pool has opened it.
    Before that the adapter doesn't yet know whether the device is a
    controller (``open()`` adds ``HAS_SETPOINT`` / ``HAS_VALVE_HOLD``), so
    we render *all* possible sections and let the adapter reject
    unsupported verbs at command-time. The first readback after the pool
    opens rebuilds the sections from the live flagset, so a meter loses
    its Setpoint / Valves sections.
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
        self._capabilities: frozenset[Capability] = (
            self._live_capabilities() or _default_alicat_capabilities()
        )
        self.set_subtitle(f"Device: {spec.name}   Adapter: {self._adapter_label()}")
        self._gas_combo: QComboBox | None = None
        self._setpoint_spin: QDoubleSpinBox | None = None
        self._setpoint_unit_label: QLabel | None = None
        # The device's setpoint unit from the last read-back; sent with each
        # setpoint so a unit changed on the device since is refused.
        self._setpoint_unit: str | None = None
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
        spin.setToolTip("Setpoint value, in the device's engineering units shown beside it.")
        row.addWidget(spin)
        # The device applies the value in its own engineering units; there is
        # no conversion, so the unit is shown, not chosen.
        unit_label = QLabel(self._setpoint_unit or _UNKNOWN_SETPOINT_UNIT, self)
        unit_label.setToolTip(
            "The device's setpoint unit, read from the device. Change it on "
            "the device itself; the setpoint is refused if the device's unit "
            "has changed since it was read."
        )
        row.addWidget(unit_label)
        btn = QPushButton("Set", self)

        def _apply() -> None:
            payload: dict[str, Any] = {"value": spin.value()}
            if self._setpoint_unit is not None:
                payload["unit"] = self._setpoint_unit
            self.schedule_dispatch_and_read_back(kind="set_setpoint", payload=payload)

        btn.clicked.connect(_apply)
        row.addWidget(btn)
        row.addStretch(1)
        body.addLayout(row)
        self._setpoint_spin = spin
        self._setpoint_unit_label = unit_label
        for w in (spin, btn):
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
            "Gas to select. The list and the active gas are read from the "
            "device once it is connected; you can also type a wire code or "
            "fluid name (e.g. 'N2', 'Air', 'CO2')."
        )
        # Empty until the device reports its gases: a placeholder entry
        # would read as the active gas.
        line_edit = combo.lineEdit()
        if line_edit is not None:
            line_edit.setPlaceholderText("not read from device yet")
        row.addWidget(combo)
        self._gas_combo = combo
        btn_set = QPushButton("Set (session)", self)
        btn_set_persist = QPushButton("Set + save (EEPROM)", self)

        def _apply(*, save: bool) -> None:
            gas = combo.currentText().strip()
            if not gas:
                self._set_status("pick or type a gas first", level="warn")
                return
            self.schedule_dispatch_and_read_back(
                kind="set_gas",
                payload={"gas": gas, "save": save},
                destructive=save,
                destructive_summary=(
                    f"Set gas to {gas!r} AND persist to "
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
        """Re-sync the sections to the opened adapter, then show its state.

        The dock calls this when the card is built and again once the pool
        has opened. Skipped while a run is active (the adapter is busy
        streaming) and while the pool is still opening — there is no
        client to ask yet, and the pool-open call follows.
        """
        if self._engine_blocks_writes():
            return
        client = self._controller.manual_client
        if client is None:
            return
        self._sync_capability_sections()
        try:
            snapshot = await client.device_readback(self._spec.name)
        except Exception as exc:
            _logger.debug(
                "manual.alicat_readback_failed",
                device=self.device_name,
                error=str(exc),
            )
            return
        if isinstance(snapshot, AlicatStateSnapshot):
            self.apply_snapshot(snapshot)

    def apply_snapshot(self, snapshot: AlicatStateSnapshot) -> None:
        """Show a read-back: active gas and setpoint in the subtitle, the
        gas list and active gas in the combo, the setpoint and its unit
        beside the spinbox."""
        combo = self._gas_combo
        if combo is not None:
            if snapshot.gas_list:
                combo.clear()
                combo.addItems(list(snapshot.gas_list))
            if snapshot.gas is not None:
                select_or_add(combo, snapshot.gas)
            else:
                # Adding items selects the first; that isn't the device's gas.
                combo.setCurrentIndex(-1)
        if snapshot.setpoint is not None and self._setpoint_spin is not None:
            self._setpoint_spin.setValue(snapshot.setpoint)
        self._setpoint_unit = snapshot.setpoint_unit
        if self._setpoint_unit_label is not None:
            self._setpoint_unit_label.setText(snapshot.setpoint_unit or _UNKNOWN_SETPOINT_UNIT)
        parts = [f"Device: {self._spec.name}", f"Adapter: {self._adapter_label()}"]
        if snapshot.gas is not None:
            parts.append(f"Gas: {snapshot.gas}")
        if snapshot.setpoint is not None:
            parts.append(f"Setpoint: {snapshot.setpoint:g} {snapshot.setpoint_unit or ''}".rstrip())
        self.set_subtitle("   ".join(parts))

    # ------------------------------------------------------------------ capabilities

    def _live_capabilities(self) -> frozenset[Capability]:
        """The opened adapter's flagset, or empty until the pool is open.

        Before ``open()`` identifies the device the adapter's flagset lacks
        the controller-only flags, so it can't gate the sections yet.
        """
        pool = self._controller.worker_pool
        if pool is None or not self._hardware_ready_for_writes():
            return frozenset()
        try:
            adapter = pool.worker_for(self._spec.name).adapters.get(self._spec.name)
        except Exception:
            return frozenset()
        return frozenset(getattr(adapter, "capabilities", frozenset()))

    def _sync_capability_sections(self) -> None:
        """Rebuild the sections if the opened adapter's flags differ from
        the ones the card was built with."""
        relevant = frozenset(RELEVANT_CAPABILITIES)
        live = self._live_capabilities()
        if not live or live & relevant == self._capabilities & relevant:
            return
        self._capabilities = live
        self.clear_sections()
        self._gas_combo = None
        self._setpoint_spin = None
        self._setpoint_unit_label = None
        self._build_capability_sections()

    def _adapter_label(self) -> str:
        return self._spec.adapter.rsplit(".", 1)[-1]


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
