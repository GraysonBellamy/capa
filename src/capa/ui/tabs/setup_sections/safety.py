"""Safety section — rule table + per-row detail editor.

Top half: :class:`SafetyRuleTableModel` over the rules tuple.
Bottom half: an auto-form for the selected :class:`SafetyRuleConfig`.

``SafetyRuleConfig.params`` is a free-form ``dict[str, Any]`` so the
auto-form falls back to a JSON editor; per-``kind`` curated forms can
land later. JSON is the operator's escape hatch.

The default-abort knob (``SafetyPolicy.default_abort``) lives in a tiny
form above the table because it's a single field and operators look
for it at the top of the screen, not buried in a properties pane.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from capa.experiment.config import SafetyRuleConfig
from capa.ui.forms import build_form
from capa.ui.tabs.setup_sections._base import SectionWidget
from capa.ui.tabs.setup_sections._models import horizontal_header

if TYPE_CHECKING:
    from capa.ui.forms.from_model import ModelForm
    from capa.ui.tabs.setup_state import SetupDraft


# ---------------------------------------------------------------------------
# Table model.
# ---------------------------------------------------------------------------


class SafetyRuleTableModel(QAbstractTableModel):
    """Editable list-of-rules model."""

    rulesChanged = Signal()  # noqa: N815 — Qt signal naming convention
    """Fires after any row insert/remove/edit. The Safety section
    connects this to its ``valuesChanged`` re-emit."""

    HEADERS: tuple[str, ...] = ("id", "kind", "action")

    def __init__(self) -> None:
        super().__init__()
        self._rules: list[dict[str, Any]] = []

    def rules(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rules]

    def set_rules(self, rules: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self._rules = [dict(row) for row in rules]
        self.endResetModel()

    def rule_at(self, row: int) -> dict[str, Any] | None:
        if 0 <= row < len(self._rules):
            return dict(self._rules[row])
        return None

    def update_rule(self, row: int, rule: dict[str, Any]) -> None:
        if not (0 <= row < len(self._rules)):
            return
        self._rules[row] = dict(rule)
        top_left = self.index(row, 0)
        bottom_right = self.index(row, len(self.HEADERS) - 1)
        self.dataChanged.emit(top_left, bottom_right)
        self.rulesChanged.emit()

    def add_rule(self, rule: dict[str, Any]) -> int:
        row = len(self._rules)
        self.beginInsertRows(QModelIndex(), row, row)
        self._rules.append(dict(rule))
        self.endInsertRows()
        self.rulesChanged.emit()
        return row

    def remove_rule(self, row: int) -> None:
        if not (0 <= row < len(self._rules)):
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        self._rules.pop(row)
        self.endRemoveRows()
        self.rulesChanged.emit()

    # -- QAbstractTableModel ------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        if parent.isValid():
            return 0
        return len(self._rules)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        if parent.isValid():
            return 0
        return len(self.HEADERS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        return horizontal_header(self.HEADERS, section, orientation, role)

    def data(  # type: ignore[override]
        self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        rule = self.rule_at(index.row())
        if rule is None:
            return None
        key = self.HEADERS[index.column()]
        value = rule.get(key, "")
        return str(value) if value is not None else ""


# ---------------------------------------------------------------------------
# Section widget.
# ---------------------------------------------------------------------------


def _default_rule() -> dict[str, Any]:
    return {
        "id": "new_rule",
        "kind": "max_temperature",
        "params": {},
        "action": "warn",
    }


class SafetySection(SectionWidget):
    """SafetyPolicy editor — table + detail form."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._draft: SetupDraft | None = None
        self._suppress_signals = False
        self._current_form: ModelForm | None = None
        self._current_row: int = -1

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        title = QLabel("Safety", self)
        title.setStyleSheet("font-size: 14pt; font-weight: 600;")
        outer.addWidget(title)

        # Default-abort knob (single-field form so it can move into a
        # group with other policy-wide knobs later without reshuffling
        # the section).
        default_form = QFormLayout()
        self._default_abort_edit = QLineEdit(self)
        self._default_abort_edit.setPlaceholderText("safe_shutdown / abort_run")
        self._default_abort_edit.textChanged.connect(self._on_default_abort_changed)
        default_form.addRow("Default abort:", self._default_abort_edit)
        outer.addLayout(default_form)

        # Table + detail splitter.
        splitter = QSplitter(Qt.Orientation.Vertical, self)

        # Table region with action buttons.
        table_region = QWidget(splitter)
        table_layout = QVBoxLayout(table_region)
        table_layout.setContentsMargins(0, 0, 0, 0)

        button_row = QHBoxLayout()
        self._add_btn = QPushButton("Add rule", self)
        self._duplicate_btn = QPushButton("Duplicate", self)
        self._remove_btn = QPushButton("Delete", self)
        button_row.addWidget(self._add_btn)
        button_row.addWidget(self._duplicate_btn)
        button_row.addWidget(self._remove_btn)
        button_row.addStretch(1)
        self._add_btn.clicked.connect(self._on_add)
        self._duplicate_btn.clicked.connect(self._on_duplicate)
        self._remove_btn.clicked.connect(self._on_remove)
        table_layout.addLayout(button_row)

        self._model = SafetyRuleTableModel()
        self._model.rulesChanged.connect(self._on_rules_changed)
        self._table = QTableView(self)
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        # Size to row count (capped) so the rules table doesn't hold an
        # internal scrollbar while the section pane still has slack.
        self._table.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)
        self._table.setMinimumHeight(120)
        self._table.setMaximumHeight(500)
        header = self._table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            header.setStretchLastSection(True)
        selection_model = self._table.selectionModel()
        if selection_model is not None:
            selection_model.selectionChanged.connect(self._on_row_changed)
        table_layout.addWidget(self._table)
        splitter.addWidget(table_region)

        # Detail region.
        self._detail_container = QWidget(splitter)
        detail_layout = QVBoxLayout(self._detail_container)
        detail_layout.setContentsMargins(0, 8, 0, 0)
        self._detail_placeholder = QLabel(
            "Select a rule to edit its parameters.", self._detail_container
        )
        self._detail_placeholder.setStyleSheet("color: #888;")
        detail_layout.addWidget(self._detail_placeholder)
        self._detail_layout = detail_layout
        splitter.addWidget(self._detail_container)
        # Table region uses its sizeHint; detail soaks up the rest.
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        outer.addWidget(splitter, stretch=1)

    # -- SectionWidget API --------------------------------------------------

    def set_draft(self, draft: SetupDraft) -> None:
        self._draft = draft
        self.refresh()

    def refresh(self) -> None:
        if self._draft is None:
            return
        safety = self._draft.document.experiment_payload.get("safety")
        rules: list[dict[str, Any]] = []
        default_abort = "safe_shutdown"
        if isinstance(safety, dict):
            raw_rules = safety.get("rules", [])
            if isinstance(raw_rules, list):
                rules = [dict(r) for r in raw_rules if isinstance(r, dict)]
            default_abort = str(safety.get("default_abort", "safe_shutdown"))
        self._suppress_signals = True
        try:
            self._default_abort_edit.setText(default_abort)
            self._model.set_rules(rules)
        finally:
            self._suppress_signals = False
        self._reset_detail()

    def payload(self) -> dict[str, object]:
        return {
            "safety": {
                "rules": self._model.rules(),
                "default_abort": self._default_abort_edit.text().strip() or "safe_shutdown",
            }
        }

    # -- slots --------------------------------------------------------------

    def _on_default_abort_changed(self, _text: str) -> None:
        if self._suppress_signals:
            return
        self.valuesChanged.emit()

    def _on_rules_changed(self) -> None:
        if self._suppress_signals:
            return
        self.valuesChanged.emit()

    def _on_add(self) -> None:
        new_row = self._model.add_rule(_default_rule())
        self._table.selectRow(new_row)

    def _on_duplicate(self) -> None:
        if self._current_row < 0:
            return
        rule = self._model.rule_at(self._current_row)
        if rule is None:
            return
        rule = dict(rule)
        rule["id"] = f"{rule.get('id', 'rule')}_copy"
        new_row = self._model.add_rule(rule)
        self._table.selectRow(new_row)

    def _on_remove(self) -> None:
        if self._current_row < 0:
            return
        self._model.remove_rule(self._current_row)
        self._reset_detail()

    def _on_row_changed(self) -> None:
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if not rows:
            self._reset_detail()
            return
        row = rows[0].row()
        self._current_row = row
        rule = self._model.rule_at(row)
        if rule is None:
            self._reset_detail()
            return
        self._build_detail_form(rule)

    # -- internals ----------------------------------------------------------

    def _reset_detail(self) -> None:
        self._current_row = -1
        if self._current_form is not None:
            self._current_form.deleteLater()
            self._current_form = None
        self._detail_placeholder.show()

    def _build_detail_form(self, rule: dict[str, Any]) -> None:
        # Replace any existing form.
        if self._current_form is not None:
            self._current_form.deleteLater()
            self._current_form = None
        self._detail_placeholder.hide()
        form = build_form(SafetyRuleConfig, parent=self._detail_container)
        with contextlib.suppress(Exception):
            form.set_values(rule)
        form.valuesChanged.connect(self._on_detail_changed)
        self._detail_layout.addWidget(form)
        self._current_form = form

    def _on_detail_changed(self) -> None:
        if self._current_form is None or self._current_row < 0:
            return
        self._model.update_rule(self._current_row, self._current_form.values())


__all__ = ["SafetyRuleTableModel", "SafetySection"]
