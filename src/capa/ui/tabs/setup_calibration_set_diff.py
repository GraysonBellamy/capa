""":class:`CalibrationSetDiffDialog` — apply-set diff.

Operators load a calibration set (a TOML file) and the dialog shows a
diff against the channels currently in the draft. Five row kinds (see
:class:`~capa.config.calibration_set_io.DiffKind`):

* **override_identity** — channel currently Identity; set provides a
  real curve. Pre-checked.
* **override_existing** — channel already has a non-Identity curve.
  Not pre-checked — overrides destroy operator characterisation work.
* **matches** — channel already matches the set's entry; informational.
* **set_only** — set defines a curve for a channel the draft doesn't
  have. Informational (cannot be applied).
* **channel_only** — channel has no entry in the set. Informational.

The dialog is exec-modal; on accept it returns the set of channel
names the operator chose to apply. The Setup tab does the mutation.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from capa.config.calibration_set_io import CalibrationDiffEntry, DiffKind

_KIND_LABEL: dict[DiffKind, str] = {
    "override_identity": "→ apply",
    "override_existing": "⚠ overrides",
    "matches": "= matches",
    "set_only": "(no channel)",
    "channel_only": "(no set entry)",
}


class CalibrationSetDiffDialog(QDialog):
    """Diff preview before committing a calibration set onto the draft."""

    def __init__(
        self,
        *,
        set_name: str,
        revision: str,
        entries: list[CalibrationDiffEntry],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Apply calibration set — {set_name}")
        self.resize(640, 480)
        self._entries = list(entries)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        header = QLabel(
            f"<b>{set_name}</b> revision <b>{revision}</b><br>"
            "Tick the rows to apply. Pre-checked rows would override"
            " an Identity calibration (safe); existing non-Identity"
            " curves are left unchecked because applying overwrites"
            " characterisation work.",
            self,
        )
        header.setTextFormat(Qt.TextFormat.RichText)
        header.setWordWrap(True)
        outer.addWidget(header)

        self._table = QTableWidget(len(self._entries), 4, self)
        self._table.setHorizontalHeaderLabels(["Apply", "Channel", "Status", "Set → curve"])
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        h_header = self._table.horizontalHeader()
        if h_header is not None:
            h_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            h_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            h_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            h_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        outer.addWidget(self._table, stretch=1)

        for row_idx, entry in enumerate(self._entries):
            self._populate_row(row_idx, entry)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # ------------------------------------------------------------------
    # Public construction.
    # ------------------------------------------------------------------

    @classmethod
    def choose(
        cls,
        *,
        set_name: str,
        revision: str,
        entries: list[CalibrationDiffEntry],
        parent: QWidget | None,
    ) -> set[str]:
        """Run the dialog modally; return the channel names to apply."""
        dialog = cls(
            set_name=set_name,
            revision=revision,
            entries=entries,
            parent=parent,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return set()
        return dialog.selected_targets()

    def selected_targets(self) -> set[str]:
        """Tuple of selected target identifiers."""
        out: set[str] = set()
        for idx, entry in enumerate(self._entries):
            if not entry.actionable:
                continue
            item = self._table.item(idx, 0)
            if item is None:
                continue
            if item.checkState() == Qt.CheckState.Checked:
                out.add(entry.channel_name)
        return out

    # ------------------------------------------------------------------
    # Row rendering.
    # ------------------------------------------------------------------

    def _populate_row(self, row_idx: int, entry: CalibrationDiffEntry) -> None:
        apply_item = QTableWidgetItem("")
        if entry.actionable:
            apply_item.setFlags(apply_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            apply_item.setCheckState(
                Qt.CheckState.Checked if entry.recommended else Qt.CheckState.Unchecked
            )
        else:
            apply_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            apply_item.setForeground(Qt.GlobalColor.gray)
        self._table.setItem(row_idx, 0, apply_item)

        name_item = QTableWidgetItem(entry.channel_name)
        if not entry.actionable:
            name_item.setForeground(Qt.GlobalColor.gray)
        self._table.setItem(row_idx, 1, name_item)

        status_item = QTableWidgetItem(_KIND_LABEL[entry.kind])
        if not entry.actionable:
            status_item.setForeground(Qt.GlobalColor.gray)
        self._table.setItem(row_idx, 2, status_item)

        if entry.set_calibration is not None:
            set_kind = entry.set_calibration.get("kind", "?")
        else:
            set_kind = "—"
        self._table.setItem(row_idx, 3, QTableWidgetItem(str(set_kind)))


__all__ = ["CalibrationSetDiffDialog"]
