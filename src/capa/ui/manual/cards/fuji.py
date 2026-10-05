""":class:`FujiCard` — manual control card for a Fuji ZP-series gas analyzer.

Gated on the adapter's :class:`Capability` flagset:

* response time, range and range method per gas,
  output hold, calibration gas — ``HAS_PARAMETER_CONFIG``
* manual zero / span           — ``HAS_GAS_CALIBRATION``

The card names each channel by the gas the channel map asserts (``CO2``,
not ``CH1``), each range by its span (``0–25 vol%``, not ``1``), and each
option by what it does.

Every field shows the analyzer's setting, from the adapter's read-back
(:class:`~capa.devices.fuji.FujiStateSnapshot`): fetched once a second
while the card is visible and no run is active, and again after each
command. The readings are polled on every fetch; the settings are read
again once they are ten seconds old, and at once after a change made here.
A field stays empty until the analyzer has reported it. One the operator
has changed but not yet applied, or is editing, keeps the change through a
read-back.

The settings are written once and read back by the adapter; a calibration-gas
setting asks for confirmation first, because the next calibration is computed
from it.

A zero or span is a guarded sequence. **Plan** reads what the calibration
would reach: every channel and range, and the calibration gas of each. The
operator puts the gas at the inlet and names it, **Begin** takes the
analyzer's front panel to its wait step, and the card shows whether the
reading is steady on that gas, refreshed from the adapter's read-back.
**Calibrate** is a hold-to-confirm button, enabled only while the reading is
steady; **Cancel** leaves the wait step. When the run ends, its record is
saved as JSON under ``configs/calibrations/analyzer/``.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import structlog
from fujilib import ChannelId
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from capa.devices.adapter import Capability
from capa.devices.fuji import FujiChannelSettings, FujiRange, FujiStateSnapshot
from capa.devices.fuji_calibration import CalibrationStatus
from capa.devices.fuji_labels import (
    HOLD_MODE_NAMES,
    RANGE_METHOD_NAMES,
    channel_names,
    gas_name,
    is_measured,
)
from capa.experiment.config import DeviceConfig
from capa.ui.async_util import schedule_bg
from capa.ui.hold_to_confirm import HoldToConfirmButton
from capa.ui.manual.cards.base import DeviceCard
from capa.ui.state import RunController
from capa.ui.statusbar import OperatorIdProvider
from capa.ui.theme import COLOR_FAIL, COLOR_IDLE, COLOR_OK, COLOR_WARN, monospace_font

_logger = structlog.get_logger("capa.ui.manual.fuji")

NOT_READ: Final[str] = "not read yet"
"""What an empty field says until the analyzer has reported its value."""

RANGE_METHODS: Final[tuple[tuple[str, str], ...]] = tuple(
    (RANGE_METHOD_NAMES[method], method) for method in ("manual", "auto")
)
"""``(label, adapter value)`` of the range methods the card can set."""
HOLD_MODES: Final[tuple[tuple[str, str], ...]] = tuple(
    (HOLD_MODE_NAMES[mode], mode) for mode in ("last_value", "setting")
)
OUTPUT_HOLD: Final[tuple[tuple[str, bool], ...]] = (("Off", False), ("On", True))
CALIBRATION_KINDS: Final[tuple[tuple[str, str], ...]] = (("Zero", "zero"), ("Span", "span"))

DEFAULT_CALIBRATION_DIR: Final[str] = "configs/calibrations/analyzer"
"""Where calibration records are saved, relative to the working directory:
beside the heat-flux tune artifacts under ``configs/calibrations/flux``."""

READBACK_PERIOD_MS: Final[int] = 1000
"""How often a visible card reads the analyzer back while no run is active.
Idle, each read-back is one poll of the analyzer, plus a read of the
settings when they are old; during a calibration it costs no traffic,
since the calibration's own reads are shown."""

CALIBRATION_GAS_NOTE: Final[str] = (
    "The analyzer keeps this setting through a power cycle, and the next "
    "calibration of this range is computed from it."
)
CALIBRATION_BEGIN_NOTE: Final[str] = (
    "This presses the analyzer's calibration keys. Its front panel stays on the "
    "wait step, and its readings are marked calibrating, until you calibrate or "
    "cancel. Nothing is calibrated until you hold Calibrate."
)

# Capability flags that justify rendering a FujiCard at all.
RELEVANT_CAPABILITIES: Final[tuple[Capability, ...]] = (
    Capability.HAS_PARAMETER_CONFIG,
    Capability.HAS_GAS_CALIBRATION,
)


def is_fuji_device(spec: DeviceConfig) -> bool:
    """The adapter import path is the cheapest fingerprint we have for
    "this is a Fuji analyzer adapter" without opening it."""
    return "fuji" in spec.adapter.lower()


class FujiCard(DeviceCard):
    """Per-analyzer manual-control card.

    Capabilities are read once at construction. The gases come from the
    device's ``channel_map``, and from the read-back once there is one. The
    readings, the settings and the calibration's state come from the
    adapter's read-back (:class:`~capa.devices.fuji.FujiStateSnapshot`),
    fetched once a second while the card is visible and no run is active.
    """

    def __init__(
        self,
        *,
        spec: DeviceConfig,
        controller: RunController,
        operator_provider: OperatorIdProvider,
        calibration_dir: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            name=spec.name,
            title=f"Gas analyzer: {spec.name}",
            controller=controller,
            operator_provider=operator_provider,
            parent=parent,
        )
        self._spec: DeviceConfig = spec
        self._calibration_dir: Path = (
            calibration_dir if calibration_dir is not None else Path.cwd() / DEFAULT_CALIBRATION_DIR
        )
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
            caps = _default_fuji_capabilities()
        self._capabilities: frozenset[Capability] = caps
        channel_map = spec.params.get("channel_map")
        # Each channel by its gas; the measured ones (CH1-CH5) have settings.
        self._names: dict[str, str] = (
            channel_names({str(c): str(g) for c, g in channel_map.items()})
            if isinstance(channel_map, Mapping)
            else {}
        )
        self._measured: tuple[str, ...] = _measured(self._names)
        self._snapshot: FujiStateSnapshot = FujiStateSnapshot()
        self._calibration: CalibrationStatus = CalibrationStatus()
        self._saved_record_key: object | None = None
        self.last_saved_record: Path | None = None

        self._gas_combos: list[QComboBox] = []
        self._settings_gas: QComboBox | None = None
        self._response_spin: QSpinBox | None = None
        self._range_combo: QComboBox | None = None
        self._method_combo: QComboBox | None = None
        self._hold_combo: QComboBox | None = None
        self._hold_mode_combo: QComboBox | None = None
        self._gas_setting_gas: QComboBox | None = None
        self._gas_setting_range: QComboBox | None = None
        self._gas_setting_kind: QComboBox | None = None
        self._gas_setting_value: QDoubleSpinBox | None = None
        self._gas_setting_unit: QLabel | None = None
        self._cal_channel: QComboBox | None = None
        self._cal_kind: QComboBox | None = None
        self._cal_value: QDoubleSpinBox | None = None
        self._cal_unit: QLabel | None = None
        self._cal_setting: QLabel | None = None
        self._cal_label: QLineEdit | None = None
        self._calibration_label: QLabel | None = None
        self._begin_button: QPushButton | None = None
        self._commit_button: HoldToConfirmButton | None = None
        self._cancel_button: QPushButton | None = None
        self.set_subtitle(f"{self._identity_line()}   Waiting for a reading")
        self._build_capability_sections()

        self._timer = QTimer(self)
        self._timer.setInterval(READBACK_PERIOD_MS)
        self._timer.timeout.connect(self._on_timer)
        self._timer.start()

    # ------------------------------------------------------------------ build

    def _build_capability_sections(self) -> None:
        if Capability.HAS_PARAMETER_CONFIG in self._capabilities:
            self._build_settings_section()
            self._build_hold_section()
            self._build_calibration_gas_section()
        if Capability.HAS_GAS_CALIBRATION in self._capabilities:
            self._build_calibration_section()
        if Capability.HAS_PARAMETER_CONFIG in self._capabilities:
            self._build_front_panel_section()

    def _build_settings_section(self) -> None:
        body = self.add_section("Settings")
        row = self._row(body, "Gas:")
        self._settings_gas = self._gas_combo(row, "The gas whose settings are shown below.")
        self._settings_gas.currentIndexChanged.connect(self._on_settings_gas_changed)
        row.addStretch(1)

        row = self._row(body, "Response time:")
        spin = QSpinBox(self)
        spin.setMaximum(60)
        spin.setSuffix(" s")
        _show_number(spin, None)
        spin.setToolTip(
            "The response-time filter of this gas's reading, 0-60 s; 0 switches it off. "
            "Read from the analyzer."
        )
        spin.valueChanged.connect(lambda _value: self._unapplied_edits.add("response_time"))
        self.register_action_widget(spin)
        row.addWidget(spin)
        self._response_spin = spin
        self._apply_button(row, self._on_apply_response_time)

        row = self._row(body, "Range:")
        self._range_combo = self._choice_combo(
            row,
            "range",
            "The range this gas measures on, by its span. Selected only while the "
            "range method is Manual. A range in another unit stops a capa channel "
            "declared in the old one for the rest of a run.",
        )
        self._apply_button(row, self._on_apply_range)

        row = self._row(body, "Range method:")
        self._method_combo = self._choice_combo(
            row,
            "range_method",
            "Manual: the range above. Auto: up at 90 % of the low range, back down below 80 %.",
        )
        self._apply_button(row, self._on_apply_range_method)

    def _build_hold_section(self) -> None:
        body = self.add_section("Output hold during calibration")
        row = self._row(body, "Output hold:")
        self._hold_combo = self._choice_combo(
            row, "output_hold", "Hold the outputs, and the recorded values, during a calibration."
        )
        self._apply_button(row, self._on_apply_output_hold)
        row = self._row(body, "Hold mode:")
        self._hold_mode_combo = self._choice_combo(
            row,
            "hold_mode",
            "What the outputs hold: the last reading before the calibration, or "
            "each channel's preset value.",
        )
        self._apply_button(row, self._on_apply_hold_mode)

    def _build_calibration_gas_section(self) -> None:
        body = self.add_section("Calibration gas setting")
        # Which gas on one line, its concentration on the next: one line
        # of all of it would set the width of the whole right-hand column.
        row = self._row(body, "Gas:")
        self._gas_setting_gas = self._gas_combo(row, "The gas whose calibration gas is shown.")
        self._gas_setting_range = self._choice_combo(
            row, "gas_setting_range", "Each range has its own calibration gases."
        )
        self._gas_setting_kind = self._fixed_combo(row, CALIBRATION_KINDS, "Zero gas or span gas.")
        # A fill blocks these signals, so each change here is the operator's.
        self._gas_setting_gas.currentIndexChanged.connect(
            lambda _index: self._on_gas_setting_selected({"gas_setting_range"})
        )
        self._gas_setting_range.currentIndexChanged.connect(
            lambda _index: self._on_gas_setting_selected(set(), picked_range=True)
        )
        self._gas_setting_kind.currentIndexChanged.connect(
            lambda _index: self._on_gas_setting_selected(set())
        )
        row.addStretch(1)
        row = self._row(body, "Value:")
        value = QDoubleSpinBox(self)
        value.setDecimals(3)
        value.setMaximum(100000.0)
        _show_number(value, None)
        value.setToolTip(
            "The analyzer's calibration gas for this range, in the range's unit. "
            "Span gas is 1-105 % and zero gas 0-100 % of the range's full scale."
        )
        value.valueChanged.connect(lambda _value: self._unapplied_edits.add("calibration_gas"))
        self.register_action_widget(value)
        row.addWidget(value)
        self._gas_setting_value = value
        self._gas_setting_unit = QLabel("", self)
        row.addWidget(self._gas_setting_unit)
        self._apply_button(row, self._on_apply_calibration_gas)

    def _build_calibration_section(self) -> None:
        body = self.add_section("Zero / span calibration")
        row = self._row(body, "Calibrate:")
        self._cal_channel = self._gas_combo(row, "The gas to calibrate.")
        self._cal_kind = self._fixed_combo(row, CALIBRATION_KINDS, "Zero or span.")
        for selector in (self._cal_channel, self._cal_kind):
            selector.currentIndexChanged.connect(lambda _index: self._show_calibration_gas())
        row.addStretch(1)
        row = self._row(body, "Gas value:")
        value = QDoubleSpinBox(self)
        value.setDecimals(3)
        value.setRange(0.0, 100000.0)
        value.setToolTip(
            "The gas at the inlet. It must equal the analyzer's calibration-gas setting, "
            "shown beside it."
        )
        self.register_action_widget(value)
        row.addWidget(value)
        self._cal_value = value
        self._cal_unit = QLabel("", self)
        self._cal_unit.setToolTip("The unit of the range the gas measures on.")
        row.addWidget(self._cal_unit)
        row.addStretch(1)
        # On a line of its own, so it does not set the card's width.
        self._cal_setting = QLabel("", self)
        self._cal_setting.setWordWrap(True)
        self._cal_setting.setStyleSheet(f"color: {COLOR_IDLE.name()};")
        body.addWidget(self._cal_setting)

        row = self._row(body, "Gas label:")
        label = QLineEdit(self)
        label.setPlaceholderText("cylinder / lot, kept in the record")
        self.register_action_widget(label)
        row.addWidget(label)
        self._cal_label = label

        row = QHBoxLayout()
        row.setSpacing(6)
        plan = QPushButton("Plan", self)
        plan.setToolTip(
            "What this calibration would reach: every gas and range, and the "
            "calibration gas of each. Reads only."
        )
        plan.clicked.connect(self._on_plan)
        self.register_action_widget(plan)
        row.addWidget(plan)
        self._begin_button = QPushButton("Begin…", self)
        self._begin_button.setToolTip(
            "Take the analyzer's panel to the wait step for this gas. "
            "Put the gas at the inlet first."
        )
        self._begin_button.clicked.connect(self._on_begin)
        self.register_action_widget(self._begin_button)
        row.addWidget(self._begin_button)
        self._commit_button = HoldToConfirmButton(
            "Hold to calibrate", accent=COLOR_FAIL, parent=self
        )
        self._commit_button.setToolTip(
            "Overwrites the gas's calibration against the named gas. "
            "Enabled only while the reading is steady on it."
        )
        self._commit_button.confirmed.connect(
            lambda: self.schedule_calibration(kind="calibration_commit")
        )
        self._commit_button.setEnabled(False)
        row.addWidget(self._commit_button)
        self._cancel_button = QPushButton("Cancel", self)
        self._cancel_button.setToolTip("Leave the wait step without calibrating.")
        self._cancel_button.clicked.connect(
            lambda: self.schedule_calibration(kind="calibration_cancel")
        )
        self._cancel_button.setEnabled(False)
        row.addWidget(self._cancel_button)
        row.addStretch(1)
        body.addLayout(row)

        self._calibration_label = QLabel("no calibration under way", self)
        self._calibration_label.setFont(monospace_font(point_size=9))
        self._calibration_label.setWordWrap(True)
        self._calibration_label.setStyleSheet(f"color: {COLOR_IDLE.name()};")
        body.addWidget(self._calibration_label)

    def _build_front_panel_section(self) -> None:
        body = self.add_section("Front panel")
        row = QHBoxLayout()
        row.setSpacing(6)
        back = QPushButton("Return to measurement", self)
        back.setToolTip(
            "Bring the analyzer's front panel back to the measurement screen; a "
            "setting is refused while the panel is in a menu. Refused while a "
            "calibration is under way."
        )
        back.clicked.connect(lambda: self.schedule_dispatch(kind="return_to_measurement"))
        self.register_action_widget(back)
        row.addWidget(back)
        row.addStretch(1)
        body.addLayout(row)

    # ------------------------------------------------------------------ apply

    def _on_apply_response_time(self) -> None:
        spin = self._response_spin
        channel = self._current_channel(self._settings_gas)
        if spin is None or channel is None:
            self._set_status("no gas to set", level="warn")
            return
        if spin.value() < 0:
            self._set_status("pick a response time first", level="warn")
            return
        self._apply_field(
            "response_time",
            kind="set_response_time",
            payload={"target": channel, "seconds": spin.value()},
        )

    def _on_apply_range(self) -> None:
        combo = self._range_combo
        channel = self._current_channel(self._settings_gas)
        number = combo.currentData() if combo is not None else None
        if channel is None or number is None:
            self._set_status("ranges not read from the analyzer yet", level="warn")
            return
        settings = self._settings_of(channel)
        method = settings.range_method if settings is not None else None
        if method is not None and method != "manual":
            shown = RANGE_METHOD_NAMES.get(method, method)
            self._set_status(
                f"{self._gas_of(channel)}'s range method is {shown}; set it to Manual first",
                level="warn",
            )
            return
        self._apply_field("range", kind="set_range", payload={"channel": channel, "range": number})

    def _on_apply_range_method(self) -> None:
        combo = self._method_combo
        channel = self._current_channel(self._settings_gas)
        method = combo.currentData() if combo is not None else None
        if channel is None or method is None:
            self._set_status("pick a range method first", level="warn")
            return
        if method not in {value for _label, value in RANGE_METHODS}:
            shown = RANGE_METHOD_NAMES.get(method, method)
            self._set_status(f"{shown} is set at the analyzer, not from capa", level="warn")
            return
        self._apply_field(
            "range_method",
            kind="set_range_method",
            payload={"channel": channel, "method": method},
        )

    def _on_apply_output_hold(self) -> None:
        enabled = self._hold_combo.currentData() if self._hold_combo is not None else None
        if enabled is None:
            self._set_status("pick on or off first", level="warn")
            return
        self._apply_field("output_hold", kind="set_output_hold", payload={"enabled": enabled})

    def _on_apply_hold_mode(self) -> None:
        mode = self._hold_mode_combo.currentData() if self._hold_mode_combo is not None else None
        if mode is None:
            self._set_status("pick a hold mode first", level="warn")
            return
        self._apply_field("hold_mode", kind="set_hold_mode", payload={"mode": mode})

    def _on_apply_calibration_gas(self) -> None:
        channel = self._current_channel(self._gas_setting_gas)
        settings = self._settings_of(channel)
        number = self._gas_setting_range.currentData() if self._gas_setting_range else None
        kind = self._gas_setting_kind.currentData() if self._gas_setting_kind else None
        value = self._gas_setting_value.value() if self._gas_setting_value else -1.0
        selected = settings.range(number) if settings is not None else None
        if channel is None or selected is None or kind is None:
            self._set_status("ranges not read from the analyzer yet", level="warn")
            return
        if value < 0:
            self._set_status("enter the gas's concentration first", level="warn")
            return
        summary = (
            f"Set the {kind} gas of {self._gas_of(channel)} {selected.name} to "
            f"{value:g} {selected.unit} on {self.device_name}."
        )
        self._apply_field(
            "calibration_gas",
            kind="set_calibration_gas",
            payload={
                "channel": channel,
                "range": selected.number,
                "kind": kind,
                "value": value,
                "unit": selected.unit,
            },
            destructive=True,
            destructive_summary=summary,
            destructive_note=CALIBRATION_GAS_NOTE,
        )

    def _on_plan(self) -> None:
        channel = self._current_channel(self._cal_channel)
        if channel is None or self._cal_kind is None:
            self._set_status("no gas to calibrate", level="warn")
            return
        self.schedule_dispatch(
            kind="calibration_plan",
            payload={"channel": channel, "kind": self._cal_kind.currentData()},
        )

    def _on_begin(self) -> None:
        channel = self._current_channel(self._cal_channel)
        if channel is None or self._cal_kind is None or self._cal_value is None:
            self._set_status("no gas to calibrate", level="warn")
            return
        kind = self._cal_kind.currentData()
        value = self._cal_value.value()
        active = self._active_range(channel)
        unit = active.unit if active is not None else None
        if value != 0 and unit is None:
            self._set_status(
                "the analyzer's ranges are not read yet, so the gas has no unit", level="warn"
            )
            return
        label = (self._cal_label.text().strip() if self._cal_label is not None else "") or None
        gas = f"{value:g} {unit}" if unit else f"{value:g}"
        self.schedule_calibration(
            kind="calibration_begin",
            payload={
                "channel": channel,
                "kind": kind,
                "gas_value": value,
                # A zero gas of 0 needs no unit.
                "gas_unit": None if value == 0 else unit,
                "gas_label": label,
            },
            destructive=True,
            destructive_summary=(
                f"Begin a {kind} calibration of {self._gas_of(channel)} on {self.device_name} "
                f"against {gas}" + (f" ({label})" if label else "") + ". The gas must be at "
                "the inlet."
            ),
            destructive_note=CALIBRATION_BEGIN_NOTE,
        )

    def schedule_calibration(self, **dispatch: Any) -> None:
        """Dispatch a calibration command, then read back at once.

        The buttons follow the calibration's state, and a finished run's
        record is saved from the read-back, so neither waits for the timer.
        """
        self.schedule_dispatch_and_read_back(**dispatch)

    # ------------------------------------------------------------------ helpers

    def _row(self, body: QVBoxLayout, label: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        lbl = QLabel(label, self)
        lbl.setMinimumWidth(120)
        row.addWidget(lbl)
        body.addLayout(row)
        return row

    def _gas_combo(self, row: QHBoxLayout, tooltip: str) -> QComboBox:
        """A gas picker: each measured channel by its gas, the channel id as
        item data."""
        combo = QComboBox(self)
        combo.setPlaceholderText("no gases")
        combo.setToolTip(tooltip)
        for channel in self._measured:
            combo.addItem(self._gas_of(channel), channel)
        # With a placeholder, Qt leaves the first item added unselected.
        combo.setCurrentIndex(0 if combo.count() else -1)
        self.register_action_widget(combo)
        row.addWidget(combo)
        self._gas_combos.append(combo)
        return combo

    def _fixed_combo(
        self, row: QHBoxLayout, choices: Sequence[tuple[str, object]], tooltip: str
    ) -> QComboBox:
        """A picker of fixed ``(label, value)`` choices the operator selects
        from; not a setting the analyzer reports."""
        combo = QComboBox(self)
        for label, value in choices:
            combo.addItem(label, value)
        combo.setToolTip(tooltip)
        self.register_action_widget(combo)
        row.addWidget(combo)
        return combo

    def _choice_combo(self, row: QHBoxLayout, field: str, tooltip: str) -> QComboBox:
        """A setting's picker: empty until a read-back fills it, and ``field``
        counted as changed once the operator picks."""
        combo = QComboBox(self)
        combo.setPlaceholderText(NOT_READ)
        combo.setToolTip(tooltip)
        # ``activated`` fires only for the operator's pick, not for a fill.
        combo.activated.connect(lambda _index: self._unapplied_edits.add(field))
        self.register_action_widget(combo)
        row.addWidget(combo)
        return combo

    def _apply_button(self, row: QHBoxLayout, apply: Any) -> QPushButton:
        btn = QPushButton("Apply", self)
        btn.clicked.connect(apply)
        self.register_action_widget(btn)
        row.addWidget(btn)
        row.addStretch(1)
        return btn

    def _gas_of(self, channel: str) -> str:
        return self._names.get(channel, channel)

    @staticmethod
    def _current_channel(combo: QComboBox | None) -> str | None:
        data = combo.currentData() if combo is not None else None
        return data if isinstance(data, str) else None

    def _settings_of(self, channel: str | None) -> FujiChannelSettings | None:
        return next((c for c in self._snapshot.channels if c.channel == channel), None)

    def _active_range(self, channel: str | None) -> FujiRange | None:
        settings = self._settings_of(channel)
        return settings.range(settings.current_range) if settings is not None else None

    def _identity_line(self) -> str:
        return f"Device: {self._spec.name}   Adapter: {self._spec.adapter.rsplit('.', 1)[-1]}"

    def _fill_combo(
        self,
        field: str,
        combo: QComboBox | None,
        choices: Sequence[tuple[str, object]],
        active: object | None,
    ) -> None:
        """Offer ``choices`` (``(label, value)``) and select the analyzer's
        ``active`` value. The operator's unapplied pick for ``field`` stays
        selected while it is still offered; an open list is left alone."""
        if combo is None or combo.view().isVisible():
            return
        offered = [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]
        pick = combo.currentData() if field in self._unapplied_edits else None
        combo.blockSignals(True)
        try:
            if offered != list(choices):
                combo.clear()
                for label, value in choices:
                    combo.addItem(label, value)
            index = _find(combo, pick)
            if index < 0:
                self._unapplied_edits.discard(field)
                index = _find(combo, active)
            if combo.currentIndex() != index:
                combo.setCurrentIndex(index)
        finally:
            combo.blockSignals(False)

    def _fill_number(
        self, field: str, spin: QSpinBox | QDoubleSpinBox | None, value: float | None
    ) -> None:
        """Show the analyzer's ``value`` unless the operator has changed the
        field and not applied it, or is typing in it."""
        if spin is None or field in self._unapplied_edits or spin.hasFocus():
            return
        _show_number(spin, value)

    # ------------------------------------------------------------------ live readback

    def _on_timer(self) -> None:
        if not self.isVisible() or self._engine_blocks_writes():
            return
        _ = schedule_bg(self.refresh_readback())

    async def refresh_readback(self) -> None:
        """Fetch the adapter's read-back and apply it.

        Best-effort: a failure leaves the card as it is. Run-state-blocked,
        like every manual-card read: during a run the Run tab shows the data.
        """
        if self._engine_blocks_writes():
            return
        client = self._controller.manual_client
        if client is None:
            return
        try:
            snapshot = await client.device_readback(self._spec.name)
        except Exception as exc:
            _logger.debug("manual.fuji_readback_failed", device=self.device_name, error=str(exc))
            return
        if isinstance(snapshot, FujiStateSnapshot):
            self.apply_snapshot(snapshot)

    def apply_snapshot(self, snapshot: FujiStateSnapshot) -> None:
        """Show a read-back: the readings, every setting, the calibration's
        state, and a finished calibration's record saved to disk."""
        self._snapshot = snapshot
        self._sync_gases(snapshot)
        self._show_settings()
        self._fill_combo("output_hold", self._hold_combo, OUTPUT_HOLD, snapshot.output_hold)
        self._fill_combo("hold_mode", self._hold_mode_combo, HOLD_MODES, snapshot.hold_mode)
        self._show_gas_setting()
        self._show_calibration_gas()
        if snapshot.readings:
            readings = "   ".join(
                f"{self._names.get(r.channel) or gas_name(r.gas)} "
                + ("—" if r.value is None else f"{r.value:g}")
                + f" {r.unit}"
                + ("" if r.state == "ok" else f" [{r.state}]")
                for r in snapshot.readings
            )
            self.set_subtitle(f"{self._identity_line()}   {readings}")
        else:
            self.set_subtitle(f"{self._identity_line()}   No reading: the analyzer did not answer")
        self._apply_calibration(snapshot.calibration)

    def _sync_gases(self, snapshot: FujiStateSnapshot) -> None:
        """Take the gases from the read-back, which names every measured
        channel the adapter's channel map asserts."""
        if not snapshot.channels:
            return
        names = {**self._names, **{c.channel: c.name for c in snapshot.channels}}
        measured = tuple(c.channel for c in snapshot.channels)
        if names == self._names and measured == self._measured:
            return
        self._names, self._measured = names, measured
        choices = [(self._gas_of(channel), channel) for channel in measured]
        for combo in self._gas_combos:
            if combo.view().isVisible():
                continue
            selected = combo.currentData()
            combo.blockSignals(True)
            try:
                combo.clear()
                for label, channel in choices:
                    combo.addItem(label, channel)
                combo.setCurrentIndex(max(_find(combo, selected), 0))
            finally:
                combo.blockSignals(False)

    def _show_settings(self) -> None:
        """The selected gas's response time, range and range method."""
        settings = self._settings_of(self._current_channel(self._settings_gas))
        if settings is None:
            self._fill_number("response_time", self._response_spin, None)
            self._fill_combo("range", self._range_combo, (), None)
            self._fill_combo("range_method", self._method_combo, RANGE_METHODS, None)
            return
        self._fill_number("response_time", self._response_spin, settings.response_time_s)
        self._fill_combo(
            "range",
            self._range_combo,
            [(r.name, r.number) for r in settings.ranges],
            settings.current_range,
        )
        methods: list[tuple[str, object]] = list(RANGE_METHODS)
        method = settings.range_method
        if method is not None and method not in {value for _label, value in RANGE_METHODS}:
            # Remote follows a contact input; shown, not offered.
            methods.append((RANGE_METHOD_NAMES.get(method, method), method))
        self._fill_combo("range_method", self._method_combo, methods, method)

    def _on_settings_gas_changed(self, _index: int) -> None:
        self._unapplied_edits.difference_update({"response_time", "range", "range_method"})
        self._show_settings()

    def _show_gas_setting(self) -> None:
        """The selected gas's ranges, and the calibration gas of the selected
        range and kind."""
        settings = self._settings_of(self._current_channel(self._gas_setting_gas))
        ranges = settings.ranges if settings is not None else ()
        self._fill_combo(
            "gas_setting_range",
            self._gas_setting_range,
            [(r.name, r.number) for r in ranges],
            settings.current_range if settings is not None else None,
        )
        number = self._gas_setting_range.currentData() if self._gas_setting_range else None
        selected = settings.range(number) if settings is not None else None
        kind = self._gas_setting_kind.currentData() if self._gas_setting_kind else None
        gas = None
        if selected is not None:
            gas = selected.zero_gas if kind == "zero" else selected.span_gas
        self._fill_number("calibration_gas", self._gas_setting_value, gas)
        if self._gas_setting_unit is not None:
            self._gas_setting_unit.setText(selected.unit if selected is not None else "")

    def _on_gas_setting_selected(self, drop: set[str], *, picked_range: bool = False) -> None:
        """Another gas, range or kind: show its calibration gas, dropping
        the value typed for the last one."""
        self._unapplied_edits.difference_update({"calibration_gas", *drop})
        if picked_range:
            self._unapplied_edits.add("gas_setting_range")
        self._show_gas_setting()

    def _show_calibration_gas(self) -> None:
        """Beside the gas the operator names: the unit of the range the gas
        measures on, and the analyzer's calibration gas for it."""
        if self._cal_unit is None or self._cal_setting is None or self._cal_kind is None:
            return
        channel = self._current_channel(self._cal_channel)
        active = self._active_range(channel)
        self._cal_unit.setText(active.unit if active is not None else "")
        kind = self._cal_kind.currentData()
        if active is None:
            self._cal_setting.setText("")
            return
        setting = active.zero_gas if kind == "zero" else active.span_gas
        shown = "unreadable" if setting is None else f"{setting:g} {active.unit}"
        self._cal_setting.setText(f"analyzer's {kind} gas ({active.name}): {shown}")

    def _apply_calibration(self, status: CalibrationStatus) -> None:
        self._calibration = status
        waiting = status.state == "waiting"
        enabled = self._manual_controls_enabled()
        if self._commit_button is not None:
            self._commit_button.setEnabled(enabled and waiting and bool(status.steady))
        if self._cancel_button is not None:
            self._cancel_button.setEnabled(enabled and waiting)
        if self._begin_button is not None:
            self._begin_button.setEnabled(
                enabled and status.state not in ("starting", "waiting", "calibrating")
            )
        label = self._calibration_label
        if label is not None:
            text, color = _calibration_text(status, self._names)
            label.setText(text)
            label.setStyleSheet(f"color: {color.name()};")
        if status.state == "ended" and status.record is not None:
            self._save_record(status)

    def _save_record(self, status: CalibrationStatus) -> None:
        """Save a finished run's record once, never over an existing file.

        A run refused before its first key did nothing at the analyzer and
        leaves no file. The file is named by the time the run ended, so a
        card built again while the adapter still holds the run finds the
        file it wrote and does not write a second.
        """
        record = status.record
        if record is None or (status.outcome is None and not record.get("keys")):
            return
        ended_at = record.get("run_ended_at")
        key = ended_at or id(record)
        if key == self._saved_record_key:
            return
        self._saved_record_key = key
        analyzer = record.get("analyzer")
        serial = analyzer.get("serial_number") if isinstance(analyzer, Mapping) else None
        stem = f"{serial or self.device_name}_{status.channel}_{status.kind}_{_stamp(ended_at)}"
        text = json.dumps(dict(record), indent=2, default=str)
        try:
            self._calibration_dir.mkdir(parents=True, exist_ok=True)
            path = self._calibration_dir / f"{stem}.json"
            suffix = 1
            while path.exists():
                if path.read_text(encoding="utf-8") == text:
                    self.last_saved_record = path
                    return
                suffix += 1
                path = self._calibration_dir / f"{stem}_{suffix}.json"
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            self._set_status(f"calibration record not saved: {exc}", level="error")
            self._emit_manual_event(
                kind="calibration_record", severity="error", message=f"record not saved: {exc}"
            )
            return
        self.last_saved_record = path
        name = status.channel_name or self._gas_of(status.channel or "")
        self._emit_manual_event(
            kind="calibration_record",
            severity="info",
            message=(
                f"{status.kind} of {name} ended {status.outcome or 'without a result'}; "
                f"record saved to {path}"
            ),
        )


def _measured(names: Mapping[str, str]) -> tuple[str, ...]:
    """The measured channels of ``names``, in channel order."""
    return tuple(sorted((c for c in names if is_measured(c)), key=lambda c: ChannelId(c).number))


def _find(combo: QComboBox, value: object | None) -> int:
    """The index of the item whose data is ``value``; -1 if none is."""
    if value is None:
        return -1
    return next((i for i in range(combo.count()) if combo.itemData(i) == value), -1)


def _show_number(spin: QSpinBox | QDoubleSpinBox, value: float | None) -> None:
    """Show ``value`` in ``spin`` without counting it as an operator edit.

    ``None`` shows :data:`NOT_READ`: the spinbox's minimum drops to -1 and
    Qt shows the special text there. A value puts the minimum back at 0
    and drops the text, which Qt would otherwise show for a real 0.
    """
    spin.blockSignals(True)
    try:
        spin.setSpecialValueText(NOT_READ if value is None else "")
        if isinstance(spin, QSpinBox):
            spin.setMinimum(-1 if value is None else 0)
            spin.setMaximum(max(spin.maximum(), int(value or 0)))
            spin.setValue(-1 if value is None else int(value))
        else:
            spin.setMinimum(-1.0 if value is None else 0.0)
            spin.setMaximum(max(spin.maximum(), value or 0.0))
            spin.setValue(-1.0 if value is None else value)
    finally:
        spin.blockSignals(False)


def _stamp(ended_at: object) -> str:
    """``20261001T153339Z`` from a record's end time; the time now without one."""
    moment = datetime.now(UTC)
    if isinstance(ended_at, str):
        with contextlib.suppress(ValueError):
            moment = datetime.fromisoformat(ended_at).astimezone(UTC)
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _calibration_text(
    status: CalibrationStatus, names: Mapping[str, str] | None = None
) -> tuple[str, Any]:
    """The calibration readout and its colour, each channel by its gas."""
    named = names or {}
    if status.state == "idle":
        return "no calibration under way", COLOR_IDLE
    channel = status.channel or ""
    what = f"{status.kind} of {status.channel_name or named.get(channel, channel)}"
    readings = "  ".join(
        f"{named.get(ch, ch)} " + ("—" if value is None else f"{value:g}")
        for ch, value in status.readings.items()
    )
    if status.state == "waiting":
        # The plan names every gas and range the key would calibrate.
        plan = status.plan or what
        if status.steady:
            return f"{plan}: STEADY on the gas — hold Calibrate.   {readings}", COLOR_OK
        why = "; ".join(status.reasons) or "waiting for the first read"
        return f"{plan}: waiting for the gas to settle ({why}).   {readings}", COLOR_WARN
    if status.state in ("starting", "calibrating"):
        return f"{what}: {status.state}…", COLOR_WARN
    outcome = status.outcome or "without a result"
    if status.error is not None or status.clean is False:
        return (
            f"{what} ended {outcome}: {status.error or 'the panel was not left clean'}",
            COLOR_FAIL,
        )
    return f"{what} ended: {outcome}", COLOR_OK if outcome == "completed" else COLOR_IDLE


def _default_fuji_capabilities() -> frozenset[Capability]:
    """The flagset every Fuji adapter declares. Used as a fallback when the
    card is constructed before the adapter has been opened."""
    return frozenset({Capability.HAS_PARAMETER_CONFIG, Capability.HAS_GAS_CALIBRATION})


__all__ = ["FujiCard", "is_fuji_device"]
