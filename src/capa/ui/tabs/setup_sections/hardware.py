"""Hardware glance view — read-only three-table summary.

When the operator selects the **Hardware** parent in the
outline, this pane stacks compact Devices / Channels / Cameras tables
in a vertical splitter so the whole rig is visible at a glance. Each
sub-section has an "Edit…" button that jumps to its dedicated outline
entry; the actual editing always happens in the child sections, never
here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from capa.ui.tabs.setup_sections._base import SectionWidget
from capa.ui.tabs.setup_sections._models import horizontal_header

if TYPE_CHECKING:
    from capa.ui.tabs.setup_state import SetupDraft


class _SummaryTableModel(QAbstractTableModel):
    """Tiny read-only table model used by every sub-section.

    Each row is a flat dict keyed by the columns in ``HEADERS``. The
    glance view rebuilds it from scratch on every :meth:`refresh` — no
    incremental updates, no Qt model invariants to maintain.
    """

    def __init__(self, headers: tuple[str, ...]) -> None:
        super().__init__()
        self._headers = headers
        self._rows: list[dict[str, str]] = []

    def set_rows(self, rows: list[dict[str, str]]) -> None:
        """Replace the table's rows."""
        self.beginResetModel()
        self._rows = [dict(r) for r in rows]
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        """Number of rows in the model. See :class:`PySide6.QtCore.QAbstractTableModel`."""
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        """Number of columns in the model. See :class:`PySide6.QtCore.QAbstractTableModel`."""
        if parent.isValid():
            return 0
        return len(self._headers)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        """Header label for the given section / orientation. See :class:`PySide6.QtCore.QAbstractItemModel`."""
        return horizontal_header(self._headers, section, orientation, role)

    def data(  # type: ignore[override]
        self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        """Return the value at ``index`` for ``role``. See :class:`PySide6.QtCore.QAbstractItemModel`."""
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        row = self._rows[index.row()] if 0 <= index.row() < len(self._rows) else None
        if row is None:
            return None
        return row.get(self._headers[index.column()], "")


def _summary_table(
    parent: QWidget, headers: tuple[str, ...]
) -> tuple[QTableView, _SummaryTableModel]:
    view = QTableView(parent)
    model = _SummaryTableModel(headers)
    view.setModel(model)
    view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QTableView.SelectionMode.NoSelection)
    view.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
    header = view.horizontalHeader()
    if header is not None:
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
    vheader = view.verticalHeader()
    if vheader is not None:
        vheader.hide()
    return view, model


class HardwareGlanceSection(SectionWidget):
    """Read-only stacked-tables summary of the hardware payload."""

    editSectionRequested = Signal(str)  # noqa: N815 — Qt signal naming convention
    """Operator clicked an "Edit…" button. Argument is the target outline
    section id (``"devices"``, ``"channels"``, ``"cameras"``)."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._draft: SetupDraft | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        title = QLabel("Hardware", self)
        title.setStyleSheet("font-size: 14pt; font-weight: 600;")
        outer.addWidget(title)

        splitter = QSplitter(Qt.Orientation.Vertical, self)

        # Devices.
        self._devices_view, self._devices_model = _summary_table(splitter, ("name", "adapter"))
        splitter.addWidget(
            _grouped("Devices", self._devices_view, "devices", self._on_edit_requested)
        )

        # Channels.
        self._channels_view, self._channels_model = _summary_table(
            splitter, ("name", "kind", "device", "unit")
        )
        splitter.addWidget(
            _grouped("Channels", self._channels_view, "channels", self._on_edit_requested)
        )

        # Cameras.
        self._cameras_view, self._cameras_model = _summary_table(
            splitter, ("name", "kind", "adapter")
        )
        splitter.addWidget(
            _grouped("Cameras", self._cameras_view, "cameras", self._on_edit_requested)
        )

        splitter.setSizes([200, 240, 140])
        outer.addWidget(splitter, stretch=1)

        # Aggregate summary line at the bottom.
        self._summary = QLabel("—", self)
        self._summary.setStyleSheet("color: #555;")
        outer.addWidget(self._summary)

    # -- SectionWidget API --------------------------------------------------

    def set_draft(self, draft: SetupDraft) -> None:
        """Replace the in-progress draft."""
        self._draft = draft
        self.refresh()

    def refresh(self) -> None:
        """Recompute the form from the current draft."""
        if self._draft is None:
            self._devices_model.set_rows([])
            self._channels_model.set_rows([])
            self._cameras_model.set_rows([])
            self._summary.setText("—")
            return
        hw = self._draft.document.hardware_payload
        devices = hw.get("devices") if isinstance(hw, dict) else None
        channels = hw.get("channels") if isinstance(hw, dict) else None
        cameras = hw.get("cameras") if isinstance(hw, dict) else None

        device_rows: list[dict[str, str]] = []
        if isinstance(devices, list):
            for entry in devices:
                if not isinstance(entry, dict):
                    continue
                device_rows.append(
                    {
                        "name": str(entry.get("name", "")),
                        "adapter": str(entry.get("adapter", "")),
                    }
                )
        self._devices_model.set_rows(device_rows)

        channel_rows: list[dict[str, str]] = []
        if isinstance(channels, list):
            for entry in channels:
                if not isinstance(entry, dict):
                    continue
                source = entry.get("source") or {}
                channel_rows.append(
                    {
                        "name": str(entry.get("name", "")),
                        "kind": str(entry.get("kind", "")),
                        "device": str(source.get("device", "")) if isinstance(source, dict) else "",
                        "unit": str(entry.get("unit", "")),
                    }
                )
        self._channels_model.set_rows(channel_rows)

        camera_rows: list[dict[str, str]] = []
        if isinstance(cameras, list):
            for entry in cameras:
                if not isinstance(entry, dict):
                    continue
                camera_rows.append(
                    {
                        "name": str(entry.get("name", "")),
                        "kind": str(entry.get("kind", "")),
                        "adapter": str(entry.get("adapter", "")),
                    }
                )
        self._cameras_model.set_rows(camera_rows)

        self._summary.setText(
            f"{len(device_rows)} device(s) · {len(channel_rows)} channel(s)"
            f" · {len(camera_rows)} camera(s)"
        )

    # -- slots --------------------------------------------------------------

    def _on_edit_requested(self, target_section: str) -> None:
        self.editSectionRequested.emit(target_section)


def _grouped(
    title: str,
    view: QTableView,
    target_section: str,
    edit_slot: Callable[[str], None],
) -> QFrame:
    frame = QFrame()
    frame.setFrameShape(QFrame.Shape.StyledPanel)
    box = QVBoxLayout(frame)
    box.setContentsMargins(8, 8, 8, 8)
    box.setSpacing(4)
    header_row = QHBoxLayout()
    header = QLabel(title, frame)
    header.setStyleSheet("font-weight: 600;")
    header_row.addWidget(header)
    header_row.addStretch(1)
    edit = QPushButton("Edit…", frame)
    edit.clicked.connect(lambda: edit_slot(target_section))
    header_row.addWidget(edit)
    box.addLayout(header_row)
    box.addWidget(view)
    return frame


__all__ = ["HardwareGlanceSection"]
