""":class:`FujiCard` — manual control card for a Fuji ZP-series gas analyzer.

Gated on the adapter's :class:`Capability` flagset:

* response times, ranges,
  output hold, calibration gas — ``HAS_PARAMETER_CONFIG``
* manual zero / span           — ``HAS_GAS_CALIBRATION``

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
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import structlog
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
from capa.devices.fuji import FujiStateSnapshot
from capa.devices.fuji_calibration import CalibrationStatus
from capa.experiment.config import DeviceConfig
from capa.ui.async_util import schedule_bg
from capa.ui.hold_to_confirm import HoldToConfirmButton
from capa.ui.manual.cards.base import DeviceCard
from capa.ui.state import RunController
from capa.ui.statusbar import OperatorIdProvider
from capa.ui.theme import COLOR_FAIL, COLOR_IDLE, COLOR_OK, COLOR_WARN, monospace_font

_logger = structlog.get_logger("capa.ui.manual.fuji")

RESPONSE_SLOTS: Final[tuple[str, ...]] = ("o2", "ndir1", "ndir2", "ndir3", "ndir4")
HOLD_MODES: Final[tuple[str, ...]] = ("last_value", "setting")
RANGE_METHODS: Final[tuple[str, ...]] = ("manual", "auto")
GAS_UNITS: Final[tuple[str, ...]] = ("vol%", "ppm")
CALIBRATION_KINDS: Final[tuple[str, ...]] = ("zero", "span")

DEFAULT_CALIBRATION_DIR: Final[str] = "configs/calibrations/analyzer"
"""Where calibration records are saved, relative to the working directory:
beside the heat-flux tune artifacts under ``configs/calibrations/flux``."""

READBACK_PERIOD_MS: Final[int] = 1000
"""How often a visible card reads the analyzer back while no run is active.
Idle, each read-back is one poll of the analyzer; during a calibration it
costs no traffic, since the calibration's own reads are shown."""

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

    Capabilities are read once at construction. The readings, the cached
    settings and the calibration's state come from the adapter's read-back
    (:class:`~capa.devices.fuji.FujiStateSnapshot`), fetched once a second
    while the card is visible and no run is active.
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
        self._channels: tuple[str, ...] = (
            tuple(str(channel).upper() for channel in channel_map)
            if isinstance(channel_map, Mapping) and channel_map
            else ("CH1", "CH2", "CH3")
        )
        self._settings: Mapping[str, Any] = {}
        self._calibration: CalibrationStatus = CalibrationStatus()
        self._saved_record_key: object | None = None
        self.last_saved_record: Path | None = None
        self._calibration_label: QLabel | None = None
        self._begin_button: QPushButton | None = None
        self._commit_button: HoldToConfirmButton | None = None
        self._cancel_button: QPushButton | None = None
        self.set_subtitle(f"Model: {spec.adapter.rsplit('.', 1)[-1]}   Waiting for a reading")
        self._build_capability_sections()

        self._timer = QTimer(self)
        self._timer.setInterval(READBACK_PERIOD_MS)
        self._timer.timeout.connect(self._on_timer)
        self._timer.start()

    # ------------------------------------------------------------------ build

    def _build_capability_sections(self) -> None:
        if Capability.HAS_PARAMETER_CONFIG in self._capabilities:
            self._build_settings_section()
            self._build_calibration_gas_section()
        if Capability.HAS_GAS_CALIBRATION in self._capabilities:
            self._build_calibration_section()

    def _build_settings_section(self) -> None:
        body = self.add_section("Settings")

        # Response time: per NDIR component slot and the O2 slot.
        row = self._row(body, "Response time:")
        slot = self._combo(row, RESPONSE_SLOTS, "The O2 slot or an NDIR component slot.")
        seconds = QSpinBox(self)
        seconds.setRange(0, 60)
        seconds.setSuffix(" s")
        seconds.setValue(15)
        seconds.setToolTip("0-60 s. 0 switches the analyzer's filter off.")
        self.register_action_widget(seconds)
        row.addWidget(seconds)
        self._apply_button(
            row,
            lambda: self.schedule_dispatch(
                kind="set_response_time",
                payload={"target": slot.currentText(), "seconds": seconds.value()},
            ),
        )

        # Range and range method per channel.
        row = self._row(body, "Range:")
        range_channel = self._combo(row, self._channels, "Analyzer channel.")
        range_number = self._combo(row, ("1", "2"), "Range 1 or 2; the method must be manual.")
        self._apply_button(
            row,
            lambda: self.schedule_dispatch(
                kind="set_range",
                payload={
                    "channel": range_channel.currentText(),
                    "range": int(range_number.currentText()),
                },
            ),
        )
        row = self._row(body, "Range method:")
        method_channel = self._combo(row, self._channels, "Analyzer channel.")
        method = self._combo(row, RANGE_METHODS, "How the channel changes range.")
        self._apply_button(
            row,
            lambda: self.schedule_dispatch(
                kind="set_range_method",
                payload={"channel": method_channel.currentText(), "method": method.currentText()},
            ),
        )

        # Output hold during a calibration.
        row = self._row(body, "Output hold:")
        hold = self._combo(
            row, ("off", "on"), "Hold the outputs, and the Modbus values, during a calibration."
        )
        self._apply_button(
            row,
            lambda: self.schedule_dispatch(
                kind="set_output_hold", payload={"enabled": hold.currentText() == "on"}
            ),
        )
        row = self._row(body, "Hold mode:")
        hold_mode = self._combo(row, HOLD_MODES, "What the outputs hold.")
        self._apply_button(
            row,
            lambda: self.schedule_dispatch(
                kind="set_hold_mode", payload={"mode": hold_mode.currentText()}
            ),
        )

        row = QHBoxLayout()
        row.setSpacing(6)
        back = QPushButton("Return to measurement", self)
        back.setToolTip(
            "Bring the analyzer's front panel back to the measurement screen. "
            "Refused while a calibration is under way."
        )
        back.clicked.connect(lambda: self.schedule_dispatch(kind="return_to_measurement"))
        self.register_action_widget(back)
        row.addWidget(back)
        row.addStretch(1)
        body.addLayout(row)

    def _build_calibration_gas_section(self) -> None:
        body = self.add_section("Calibration gas setting")
        # Which gas on one line, its concentration on the next: one line
        # of all six would set the width of the whole right-hand column.
        row = self._row(body, "Gas:")
        channel = self._combo(row, self._channels, "Analyzer channel.")
        number = self._combo(row, ("1", "2"), "Range the gas belongs to.")
        kind = self._combo(row, CALIBRATION_KINDS, "Zero gas or span gas.")
        row.addStretch(1)
        row = self._row(body, "Value:")
        value = QDoubleSpinBox(self)
        value.setDecimals(3)
        value.setRange(0.0, 100000.0)
        value.setToolTip("The concentration, in the unit of the range.")
        self.register_action_widget(value)
        row.addWidget(value)
        unit = self._combo(row, GAS_UNITS, "Must be the unit of the channel's range.")

        def _apply() -> None:
            summary = (
                f"Set the {kind.currentText()} gas of {channel.currentText()} range "
                f"{number.currentText()} to {value.value():g} {unit.currentText()} on "
                f"{self.device_name}."
            )
            self.schedule_dispatch(
                kind="set_calibration_gas",
                payload={
                    "channel": channel.currentText(),
                    "range": int(number.currentText()),
                    "kind": kind.currentText(),
                    "value": value.value(),
                    "unit": unit.currentText(),
                },
                destructive=True,
                destructive_summary=summary,
                destructive_note=CALIBRATION_GAS_NOTE,
            )

        self._apply_button(row, _apply)

    def _build_calibration_section(self) -> None:
        body = self.add_section("Zero / span calibration")
        row = self._row(body, "Calibrate:")
        self._cal_channel = self._combo(row, self._channels, "Analyzer channel to calibrate.")
        self._cal_kind = self._combo(row, CALIBRATION_KINDS, "Zero or span.")
        row.addStretch(1)
        row = self._row(body, "Gas value:")
        self._cal_value = QDoubleSpinBox(self)
        self._cal_value.setDecimals(3)
        self._cal_value.setRange(0.0, 100000.0)
        self._cal_value.setToolTip(
            "The gas at the inlet. It must equal the analyzer's calibration-gas setting."
        )
        self.register_action_widget(self._cal_value)
        row.addWidget(self._cal_value)
        self._cal_unit = self._combo(row, GAS_UNITS, "Unit of the gas value.")
        row.addStretch(1)

        row = self._row(body, "Gas label:")
        self._cal_label = QLineEdit(self)
        self._cal_label.setPlaceholderText("cylinder / lot, kept in the record")
        self.register_action_widget(self._cal_label)
        row.addWidget(self._cal_label)

        row = QHBoxLayout()
        row.setSpacing(6)
        plan = QPushButton("Plan", self)
        plan.setToolTip(
            "What this calibration would reach: every channel and range, and the "
            "calibration gas of each. Reads only."
        )
        plan.clicked.connect(self._on_plan)
        self.register_action_widget(plan)
        row.addWidget(plan)
        self._begin_button = QPushButton("Begin…", self)
        self._begin_button.setToolTip(
            "Take the analyzer's panel to the wait step for this channel. "
            "Put the gas at the inlet first."
        )
        self._begin_button.clicked.connect(self._on_begin)
        self.register_action_widget(self._begin_button)
        row.addWidget(self._begin_button)
        self._commit_button = HoldToConfirmButton(
            "Hold to calibrate", accent=COLOR_FAIL, parent=self
        )
        self._commit_button.setToolTip(
            "Overwrites the channel's calibration against the named gas. "
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

    def _on_plan(self) -> None:
        self.schedule_dispatch(
            kind="calibration_plan",
            payload={
                "channel": self._cal_channel.currentText(),
                "kind": self._cal_kind.currentText(),
            },
        )

    def _on_begin(self) -> None:
        channel = self._cal_channel.currentText()
        kind = self._cal_kind.currentText()
        value = self._cal_value.value()
        unit = self._cal_unit.currentText()
        label = self._cal_label.text().strip() or None
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
                f"Begin a {kind} calibration of {channel} on {self.device_name} against "
                f"{value:g} {unit}" + (f" ({label})" if label else "") + ". The gas must be at "
                "the inlet."
            ),
            destructive_note=CALIBRATION_BEGIN_NOTE,
        )

    def schedule_calibration(self, **dispatch: Any) -> None:
        """Dispatch a calibration command, then read back at once.

        The buttons follow the calibration's state, and a finished run's
        record is saved from the read-back, so neither waits for the timer.
        """
        if schedule_bg(self._dispatch_and_read_back(dispatch)) is None:
            self._set_status("no event loop — UI not running?", level="error")

    async def _dispatch_and_read_back(self, dispatch: dict[str, Any]) -> None:
        if await self.dispatch(**dispatch) is not None:
            await self.refresh_readback()

    # ------------------------------------------------------------------ helpers

    def _row(self, body: QVBoxLayout, label: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        lbl = QLabel(label, self)
        lbl.setMinimumWidth(120)
        row.addWidget(lbl)
        body.addLayout(row)
        return row

    def _combo(self, row: QHBoxLayout, choices: tuple[str, ...], tooltip: str) -> QComboBox:
        combo = QComboBox(self)
        combo.addItems(list(choices))
        combo.setToolTip(tooltip)
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
        """Show a read-back: the readings, the calibration's state, and a
        finished calibration's record saved to disk."""
        self._settings = snapshot.settings
        if snapshot.readings:
            self.set_subtitle(
                "   ".join(
                    f"{r.gas.upper()} "
                    + ("—" if r.value is None else f"{r.value:g}")
                    + f" {r.unit}"
                    + ("" if r.state == "ok" else f" [{r.state}]")
                    for r in snapshot.readings
                )
            )
        else:
            self.set_subtitle("No reading: the analyzer did not answer")
        self._apply_calibration(snapshot.calibration)

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
            text, color = _calibration_text(status)
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
        self._emit_manual_event(
            kind="calibration_record",
            severity="info",
            message=(
                f"{status.kind} of {status.channel} ended {status.outcome or 'without a result'}; "
                f"record saved to {path}"
            ),
        )


def _stamp(ended_at: object) -> str:
    """``20261001T153339Z`` from a record's end time; the time now without one."""
    moment = datetime.now(UTC)
    if isinstance(ended_at, str):
        with contextlib.suppress(ValueError):
            moment = datetime.fromisoformat(ended_at).astimezone(UTC)
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _calibration_text(status: CalibrationStatus) -> tuple[str, Any]:
    """The calibration readout and its colour."""
    if status.state == "idle":
        return "no calibration under way", COLOR_IDLE
    what = f"{status.kind} of {status.channel}"
    readings = "  ".join(
        f"{channel} " + ("—" if value is None else f"{value:g}")
        for channel, value in status.readings.items()
    )
    if status.state == "waiting":
        # The plan names every channel and range the key would calibrate.
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
