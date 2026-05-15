""":class:`CalibrationSection` — cross-channel calibration view.

Hosts three actions: apply a calibration set from file (with a
diff-before-commit modal), export the draft's current calibrations as
a new set, and clear all calibrations back to Identity. The per-channel
inline editor lives in the Channels section; this section is the
cross-cutting view.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from capa.config.calibration_set_io import (
    apply_diff_selection,
    build_set_from_channels,
    diff_set_against_channels,
    load_calibration_set,
    save_calibration_set,
)
from capa.ui.tabs.setup_calibration_set_diff import CalibrationSetDiffDialog
from capa.ui.tabs.setup_sections._base import SectionWidget

if TYPE_CHECKING:
    from capa.ui.tabs.setup_state import SetupDraft


class CalibrationSection(SectionWidget):
    """Cross-channel calibration view + set apply/export/clear."""

    channelsMutated = Signal()  # noqa: N815
    """Emitted after the section mutates the draft's channels (apply
    set / clear all). The Setup tab refreshes the Channels view and
    re-validates."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._draft: SetupDraft | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        title = QLabel("Calibration", self)
        title.setStyleSheet("font-size: 14pt; font-weight: 600;")
        outer.addWidget(title)

        instructions = QLabel(
            "Per-channel calibrations are edited inline on the Channels"
            " section. This view summarises them across the draft and"
            " offers set-level actions.",
            self,
        )
        instructions.setWordWrap(True)
        instructions.setStyleSheet("color: #555;")
        outer.addWidget(instructions)

        self._table = QTableWidget(0, 3, self)
        self._table.setHorizontalHeaderLabels(["Channel", "Kind", "Summary"])
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        h_header = self._table.horizontalHeader()
        if h_header is not None:
            h_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            h_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            h_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        outer.addWidget(self._table, stretch=1)

        actions = QHBoxLayout()
        self._apply_set_btn = QPushButton("Apply set from file…", self)
        self._apply_set_btn.clicked.connect(self._on_apply_set)
        actions.addWidget(self._apply_set_btn)
        self._export_set_btn = QPushButton("Export current as set…", self)
        self._export_set_btn.clicked.connect(self._on_export_set)
        actions.addWidget(self._export_set_btn)
        self._clear_btn = QPushButton("Clear all calibrations", self)
        self._clear_btn.clicked.connect(self._on_clear_all)
        actions.addWidget(self._clear_btn)
        actions.addStretch(1)
        outer.addLayout(actions)

    # -- SectionWidget API --------------------------------------------------

    def set_draft(self, draft: SetupDraft) -> None:
        self._draft = draft
        self.refresh()

    def refresh(self) -> None:
        self._table.setRowCount(0)
        if self._draft is None:
            return
        channels = self._channel_payloads()
        for ch in channels:
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._table.setItem(row, 0, QTableWidgetItem(str(ch.get("name", "?"))))
            self._table.setItem(row, 1, QTableWidgetItem(str(ch.get("kind", "?"))))
            cal = ch.get("calibration")
            summary = "—"
            if isinstance(cal, dict):
                kind = cal.get("kind", "?")
                if kind == "identity":
                    summary = "Identity"
                elif kind == "linear_two_point":
                    summary = (
                        f"Linear ({cal.get('ref_low_raw')}→{cal.get('ref_low_value')},"
                        f" {cal.get('ref_high_raw')}→{cal.get('ref_high_value')})"
                    )
                else:
                    summary = str(kind)
            self._table.setItem(row, 2, QTableWidgetItem(summary))

    # -- Actions ------------------------------------------------------------

    def _on_apply_set(self) -> None:
        if self._draft is None:
            return
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Apply calibration set",
            str(self._initial_calibration_dir()),
            "Calibration sets (*.toml);;All files (*)",
        )
        if not path_str:
            return
        path = Path(path_str)
        try:
            cs = load_calibration_set(path)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Apply set failed",
                f"Could not load calibration set:\n{exc}",
            )
            return
        channels = self._channel_payloads()
        entries = diff_set_against_channels(set_curves=cs.curves, channels=channels)
        selected = CalibrationSetDiffDialog.choose(
            set_name=cs.name,
            revision=cs.revision,
            entries=entries,
            parent=self,
        )
        if not selected:
            return
        changed = apply_diff_selection(channels=channels, entries=entries, selected_names=selected)
        if changed > 0:
            self.refresh()
            self.channelsMutated.emit()

    def _on_export_set(self) -> None:
        if self._draft is None:
            return
        channels = self._channel_payloads()
        if not channels:
            QMessageBox.information(
                self,
                "Export set",
                "No channels in this draft to export.",
            )
            return
        name, ok = QInputDialog.getText(
            self, "Export calibration set", "Set name (e.g. thermocouples_2026Q2):"
        )
        if not ok or not name.strip():
            return
        revision, ok = QInputDialog.getText(
            self, "Export calibration set", "Revision (e.g. 1):", text="1"
        )
        if not ok:
            return
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Save calibration set",
            str(self._initial_calibration_dir() / f"{name.strip()}.toml"),
            "Calibration sets (*.toml)",
        )
        if not path_str:
            return
        cs = build_set_from_channels(
            name=name.strip(),
            revision=revision.strip() or "1",
            channels=channels,
        )
        try:
            save_calibration_set(Path(path_str), cs)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Export failed",
                f"Could not write calibration set:\n{exc}",
            )

    def _on_clear_all(self) -> None:
        if self._draft is None:
            return
        if (
            QMessageBox.question(
                self,
                "Clear all calibrations",
                "Reset every channel's calibration to Identity? This"
                " cannot be undone except by reloading the file.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        channels = self._channel_payloads()
        changed = 0
        for ch in channels:
            cal = ch.get("calibration")
            unit = ch.get("unit", "")
            derived = ch.get("derived_unit") or unit
            ch["calibration"] = {
                "kind": "identity",
                "input_unit": str(unit) if unit else "1",
                "output_unit": str(derived) if derived else (str(unit) if unit else "1"),
            }
            if cal != ch["calibration"]:
                changed += 1
        if changed > 0:
            self.refresh()
            self.channelsMutated.emit()

    # -- Helpers ------------------------------------------------------------

    def _channel_payloads(self) -> list[dict[str, object]]:
        if self._draft is None:
            return []
        channels = self._draft.document.hardware_payload.get("channels", [])
        if not isinstance(channels, list):
            return []
        return [ch for ch in channels if isinstance(ch, dict)]

    def _initial_calibration_dir(self) -> Path:
        if self._draft is not None and self._draft.document.experiment_path is not None:
            return self._draft.document.experiment_path.parent.parent / "calibrations"
        return Path.cwd() / "configs" / "calibrations"


__all__ = ["CalibrationSection"]


# Suppress unused-import warning when running outside Qt.
from PySide6.QtWidgets import QWidget  # noqa: E402
