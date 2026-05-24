""":class:`HeatFluxTuneDock` — visibility + command-dispatch tests.

The dock is the operator-control surface for an in-progress tune. Tests
focus on the two pieces of behavior that aren't obvious from reading the
code: (1) auto-show/hide on procedure id + run-state transitions, and
(2) the four button clicks each forward the correct
:class:`OperatorCommand` (or, for Abort, an ``external_stop`` request)
to the :class:`RunController`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from capa.experiment.procedures.base import OperatorCommand
from capa.experiment.procedures.builtin.heat_flux_tune.config import (
    PROCEDURE_ID as HEAT_FLUX_TUNE_PROCEDURE_ID,
)
from capa.ui.docks.heat_flux_tune import HeatFluxTuneDock
from capa.ui.state import RunUiState


def _make_controller(*, active_id: str | None, state: RunUiState) -> MagicMock:
    """Mock just enough of :class:`RunController` for the dock."""
    controller = MagicMock()
    controller.active_procedure_id = active_id
    controller.state = state
    controller.send_operator_command = MagicMock(return_value=True)
    controller.request_abort = MagicMock()
    # state_changed / procedure_tick_received are Qt signals in the
    # real class; using MagicMock lets ``.connect`` be called without
    # exploding.
    return controller


def test_dock_hidden_when_no_active_procedure(qtbot: Any) -> None:
    controller = _make_controller(active_id=None, state=RunUiState.IDLE)
    dock = HeatFluxTuneDock(controller)
    qtbot.addWidget(dock)
    assert dock.isVisible() is False


def test_dock_hidden_for_different_procedure(qtbot: Any) -> None:
    controller = _make_controller(active_id="capa.builtin.free_run", state=RunUiState.RUNNING)
    dock = HeatFluxTuneDock(controller)
    qtbot.addWidget(dock)
    dock._refresh_visibility(RunUiState.RUNNING)
    assert dock.isVisible() is False


def test_dock_hidden_outside_live_states(qtbot: Any) -> None:
    controller = _make_controller(active_id=HEAT_FLUX_TUNE_PROCEDURE_ID, state=RunUiState.SEALED)
    dock = HeatFluxTuneDock(controller)
    qtbot.addWidget(dock)
    dock._refresh_visibility(RunUiState.SEALED)
    assert dock.isVisible() is False


def test_dock_buttons_dispatch_operator_commands(qtbot: Any) -> None:
    """Pause / Resume / Accept Current each forward the correct
    OperatorCommand kind through ``RunController.send_operator_command``."""
    controller = _make_controller(active_id=HEAT_FLUX_TUNE_PROCEDURE_ID, state=RunUiState.RUNNING)
    dock = HeatFluxTuneDock(controller)
    qtbot.addWidget(dock)

    dock._pause_btn.click()
    dock._resume_btn.click()
    dock._accept_btn.click()

    kinds = [call.args[0].kind for call in controller.send_operator_command.call_args_list]
    assert kinds == ["pause", "resume", "accept_current"]


def test_dock_abort_button_calls_request_abort(qtbot: Any) -> None:
    """Abort goes through the existing external_stop path
    (``RunController.request_abort``), not the operator-command stream."""
    controller = _make_controller(active_id=HEAT_FLUX_TUNE_PROCEDURE_ID, state=RunUiState.RUNNING)
    dock = HeatFluxTuneDock(controller)
    qtbot.addWidget(dock)

    dock._abort_btn.click()

    controller.request_abort.assert_called_once_with(mode="safe_shutdown")


def test_dock_no_op_when_send_returns_false(qtbot: Any) -> None:
    """A controller that reports ``False`` (no active runner) shouldn't
    crash — the status label flips to a 'no active tune' message."""
    controller = _make_controller(active_id=HEAT_FLUX_TUNE_PROCEDURE_ID, state=RunUiState.RUNNING)
    controller.send_operator_command = MagicMock(return_value=False)
    dock = HeatFluxTuneDock(controller)
    qtbot.addWidget(dock)

    dock._pause_btn.click()

    assert "no active tune" in dock._status_label.text()


def test_dock_operator_command_uses_dataclass(qtbot: Any) -> None:
    """The command passed to ``send_operator_command`` is an
    :class:`OperatorCommand` (frozen dataclass), not a raw dict — the
    contract on the procedure side requires the dataclass."""
    controller = _make_controller(active_id=HEAT_FLUX_TUNE_PROCEDURE_ID, state=RunUiState.RUNNING)
    dock = HeatFluxTuneDock(controller)
    qtbot.addWidget(dock)

    dock._accept_btn.click()

    sent_arg = controller.send_operator_command.call_args.args[0]
    assert isinstance(sent_arg, OperatorCommand)
    assert sent_arg.kind == "accept_current"


# ---------------------------------------------------------------------------
# Live numerics
# ---------------------------------------------------------------------------


def _full_payload() -> dict[str, Any]:
    """A payload covering every field the panel reads, with values
    chosen so each rendered label is uniquely identifiable in the
    asserts below."""
    return {
        "phase": "settle",
        "target_index": 2,
        "target_count": 3,
        "target_kw_m2": 50.0,
        "tolerance_kw_m2": 0.5,
        "iteration": 4,
        "iteration_max": 10,
        "commanded_setpoint_c": 712.3,
        "pv_latest_c": 710.9,
        "mean_flux_kw_m2": 48.7,
        "std_flux_kw_m2": 0.08,
        "slope_flux_kw_m2_per_min": 0.04,
        "window_full": True,
        "predicate_dwell_s": 41.0,
        "predicate_last_reason": "flux-drifting",
        "elapsed_s": 60.0,
        "settle_budget_s": 1500.0,
        "sigma_max_kw_m2": 0.5,
        "slope_max_kw_m2_per_min": 0.15,
        "paused": False,
        "in_tol_windows": 1,
        "df_dt_used": 0.073,
        "df_dt_source": "secant",
        "runaway_count": 0,
        "error_kw_m2": -1.3,
    }


def test_panel_renders_full_payload(qtbot: Any) -> None:
    from capa.ui.docks.heat_flux_tune import _LiveNumericsPanel

    panel = _LiveNumericsPanel()
    qtbot.addWidget(panel)

    panel.update_from_tick(_full_payload())

    assert "Target 2 of 3" in panel._target_label.text()
    assert "50.00" in panel._target_label.text()
    assert "Iteration 4 of 10" in panel._iteration_label.text()
    assert "712.30" in panel._iteration_label.text()
    assert "48.70" in panel._flux_value.text()
    # Out-of-tolerance error: amber, no checkmark.
    assert "err -1.30" in panel._flux_extra.text()
    assert "✓" not in panel._flux_extra.text()
    assert "0.080" in panel._std_value.text()
    assert "+0.040" in panel._slope_value.text()
    assert "710.90" in panel._pv_value.text()
    assert "dwell 41" in panel._settle_value.text()
    assert "60/1500" in panel._settle_extra.text()
    assert panel._phase_value.text() == "settle"
    assert panel._phase_extra.text() == "flux-drifting"
    assert "1 of 2" in panel._intol_value.text()
    assert panel._intol_extra.text() == ""  # runaway_count == 0
    assert "0.0730" in panel._dfdt_value.text()
    assert panel._dfdt_extra.text() == "secant"


def test_panel_flux_in_tolerance_shows_checkmark(qtbot: Any) -> None:
    from capa.ui.docks.heat_flux_tune import _LiveNumericsPanel

    panel = _LiveNumericsPanel()
    qtbot.addWidget(panel)

    payload = _full_payload()
    payload["error_kw_m2"] = 0.2
    payload["mean_flux_kw_m2"] = 49.8
    panel.update_from_tick(payload)

    assert "✓" in panel._flux_extra.text()


def test_panel_runaway_count_surfaced_in_intol_extra(qtbot: Any) -> None:
    from capa.ui.docks.heat_flux_tune import _LiveNumericsPanel

    panel = _LiveNumericsPanel()
    qtbot.addWidget(panel)

    payload = _full_payload()
    payload["runaway_count"] = 2
    panel.update_from_tick(payload)

    assert "runaway 2" in panel._intol_extra.text()


def test_panel_handles_none_values_gracefully(qtbot: Any) -> None:
    """Missing optional fields (pv_latest, error_kw_m2) render as dashes
    without raising. Mirrors the first-iteration condition where no
    flux mean has been compared to the target yet."""
    from capa.ui.docks.heat_flux_tune import _LiveNumericsPanel

    panel = _LiveNumericsPanel()
    qtbot.addWidget(panel)

    payload = _full_payload()
    payload["pv_latest_c"] = None
    payload["error_kw_m2"] = None
    panel.update_from_tick(payload)

    assert "—" in panel._pv_value.text()
    # No error overlay when error is None.
    assert panel._flux_extra.text() == ""


def test_panel_holding_phase_renders_in_success_green(qtbot: Any) -> None:
    """The hold-mode tick (``phase="holding"``) styles the phase value
    in success green to match the converged-window styling. Pins the
    visual confirmation the dock latches before auto-hiding on run
    completion."""
    from capa.ui.docks.heat_flux_tune import _LiveNumericsPanel

    panel = _LiveNumericsPanel()
    qtbot.addWidget(panel)

    payload = _full_payload()
    payload["phase"] = "holding"
    panel.update_from_tick(payload)

    assert panel._phase_value.text() == "holding"
    style = panel._phase_value.styleSheet()
    assert "#2c8e3f" in style, f"expected success-green color, got: {style!r}"


def test_panel_non_holding_phase_uses_default_style(qtbot: Any) -> None:
    """Non-holding phases (settle / verify / etc.) keep the default
    monospace styling — the green is reserved for the final hold tick."""
    from capa.ui.docks.heat_flux_tune import _LiveNumericsPanel

    panel = _LiveNumericsPanel()
    qtbot.addWidget(panel)
    panel.update_from_tick(_full_payload())  # phase == "settle"

    style = panel._phase_value.styleSheet()
    assert "#2c8e3f" not in style


def test_panel_holding_phase_survives_stale_transition(qtbot: Any) -> None:
    """A holding tick arriving while the panel was stale must keep its
    success-green styling. Regression guard: ``_refresh_stale_style``
    runs first now (before per-element styling) so its blanket
    setStyleSheet call doesn't overwrite the holding color.
    """
    from capa.ui.docks.heat_flux_tune import _LiveNumericsPanel

    panel = _LiveNumericsPanel()
    qtbot.addWidget(panel)
    # Seed with a fresh tick so mark_stale takes effect.
    panel.update_from_tick(_full_payload())
    panel.mark_stale()
    assert panel._stale is True

    payload = _full_payload()
    payload["phase"] = "holding"
    panel.update_from_tick(payload)

    assert panel._stale is False
    assert "#2c8e3f" in panel._phase_value.styleSheet()
    # And the rest of the labels are un-dimmed.
    assert "#999" not in panel._flux_value.styleSheet()


def test_panel_mark_stale_is_noop_before_first_tick(qtbot: Any) -> None:
    """``mark_stale`` does nothing until at least one tick has arrived —
    no point dimming labels that still read '—'."""
    from capa.ui.docks.heat_flux_tune import _LiveNumericsPanel

    panel = _LiveNumericsPanel()
    qtbot.addWidget(panel)

    panel.mark_stale()
    assert panel._stale is False


def test_panel_mark_stale_after_tick_dims_values(qtbot: Any) -> None:
    from capa.ui.docks.heat_flux_tune import _LiveNumericsPanel

    panel = _LiveNumericsPanel()
    qtbot.addWidget(panel)

    panel.update_from_tick(_full_payload())
    panel.mark_stale()

    assert panel._stale is True
    # Stylesheet now carries the dimmed color cue.
    assert "#999" in panel._flux_value.styleSheet()


def test_panel_fresh_tick_clears_stale(qtbot: Any) -> None:
    from capa.ui.docks.heat_flux_tune import _LiveNumericsPanel

    panel = _LiveNumericsPanel()
    qtbot.addWidget(panel)

    panel.update_from_tick(_full_payload())
    panel.mark_stale()
    assert panel._stale is True
    panel.update_from_tick(_full_payload())
    assert panel._stale is False
    assert "#999" not in panel._flux_value.styleSheet()


def test_dock_routes_matching_procedure_tick_to_panel(qtbot: Any) -> None:
    """A ProcedureTick whose procedure_id matches the dock updates the
    panel; the staleness timer is (re)started."""
    from capa.runtime.emissions import ProcedureTick

    controller = _make_controller(active_id=HEAT_FLUX_TUNE_PROCEDURE_ID, state=RunUiState.RUNNING)
    dock = HeatFluxTuneDock(controller)
    qtbot.addWidget(dock)

    tick = ProcedureTick(
        procedure_id=HEAT_FLUX_TUNE_PROCEDURE_ID,
        t_mono_ns=0,
        payload=_full_payload(),
    )
    dock._on_procedure_tick(tick)

    assert "48.70" in dock._numerics._flux_value.text()
    assert dock._stale_timer.isActive()


def test_dock_ignores_tick_from_other_procedure(qtbot: Any) -> None:
    """Foreign procedure ticks must not corrupt the panel."""
    from capa.runtime.emissions import ProcedureTick

    controller = _make_controller(active_id=HEAT_FLUX_TUNE_PROCEDURE_ID, state=RunUiState.RUNNING)
    dock = HeatFluxTuneDock(controller)
    qtbot.addWidget(dock)

    tick = ProcedureTick(
        procedure_id="capa.builtin.tga_tune",
        t_mono_ns=0,
        payload=_full_payload(),
    )
    dock._on_procedure_tick(tick)

    # Panel never received a tick — _has_tick stays False.
    assert dock._numerics._has_tick is False


def test_dock_ignores_non_procedure_tick(qtbot: Any) -> None:
    """``_on_procedure_tick`` is connected to a Qt signal typed as
    ``object``; if some other emission type accidentally arrived it
    must not crash the dock."""
    controller = _make_controller(active_id=HEAT_FLUX_TUNE_PROCEDURE_ID, state=RunUiState.RUNNING)
    dock = HeatFluxTuneDock(controller)
    qtbot.addWidget(dock)

    dock._on_procedure_tick("not a tick")
    dock._on_procedure_tick(None)
    dock._on_procedure_tick(42)
    assert dock._numerics._has_tick is False
