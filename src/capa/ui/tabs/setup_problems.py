"""Problems panel — navigable validation findings.

Reads :class:`~capa.config.problems.ConfigProblem`\\ s from the Setup
draft and renders them as a small table. Clicking a row activates the
problem; the Setup tab routes that to outline navigation.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    Qt,
    Signal,
)
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from capa.config.problems import ConfigProblem
from capa.ui.tabs.setup_sections._models import horizontal_header

_HEADERS: tuple[str, ...] = ("Severity", "Section", "Message")

_SEVERITY_ORDER: dict[str, int] = {"error": 0, "warning": 1, "info": 2}

_SEVERITY_BRUSH: dict[str, QColor] = {
    "error": QColor("#b33"),
    "warning": QColor("#b80"),
    "info": QColor("#555"),
}


class ProblemsTableModel(QAbstractTableModel):
    """Read-only table model over a list of :class:`ConfigProblem`\\ s."""

    def __init__(self) -> None:
        super().__init__()
        self._problems: list[ConfigProblem] = []

    def set_problems(self, problems: list[ConfigProblem]) -> None:
        self.beginResetModel()
        self._problems = sorted(
            problems, key=lambda p: (_SEVERITY_ORDER.get(p.severity, 99), p.section)
        )
        self.endResetModel()

    def problem_at(self, row: int) -> ConfigProblem | None:
        if 0 <= row < len(self._problems):
            return self._problems[row]
        return None

    # -- QAbstractTableModel ------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        if parent.isValid():
            return 0
        return len(self._problems)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        if parent.isValid():
            return 0
        return len(_HEADERS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        return horizontal_header(_HEADERS, section, orientation, role)

    def data(  # type: ignore[override]
        self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if not index.isValid():
            return None
        row = index.row()
        col = index.column()
        if row < 0 or row >= len(self._problems):
            return None
        problem = self._problems[row]
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return _severity_glyph(problem.severity)
            if col == 1:
                return problem.section
            if col == 2:
                return problem.message
        if role == Qt.ItemDataRole.ToolTipRole and col == 2:
            return f"{problem.code} — {problem.message}"
        if role == Qt.ItemDataRole.ForegroundRole and col == 0:
            colour = _SEVERITY_BRUSH.get(problem.severity)
            if colour is not None:
                return QBrush(colour)
        return None


def _severity_glyph(severity: str) -> str:
    return {"error": "✗ Error", "warning": "⚠ Warn", "info": "ⓘ Info"}.get(severity, severity)


class SetupProblems(QWidget):
    """Panel hosting the problems table + a one-line summary header."""

    problemActivated = Signal(object)  # noqa: N815 — Qt signal naming convention
    """``ConfigProblem`` — fires when the operator activates a row
    (single click or Return). The Setup tab routes this to outline
    navigation."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 0)
        outer.setSpacing(4)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(8, 0, 8, 0)
        self._summary = QLabel("Problems (0)", self)
        self._summary.setStyleSheet("font-weight: 600;")
        header_row.addWidget(self._summary)
        header_row.addStretch(1)
        outer.addLayout(header_row)

        self._model = ProblemsTableModel()
        self._table = QTableView(self)
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._table.clicked.connect(self._on_row_activated)
        self._table.activated.connect(self._on_row_activated)
        outer.addWidget(self._table)

        self.setMaximumHeight(160)
        self.setMinimumHeight(80)

    # -- API ----------------------------------------------------------------

    def set_problems(self, problems: list[ConfigProblem]) -> None:
        self._model.set_problems(problems)
        errs = sum(1 for p in problems if p.severity == "error")
        warns = sum(1 for p in problems if p.severity == "warning")
        infos = sum(1 for p in problems if p.severity == "info")
        parts: list[str] = []
        if errs:
            parts.append(f"{errs} error(s)")
        if warns:
            parts.append(f"{warns} warning(s)")
        if infos:
            parts.append(f"{infos} info")
        suffix = " — " + ", ".join(parts) if parts else ""
        self._summary.setText(f"Problems ({len(problems)}){suffix}")

    # -- slots --------------------------------------------------------------

    def _on_row_activated(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        problem = self._model.problem_at(index.row())
        if problem is not None:
            self.problemActivated.emit(problem)


__all__ = ["ProblemsTableModel", "SetupProblems"]
