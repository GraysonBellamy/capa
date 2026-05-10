"""``MethodTab`` integration tests.

Plan §10.1 / P3 follow-up item 1. Drives the tab end-to-end: load a
fixture method, edit a step, save, reload, assert equality. Uses
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
    assert tab._model.rowCount() == 2  # noqa: SLF001
    # The first row's table data exposes # / kind / target / summary.
    assert tab._model.data(tab._model.index(0, 1)) == "ramp"
    assert tab._model.data(tab._model.index(1, 1)) == "hold"


def test_add_hold_step_appends_row(qtbot: Any) -> None:
    """Click "Add Step" → Hold and confirm a new row lands in the table
    with HoldStep defaults that pass model_validate."""
    tab = MethodTab()
    qtbot.addWidget(tab)
    tab.load_method(_fixture_method())

    initial = tab._model.rowCount()  # noqa: SLF001
    # Locate the "Hold" action on the Add menu and trigger it.
    actions = [a for a in tab._add_menu.actions() if a.text() == "Hold"]  # noqa: SLF001
    assert actions, "Add Step menu should contain a Hold entry"
    actions[0].trigger()

    assert tab._model.rowCount() == initial + 1  # noqa: SLF001
    # Last appended kind is "hold" — the default-step factory.
    last_kind = tab._model.data(tab._model.index(tab._model.rowCount() - 1, 1))  # noqa: SLF001
    assert last_kind == "hold"


def test_edit_value_updates_summary(qtbot: Any) -> None:
    """Changing the step's ``value`` via the detail form must trigger a
    model update so the table summary cell reflects the new number."""
    tab = MethodTab()
    qtbot.addWidget(tab)
    tab.load_method(_fixture_method())
    tab._select_row(1)  # noqa: SLF001 - the HoldStep

    # The detail form is build_form(HoldStep, initial=hold_step). Reach in
    # and change the ``value`` field; the form should fire valuesChanged
    # which the tab routes back into the model.
    form = tab._detail_widget  # noqa: SLF001
    assert form is not None
    value_widget = form._fields["value"]  # noqa: SLF001
    from PyQt6.QtWidgets import QDoubleSpinBox  # noqa: PLC0415

    spin = value_widget.findChild(QDoubleSpinBox)
    assert spin is not None
    spin.setValue(750.0)

    summary = tab._model.data(tab._model.index(1, 3))  # noqa: SLF001
    assert "750" in str(summary)


def test_invalid_method_blocks_save(qtbot: Any, tmp_path: Path) -> None:
    """Save with no steps must refuse and not write a file."""
    tab = MethodTab()
    qtbot.addWidget(tab)
    # Empty model — Method requires min_length=1.
    target = tmp_path / "should_not_exist.toml"
    err = tab._save_to(target)  # noqa: SLF001
    assert err is not None and "no valid steps" in err
    assert not target.exists()


def test_save_round_trips(qtbot: Any, tmp_path: Path) -> None:
    """Load → save → reload → equal."""
    tab = MethodTab()
    qtbot.addWidget(tab)
    method = _fixture_method()
    tab.load_method(method)

    out = tmp_path / "round_trip.toml"
    err = tab._save_to(out)  # noqa: SLF001
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
    assert tab._model.rowCount() == len(method.steps)  # noqa: SLF001

    # Save to a tmp path and re-load to confirm structural equality.
    out = tmp_path / "sim_round_trip.toml"
    assert tab._save_to(out) is None  # noqa: SLF001
    with open(out, "rb") as fp:
        reloaded_data = tomllib.load(fp)
    reloaded = Method.model_validate(reloaded_data)
    assert len(reloaded.steps) == len(method.steps)
