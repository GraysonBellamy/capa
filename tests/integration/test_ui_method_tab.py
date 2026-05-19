"""``MethodTab`` integration tests.

Drives the tab end-to-end: load a fixture method, edit a step, save,
reload, assert equality. Uses
``qtbot`` for widget lifecycle; no real engine or controller is
involved here — the Method tab owns its own state."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from capa.experiment.method import (
    ChannelRef,
    HoldStep,
    Method,
    RampStep,
)
from capa.ui.forms import ModelForm
from capa.ui.tabs.method import MethodTab


def _fixture_method() -> Method:
    """Realistic two-step method: ramp the heater to 600°C, hold there."""
    return Method(
        name="ui-fixture",
        description="UI test fixture",
        steps=(
            RampStep(
                target=ChannelRef(name="heater.setpoint"),
                end_value=600.0,
                duration_s=120.0,
            ),
            HoldStep(
                target=ChannelRef(name="heater.setpoint"),
                value=600.0,
                duration_s=300.0,
            ),
        ),
    )


def test_load_method_renders_rows(qtbot: Any) -> None:
    tab = MethodTab()
    qtbot.addWidget(tab)
    tab.load_method(_fixture_method())
    assert tab._model.rowCount() == 2
    # The first row's table data exposes # / kind / target / summary.
    assert tab._model.data(tab._model.index(0, 1)) == "ramp"
    assert tab._model.data(tab._model.index(1, 1)) == "hold"


def test_add_hold_step_appends_row(qtbot: Any) -> None:
    """Click "Add Step" → Hold and confirm a new row lands in the table
    with HoldStep defaults that pass model_validate."""
    tab = MethodTab()
    qtbot.addWidget(tab)
    tab.load_method(_fixture_method())

    initial = tab._model.rowCount()
    # Locate the "Hold" action on the Add menu and trigger it.
    actions = [a for a in tab._add_menu.actions() if a.text() == "Hold"]
    assert actions, "Add Step menu should contain a Hold entry"
    actions[0].trigger()

    assert tab._model.rowCount() == initial + 1
    # Last appended kind is "hold" — the default-step factory.
    last_kind = tab._model.data(tab._model.index(tab._model.rowCount() - 1, 1))
    assert last_kind == "hold"


def test_edit_value_updates_summary(qtbot: Any) -> None:
    """Changing the step's ``value`` via the detail form must trigger a
    model update so the table summary cell reflects the new number."""
    tab = MethodTab()
    qtbot.addWidget(tab)
    tab.load_method(_fixture_method())
    tab._select_row(1)

    # The detail form is build_form(HoldStep, initial=hold_step). Reach in
    # and change the ``value`` field; the form should fire valuesChanged
    # which the tab routes back into the model.
    form = tab._detail_widget
    assert form is not None
    assert isinstance(form, ModelForm)
    value_widget = form._fields["value"]
    from PySide6.QtWidgets import QDoubleSpinBox

    spin = value_widget.findChild(QDoubleSpinBox)
    assert spin is not None
    spin.setValue(750.0)

    summary = tab._model.data(tab._model.index(1, 3))
    assert "750" in str(summary)


def test_invalid_method_blocks_save(qtbot: Any, tmp_path: Path) -> None:
    """Save with no steps must refuse and not write a file."""
    tab = MethodTab()
    qtbot.addWidget(tab)
    # Empty model — Method requires min_length=1.
    target = tmp_path / "should_not_exist.toml"
    err = tab._save_to(target)
    assert err is not None and "no valid steps" in err
    assert not target.exists()


def test_save_round_trips(qtbot: Any, tmp_path: Path) -> None:
    """Load → save → reload → equal."""
    tab = MethodTab()
    qtbot.addWidget(tab)
    method = _fixture_method()
    tab.load_method(method)

    out = tmp_path / "round_trip.toml"
    err = tab._save_to(out)
    assert err is None
    assert out.is_file()

    with open(out, "rb") as fp:
        data = tomllib.load(fp)
    reloaded = Method.model_validate(data)
    assert reloaded.name == method.name
    assert len(reloaded.steps) == len(method.steps)
    # Step-by-step structural equality.
    for orig, reread in zip(method.steps, reloaded.steps, strict=True):
        assert type(orig) is type(reread)
        assert orig.kind == reread.kind


def test_existing_sim_method_loads_and_round_trips(qtbot: Any, tmp_path: Path) -> None:
    """The shipped sim method file must load cleanly through the tab —
    catches drift in the format the writer produces vs. what the
    loader accepts."""
    repo_root = Path(__file__).resolve().parents[2]
    method_path = repo_root / "configs" / "methods" / "sim_capa_pyrolysis.method.toml"
    if not method_path.is_file():
        pytest.skip("sim CAPA method fixture missing")

    with open(method_path, "rb") as fp:
        data = tomllib.load(fp)
    method = Method.model_validate(data)

    tab = MethodTab()
    qtbot.addWidget(tab)
    tab.load_method(method, path=method_path)
    assert tab._model.rowCount() == len(method.steps)

    # Save to a tmp path and re-load to confirm structural equality.
    out = tmp_path / "sim_round_trip.toml"
    assert tab._save_to(out) is None
    with open(out, "rb") as fp:
        reloaded_data = tomllib.load(fp)
    reloaded = Method.model_validate(reloaded_data)
    assert len(reloaded.steps) == len(method.steps)


def test_clear_resets_table_and_path(qtbot: Any) -> None:
    """``clear()`` must wipe the loaded method so the tab agrees with a
    free-run experiment that has no method attached."""
    tab = MethodTab()
    qtbot.addWidget(tab)
    tab.load_method(_fixture_method(), path=Path("/tmp/whatever.method.toml"))
    assert tab.has_method() is True

    tab.clear()
    assert tab.has_method() is False
    assert tab._model.rowCount() == 0
    assert tab._method_path is None
    assert tab.current_method_name() == "untitled"


def test_load_method_emits_methodChanged(qtbot: Any) -> None:  # noqa: N802
    """``methodChanged`` fires on both load and clear so the main window
    can refresh the tab title without coupling to MethodTab internals."""
    tab = MethodTab()
    qtbot.addWidget(tab)

    with qtbot.waitSignal(tab.methodChanged, timeout=1000):
        tab.load_method(_fixture_method())

    with qtbot.waitSignal(tab.methodChanged, timeout=1000):
        tab.clear()


def test_main_window_autoloads_method_from_experiment(qtbot: Any, tmp_path: Path) -> None:
    """Opening an experiment that declares ``method:`` must populate the
    Method tab from that file — the whole point of this wiring."""
    from capa.experiment.config import ExperimentConfig
    from capa.ui.main_window import MainWindow

    repo_root = Path(__file__).resolve().parents[2]
    exp_path = repo_root / "configs" / "experiments" / "sim_capa_pyrolysis.yaml"
    if not exp_path.is_file():
        pytest.skip("sim CAPA experiment fixture missing")

    cfg = ExperimentConfig.load(exp_path)
    assert cfg.method is not None  # sanity: fixture invariant we depend on
    assert cfg.method_source_path is not None
    assert cfg.method_source_path.name == "sim_capa_pyrolysis.method.toml"

    window = MainWindow(runs_root=tmp_path, configure_logging_for_bundle=False)
    qtbot.addWidget(window)
    window._apply_loaded_config(cfg, exp_path)

    method_tab = window.method_tab
    assert method_tab.has_method() is True
    assert method_tab._model.rowCount() == len(cfg.method.steps)
    assert method_tab._method_path == cfg.method_source_path
    assert method_tab.current_method_name() == cfg.method.name
    # Tab title must mirror the method name so the operator can see at a
    # glance which method is loaded.
    assert "sim_capa_pyrolysis" in window._tabs.tabText(1)


def test_main_window_clears_method_on_freerun_load(qtbot: Any, tmp_path: Path) -> None:
    """Loading a free-run config after an experiment-with-method must
    wipe the prior method so the tab agrees with the new experiment.

    FreeRun also has ``uses_method=False``, so the Method tab is
    disabled and its label switches to the "(not used)" form — the
    operator can't author a method that would be silently ignored.
    """
    from capa.experiment.config import ExperimentConfig
    from capa.ui.main_window import MainWindow

    repo_root = Path(__file__).resolve().parents[2]
    with_method = repo_root / "configs" / "experiments" / "sim_capa_pyrolysis.yaml"
    freerun = repo_root / "configs" / "experiments" / "sim_freerun.yaml"
    if not with_method.is_file() or not freerun.is_file():
        pytest.skip("required experiment fixtures missing")

    window = MainWindow(runs_root=tmp_path, configure_logging_for_bundle=False)
    qtbot.addWidget(window)

    window._apply_loaded_config(ExperimentConfig.load(with_method), with_method)
    assert window.method_tab.has_method() is True
    assert window._tabs.isTabEnabled(1) is True

    freerun_cfg = ExperimentConfig.load(freerun)
    assert freerun_cfg.method is None  # sanity: fixture invariant
    window._apply_loaded_config(freerun_cfg, freerun)

    assert window.method_tab.has_method() is False
    assert window._tabs.isTabEnabled(1) is False
    assert window._tabs.tabText(1) == "Method (not used)"


# ---------------------------------------------------------------------------
# Method-tab gating: procedures that don't consume a method disable the tab
# so the operator doesn't waste time editing steps that will never run.
# ---------------------------------------------------------------------------


_METHOD_TAB_INDEX = 1


def test_method_tab_disabled_when_procedure_does_not_use_method(qtbot: Any, tmp_path: Path) -> None:
    """Selecting Heat-Flux Tune as the procedure must disable the Method tab.

    HeatFluxTune is self-driving — it commands its own setpoints and
    ignores ``ExperimentConfig.method``. The UI must signal that
    clearly rather than letting the operator edit a method that will
    be silently dropped at run start.
    """
    from capa.ui.main_window import MainWindow

    window = MainWindow(runs_root=tmp_path, configure_logging_for_bundle=False)
    qtbot.addWidget(window)

    # Baseline: Method tab is enabled by default.
    assert window._tabs.isTabEnabled(_METHOD_TAB_INDEX) is True

    window._on_procedure_changed("capa.builtin.heat_flux_tune")

    assert window._tabs.isTabEnabled(_METHOD_TAB_INDEX) is False
    assert window._tabs.tabText(_METHOD_TAB_INDEX) == "Method (not used)"
    tooltip = window._tabs.tabToolTip(_METHOD_TAB_INDEX)
    assert "heat_flux_tune" in tooltip
    assert "does not use a method" in tooltip


def test_method_tab_reenabled_when_switching_to_recipe_runner(qtbot: Any, tmp_path: Path) -> None:
    """Switching back to a method-driven procedure must re-enable the tab."""
    from capa.ui.main_window import MainWindow

    window = MainWindow(runs_root=tmp_path, configure_logging_for_bundle=False)
    qtbot.addWidget(window)

    window._on_procedure_changed("capa.builtin.heat_flux_tune")
    assert window._tabs.isTabEnabled(_METHOD_TAB_INDEX) is False

    window._on_procedure_changed("capa.builtin.recipe_runner")
    assert window._tabs.isTabEnabled(_METHOD_TAB_INDEX) is True
    # Tab title falls back to "Method" when no method is loaded; the
    # disabled-state label has been cleared.
    assert window._tabs.tabText(_METHOD_TAB_INDEX) == "Method"
    assert window._tabs.tabToolTip(_METHOD_TAB_INDEX) == ""


def test_method_tab_auto_switches_away_when_disabled(qtbot: Any, tmp_path: Path) -> None:
    """If the operator is on the Method tab when it gets disabled, they
    must be bounced back to Setup. Leaving them parked on a disabled
    tab body would be confusing — the tab would be the active one but
    the click target would refuse selection."""
    from capa.ui.main_window import MainWindow

    window = MainWindow(runs_root=tmp_path, configure_logging_for_bundle=False)
    qtbot.addWidget(window)

    # Park the operator on the Method tab.
    window._tabs.setCurrentIndex(_METHOD_TAB_INDEX)
    assert window._tabs.currentIndex() == _METHOD_TAB_INDEX

    window._on_procedure_changed("capa.builtin.heat_flux_tune")

    assert window._tabs.isTabEnabled(_METHOD_TAB_INDEX) is False
    assert window._tabs.currentWidget() is window.setup_tab


def test_method_tab_unknown_procedure_keeps_tab_enabled(qtbot: Any, tmp_path: Path) -> None:
    """A procedure id the registry doesn't know defaults to ``uses_method=True``.

    This avoids the failure mode where a typo in the procedure id
    silently hides the Method tab — the operator can still author a
    method while they sort out the misspelling.
    """
    from capa.ui.main_window import MainWindow

    window = MainWindow(runs_root=tmp_path, configure_logging_for_bundle=False)
    qtbot.addWidget(window)

    window._on_procedure_changed("not.a.real.procedure")

    assert window._tabs.isTabEnabled(_METHOD_TAB_INDEX) is True
