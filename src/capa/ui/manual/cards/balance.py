""":class:`BalanceCard` — manual control card for Sartorius balances.

Gated entirely on the adapter's :class:`Capability` flagset:

* tare / zero            — ``HAS_TARE`` / ``HAS_ZERO``
* internal cal           — ``HAS_INTERNAL_CAL`` (destructive)
* filter / app filter /
  stability range and
  delay / auto-zero /
  display unit / tare    — ``HAS_PARAMETER_CONFIG``
* save / reload menu     — ``HAS_PARAMETER_CONFIG`` (destructive — EEPROM)

The menu settings and the last calibration ("Last cal: 22.4 °C") are read
from the balance via :meth:`ManualClient.device_readback` (the adapter's
``read_state_snapshot``) once the pool is open, and again after each command
that changes them.
"""

from __future__ import annotations

from typing import Final, get_args

import structlog
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from capa.devices.adapter import Capability
from capa.devices.sartorius import (
    AppFilterLabel,
    AutoZeroLabel,
    FilterModeLabel,
    SartoriusStateSnapshot,
    StabilityDelayLabel,
    StabilityRangeLabel,
    TareBehaviorLabel,
)
from capa.experiment.config import DeviceConfig
from capa.ui.manual.cards.base import DeviceCard, select_or_add
from capa.ui.state import RunController
from capa.ui.statusbar import OperatorIdProvider

_logger = structlog.get_logger("capa.ui.manual.balance")


# The adapter's menu labels: what :class:`SartoriusStateSnapshot` reports,
# so a read-back selects its entry, and what the ``set_*`` commands accept.
FILTER_MODES: Final[tuple[str, ...]] = get_args(FilterModeLabel)
APP_FILTERS: Final[tuple[str, ...]] = get_args(AppFilterLabel)
STABILITY_RANGES: Final[tuple[str, ...]] = get_args(StabilityRangeLabel)
STABILITY_DELAYS: Final[tuple[str, ...]] = get_args(StabilityDelayLabel)
AUTO_ZERO_MODES: Final[tuple[str, ...]] = get_args(AutoZeroLabel)
DISPLAY_UNITS: Final[tuple[str, ...]] = ("g", "kg", "mg", "ct", "oz")
TARE_BEHAVIORS: Final[tuple[str, ...]] = get_args(TareBehaviorLabel)


# Capability flags that justify rendering a BalanceCard at all. Below any
# of these the card would be empty.
RELEVANT_CAPABILITIES: Final[tuple[Capability, ...]] = (
    Capability.HAS_TARE,
    Capability.HAS_ZERO,
    Capability.HAS_INTERNAL_CAL,
    Capability.HAS_PARAMETER_CONFIG,
)


def is_balance_device(spec: DeviceConfig) -> bool:
    """The adapter import path is the cheapest fingerprint we have for
    "this is a Sartorius adapter" without opening it. Mirrors how
    ``construct_adapters`` resolves classes."""
    return "sartorius" in spec.adapter.lower()


class BalanceCard(DeviceCard):
    """Per-balance manual-control card.

    Capabilities are read once at construction. The Sartorius adapter sets
    them in its constructor and never changes them, so we don't re-poll.
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
            title=f"Balance: {spec.name}",
            controller=controller,
            operator_provider=operator_provider,
            parent=parent,
        )
        self._spec: DeviceConfig = spec
        # Capabilities are read off the pool-hosted adapter when one
        # exists; otherwise fall back to the Sartorius default set.
        # The pool is opened asynchronously after
        # :meth:`set_active_config` returns, so the card may build before
        # the worker is up — the fallback is what keeps the UI consistent
        # in that window.
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
            caps = _default_sartorius_capabilities()
        self._capabilities: frozenset[Capability] = caps
        self.set_subtitle(f"Device: {spec.name}   Adapter: {self._adapter_label()}")
        # Parameter combos keyed by the SartoriusStateSnapshot field they show.
        self._param_combos: dict[str, QComboBox] = {}
        self._build_capability_sections()

    # ------------------------------------------------------------------ build

    def _build_capability_sections(self) -> None:
        if Capability.HAS_TARE in self._capabilities or Capability.HAS_ZERO in self._capabilities:
            self._build_tare_zero_section()
        if Capability.HAS_INTERNAL_CAL in self._capabilities:
            self._build_internal_cal_section()
        if Capability.HAS_PARAMETER_CONFIG in self._capabilities:
            self._build_parameters_section()
            self._build_persist_section()

    def _build_tare_zero_section(self) -> None:
        body = self.add_section("Tare / Zero")
        row = QHBoxLayout()
        row.setSpacing(6)
        if Capability.HAS_TARE in self._capabilities:
            btn_tare = QPushButton("Tare", self)
            btn_tare.setToolTip(
                "Zero the displayed weight at the current load. "
                "Combined tare (xBPI 0x14 / SBI 'ESC T')."
            )
            btn_tare.clicked.connect(lambda: self.schedule_dispatch(kind="tare"))
            self.register_action_widget(btn_tare)
            row.addWidget(btn_tare)
        if Capability.HAS_ZERO in self._capabilities:
            btn_zero = QPushButton("Zero", self)
            btn_zero.setToolTip(
                "Zero the displayed weight at the current load (xBPI 0x18). "
                "Distinct from Tare on multi-range balances."
            )
            btn_zero.clicked.connect(lambda: self.schedule_dispatch(kind="zero"))
            self.register_action_widget(btn_zero)
            row.addWidget(btn_zero)
        row.addStretch(1)
        body.addLayout(row)

    def _build_internal_cal_section(self) -> None:
        body = self.add_section("Internal calibration")
        row = QHBoxLayout()
        row.setSpacing(6)
        btn = QPushButton("Run internal calibration…", self)
        btn.setToolTip(
            "Motorized internal-weight adjustment. Forbidden mid-run. "
            "Drops the pan briefly while the motorized weight cycles."
        )
        btn.clicked.connect(
            lambda: self.schedule_dispatch_and_read_back(
                kind="internal_adjust",
                payload={"cal_type": None},
                destructive=True,
                destructive_summary=(
                    "Run internal calibration on the balance (motorized "
                    "weight). Sample must be off the pan."
                ),
            )
        )
        self.register_action_widget(btn)
        row.addWidget(btn)
        row.addStretch(1)
        body.addLayout(row)

    def _build_parameters_section(self) -> None:
        body = self.add_section("Parameters")
        self._param_combos["filter_mode"] = self._add_combo_row(
            body,
            label="Filter mode:",
            choices=FILTER_MODES,
            tooltip=(
                "Trade off settling time vs. resistance to bench vibration. "
                "Writes to xBPI p01 — runtime menu only until Save."
            ),
            apply_kind="set_filter_mode",
            payload_key="mode",
        )
        self._param_combos["app_filter"] = self._add_combo_row(
            body,
            label="App filter:",
            choices=APP_FILTERS,
            tooltip="Application filter (xBPI p02) — runtime menu only until Save.",
            apply_kind="set_app_filter",
            payload_key="mode",
        )
        self._param_combos["stability_range"] = self._add_combo_row(
            body,
            label="Stability range:",
            choices=STABILITY_RANGES,
            tooltip=(
                "How narrow a band the reading must stay in to count as stable "
                "(xBPI p03) — runtime menu only until Save."
            ),
            apply_kind="set_stability_range",
            payload_key="mode",
        )
        self._param_combos["stability_delay"] = self._add_combo_row(
            body,
            label="Stability delay:",
            choices=STABILITY_DELAYS,
            tooltip=(
                "How long the reading must stay in band before it's stable "
                "(xBPI p04) — runtime menu only until Save."
            ),
            apply_kind="set_stability_delay",
            payload_key="mode",
        )
        self._param_combos["auto_zero"] = self._add_combo_row(
            body,
            label="Auto-zero:",
            choices=AUTO_ZERO_MODES,
            tooltip="Toggle automatic zero-tracking (xBPI p06).",
            apply_kind="set_auto_zero",
            payload_key="mode",
        )
        self._param_combos["display_unit"] = self._add_combo_row(
            body,
            label="Display unit:",
            choices=DISPLAY_UNITS,
            tooltip="Front-panel weight unit (xBPI p07).",
            apply_kind="set_display_unit",
            payload_key="unit",
        )
        self._param_combos["tare_behavior"] = self._add_combo_row(
            body,
            label="Tare behavior:",
            choices=TARE_BEHAVIORS,
            tooltip="Whether a tare waits for a stable reading (xBPI p05).",
            apply_kind="set_tare_behavior",
            payload_key="mode",
        )

    def _build_persist_section(self) -> None:
        body = self.add_section("Persist menu (EEPROM)")
        row = QHBoxLayout()
        row.setSpacing(6)
        btn_save = QPushButton("Save to EEPROM", self)
        btn_save.setToolTip(
            "Write the current runtime menu to EEPROM (xBPI 0x47). Persistent across power-cycle."
        )
        btn_save.clicked.connect(
            lambda: self.schedule_dispatch_and_read_back(
                kind="save_menu",
                destructive=True,
                destructive_summary=(
                    "Save the current balance menu to EEPROM. "
                    "Persists across power-cycle and wears flash."
                ),
            )
        )
        self.register_action_widget(btn_save)
        row.addWidget(btn_save)
        btn_reload = QPushButton("Reload from EEPROM", self)
        btn_reload.setToolTip(
            "Reload the saved menu from EEPROM (xBPI 0x46). Discards unsaved runtime changes."
        )
        btn_reload.clicked.connect(
            lambda: self.schedule_dispatch_and_read_back(
                kind="reload_menu",
                destructive=True,
                destructive_summary=(
                    "Reload the saved menu from EEPROM — any unsaved "
                    "runtime parameter changes will be discarded."
                ),
            )
        )
        self.register_action_widget(btn_reload)
        row.addWidget(btn_reload)
        row.addStretch(1)
        body.addLayout(row)

    # ------------------------------------------------------------------ helpers

    def _add_combo_row(
        self,
        body: QVBoxLayout,
        *,
        label: str,
        choices: tuple[str, ...],
        tooltip: str,
        apply_kind: str,
        payload_key: str,
    ) -> QComboBox:
        row = QHBoxLayout()
        row.setSpacing(6)
        lbl = QLabel(label, self)
        lbl.setMinimumWidth(120)
        row.addWidget(lbl)
        combo = QComboBox(self)
        combo.addItems(list(choices))
        # Nothing selected until the balance reports its setting: a preset
        # first entry would read as the balance's current value.
        combo.setCurrentIndex(-1)
        combo.setPlaceholderText("not read yet")
        combo.setToolTip(tooltip)
        row.addWidget(combo)
        btn = QPushButton("Apply", self)

        def _apply() -> None:
            value = combo.currentText()
            if not value:
                self._set_status(f"pick a {label.rstrip(':').lower()} first", level="warn")
                return
            self.schedule_dispatch_and_read_back(
                kind=apply_kind,
                payload={payload_key: value},
            )

        btn.clicked.connect(_apply)
        row.addWidget(btn)
        row.addStretch(1)
        self.register_action_widget(combo)
        self.register_action_widget(btn)
        body.addLayout(row)
        return combo

    # ------------------------------------------------------------------ live readback

    async def refresh_readback(self) -> None:
        """Fetch the balance's menu settings and last calibration, then
        show them. The dock calls this on card build and pool open; the
        commands that change them call it after they land.

        Best-effort: a failure leaves the card as it is. Skipped while a
        run is active (the adapter is busy streaming) and while the pool is
        still opening — there is no client to ask yet.
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
                "manual.balance_readback_failed",
                device=self.device_name,
                error=str(exc),
            )
            return
        if isinstance(snapshot, SartoriusStateSnapshot):
            self.apply_snapshot(snapshot)

    def apply_snapshot(self, snapshot: SartoriusStateSnapshot) -> None:
        """Select each parameter combo's read-back value (none when the
        balance didn't report it) and show the last calibration."""
        for field, combo in self._param_combos.items():
            value = getattr(snapshot, field)
            if value is None:
                combo.setCurrentIndex(-1)
            else:
                select_or_add(combo, value)
        parts = [f"Device: {self._spec.name}", f"Adapter: {self._adapter_label()}"]
        # The record has no timestamp; the temperature at the calibration is
        # the one detail it carries.
        if snapshot.cal_on_record is False:
            parts.append("Last cal: none since power-up")
        elif snapshot.cal_on_record:
            temperature = snapshot.cal_temperature_c
            parts.append(
                "Last cal: on record" if temperature is None else f"Last cal: {temperature:.1f} °C"
            )
        self.set_subtitle("   ".join(parts))

    def _adapter_label(self) -> str:
        return self._spec.adapter.rsplit(".", 1)[-1]


def _default_sartorius_capabilities() -> frozenset[Capability]:
    """The flagset every Sartorius adapter declares — see
    [src/capa/devices/sartorius.py:266](../../../devices/sartorius.py#L266).
    Used as a fallback when the card is constructed before the adapter
    has been opened (lazy-connect path)."""
    return frozenset(
        {
            Capability.HAS_TARE,
            Capability.HAS_ZERO,
            Capability.EMITS_STABILITY_FLAG,
            Capability.HAS_INTERNAL_CAL,
            Capability.HAS_PARAMETER_CONFIG,
        }
    )


__all__ = ["BalanceCard", "is_balance_device"]
