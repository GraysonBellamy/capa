""":class:`HeaterCard` — manual control card for Watlow temperature controllers.

Gated on the adapter's :class:`Capability` flagset:

* setpoint              — ``HAS_SETPOINT`` (controllers always advertise this)
* raw parameter write   — ``HAS_PARAMETER_CONFIG``

The Watlow has two on-device "display unit" registers (3005 / 17050)
that *claim* to control the comms wire scale but in practice are
decoupled from it on the rig's PM3R1CA fw=1 (and at least one other
documented firmware revision). The card does not expose either —
operators work entirely in the channel's user-facing unit
(``derived_unit``), and the watlow adapter handles wire-unit
inversion on the write path via the channel calibration.

Live read-back (current process value) is refreshed on card open and
after each successful command via :meth:`refresh_readback`. The PV is
shown in the bound channel's ``derived_unit`` by applying the forward
calibration to the wire-side reading.
"""

from __future__ import annotations

import json
from typing import Final

import structlog
from PySide6.QtCore import Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from capa.devices.adapter import Capability
from capa.devices.watlow import WatlowStateSnapshot
from capa.experiment.config import DeviceConfig
from capa.experiment.procedures.builtin.heat_flux_tune.config import (
    PROCEDURE_ID as HEAT_FLUX_TUNE_ID,
)
from capa.experiment.procedures.builtin.heat_flux_tune.config import (
    HeatFluxTuneConfig,
)
from capa.runtime.dispatch import ManualClient
from capa.ui.async_util import schedule_bg
from capa.ui.forms import build_form
from capa.ui.manual.cards.base import DeviceCard
from capa.ui.state import RunController
from capa.ui.statusbar import OperatorIdProvider

_logger = structlog.get_logger("capa.ui.manual.heater")


RELEVANT_CAPABILITIES: Final[tuple[Capability, ...]] = (
    Capability.HAS_SETPOINT,
    Capability.HAS_PARAMETER_CONFIG,
)


def is_heater_device(spec: DeviceConfig) -> bool:
    """Fingerprint a Watlow adapter by adapter import path.

    Catches both the real ``capa.devices.watlow`` and the sim
    ``capa.devices.sim.watlow_sim`` — the sim accepts setpoint commands
    too (returns ack without actually moving a heater), so the card
    renders for both.
    """
    return "watlow" in spec.adapter.lower()


_HEAT_FLUX_GAUGE_CHANNEL = "heat_flux_gauge"
"""Channel name conventionally bound to the Schmidt-Boelter heat-flux
gauge. The heater card renders its Heat-Flux Tune section only when a
channel of this name exists in the active hardware profile — defensive
UX so the launcher isn't visible on a rig that can't measure flux."""


_FALLBACK_SAFE_C: Final[float] = 25.0
"""Safe-cool fallback when the active method has no Heat-Flux Tune config
to read ``t_safe_c`` from. Matches :attr:`HeatFluxTuneConfig.t_safe_c`'s
own default — picking a different value would surprise an operator who
has been seeing 25 °C in the procedure dialogs."""


class HeaterCard(DeviceCard):
    """Per-Watlow manual-control card.

    Capabilities are read off the live worker-hosted adapter when one
    exists; otherwise we fall back to the Watlow default flagset (setpoint
    + parameter config) so the card is usable in the lazy-connect window
    after a fresh config load.
    """

    tune_requested = Signal(dict)
    """Emitted when the operator confirms the Heat-Flux Tune dialog.

    Carries the validated procedure-config dict (the same shape
    ``HeatFluxTuneConfig.model_dump()`` produces). MainWindow can connect
    a slot to route this into the Setup tab's procedure section; if
    unconnected, the dialog's clipboard fallback still gives the
    operator a way to paste the config into Setup → Procedure manually.
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
            title=f"Heater: {spec.name}",
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
            caps = _default_watlow_capabilities()
        self._capabilities: frozenset[Capability] = caps
        # Spinbox refs populated by _build_setpoint_section and rewritten
        # from the live snapshot in refresh_readback / _on_pool_changed.
        self._setpoint_spin: QDoubleSpinBox | None = None
        self._instance_spin: QDoubleSpinBox | None = None
        self._prefilled: bool = False
        self.set_subtitle(f"Device: {spec.name}   Adapter: {spec.adapter.rsplit('.', 1)[-1]}")
        self._build_capability_sections()
        # When the pool first opens (typical sequence: dock builds cards
        # before the pool has finished opening adapters), kick the
        # readback fetch so the spinbox prefills automatically. Same
        # signal the WebcamCard uses to populate UVC ranges.
        self._controller.pool_changed.connect(self._on_pool_changed)

    # ------------------------------------------------------------------ build

    def _build_capability_sections(self) -> None:
        if Capability.HAS_SETPOINT in self._capabilities:
            self._build_setpoint_section()
            self._build_safe_cool_section()
        if Capability.HAS_PARAMETER_CONFIG in self._capabilities:
            self._build_parameter_section()
        if self._heat_flux_gauge_available():
            self._build_heat_flux_tune_section()

    def _heat_flux_gauge_available(self) -> bool:
        """Return ``True`` when the active config exposes a
        ``heat_flux_gauge`` channel.

        Defensive check: the procedure's own preflight would catch a
        missing gauge channel too, but hiding the launcher entirely is
        clearer UX than letting the operator click a button that will
        only refuse a few seconds later.
        """
        config = self._controller.active_config
        if config is None:
            return False
        channels = config.hardware.channels or ()
        return any(ch.name == _HEAT_FLUX_GAUGE_CHANNEL for ch in channels)

    def _build_heat_flux_tune_section(self) -> None:
        """Compose the Heat-Flux Tune launcher row.

        One button: open a modal dialog with the auto-form for
        :class:`HeatFluxTuneConfig`, validate on accept, copy the
        resulting dict to the clipboard, and emit
        :attr:`tune_requested` for any wired-up handler. The clipboard
        path is the always-available fallback so the operator can
        paste into Setup → Procedure → Config even when no MainWindow
        slot is connected to the signal.
        """
        body = self.add_section("Heat-Flux Tune")
        row = QHBoxLayout()
        row.setSpacing(6)
        info_label = QLabel(
            f"Gauge channel '{_HEAT_FLUX_GAUGE_CHANNEL}' detected.",
            self,
        )
        info_label.setStyleSheet("color: #2a7;")
        row.addWidget(info_label, stretch=1)
        btn = QPushButton("Launch tune…", self)
        btn.setToolTip(
            "Open the Heat-Flux Tune config dialog. On accept the "
            "validated config is copied to the clipboard for pasting "
            "into Setup → Procedure, and a tune_requested signal is "
            "emitted for the main window to act on."
        )
        btn.clicked.connect(self._on_launch_tune_clicked)
        row.addWidget(btn)
        body.addLayout(row)
        # Tune launches its own audited setpoint sequence — gating this
        # button on the run-state machine would refuse it during a run,
        # which is what we want (you can't tune while a method runs).
        self.register_action_widget(btn)

    def _on_launch_tune_clicked(self) -> None:
        """Pop the auto-form dialog and route the result."""
        # Sensible defaults: 50 kW/m² target, 900 °C session ceiling.
        initial = {
            "targets_kw_m2": [50.0],
            "t_set_max_c": 900.0,
            "operator_id": self._operator_provider.current_operator_id() or None,
        }
        dialog = _HeatFluxTuneLaunchDialog(initial=initial, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        config_dict = dialog.validated_config()
        if config_dict is None:
            return
        # Copy a clipboard-ready payload mirroring what the procedure
        # section expects under ``experiment_payload["procedure"]``.
        clipboard_text = json.dumps(
            {"id": HEAT_FLUX_TUNE_ID, "config": config_dict},
            indent=2,
            default=str,
        )
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(clipboard_text)
        self._set_status(
            "tune config copied to clipboard — paste into Setup → Procedure",
            level="ok",
        )
        # Emit for any wired-up handler. The signal is one-way; MainWindow
        # can connect to push directly into the Setup tab.
        self.tune_requested.emit(config_dict)

    def _build_setpoint_section(self) -> None:
        body = self.add_section("Setpoint")
        row = QHBoxLayout()
        row.setSpacing(6)
        value_label = QLabel("Value:", self)
        value_label.setMinimumWidth(80)
        row.addWidget(value_label)
        spin = QDoubleSpinBox(self)
        spin.setRange(-1999.0, 9999.0)  # PM3 published range
        spin.setDecimals(2)
        spin.setSingleStep(5.0)
        spin.setToolTip(
            "Setpoint in the bound channel's user-facing unit "
            "(``derived_unit``). The watlow adapter inverts the channel "
            "calibration before writing, so the value the device sees is "
            "in its wire unit (typically °F on this rig). If the channel "
            "calibration is non-invertible, the value is sent as-is."
        )
        row.addWidget(spin)
        self._setpoint_spin = spin
        instance_label = QLabel("Loop:", self)
        row.addWidget(instance_label)
        instance_spin = QDoubleSpinBox(self)
        instance_spin.setRange(1, 4)
        instance_spin.setDecimals(0)
        instance_spin.setValue(1)
        instance_spin.setToolTip("Loop / instance selector (1-indexed). Single-loop PM SKUs use 1.")
        row.addWidget(instance_spin)
        self._instance_spin = instance_spin
        btn = QPushButton("Set", self)

        def _apply() -> None:
            self.schedule_dispatch(
                kind="set_setpoint",
                payload={
                    "value": spin.value(),
                    "instance": int(instance_spin.value()),
                },
            )

        btn.clicked.connect(_apply)
        row.addWidget(btn)
        row.addStretch(1)
        body.addLayout(row)
        for w in (spin, instance_spin, btn):
            self.register_action_widget(w)

    def _build_safe_cool_section(self) -> None:
        """Compose the Cool-to-safe quick-action row.

        The button issues a single setpoint write to the safe temperature
        read from the active method's :class:`HeatFluxTuneConfig` (when
        present) or :data:`_FALLBACK_SAFE_C`. Confirmation dialog gives
        one click of friction so a fat-finger on a hot rig doesn't drive
        the heater to ambient mid-experiment.

        Ships independent of hold mode: even without the
        ``hold_at_completion`` flag, this affordance is a generally
        useful safety control — a faster path than typing 25 into the
        setpoint spinbox.
        """
        body = self.add_section("Safe cool")
        row = QHBoxLayout()
        row.setSpacing(6)
        safe_c = self._resolve_safe_temp_c()
        info_label = QLabel(
            f"Drive heater to {safe_c:g} °C.",
            self,
        )
        info_label.setStyleSheet("color: #666;")
        row.addWidget(info_label, stretch=1)
        btn = QPushButton("Cool to safe", self)
        btn.setObjectName("heater_cool_to_safe_button")
        btn.setToolTip(
            "Issue a single setpoint write driving the heater to its "
            "safe temperature. Safe temperature is read from the active "
            "method's Heat-Flux Tune config (t_safe_c) when present, "
            f"otherwise {_FALLBACK_SAFE_C:g} °C."
        )
        btn.clicked.connect(self._on_safe_cool_clicked)
        row.addWidget(btn)
        body.addLayout(row)
        self.register_action_widget(btn)

    def _on_safe_cool_clicked(self) -> None:
        """Confirm + dispatch the safe-cool setpoint write.

        Re-reads the safe temperature at click time (rather than caching
        it at section-build time) so a mid-session active-config swap
        picks up the new value.
        """
        safe_c = self._resolve_safe_temp_c()
        instance = int(self._instance_spin.value()) if self._instance_spin is not None else 1
        reply = QMessageBox.question(
            self,
            "Cool heater to safe?",
            f"Drive heater to {safe_c:g} °C? The current setpoint will be overwritten.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Ok:
            return
        self.schedule_dispatch(
            kind="set_setpoint",
            payload={"value": float(safe_c), "instance": instance},
            destructive=True,
            destructive_summary=(
                f"Cool heater {self._spec.name!r} to safe temperature {safe_c:g} °C."
            ),
        )

    def _resolve_safe_temp_c(self) -> float:
        """Look up ``t_safe_c`` from the active Heat-Flux Tune config.

        Returns :data:`_FALLBACK_SAFE_C` when no config is loaded, when
        the procedure is something other than heat-flux tune, or when
        ``t_safe_c`` is missing / non-numeric. The procedure config dict
        is the raw operator-supplied blob — pydantic validation lives
        with the procedure plugin, not here, so we treat the dict
        defensively.
        """
        config = self._controller.active_config
        if config is None:
            return _FALLBACK_SAFE_C
        procedure_ref = getattr(config, "procedure", None)
        if procedure_ref is None or procedure_ref.id != HEAT_FLUX_TUNE_ID:
            return _FALLBACK_SAFE_C
        raw = procedure_ref.config.get("t_safe_c")
        try:
            return float(raw) if raw is not None else _FALLBACK_SAFE_C
        except (TypeError, ValueError):
            return _FALLBACK_SAFE_C

    def _build_parameter_section(self) -> None:
        body = self.add_section("Raw parameter write")
        row = QHBoxLayout()
        row.setSpacing(6)
        name_lbl = QLabel("Param:", self)
        name_lbl.setMinimumWidth(80)
        row.addWidget(name_lbl)
        name_edit = QLineEdit(self)
        name_edit.setPlaceholderText("e.g. heat_algorithm")
        name_edit.setToolTip(
            "Canonical parameter name or id from watlowlib.PARAMETERS. "
            "Writes go through write_parameter with confirm=True — the "
            "library will reject out-of-range values pre-I/O."
        )
        row.addWidget(name_edit)
        val_lbl = QLabel("Value:", self)
        row.addWidget(val_lbl)
        val_edit = QLineEdit(self)
        val_edit.setPlaceholderText("number or string")
        val_edit.setMaximumWidth(120)
        row.addWidget(val_edit)
        btn = QPushButton("Write", self)

        def _apply() -> None:
            name = name_edit.text().strip()
            if not name:
                self._set_status("parameter name required", level="warn")
                return
            raw = val_edit.text().strip()
            if not raw:
                self._set_status("value required", level="warn")
                return
            # Try numeric first; fall back to string (some params accept
            # string codes, e.g. tc_type="K").
            value: float | int | str
            try:
                value = float(raw) if "." in raw else int(raw)
            except ValueError:
                value = raw
            self.schedule_dispatch(
                kind="write_parameter",
                payload={"name": name, "value": value},
                destructive=True,
                destructive_summary=(
                    f"Write parameter {name!r} = {value!r} on heater {self._spec.name!r}. "
                    "Most writes are persistent (RWE / RWES) and survive "
                    "power-cycle."
                ),
            )

        btn.clicked.connect(_apply)
        row.addWidget(btn)
        row.addStretch(1)
        body.addLayout(row)
        for w in (name_edit, val_edit, btn):
            self.register_action_widget(w)

    # ------------------------------------------------------------------ live readback

    def _on_pool_changed(self, pool: object) -> None:
        """Kick off the readback fetch when the pool first publishes itself.

        The dock typically builds cards before the pool has finished opening
        adapters; ``pool_changed`` fires once the pool is open and the
        worker-hosted adapter is reachable. We only prefill once (latch on
        first non-``None`` pool) so subsequent toggles don't stomp operator
        edits to the spinbox.
        """
        if pool is None or self._prefilled:
            return
        client = self._controller.manual_client
        if client is None:
            return
        schedule_bg(self._fetch_and_apply_readback(client))

    async def _fetch_and_apply_readback(self, client: ManualClient) -> None:
        """Cross-loop fetch of the operator-facing snapshot, then apply.

        Best-effort: any failure leaves the card on its static defaults.
        Run-state-blocked because the adapter is busy streaming during a
        run — the worker-side device_readback() also rejects in DRAINING /
        CLOSED, so this caller-side guard is belt-and-braces.
        """
        if self._engine_blocks_writes():
            return
        try:
            snapshot = await client.device_readback(self._spec.name)
        except Exception as exc:
            _logger.debug(
                "manual.heater_readback_failed",
                device=self.device_name,
                error=str(exc),
            )
            return
        if not isinstance(snapshot, WatlowStateSnapshot):
            return
        self._apply_snapshot(snapshot)
        self._prefilled = True

    async def refresh_readback(self) -> None:
        """Re-fetch and apply the snapshot.

        Called by the dock after the card is built (initial refresh) and
        could be called after destructive commands. Always re-applies (no
        latch) because the operator's last action may have changed the
        device state — we want to reflect that.
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
                "manual.heater_readback_failed",
                device=self.device_name,
                error=str(exc),
            )
            return
        if not isinstance(snapshot, WatlowStateSnapshot):
            return
        self._apply_snapshot(snapshot)
        self._prefilled = True

    def _apply_snapshot(self, snapshot: WatlowStateSnapshot) -> None:
        """Apply a fresh readback to the spinbox + subtitle.

        Setpoint is forward-calibrated through the bound ``setpoint``
        channel (wire → user-facing); PV is forward-calibrated through the
        bound ``process_value`` channel. Both fall back to the raw wire
        value when no channel binding is configured, so a one-shot
        diagnostic config still shows something usable.
        """
        adapter = self._opened_adapter()
        spin = self._setpoint_spin
        if spin is not None and snapshot.setpoint is not None:
            calibrated, _ = _calibrate_parameter(adapter, snapshot.setpoint, "setpoint")
            spin.blockSignals(True)
            try:
                spin.setValue(calibrated)
            finally:
                spin.blockSignals(False)
        pv_text = "—"
        unit_text = ""
        if snapshot.process_value is not None:
            calibrated_pv, derived_unit = _calibrate_parameter(
                adapter, snapshot.process_value, "process_value"
            )
            pv_text = f"{calibrated_pv:.2f}"
            if derived_unit:
                unit_text = derived_unit
        self.set_subtitle(
            f"Device: {self._spec.name}   "
            f"Adapter: {self._spec.adapter.rsplit('.', 1)[-1]}   "
            f"PV: {pv_text} {unit_text}".rstrip()
        )

    def _opened_adapter(self) -> object | None:
        """Return the worker-hosted adapter (read-only attribute access).

        Used to walk ``_channels`` for calibration lookup. Touching the
        adapter's *async* methods from this loop violates worker-loop
        ownership of the handle, but a pure attribute read of
        ``_channels`` is fine. Returns ``None`` when the pool hasn't
        opened yet.
        """
        pool = self._controller.worker_pool
        if pool is None:
            return None
        try:
            worker = pool.worker_for(self._spec.name)
        except Exception:
            return None
        return worker.adapters.get(self._spec.name)


def _calibrate_parameter(
    adapter: object | None, raw_value: float, parameter: str
) -> tuple[float, str]:
    """Look up the channel bound to ``(parameter, instance=1)`` on
    ``adapter`` and apply its forward calibration to ``raw_value``.

    Returns ``(calibrated_value, derived_unit_label)``. Falls back to
    ``(raw_value, "")`` when no channel is bound or the calibration is
    absent — the raw wire value is still informative even without unit
    context (the operator can tell ``212`` is in °F at a glance on a
    rig where the channel-derived display would be °C).
    """
    if adapter is None:
        return raw_value, ""
    channels = getattr(adapter, "_channels", None)
    if not channels:
        return raw_value, ""
    for spec in channels:
        binding = getattr(spec, "source", None)
        if binding is None:
            continue
        if getattr(binding, "parameter", None) != parameter:
            continue
        if getattr(binding, "instance", None) != 1:
            continue
        cal = getattr(spec, "calibration", None)
        if cal is None:
            return raw_value, str(getattr(spec, "derived_unit", "") or getattr(spec, "unit", ""))
        try:
            calibrated = float(cal.evaluate(raw_value))
        except Exception:
            return raw_value, ""
        derived = str(getattr(spec, "derived_unit", "") or getattr(spec, "unit", ""))
        return calibrated, derived
    return raw_value, ""


class _HeatFluxTuneLaunchDialog(QDialog):
    """Modal config-editor for the Heat-Flux Tune procedure.

    Embeds the auto-form for :class:`HeatFluxTuneConfig` plus a
    standard OK/Cancel button row. ``validated_config`` returns the
    Pydantic-validated dict on accept; on validation failure the
    dialog shows the errors inline and stays open.
    """

    def __init__(
        self,
        *,
        initial: dict[str, object] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Heat-Flux Tune")
        self.setMinimumWidth(540)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        intro = QLabel(
            "Configure the Heat-Flux Tune procedure. On accept the "
            "validated config is copied to the clipboard for pasting "
            "into Setup → Procedure → Config.",
            self,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self._form = build_form(HeatFluxTuneConfig, initial=initial, parent=self)
        layout.addWidget(self._form)

        self._error_label = QLabel("", self)
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet("color: #b33;")
        layout.addWidget(self._error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._validated: dict[str, object] | None = None

    def validated_config(self) -> dict[str, object] | None:
        """The validated config dict from the last successful accept."""
        return self._validated

    def _on_accept(self) -> None:
        values = self._form.values()
        try:
            cfg = HeatFluxTuneConfig.model_validate(values)
        except Exception as exc:
            self._error_label.setText(f"config invalid: {exc}")
            return
        self._validated = cfg.model_dump()
        self._error_label.setText("")
        self.accept()


def _default_watlow_capabilities() -> frozenset[Capability]:
    """Capabilities the Watlow adapter always advertises (see
    [src/capa/devices/watlow.py](../../../devices/watlow.py)). Used as a
    fallback when the card is built before the worker pool has opened
    the adapter."""
    return frozenset(
        {
            Capability.HAS_SETPOINT,
            Capability.HAS_RAMP,
            Capability.READS_PROCESS_VAR,
            Capability.HAS_PARAMETER_CONFIG,
        }
    )


__all__ = ["HeaterCard", "is_heater_device"]
