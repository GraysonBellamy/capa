"""``MethodTab`` — author and edit segmented method profiles.

Plan §10.1. Three regions:

* **Toolbar** — Open / Save / Save As / Validate / Add Step menu /
  Delete. Save validates before writing; an invalid method shows the
  error list in a :class:`QMessageBox` and refuses to write.
* **Step table + detail** — :class:`MethodTableModel` on the left,
  auto-form for the selected step on the right. Detail edits flow back
  into the model immediately, with the table summary updating live.
* **Profile graph** — PyQtGraph plot of setpoint vs. elapsed time.

Scope shaped by the 90/10 rule: the single-setpoint-hold case is
ergonomic (one click to add a HoldStep with sane defaults, immediate
edit on the right, save). The dynamic-program case (ramps, multi-step)
works correctly without gold plating — no drag-to-edit, no fancy
multi-axis plots.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pyqtgraph as pg
import tomli_w
import tomllib
from PyQt6.QtCore import QItemSelectionModel, Qt
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)
from pydantic import BaseModel, ValidationError

from capa.experiment.method import (
    AcquireStep,
    AlarmOverride,
    ChannelRef,
    CustomStep,
    EndCondition,
    HoldStep,
    Method,
    PromptStep,
    RampStep,
    SafeShutdownStep,
    SetpointStep,
    Step,
    WaitStep,
)
from capa.ui.forms import build_form
from capa.ui.tabs.method_graph import render_method_graph
from capa.ui.tabs.method_table import MethodTableModel


def _toml_safe(value: Any) -> Any:
    """Drop None / unhashable mid-mapping shapes that ``tomli_w``
    refuses. Mirrors the bundle writer's helper of the same name; kept
    private here to avoid leaking storage/bundle as a UI-tab dependency."""
    if isinstance(value, dict):
        return {k: _toml_safe(v) for k, v in value.items() if v is not None}
    if isinstance(value, list | tuple):
        return [_toml_safe(v) for v in value if v is not None]
    return value


# Default-step factories — minimum-viable instances per kind. Used by
# the "Add Step" menu so a fresh row is well-formed (passes
# Step.model_validate) without operator effort. Values are deliberately
# tame — a hold at 25 °C for 60 s won't damage a real reactor if the
# operator forgets to edit it.
def _default_hold() -> HoldStep:
    return HoldStep(target=ChannelRef(name="heater.setpoint"), value=25.0, duration_s=60.0)


def _default_ramp() -> RampStep:
    return RampStep(target=ChannelRef(name="heater.setpoint"), end_value=100.0, duration_s=60.0)


def _default_setpoint() -> SetpointStep:
    return SetpointStep(target=ChannelRef(name="heater.setpoint"), value=25.0)


def _default_wait() -> WaitStep:
    return WaitStep(duration_s=10.0)


def _default_prompt() -> PromptStep:
    return PromptStep(message="Operator action required")


def _default_acquire() -> AcquireStep:
    return AcquireStep(duration_s=10.0)


def _default_safe_shutdown() -> SafeShutdownStep:
    return SafeShutdownStep(cool_target={"heater.setpoint": 25.0})


def _default_custom() -> CustomStep:
    return CustomStep(handler_id="capa.custom.unset")


_STEP_FACTORIES: list[tuple[str, type[Step], Any]] = [
    ("Hold", HoldStep, _default_hold),
    ("Ramp", RampStep, _default_ramp),
    ("Setpoint", SetpointStep, _default_setpoint),
    ("Wait", WaitStep, _default_wait),
    ("Prompt", PromptStep, _default_prompt),
    ("Acquire", AcquireStep, _default_acquire),
    ("Safe shutdown", SafeShutdownStep, _default_safe_shutdown),
    ("Custom", CustomStep, _default_custom),
]


class MethodTab(QWidget):
    """Author and edit a method without leaving the GUI.

    Construct via ``MethodTab(parent=main_window)``. The tab owns its
    own model state; it does not reach into the controller or live run
    state. Methods are loaded/saved via :class:`QFileDialog`."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._method_path: Path | None = None
        self._method_name: str = "untitled"
        self._method_description: str = ""

        self._model = MethodTableModel()
        self._detail_widget: QWidget | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)

        # Toolbar.
        self._toolbar = QToolBar("Method", self)
        self._toolbar.setMovable(False)
        self._toolbar.addAction("Open", self._on_open)
        self._toolbar.addAction("Save", self._on_save)
        self._toolbar.addAction("Save As", self._on_save_as)
        self._toolbar.addAction("Validate", self._on_validate)
        self._toolbar.addSeparator()

        self._add_button = QPushButton("Add Step", self)
        self._add_menu = QMenu(self._add_button)
        for label, _cls, factory in _STEP_FACTORIES:
            self._add_menu.addAction(label, lambda f=factory: self._on_add_step(f()))
        self._add_button.setMenu(self._add_menu)
        self._toolbar.addWidget(self._add_button)
        self._toolbar.addAction("Delete Step", self._on_delete_step)
        outer.addWidget(self._toolbar)

        # Top half: table + detail panel.
        upper = QSplitter(Qt.Orientation.Horizontal, self)

        self._table = QTableView(self)
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        header = self._table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            header.setStretchLastSection(True)
        self._table.selectionModel().selectionChanged.connect(self._on_row_changed)
        self._model.dataChanged.connect(self._on_table_changed)
        self._model.modelReset.connect(self._on_table_changed)
        self._model.rowsInserted.connect(self._on_table_changed)
        self._model.rowsRemoved.connect(self._on_table_changed)
        upper.addWidget(self._table)

        # Detail container — replaced on every selection change.
        self._detail_container = QWidget(self)
        detail_layout = QVBoxLayout(self._detail_container)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        upper.addWidget(self._detail_container)
        upper.setSizes([400, 600])

        outer.addWidget(upper, stretch=2)

        # Profile graph.
        self._plot = pg.PlotWidget(self)
        self._plot.setMinimumHeight(200)
        outer.addWidget(self._plot, stretch=1)

    # ------------------------------------------------------------------ API

    def method(self) -> Method | None:
        """Return the current method as a validated :class:`Method`.

        Returns ``None`` when the model has no steps (Method requires
        ``min_length=1``); the toolbar's Save action handles that
        explicitly."""
        steps = self._model.steps()
        if not steps:
            return None
        try:
            return Method.model_validate(
                {
                    "name": self._method_name,
                    "description": self._method_description,
                    "steps": [step.model_dump(mode="python") for step in steps],
                }
            )
        except ValidationError:
            return None

    def load_method(self, method: Method, *, path: Path | None = None) -> None:
        self._method_name = method.name
        self._method_description = method.description
        self._method_path = path
        self._model.set_steps(method.steps)
        self._reset_detail()
        if self._model.rowCount() > 0:
            self._select_row(0)

    # ------------------------------------------------------------------ slots

    def _on_open(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Open method", str(self._initial_dir()), "Method (*.toml);;All files (*)"
        )
        if not path_str:
            return
        path = Path(path_str)
        try:
            with open(path, "rb") as fp:
                data = tomllib.load(fp)
            method = Method.model_validate(data)
        except (OSError, ValidationError, tomllib.TOMLDecodeError) as exc:
            QMessageBox.critical(self, "Load failed", f"{path}\n\n{exc}")
            return
        self.load_method(method, path=path)

    def _on_save(self) -> None:
        if self._method_path is None:
            self._on_save_as()
            return
        err = self._save_to(self._method_path)
        if err is not None:
            self._show_save_error(err)

    def _on_save_as(self) -> None:
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Save method",
            str(self._initial_dir()),
            "Method (*.toml);;All files (*)",
        )
        if not path_str:
            return
        path = Path(path_str)
        if path.suffix.lower() != ".toml":
            path = path.with_suffix(".toml")
        err = self._save_to(path)
        if err is None:
            self._method_path = path
        else:
            self._show_save_error(err)

    def _show_save_error(self, message: str) -> None:
        """Show the save-failed dialog. Pulled out of :meth:`_save_to` so
        the save logic itself never spawns a modal dialog — that lets
        tests drive ``_save_to`` directly without deadlocking on Qt's
        modal event loop."""
        QMessageBox.warning(self, "Save failed", message)

    def _on_validate(self) -> None:
        method = self.method()
        if method is None:
            QMessageBox.information(
                self,
                "Validate",
                "Method has no valid steps yet — add at least one and fix any field errors.",
            )
            return
        QMessageBox.information(
            self,
            "Validate",
            f"OK — {len(method.steps)} step(s).",
        )

    def _on_add_step(self, step: Step) -> None:
        # Insert after the current selection, or at the end if nothing
        # is selected. Selecting the new row drives the detail panel to
        # render the form for it.
        current = self._current_row()
        new_row = len(self._model.steps()) if current is None else current + 1
        self._model.insert_step(new_row, step)
        self._select_row(new_row)

    def _on_delete_step(self) -> None:
        row = self._current_row()
        if row is None:
            return
        self._model.remove_step(row)
        self._reset_detail()
        self._render_graph()
        # Re-select a sensible neighbor.
        new_count = self._model.rowCount()
        if new_count > 0:
            self._select_row(min(row, new_count - 1))

    def _on_row_changed(self) -> None:
        row = self._current_row()
        self._build_detail_for_row(row)

    def _on_table_changed(self, *_args: object) -> None:
        self._render_graph()

    # ------------------------------------------------------------------ detail panel

    def _build_detail_for_row(self, row: int | None) -> None:
        # Tear down the previous detail widget to avoid stale signals.
        layout = self._detail_container.layout()
        if layout is not None:
            while layout.count():
                item = layout.takeAt(0)
                if item is None:
                    continue
                w = item.widget()
                if w is not None:
                    w.deleteLater()

        if row is None:
            return
        step = self._model.step_at(row)
        if step is None:
            return

        step_cls = type(step)
        form = build_form(step_cls, initial=step)
        # Stash on self so tests can reach the live form widget.
        self._detail_widget = form

        def _on_form_changed() -> None:
            # Try to rebuild the step from the form's current values.
            # Validation errors leave the existing row in place but
            # paint inline indicators on the offending widget(s).
            try:
                values = form.values()
                # Re-inject the discriminator so model_validate dispatches
                # back to the right subclass (the form hides ``kind``).
                values["kind"] = step.kind
                new_step = cast(Step, step_cls.model_validate(values))
            except ValidationError:
                form.validate()  # paints inline errors; do not write back.
                return
            self._model.replace_step(row, new_step)

        form.valuesChanged.connect(_on_form_changed)
        if layout is not None:
            layout.addWidget(form)

    def _reset_detail(self) -> None:
        self._build_detail_for_row(None)
        self._detail_widget = None

    # ------------------------------------------------------------------ helpers

    def _current_row(self) -> int | None:
        sel = self._table.selectionModel()
        if sel is None:
            return None
        idx = sel.currentIndex()
        if not idx.isValid():
            return None
        return idx.row()

    def _select_row(self, row: int) -> None:
        sel = self._table.selectionModel()
        if sel is None:
            return
        idx = self._model.index(row, 0)
        sel.setCurrentIndex(
            idx,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )

    def _save_to(self, path: Path) -> str | None:
        """Try to write the current method to ``path``. Returns ``None``
        on success or a human-readable error string on failure. Never
        spawns a modal dialog — the caller decides whether to surface
        the error to the operator."""
        method = self.method()
        if method is None:
            return (
                "Method has no valid steps. Add at least one step and "
                "fix any field errors before saving."
            )
        try:
            payload = _toml_safe(method.model_dump(mode="json"))
            path.write_text(tomli_w.dumps(payload), encoding="utf-8")
        except (OSError, ValueError) as exc:
            return f"{path}\n\n{exc}"
        return None

    def _initial_dir(self) -> Path:
        return self._method_path.parent if self._method_path is not None else Path.home()

    def _render_graph(self) -> None:
        render_method_graph(self._plot, self._model.steps())


__all__ = ["MethodTab"]
