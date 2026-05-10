"""``MethodTableModel`` — :class:`QAbstractTableModel` over ``list[Step]``.

Plan §10.1. The Method tab's left pane is a QTableView bound to this
model; selection drives which step is shown in the detail pane on the
right. Columns are intentionally narrow:

* ``#`` — 1-based step number for operator readability (matches the
  ``[NN]`` index format used by ``capa method validate`` output);
* ``kind`` — the discriminator literal (``hold`` / ``ramp`` / …);
* ``target`` — the channel the step commands, or ``-`` for steps that
  don't drive a channel (wait, prompt, acquire, safe_shutdown, custom);
* ``summary`` — a one-line preview tuned per step kind (e.g.
  ``"650 °C, 5 min"`` for a hold).

The model owns the canonical ``list[Step]``; replace by index, never
mutate, since :class:`~capa.experiment.method.Step` subclasses are
``frozen``."""

from __future__ import annotations

from typing import Any, Final

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt

from capa.experiment.method import (
    AcquireStep,
    CustomStep,
    HoldStep,
    PromptStep,
    RampStep,
    SafeShutdownStep,
    SetpointStep,
    Step,
    WaitStep,
)

_HEADERS: Final[tuple[str, ...]] = ("#", "kind", "target", "summary")


def _summary(step: Step) -> str:
    """One-line description tuned per step kind. Kept narrow so the
    summary column stays readable in the default table width."""
    if isinstance(step, HoldStep):
        d = step.duration_s
        if d is None:
            return f"{step.value:g} (until end_condition)"
        return f"{step.value:g} for {d:g} s"
    if isinstance(step, RampStep):
        end = step.end_value
        if step.start_value is not None:
            return f"{step.start_value:g} → {end:g}"
        return f"→ {end:g}"
    if isinstance(step, SetpointStep):
        return f"set {step.value:g}"
    if isinstance(step, WaitStep):
        if step.duration_s is not None:
            return f"wait {step.duration_s:g} s"
        if step.end_condition is not None:
            ec = step.end_condition
            return f"until {ec.channel} {ec.op} {ec.value:g}"
        return "wait"
    if isinstance(step, PromptStep):
        snippet = step.message[:40] + ("…" if len(step.message) > 40 else "")
        return f"prompt: {snippet}"
    if isinstance(step, AcquireStep):
        return f"acquire {step.duration_s:g} s"
    if isinstance(step, SafeShutdownStep):
        if step.cool_target:
            targets = ", ".join(f"{k}={v:g}" for k, v in step.cool_target.items())
            return f"shutdown ({targets})"
        return "shutdown"
    if isinstance(step, CustomStep):
        return f"custom: {step.handler_id}"
    return "—"


def _target_name(step: Step) -> str:
    """Channel name driven by this step, or ``"-"`` if none. Kept simple
    — the summary column carries the rich description; this column is
    just a quick visual anchor."""
    target = getattr(step, "target", None)
    if target is not None and hasattr(target, "name"):
        return str(target.name)
    return "-"


class MethodTableModel(QAbstractTableModel):
    """Table model exposing one row per step. The model is the canonical
    owner of the step list; the parent :class:`MethodTab` queries
    :meth:`steps` to read and :meth:`replace_step` / :meth:`insert_step`
    / :meth:`remove_step` to write."""

    def __init__(self, steps: tuple[Step, ...] = ()) -> None:
        super().__init__()
        self._steps: list[Step] = list(steps)

    # ----------------------------------------------------------- Qt overrides

    def rowCount(self, parent: QModelIndex | None = None) -> int:  # noqa: N802 - Qt
        return 0 if parent is not None and parent.isValid() else len(self._steps)

    def columnCount(self, parent: QModelIndex | None = None) -> int:  # noqa: N802 - Qt
        return 0 if parent is not None and parent.isValid() else len(_HEADERS)

    def data(  # noqa: PLR0911 - one branch per column is the clearest shape
        self,
        index: QModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        row = index.row()
        if not 0 <= row < len(self._steps):
            return None
        step = self._steps[row]
        col = index.column()
        if col == 0:
            return row + 1
        if col == 1:
            return step.kind
        if col == 2:
            return _target_name(step)
        if col == 3:
            return _summary(step)
        return None

    def headerData(  # noqa: N802 - Qt
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(_HEADERS):
            return _HEADERS[section]
        return None

    # ----------------------------------------------------------- public API

    def steps(self) -> tuple[Step, ...]:
        """Snapshot of the current step list."""
        return tuple(self._steps)

    def step_at(self, row: int) -> Step | None:
        if 0 <= row < len(self._steps):
            return self._steps[row]
        return None

    def set_steps(self, steps: tuple[Step, ...]) -> None:
        self.beginResetModel()
        self._steps = list(steps)
        self.endResetModel()

    def replace_step(self, row: int, step: Step) -> None:
        """Replace one step in place. Step subclasses are ``frozen``, so
        callers always create a new instance via ``model_validate``;
        this method swaps the new instance into the list and fires the
        per-row dataChanged signal."""
        if not 0 <= row < len(self._steps):
            return
        self._steps[row] = step
        top = self.index(row, 0)
        bottom = self.index(row, len(_HEADERS) - 1)
        self.dataChanged.emit(top, bottom)

    def insert_step(self, row: int, step: Step) -> None:
        """Insert ``step`` at ``row``. Use ``len(steps)`` to append."""
        row = max(0, min(row, len(self._steps)))
        self.beginInsertRows(QModelIndex(), row, row)
        self._steps.insert(row, step)
        self.endInsertRows()

    def remove_step(self, row: int) -> None:
        if not 0 <= row < len(self._steps):
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        self._steps.pop(row)
        self.endRemoveRows()


__all__ = ["MethodTableModel"]
