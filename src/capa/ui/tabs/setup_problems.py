"""Problems panel — navigable validation findings.

Reads :class:`~capa.config.problems.ConfigProblem`\\ s from the Setup
draft and renders them as a small table. Clicking a row activates the
problem; the Setup tab routes that to outline navigation. Right-click
context menu offers Copy and Copy All so operators can paste the
problem text into a bug report or chat.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    Qt,
    Signal,
)
from PySide6.QtGui import QAction, QBrush, QColor, QGuiApplication
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QStyledItemDelegate,
    QStyleOptionViewItem,
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

# Glyphs are shape-distinct as well as color-distinct so colorblind
# operators can read severity from the symbol alone.
_SEVERITY_GLYPH: dict[str, str] = {
    "error": "✗",
    "warning": "⚠",
    "info": "ⓘ",
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

    def problems(self) -> list[ConfigProblem]:
        """Return the current ordered problem list (for copy-all)."""
        return list(self._problems)

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
                return _SEVERITY_GLYPH.get(problem.severity, problem.severity)
            if col == 1:
                return problem.section
            if col == 2:
                return problem.message
        if role == Qt.ItemDataRole.ToolTipRole:
            # Full text on every column — long messages truncate visually
            # but the tooltip always renders the entire problem so
            # screen readers and accessibility paths see it too.
            return f"{problem.code}\n{problem.section}: {problem.message}"
        if role == Qt.ItemDataRole.ForegroundRole and col == 0:
            colour = _SEVERITY_BRUSH.get(problem.severity)
            if colour is not None:
                return QBrush(colour)
        return None


class _WordWrapDelegate(QStyledItemDelegate):
    """Item delegate that forces text wrapping on the Message column.

    ``QTableView`` doesn't wrap by default; a long problem message
    truncates with an ellipsis. The delegate widens row height to
    accommodate the wrapped text.
    """

    def initStyleOption(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> None:
        super().initStyleOption(option, index)
        option.features |= QStyleOptionViewItem.ViewItemFeature.WrapText
        option.textElideMode = Qt.TextElideMode.ElideNone


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
        self._table.setWordWrap(True)
        self._table.verticalHeader().setVisible(False)
        # Make rows tall enough to read wrapped messages without forcing
        # the whole panel taller — vertical resize mode follows contents.
        self._table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._table.setItemDelegateForColumn(2, _WordWrapDelegate(self._table))
        header = self._table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._table.clicked.connect(self._on_row_activated)
        self._table.activated.connect(self._on_row_activated)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        outer.addWidget(self._table)

        # Empty state collapses to just the summary line so the panel
        # doesn't waste ~80px of vertical space when there are no
        # problems to read. The populated state restores the 80–220 px
        # bounds in :meth:`set_problems`.
        self._table.hide()
        self.setMaximumHeight(self.sizeHint().height())

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
        if problems:
            self._table.show()
            self.setMinimumHeight(80)
            self.setMaximumHeight(220)
        else:
            self._table.hide()
            # Collapse the panel back down to the header line so the
            # central editor area reclaims the space.
            self.setMinimumHeight(0)
            self.setMaximumHeight(self.sizeHint().height())

    # -- slots --------------------------------------------------------------

    def _on_row_activated(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        problem = self._model.problem_at(index.row())
        if problem is not None:
            self.problemActivated.emit(problem)

    def _on_context_menu(self, position: QPoint) -> None:
        index = self._table.indexAt(position)
        menu = QMenu(self._table)
        copy_row = QAction("Copy message", menu)
        copy_row.setEnabled(index.isValid())
        copy_row.triggered.connect(lambda: self._copy_row(index))
        menu.addAction(copy_row)
        copy_all = QAction("Copy all problems", menu)
        copy_all.setEnabled(self._model.rowCount() > 0)
        copy_all.triggered.connect(self._copy_all)
        menu.addAction(copy_all)
        viewport = self._table.viewport()
        if viewport is not None:
            menu.exec(viewport.mapToGlobal(position))

    def _copy_row(self, index: QModelIndex) -> None:
        problem = self._model.problem_at(index.row())
        if problem is None:
            return
        QGuiApplication.clipboard().setText(_format_problem(problem))

    def _copy_all(self) -> None:
        lines = [_format_problem(p) for p in self._model.problems()]
        QGuiApplication.clipboard().setText("\n".join(lines))


def _format_problem(problem: ConfigProblem) -> str:
    """One-line representation of a problem suitable for paste-into-chat."""
    return f"[{problem.severity}] {problem.section} · {problem.code}: {problem.message}"


__all__ = ["ProblemsTableModel", "SetupProblems"]
