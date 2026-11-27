""":class:`BalanceCard` — manual control card for Sartorius balances.

Gated entirely on the adapter's :class:`Capability` flagset:

* tare / zero            — ``HAS_TARE`` / ``HAS_ZERO``
* internal cal           — ``HAS_INTERNAL_CAL`` (destructive)
* filter / app filter /
  stability range and
  delay / auto-zero /
  display unit / tare    — ``HAS_PARAMETER_CONFIG``
* save / reload menu     — ``HAS_PARAMETER_CONFIG`` (destructive — EEPROM)

The menu settings and the last calibration ("Last cal: 22.4 °C at …") are
read from the balance via :meth:`ManualClient.device_readback` (the
adapter's ``read_state_snapshot``) once the pool is open, and again after
each command that changes them.

The internal calibration takes ~25 s. While it runs the card shows its
progress and a Cancel button, greys out its other controls, and then says
how it ended.
"""

from __future__ import annotations

import time
from typing import Final, get_args

import structlog
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from capa.devices.adapter import Capability
from capa.devices.sartorius import (
    AdjustmentOutcome,
    AppFilterLabel,
    AutoZeroLabel,
    BalanceAdjustmentError,
    FilterModeLabel,
    SartoriusStateSnapshot,
    StabilityDelayLabel,
    StabilityRangeLabel,
    TareBehaviorLabel,
)
from capa.experiment.config import DeviceConfig
from capa.ui.async_util import schedule_bg
from capa.ui.manual.cards.base import DeviceCard, select_or_add
from capa.ui.state import RunController, RunUiState
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


TYPICAL_CAL_S: Final[int] = 25
"""How long an internal calibration usually takes (24.3–24.5 s on the rig's
MSE1203S); the progress bar fills over this time."""

CALIBRATING_TEXT: Final[str] = "Calibrating — keep the pan clear"

_CAL_ENDINGS: Final[dict[AdjustmentOutcome, tuple[str, str]]] = {
    "aborted": (
        "Calibration stopped before it finished, from the balance's front panel or by "
        "the balance itself (pan not clear, or the bench too unsteady). The previous "
        "calibration is kept.",
        "warn",
    ),
    "timeout": (
        "No result within the time limit (the adapter's adjust_timeout_s). capa stopped "
        "waiting but did not stop the balance: check its display before weighing.",
        "error",
    ),
    "not_started": (
        "The balance accepted the command but never started calibrating. Check its "
        "display for a message, clear the pan and try again.",
        "error",
    ),
    "in_progress": (
        "A calibration was already running on the balance. Wait for it to finish.",
        "warn",
    ),
}
"""Status line and level for each way a calibration can end without a new
calibration, other than a Cancel from this card."""

CANCELLED_TEXT: Final[str] = "Calibration cancelled. The balance kept its previous calibration."


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
        # Internal calibration: when the one in flight started (monotonic
        # seconds; ``None`` when none is) and whether Cancel was sent for it.
        self._cal_started: float | None = None
        self._cancel_sent: bool = False
        self._cancel_button: QPushButton | None = None
        self._cal_progress: QProgressBar | None = None
        self._cal_timer = QTimer(self)
        self._cal_timer.setInterval(250)
        self._cal_timer.timeout.connect(self._show_cal_progress)
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
            "Motorized internal-weight adjustment, about 25 s. Forbidden mid-run. "
            "Drops the pan briefly while the motorized weight cycles; the card "
            "says when it is done."
        )
        btn.clicked.connect(self._on_run_calibration)
        self.register_action_widget(btn)
        row.addWidget(btn)
        # Not an action widget: enabled only while a calibration runs.
        cancel = QPushButton("Cancel", self)
        cancel.setToolTip(
            "Stop the calibration (xBPI 0x29). The balance unloads its internal "
            "weight and keeps its previous calibration."
        )
        cancel.clicked.connect(self._on_cancel_calibration)
        cancel.setEnabled(False)
        row.addWidget(cancel)
        self._cancel_button = cancel
        row.addStretch(1)
        body.addLayout(row)
        progress = QProgressBar(self)
        progress.setRange(0, TYPICAL_CAL_S * 1000)
        progress.setVisible(False)
        body.addWidget(progress)
        self._cal_progress = progress

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

    # ------------------------------------------------------------------ internal calibration

    @property
    def calibrating(self) -> bool:
        """Whether an internal calibration started from this card is running."""
        return self._cal_started is not None

    def _on_run_calibration(self) -> None:
        if schedule_bg(self._run_calibration()) is None:
            self._set_status("no event loop — UI not running?", level="error")

    async def _run_calibration(self) -> None:
        """Confirm, run the calibration with the card in its calibrating
        state, say how it ended, then re-read the balance ("Last cal")."""
        cmd = await self.prepare_command(
            kind="internal_adjust",
            payload={"cal_type": None},
            destructive=True,
            destructive_summary=(
                "Run internal calibration on the balance (motorized weight). "
                "Sample must be off the pan."
            ),
            destructive_note=(
                "The balance replaces its calibration. Keep the pan clear and the "
                "bench still for about 25 s, until this card says it is done."
            ),
        )
        if cmd is None:
            return
        self._begin_calibrating()
        try:
            result = await self.send_command(cmd)
        except Exception as exc:
            text, level = self._calibration_failure(exc)
            self._end_calibrating()
            self._set_status(text, level=level)
            self._emit_manual_event(
                kind=cmd.kind,
                severity="error" if level == "error" else "warning",
                message=str(exc),
            )
            _logger.warning(
                "manual.balance_calibration_failed", device=self.device_name, error=str(exc)
            )
        else:
            self._end_calibrating()
            if result.accepted:
                self._set_status(f"✓ {result.detail}", level="ok")
                self._emit_manual_event(kind=cmd.kind, severity="info", message=result.detail)
            else:
                self._set_status(f"✗ calibration refused: {result.detail}", level="warn")
                self._emit_manual_event(kind=cmd.kind, severity="warning", message=result.detail)
        await self.refresh_readback()

    def _calibration_failure(self, exc: Exception) -> tuple[str, str]:
        """Status line and level for a calibration that raised ``exc``."""
        if not isinstance(exc, BalanceAdjustmentError):
            return f"internal calibration failed: {exc}", "error"
        if exc.outcome == "aborted" and self._cancel_sent:
            return CANCELLED_TEXT, "idle"
        return _CAL_ENDINGS[exc.outcome]

    def _on_cancel_calibration(self) -> None:
        if schedule_bg(self._cancel_calibration()) is None:
            self._set_status("no event loop — UI not running?", level="error")

    async def _cancel_calibration(self) -> None:
        """Ask the balance to stop. The calibration's own command then ends
        as aborted, which is what the card reports."""
        cmd = await self.prepare_command(kind="abort_adjustment")
        if cmd is None or not self.calibrating:
            return
        self._cancel_sent = True
        self._sync_action_widgets()
        self._set_status("Cancelling — the balance is unloading its weight…", level="warn")
        try:
            result = await self.send_command(cmd)
        except Exception as exc:
            self._cancel_sent = False
            self._sync_action_widgets()
            self._set_status(f"cancel failed: {exc}. Still calibrating.", level="error")
            self._emit_manual_event(kind=cmd.kind, severity="error", message=str(exc))
            return
        self._emit_manual_event(kind=cmd.kind, severity="info", message=result.detail)

    def _begin_calibrating(self) -> None:
        self._cal_started = time.monotonic()
        self._cancel_sent = False
        self._sync_action_widgets()
        if self._cal_progress is not None:
            self._cal_progress.setVisible(True)
        self._set_status(CALIBRATING_TEXT, level="warn")
        self._show_cal_progress()
        self._cal_timer.start()

    def _end_calibrating(self) -> None:
        self._cal_timer.stop()
        self._cal_started = None
        self._cancel_sent = False
        if self._cal_progress is not None:
            self._cal_progress.setVisible(False)
        self._sync_action_widgets()

    def _show_cal_progress(self) -> None:
        """Fill the bar over the usual duration; past it, the bar stays full
        and the elapsed time keeps counting."""
        if self._cal_started is None:
            return
        elapsed = time.monotonic() - self._cal_started
        progress = self._cal_progress
        if progress is not None:
            progress.setValue(min(int(elapsed * 1000), progress.maximum()))
            progress.setFormat(f"{elapsed:.0f} s (usually ~{TYPICAL_CAL_S} s)")

    def _manual_controls_enabled(self, state: RunUiState | None = None) -> bool:
        """Every control but Cancel waits while a calibration runs."""
        return super()._manual_controls_enabled(state) and not self.calibrating

    def _sync_action_widgets(self, state: RunUiState | None = None) -> None:
        super()._sync_action_widgets(state)
        if self._cancel_button is not None:
            self._cancel_button.setEnabled(
                self.calibrating
                and not self._cancel_sent
                and super()._manual_controls_enabled(state)
            )

    # ------------------------------------------------------------------ live readback

    async def refresh_readback(self) -> None:
        """Fetch the balance's menu settings and last calibration, then
        show them. The dock calls this on card build and pool open; the
        commands that change them call it after they land.

        Best-effort: a failure leaves the card as it is. Skipped while a
        run is active (the adapter is busy streaming), while the pool is
        still opening — there is no client to ask yet — and while a
        calibration runs, which re-reads once it ends.
        """
        if self._engine_blocks_writes() or self.calibrating:
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
        if snapshot.cal_on_record is False:
            parts.append("Last cal: none since power-up")
        elif snapshot.cal_on_record:
            parts.append(f"Last cal: {_describe_cal(snapshot)}")
        self.set_subtitle("   ".join(parts))

    def _adapter_label(self) -> str:
        return self._spec.adapter.rsplit(".", 1)[-1]


def _describe_cal(snapshot: SartoriusStateSnapshot) -> str:
    """``"26.9 °C at 2026-11-27 15:56 (balance clock)"``, or whichever part
    the balance reported. The time is the balance's own clock, which need
    not match the PC's, so it says so."""
    parts = []
    if snapshot.cal_temperature_c is not None:
        parts.append(f"{snapshot.cal_temperature_c:.1f} °C")
    if snapshot.cal_timestamp is not None:
        parts.append(f"{snapshot.cal_timestamp:%Y-%m-%d %H:%M} (balance clock)")
    return " at ".join(parts) or "on record"


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
