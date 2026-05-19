""":class:`HeatFluxTuneDock` — operator-control surface during a tune run.

A dock that surfaces the four operator commands the
:class:`~capa.experiment.procedures.builtin.heat_flux_tune.HeatFluxTune`
procedure accepts mid-run plus a live-numerics panel fed by
:class:`~capa.runtime.emissions.ProcedureTick`\\ s:

* **Pause / Resume** — freeze the iteration loop without changing
  commanded setpoints. The heater stays at its current SP and the
  rolling windows keep filling so the operator can inspect a steady
  state.
* **Accept Current** — force-terminate the current iteration with the
  rolling-window statistics as-is. The resulting tune point is
  recorded with ``accept_reason="operator_override"``.
* **Abort** — sets the run's ``external_stop`` event. The procedure's
  ``_safe_cool`` path drives the heater to ``t_safe_c`` before
  returning.

The dock auto-shows when :attr:`RunController.active_procedure_id`
matches the heat-flux-tune procedure id, and hides on run completion.

The live numerics panel (Phase 3.5) reads
:class:`~capa.runtime.emissions.ProcedureTick`\\ s off the
``procedure_tick_received`` signal: target progress, iteration number,
windowed flux statistics, predicate dwell, and the predicate's
last-reason string ("waiting on pv-out-of-band", etc.). Numbers go
stale (dimmed) when no tick arrives within ``STALE_AFTER_MS`` —
catches a procedure that has stopped publishing without the dock
needing to know why.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDockWidget,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from capa.experiment.procedures.base import OperatorCommand
from capa.experiment.procedures.builtin.heat_flux_tune import (
    PROCEDURE_ID as HEAT_FLUX_TUNE_PROCEDURE_ID,
)
from capa.runtime.emissions import ProcedureTick
from capa.ui.state import RunController, RunUiState

if TYPE_CHECKING:
    pass


_LIVE_STATES = frozenset(
    {
        RunUiState.PREPARING,
        RunUiState.RUNNING,
        RunUiState.DRAINING,
    }
)
"""States during which the operator buttons should be enabled. Outside
this set the procedure is not consuming the command stream so a click
would silently no-op."""


STALE_AFTER_MS = 1500
"""How long without a tick before the live numerics get visually
dimmed. Two poll cycles at the procedure's default
``poll_interval_s = 0.5``; tuned so a momentary GC pause doesn't
flicker the panel but a stalled procedure is obvious within ~1.5 s."""


def _fmt_float(value: object, fmt: str = "{:.2f}") -> str:
    """Format a number; pass through None / NaN as a neutral dash."""
    if value is None:
        return "—"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return "—"
        return fmt.format(value)
    return str(value)


class _LiveNumericsPanel(QFrame):
    """Compact grid of QLabels driven by
    :class:`~capa.runtime.emissions.ProcedureTick` payloads.

    Stays passive: every widget update happens inside
    :meth:`update_from_tick` so tests can drive it with a synthesized
    tick and assert the rendered text. A separate
    :meth:`mark_stale` path dims the values when ticks dry up — the
    parent dock owns the QTimer that drives staleness so the panel
    itself stays Qt-event-loop-agnostic.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFrameShadow(QFrame.Shadow.Sunken)
        grid = QGridLayout(self)
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(2)

        self._target_label = QLabel("Target — · — kW/m²", self)
        self._target_label.setStyleSheet("font-weight: bold;")
        grid.addWidget(self._target_label, 0, 0, 1, 3)

        self._iteration_label = QLabel("Iteration — of —", self)
        grid.addWidget(self._iteration_label, 1, 0, 1, 3)

        # Live-numerics grid. Each (caption, value, unit/extra) trio.
        captions = [
            ("Flux", "_flux_value", "_flux_extra"),
            ("Std", "_std_value", "_std_unit"),
            ("Slope", "_slope_value", "_slope_unit"),
            ("PV", "_pv_value", "_pv_extra"),
            ("Settle", "_settle_value", "_settle_extra"),
            ("Window", "_window_value", "_window_extra"),
            ("Phase", "_phase_value", "_phase_extra"),
            ("In-tol", "_intol_value", "_intol_extra"),
            ("dF/dT", "_dfdt_value", "_dfdt_extra"),
        ]
        for row, (caption, value_attr, extra_attr) in enumerate(captions, start=2):
            caption_label = QLabel(caption, self)
            caption_label.setStyleSheet("color: #888;")
            grid.addWidget(caption_label, row, 0)
            value_label = QLabel("—", self)
            value_label.setStyleSheet("font-family: Consolas, 'Courier New', monospace;")
            value_label.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            grid.addWidget(value_label, row, 1)
            extra_label = QLabel("", self)
            extra_label.setStyleSheet("color: #888;")
            grid.addWidget(extra_label, row, 2)
            setattr(self, value_attr, value_label)
            setattr(self, extra_attr, extra_label)

        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        self._stale = False
        self._has_tick = False

    # ------------------------------------------------------------------ updates

    def update_from_tick(self, payload: dict[str, Any]) -> None:
        """Refresh every label from a tick payload.

        Defensive against missing keys — the dock and the procedure may
        be on different versions during a hot-reload, and the panel
        should degrade to a dash rather than raise.
        """
        target_idx = payload.get("target_index", 0)
        target_count = payload.get("target_count", 0)
        target_kw_m2 = payload.get("target_kw_m2")
        tolerance = payload.get("tolerance_kw_m2", 0.0)
        if target_count > 0 and target_kw_m2 is not None:
            self._target_label.setText(
                f"Target {target_idx} of {target_count} · "
                f"{_fmt_float(target_kw_m2)} ±{_fmt_float(tolerance)} kW/m²"
            )
        else:
            self._target_label.setText("Target — · — kW/m²")

        iteration = payload.get("iteration", 0)
        iteration_max = payload.get("iteration_max", 0)
        commanded_sp = payload.get("commanded_setpoint_c")
        if iteration_max > 0:
            self._iteration_label.setText(
                f"Iteration {iteration} of {iteration_max} · SP "
                f"{_fmt_float(commanded_sp)} °C"
            )
        else:
            self._iteration_label.setText("Iteration — of —")

        mean_flux = payload.get("mean_flux_kw_m2")
        err = payload.get("error_kw_m2")
        self._flux_value.setText(f"{_fmt_float(mean_flux)} kW/m²")
        if err is None or tolerance == 0.0:
            self._flux_extra.setText("")
            self._flux_extra.setStyleSheet("color: #888;")
        elif abs(err) <= tolerance:
            self._flux_extra.setText(f"err {err:+.2f} ✓")
            self._flux_extra.setStyleSheet("color: #2c8e3f;")  # green
        else:
            self._flux_extra.setText(f"err {err:+.2f}")
            self._flux_extra.setStyleSheet("color: #b08000;")  # amber

        std_flux = payload.get("std_flux_kw_m2")
        sigma_max = payload.get("sigma_max_kw_m2")
        self._std_value.setText(f"{_fmt_float(std_flux, '{:.3f}')} kW/m²")
        self._std_unit.setText(f"≤ {_fmt_float(sigma_max, '{:.3f}')}")

        slope = payload.get("slope_flux_kw_m2_per_min")
        slope_max = payload.get("slope_max_kw_m2_per_min")
        self._slope_value.setText(
            f"{_fmt_float(slope, '{:+.3f}')} kW/m²/min"
        )
        self._slope_unit.setText(f"≤ {_fmt_float(slope_max, '{:.3f}')}")

        pv = payload.get("pv_latest_c")
        self._pv_value.setText(f"{_fmt_float(pv)} °C")
        self._pv_extra.setText(f"SP {_fmt_float(commanded_sp)} °C")

        dwell = payload.get("predicate_dwell_s", 0.0)
        budget = payload.get("settle_budget_s")
        elapsed = payload.get("elapsed_s", 0.0)
        if budget is not None and budget > 0.0:
            self._settle_value.setText(
                f"dwell {_fmt_float(dwell, '{:.0f}')} s"
            )
            self._settle_extra.setText(
                f"{_fmt_float(elapsed, '{:.0f}')}/{_fmt_float(budget, '{:.0f}')} s"
            )
        else:
            self._settle_value.setText(f"dwell {_fmt_float(dwell, '{:.0f}')} s")
            self._settle_extra.setText("")

        samples_in = payload.get("flux_samples_in_window")
        span_s = payload.get("flux_window_span_s")
        age_s = payload.get("flux_last_sample_age_s")
        if samples_in is None:
            self._window_value.setText("—")
        else:
            self._window_value.setText(f"{int(samples_in)} samples")
        extras: list[str] = []
        if span_s is not None:
            extras.append(f"span {_fmt_float(span_s, '{:.1f}')} s")
        if age_s is not None:
            extras.append(f"age {_fmt_float(age_s, '{:.1f}')} s")
        self._window_extra.setText(" · ".join(extras))

        phase = payload.get("phase", "idle")
        last_reason = payload.get("predicate_last_reason", "")
        self._phase_value.setText(str(phase))
        self._phase_extra.setText(str(last_reason))

        in_tol_windows = payload.get("in_tol_windows", 0)
        self._intol_value.setText(f"{in_tol_windows} of 2")
        runaway_count = payload.get("runaway_count", 0)
        if runaway_count > 0:
            self._intol_extra.setText(f"runaway {runaway_count}")
            self._intol_extra.setStyleSheet("color: #b08000;")
        else:
            self._intol_extra.setText("")
            self._intol_extra.setStyleSheet("color: #888;")

        df_dt = payload.get("df_dt_used")
        df_dt_source = payload.get("df_dt_source", "unknown")
        self._dfdt_value.setText(_fmt_float(df_dt, "{:.4f}"))
        self._dfdt_extra.setText(str(df_dt_source))

        self._has_tick = True
        if self._stale:
            self._stale = False
            self._refresh_stale_style()

    def mark_stale(self) -> None:
        """Dim the value labels (without changing their text) when no
        tick has arrived for :data:`STALE_AFTER_MS`. Idempotent."""
        if not self._has_tick or self._stale:
            return
        self._stale = True
        self._refresh_stale_style()

    def _refresh_stale_style(self) -> None:
        opacity_style = "color: #999;" if self._stale else ""
        # Apply to all value labels — captions / unit hints stay
        # subdued either way.
        for attr in (
            "_flux_value",
            "_std_value",
            "_slope_value",
            "_pv_value",
            "_settle_value",
            "_window_value",
            "_phase_value",
            "_intol_value",
            "_dfdt_value",
        ):
            getattr(self, attr).setStyleSheet(
                "font-family: Consolas, 'Courier New', monospace;" + opacity_style
            )


class HeatFluxTuneDock(QDockWidget):
    """Operator-control dock for an in-progress Heat-Flux Tune run."""

    def __init__(
        self,
        controller: RunController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Heat-Flux Tune", parent)
        self.setObjectName("dock_heat_flux_tune")
        self.setAllowedAreas(
            Qt.DockWidgetArea.RightDockWidgetArea | Qt.DockWidgetArea.LeftDockWidgetArea
        )
        self._controller = controller

        host = QWidget(self)
        layout = QVBoxLayout(host)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._status_label = QLabel("Idle.", host)
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        # Live numerics fed by ProcedureTick payloads. Compact grid;
        # idle until the first tick arrives.
        self._numerics = _LiveNumericsPanel(host)
        layout.addWidget(self._numerics)

        self._info_label = QLabel(
            "Pause / Resume freeze the loop without changing the heater "
            "setpoint. Accept Current force-saves the latest reading as an "
            "operator-override point. Abort drives the heater to t_safe_c.",
            host,
        )
        self._info_label.setWordWrap(True)
        self._info_label.setStyleSheet("color: #666; font-size: 9pt;")
        layout.addWidget(self._info_label)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        self._pause_btn = QPushButton("Pause", host)
        self._pause_btn.setToolTip(
            "Freeze the iteration loop. Heater setpoint is unchanged; "
            "rolling windows keep filling. Click Resume to continue."
        )
        self._pause_btn.clicked.connect(self._on_pause)
        controls.addWidget(self._pause_btn)

        self._resume_btn = QPushButton("Resume", host)
        self._resume_btn.setToolTip("Clear the pause state and continue the iteration loop.")
        self._resume_btn.clicked.connect(self._on_resume)
        controls.addWidget(self._resume_btn)

        self._accept_btn = QPushButton("Accept Current", host)
        self._accept_btn.setToolTip(
            "Terminate the current iteration with the rolling-window "
            "statistics as-is. The resulting tune point is recorded with "
            "accept_reason='operator_override'."
        )
        self._accept_btn.clicked.connect(self._on_accept_current)
        controls.addWidget(self._accept_btn)

        self._abort_btn = QPushButton("Abort", host)
        self._abort_btn.setToolTip(
            "Stop the tune; the procedure's safe-cool path drives the "
            "heater to t_safe_c before returning."
        )
        self._abort_btn.setStyleSheet("background-color: #b33; color: white;")
        self._abort_btn.clicked.connect(self._on_abort)
        controls.addWidget(self._abort_btn)

        layout.addLayout(controls)
        layout.addStretch(1)
        self.setWidget(host)

        # Auto-show/hide on state + procedure-id transitions.
        self._controller.state_changed.connect(self._refresh_visibility)
        self._refresh_visibility(self._controller.state)

        # Live-numerics wiring. Controller emits ProcedureTicks for
        # any procedure; the dock filters by procedure_id on receipt.
        self._controller.procedure_tick_received.connect(self._on_procedure_tick)

        # Staleness timer — dim the live numerics when no tick arrives
        # within STALE_AFTER_MS. The timer is single-shot, restarted on
        # every tick; if it fires we know the procedure has stopped
        # publishing (paused-and-disconnected, crashed, completed) and
        # the values shown are no longer current.
        self._stale_timer = QTimer(self)
        self._stale_timer.setSingleShot(True)
        self._stale_timer.setInterval(STALE_AFTER_MS)
        self._stale_timer.timeout.connect(self._numerics.mark_stale)

    # --------------------------------------------------------------- slots

    def _on_pause(self) -> None:
        ok = self._controller.send_operator_command(OperatorCommand(kind="pause"))
        self._status_label.setText("⏸ pause requested" if ok else "(no active tune)")

    def _on_resume(self) -> None:
        ok = self._controller.send_operator_command(OperatorCommand(kind="resume"))
        self._status_label.setText("▶ resume requested" if ok else "(no active tune)")

    def _on_accept_current(self) -> None:
        ok = self._controller.send_operator_command(OperatorCommand(kind="accept_current"))
        self._status_label.setText("✓ accept current requested" if ok else "(no active tune)")

    def _on_procedure_tick(self, tick: object) -> None:
        """Forward a tick to the live-numerics panel if it's ours.

        ``tick`` is typed as ``object`` because Qt Signals erase to
        ``QObject``; we do the type guard here so foreign procedures'
        ticks (a future TGA-style procedure, say) don't corrupt our
        labels. A malformed payload (wrong keys, wrong types) falls
        through the panel's defensive ``.get(...)`` accesses without
        raising.
        """
        if not isinstance(tick, ProcedureTick):
            return
        if tick.procedure_id != HEAT_FLUX_TUNE_PROCEDURE_ID:
            return
        # Mapping → dict so the panel can stay annotated as dict; the
        # runtime emission's payload type is the read-only Mapping.
        self._numerics.update_from_tick(dict(tick.payload))
        self._stale_timer.start()

    def _on_abort(self) -> None:
        # Abort goes through the existing external_stop path; no operator
        # command needed (see OperatorCommandKind in procedures/base.py).
        if not self._is_active():
            self._status_label.setText("(no active tune)")
            return
        self._controller.request_abort(mode="safe_shutdown")
        self._status_label.setText("⏹ abort requested (safe-cool will run)")

    # --------------------------------------------------------------- visibility

    def _refresh_visibility(self, state: object) -> None:
        """Show only when the active procedure is heat-flux-tune AND
        the run is in a state where the procedure consumes commands."""
        if not isinstance(state, RunUiState):
            self.setVisible(False)
            return
        if self._controller.active_procedure_id != HEAT_FLUX_TUNE_PROCEDURE_ID:
            self.setVisible(False)
            return
        if state not in _LIVE_STATES:
            self.setVisible(False)
            return
        self.setVisible(True)
        # Enable command buttons only while the procedure is actively
        # consuming the stream — PREPARING is too early, DRAINING is too
        # late.
        active = state is RunUiState.RUNNING
        for btn in (self._pause_btn, self._resume_btn, self._accept_btn):
            btn.setEnabled(active)
        # Abort is always available while the run is in a live state.
        self._abort_btn.setEnabled(state in _LIVE_STATES)

    def _is_active(self) -> bool:
        return (
            self._controller.active_procedure_id == HEAT_FLUX_TUNE_PROCEDURE_ID
            and self._controller.state in _LIVE_STATES
        )


__all__ = ["HeatFluxTuneDock"]
