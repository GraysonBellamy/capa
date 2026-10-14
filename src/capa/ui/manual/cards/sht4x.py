""":class:`Sht4xCard` — manual control card for the SHT4x ambient sensor.

Gated on the adapter's :class:`Capability` flagset:

* measurement precision — ``HAS_PARAMETER_CONFIG``
* heater pulse          — ``HAS_SENSOR_HEATER`` (destructive: skews the
  reading for a while after)
* soft reset            — ``HAS_PARAMETER_CONFIG``

The precision, the sensor's serial and its last reading come from
:meth:`ManualClient.device_readback` (the adapter's ``read_state_snapshot``)
once the pool is open, and again after each command.
"""

from __future__ import annotations

from typing import Final

import structlog
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from capa.devices.adapter import Capability
from capa.devices.sht4x import (
    HEATER_DURATIONS_S,
    HEATER_POWERS_MW,
    PRECISIONS,
    Sht4xStateSnapshot,
)
from capa.experiment.config import DeviceConfig
from capa.ui.manual.cards.base import DeviceCard, select_or_add
from capa.ui.state import RunController
from capa.ui.statusbar import OperatorIdProvider

_logger = structlog.get_logger("capa.ui.manual.sht4x")

RELEVANT_CAPABILITIES: Final[tuple[Capability, ...]] = (
    Capability.HAS_PARAMETER_CONFIG,
    Capability.HAS_SENSOR_HEATER,
)
"""Capability flags that justify rendering an :class:`Sht4xCard` at all."""


def is_sht4x_device(spec: DeviceConfig) -> bool:
    """The adapter import path is the cheapest fingerprint for "this is an
    SHT4x adapter" without opening it; the simulator matches too."""
    return "sht4x" in spec.adapter.lower()


class Sht4xCard(DeviceCard):
    """Per-sensor manual-control card."""

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
            title=f"Ambient sensor: {spec.name}",
            controller=controller,
            operator_provider=operator_provider,
            parent=parent,
        )
        self._spec: DeviceConfig = spec
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
            caps = frozenset(RELEVANT_CAPABILITIES)
        self._capabilities: frozenset[Capability] = caps
        self.set_subtitle(f"Device: {spec.name}   Adapter: {self._adapter_label()}")
        self._precision_combo: QComboBox | None = None
        self._power_combo: QComboBox | None = None
        self._duration_combo: QComboBox | None = None
        self._build_capability_sections()

    # ------------------------------------------------------------------ build

    def _build_capability_sections(self) -> None:
        if Capability.HAS_PARAMETER_CONFIG in self._capabilities:
            self._build_measurement_section()
        if Capability.HAS_SENSOR_HEATER in self._capabilities:
            self._build_heater_section()
        if Capability.HAS_PARAMETER_CONFIG in self._capabilities:
            self._build_maintenance_section()

    def _build_measurement_section(self) -> None:
        body = self.add_section("Measurement")
        row = QHBoxLayout()
        row.setSpacing(6)
        label = QLabel("Precision:", self)
        label.setMinimumWidth(120)
        row.addWidget(label)
        combo = QComboBox(self)
        combo.addItems(list(PRECISIONS))
        combo.setCurrentIndex(-1)
        combo.setPlaceholderText("not read yet")
        combo.setToolTip(
            "Measurement repeatability. High is an 8.3 ms conversion and the usual "
            "choice; medium and low shorten it and reduce self-heating at high rates."
        )
        row.addWidget(combo)
        button = QPushButton("Apply", self)

        def _apply() -> None:
            value = combo.currentText()
            if not value:
                self._set_status("pick a precision first", level="warn")
                return
            self.schedule_dispatch_and_read_back(
                kind="set_precision",
                payload={"precision": value},
            )

        button.clicked.connect(_apply)
        row.addWidget(button)
        row.addStretch(1)
        self.register_action_widget(combo)
        self.register_action_widget(button)
        body.addLayout(row)
        self._precision_combo = combo

    def _build_heater_section(self) -> None:
        body = self.add_section("Heater")
        row = QHBoxLayout()
        row.setSpacing(6)
        label = QLabel("Pulse:", self)
        label.setMinimumWidth(120)
        row.addWidget(label)
        power = QComboBox(self)
        for power_mw in HEATER_POWERS_MW:
            power.addItem(f"{power_mw} mW", power_mw)
        power.setToolTip("Heater power for one pulse.")
        row.addWidget(power)
        duration = QComboBox(self)
        for duration_s in HEATER_DURATIONS_S:
            duration.addItem(f"{duration_s:g} s", duration_s)
        duration.setToolTip("Heater on-time for one pulse.")
        row.addWidget(duration)
        button = QPushButton("Pulse heater…", self)
        button.setToolTip(
            "Run the sensor's heater once, to drive off condensation or check the "
            "humidity response. The reading is skewed warm and dry for a while after; "
            "never pulse during a measurement you care about. Refused above 65 °C "
            "and inside the ten-percent duty-cycle hold-off."
        )

        def _pulse() -> None:
            power_mw = power.currentData()
            duration_s = duration.currentData()
            self.schedule_dispatch_and_read_back(
                kind="heater_pulse",
                payload={"power_mw": int(power_mw), "duration_s": float(duration_s)},
                destructive=True,
                destructive_summary=(
                    f"Pulse the sensor heater at {power_mw} mW for {duration_s:g} s. "
                    "Temperature and humidity will read high and low for a while after."
                ),
                destructive_note=(
                    "Nothing persists on the sensor; the readings it logs for the next "
                    "minute or so are not ambient."
                ),
            )

        button.clicked.connect(_pulse)
        row.addWidget(button)
        row.addStretch(1)
        self.register_action_widget(power)
        self.register_action_widget(duration)
        self.register_action_widget(button)
        body.addLayout(row)
        self._power_combo = power
        self._duration_combo = duration

    def _build_maintenance_section(self) -> None:
        body = self.add_section("Maintenance")
        row = QHBoxLayout()
        row.setSpacing(6)
        button = QPushButton("Soft reset", self)
        button.setToolTip("Reset the sensor (datasheet command 0x94) and re-read its serial.")
        button.clicked.connect(lambda: self.schedule_dispatch_and_read_back(kind="soft_reset"))
        self.register_action_widget(button)
        row.addWidget(button)
        row.addStretch(1)
        body.addLayout(row)

    # ------------------------------------------------------------------ live readback

    async def refresh_readback(self) -> None:
        """Fetch the precision, identity and last reading, then show them.

        Best-effort: a failure leaves the card as it is. Skipped while a
        run is active and while the pool is still opening.
        """
        if self._engine_blocks_writes():
            return
        client = self._controller.manual_client
        if client is None:
            return
        try:
            snapshot = await client.device_readback(self._spec.name)
        except Exception as exc:
            _logger.debug(
                "manual.sht4x_readback_failed",
                device=self.device_name,
                error=str(exc),
            )
            return
        if isinstance(snapshot, Sht4xStateSnapshot):
            self.apply_snapshot(snapshot)

    def apply_snapshot(self, snapshot: Sht4xStateSnapshot) -> None:
        """Select the precision the sensor measures at and show its identity
        and last reading in the subtitle."""
        combo = self._precision_combo
        if combo is not None:
            if snapshot.precision is None:
                combo.setCurrentIndex(-1)
            else:
                select_or_add(combo, snapshot.precision)
        parts = [f"Device: {self._spec.name}", f"Adapter: {self._adapter_label()}"]
        if snapshot.sensor_serial:
            parts.append(f"Sensor: {snapshot.sensor_serial}")
        if snapshot.bridge_firmware:
            parts.append(f"Bridge: {snapshot.bridge_firmware}")
        if snapshot.last_temperature_c is not None and snapshot.last_humidity_pct is not None:
            parts.append(
                f"Last: {snapshot.last_temperature_c:.2f} °C  {snapshot.last_humidity_pct:.1f} %RH"
            )
        if snapshot.heater_pulses:
            age = snapshot.seconds_since_heater_pulse
            when = f"{age:.0f} s ago" if age is not None else "unknown"
            parts.append(f"Heater pulses: {snapshot.heater_pulses} (last {when})")
        self.set_subtitle("   ".join(parts))

    def _adapter_label(self) -> str:
        return self._spec.adapter.rsplit(".", 1)[-1]


__all__ = ["RELEVANT_CAPABILITIES", "Sht4xCard", "is_sht4x_device"]
