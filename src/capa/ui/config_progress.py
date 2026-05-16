"""Hardware-initialization progress dialog for config loads."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent, QColor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from capa.runtime.progress import DeviceInitProgress, DeviceInitStatus
from capa.ui.theme import COLOR_FAIL, COLOR_IDLE, COLOR_OK, COLOR_RUNNING, COLOR_WARN


class ConfigLoadState(StrEnum):
    """UI-facing state for config load and hardware preparation."""

    IDLE = "idle"
    BUILDING_POOL = "building_pool"
    CLOSING_PREVIOUS = "closing_previous"
    OPENING_DEVICES = "opening_devices"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ConfigLoadProgress:
    """Aggregated progress snapshot consumed by the dialog."""

    state: ConfigLoadState
    message: str
    path: Path | None = None
    devices: tuple[DeviceInitProgress, ...] = ()

    @property
    def total(self) -> int:
        return len(self.devices)

    @property
    def completed(self) -> int:
        return sum(
            1
            for row in self.devices
            if row.status in (DeviceInitStatus.READY, DeviceInitStatus.ROLLED_BACK)
        )


_STATE_TEXT: Final[dict[ConfigLoadState, str]] = {
    ConfigLoadState.IDLE: "Idle",
    ConfigLoadState.BUILDING_POOL: "Building worker pool",
    ConfigLoadState.CLOSING_PREVIOUS: "Closing previous config",
    ConfigLoadState.OPENING_DEVICES: "Opening devices",
    ConfigLoadState.READY: "Hardware ready",
    ConfigLoadState.FAILED: "Hardware failed",
}

_STATUS_COLOR: Final[dict[DeviceInitStatus, QColor]] = {
    DeviceInitStatus.PENDING: COLOR_IDLE,
    DeviceInitStatus.OPENING: COLOR_RUNNING,
    DeviceInitStatus.READY: COLOR_OK,
    DeviceInitStatus.FAILED: COLOR_FAIL,
    DeviceInitStatus.ROLLED_BACK: COLOR_WARN,
}


class HardwareInitDialog(QDialog):
    """Modal, non-cancellable progress surface for hardware initialization."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preparing hardware")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumSize(720, 420)
        self._terminal = False
        self._auto_close_scheduled = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        self._title = QLabel("Preparing hardware", self)
        title_font = self._title.font()
        title_font.setPointSize(title_font.pointSize() + 3)
        title_font.setBold(True)
        self._title.setFont(title_font)
        layout.addWidget(self._title)

        self._message = QLabel(
            "Loading config and connecting devices. Controls unlock when hardware is ready.",
            self,
        )
        self._message.setWordWrap(True)
        layout.addWidget(self._message)

        self._state_label = QLabel("Building worker pool", self)
        self._state_label.setStyleSheet(f"color: {COLOR_IDLE.name()};")
        layout.addWidget(self._state_label)

        self._progress = QProgressBar(self)
        self._progress.setTextVisible(True)
        self._progress.setRange(0, 0)
        layout.addWidget(self._progress)

        self._table = QTableWidget(0, 5, self)
        self._table.setHorizontalHeaderLabels(["Status", "Name", "Adapter", "Resource", "Detail"])
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table.setAlternatingRowColors(True)
        header = self._table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._table, 1)

        self._details = QPlainTextEdit(self)
        self._details.setReadOnly(True)
        self._details.setVisible(False)
        self._details.setMaximumHeight(96)
        layout.addWidget(self._details)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        self._close_btn = buttons.button(QDialogButtonBox.StandardButton.Close)
        assert self._close_btn is not None
        self._close_btn.setEnabled(False)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def update_progress(self, progress: ConfigLoadProgress) -> None:
        """Refresh the dialog from one aggregated progress snapshot."""
        self._terminal = progress.state in (ConfigLoadState.READY, ConfigLoadState.FAILED)
        self._close_btn.setEnabled(self._terminal)

        config_name = progress.path.name if progress.path is not None else "config"
        self._message.setText(
            f"Loading {config_name} and connecting devices. Controls unlock when hardware is ready."
        )

        state_text = _STATE_TEXT.get(progress.state, progress.state.value)
        self._state_label.setText(f"{state_text}: {progress.message}")
        if progress.state is ConfigLoadState.FAILED:
            self._state_label.setStyleSheet(f"color: {COLOR_FAIL.name()}; font-weight: bold;")
        elif progress.state is ConfigLoadState.READY:
            self._state_label.setStyleSheet(f"color: {COLOR_OK.name()}; font-weight: bold;")
        elif progress.state is ConfigLoadState.OPENING_DEVICES:
            self._state_label.setStyleSheet(f"color: {COLOR_RUNNING.name()};")
        else:
            self._state_label.setStyleSheet(f"color: {COLOR_IDLE.name()};")

        if progress.total:
            self._progress.setRange(0, progress.total)
            self._progress.setValue(progress.completed)
            self._progress.setFormat(f"{progress.completed} of {progress.total} initialized")
        else:
            self._progress.setRange(0, 0)
            self._progress.setFormat("")

        self._populate_table(progress.devices)
        self._update_failure_details(progress.devices)

        if progress.state is ConfigLoadState.READY and not self._auto_close_scheduled:
            self._auto_close_scheduled = True
            QTimer.singleShot(650, self.accept)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        if self._terminal:
            super().closeEvent(event)
        else:
            event.ignore()

    def reject(self) -> None:
        if self._terminal:
            super().reject()

    def _populate_table(self, rows: tuple[DeviceInitProgress, ...]) -> None:
        self._table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            color = _STATUS_COLOR.get(row.status, COLOR_IDLE)
            values = [
                row.status.value.replace("_", " "),
                row.name,
                row.adapter,
                row.resource_id,
                row.detail,
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setForeground(color)
                self._table.setItem(index, col, item)

    def _update_failure_details(self, rows: tuple[DeviceInitProgress, ...]) -> None:
        failed = [row for row in rows if row.status is DeviceInitStatus.FAILED]
        if not failed:
            self._details.clear()
            self._details.setVisible(False)
            return
        lines = []
        for row in failed:
            prefix = f"{row.name} ({row.resource_id})"
            err = f"{row.error_type}: " if row.error_type else ""
            lines.append(f"{prefix}: {err}{row.detail}")
        self._details.setPlainText("\n".join(lines))
        self._details.setVisible(True)


__all__ = [
    "ConfigLoadProgress",
    "ConfigLoadState",
    "HardwareInitDialog",
]
