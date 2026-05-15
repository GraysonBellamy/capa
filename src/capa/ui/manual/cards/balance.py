""":class:`BalanceCard` — manual control card for Sartorius balances.

Gated entirely on the adapter's :class:`Capability` flagset:

* tare / zero            — ``HAS_TARE`` / ``HAS_ZERO``
* internal cal           — ``HAS_INTERNAL_CAL`` (destructive)
* filter / auto-zero /
  display unit / tare    — ``HAS_PARAMETER_CONFIG``
* save / reload menu     — ``HAS_PARAMETER_CONFIG`` (destructive — EEPROM)

Live read-back ("Last cal: 2026-04-22 14:30 OK") is refreshed on card open
and after each successful command via :meth:`refresh_readback`.
"""

from __future__ import annotations

from typing import Final

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
from capa.experiment.config import DeviceConfig
from capa.ui.manual.cards.base import DeviceCard
from capa.ui.state import RunController
from capa.ui.statusbar import OperatorIdProvider

_logger = structlog.get_logger("capa.ui.manual.balance")


# Library-side fuzzy strings — sartoriuslib resolves these. Hardcoded here
# rather than enumerated from the library because the library currently
# exposes them through ``resolve_filter_mode`` (a free function), and the
# values are stable across firmware versions.
FILTER_MODES: Final[tuple[str, ...]] = (
    "very stable",
    "stable",
    "unstable",
    "very unstable",
)
AUTO_ZERO_MODES: Final[tuple[str, ...]] = ("off", "on")
DISPLAY_UNITS: Final[tuple[str, ...]] = ("g", "kg", "mg", "ct", "oz")
TARE_BEHAVIORS: Final[tuple[str, ...]] = ("manual", "auto")


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

    Capabilities are read once at construction. They never change for a
    live adapter (the adapter set them at ``open()``), so we don't re-poll.
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
        self.set_subtitle(f"Model: {spec.adapter.rsplit('.', 1)[-1]}   Use any control to connect")
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
            lambda: self.schedule_dispatch(
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
        self._filter_combo = self._add_combo_row(
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
        self._auto_zero_combo = self._add_combo_row(
            body,
            label="Auto-zero:",
            choices=AUTO_ZERO_MODES,
            tooltip="Toggle automatic zero-tracking (xBPI p06).",
            apply_kind="set_auto_zero",
            payload_key="mode",
        )
        self._unit_combo = self._add_combo_row(
            body,
            label="Display unit:",
            choices=DISPLAY_UNITS,
            tooltip="Front-panel weight unit (xBPI p07).",
            apply_kind="set_display_unit",
            payload_key="unit",
        )
        self._tare_combo = self._add_combo_row(
            body,
            label="Tare behavior:",
            choices=TARE_BEHAVIORS,
            tooltip="Tare key behavior (xBPI parameter).",
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
            lambda: self.schedule_dispatch(
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
            lambda: self.schedule_dispatch(
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
        combo.setToolTip(tooltip)
        row.addWidget(combo)
        btn = QPushButton("Apply", self)

        def _apply() -> None:
            self.schedule_dispatch(
                kind=apply_kind,
                payload={payload_key: combo.currentText()},
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
        """Call ``read_last_cal_record`` and refresh the subtitle. Called
        on card open and after successful destructive operations.

        Best-effort: a failure here just clears the subtitle to a neutral
        line — the operator can still use the card. Run-state-blocked
        because the adapter is busy streaming during sampling.
        """
        if self._engine_blocks_writes():
            return
        adapter = await self._ensure_adapter()
        if adapter is None:
            return
        reader = getattr(adapter, "read_last_cal_record", None)
        if not callable(reader):
            return
        try:
            cal = await reader()
        except Exception as exc:
            _logger.debug(
                "manual.balance_readback_failed",
                device=self.device_name,
                error=str(exc),
            )
            return
        # CalRecord shape varies across firmware — render whatever fields
        # it does expose without insisting on a specific layout.
        when = getattr(cal, "timestamp", None) or getattr(cal, "started_at", None)
        outcome = getattr(cal, "result", None) or getattr(cal, "status", None) or "—"
        when_s = when.isoformat(sep=" ", timespec="seconds") if when else "—"
        self.set_subtitle(
            f"Device: {self._spec.name}   "
            f"Adapter: {self._spec.adapter.rsplit('.', 1)[-1]}   "
            f"Last cal: {when_s} ({outcome})"
        )


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
