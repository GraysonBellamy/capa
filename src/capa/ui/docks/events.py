"""Events dock — append-only log of :class:`DeviceEvent` lines.

Subscribes (via :class:`RunController.event_received`) to adapter
events, segment transitions, and operator notes. Currently displays
only the ``DeviceEvent`` stream — Notes / segment markers come later.
Auto-scrolls to the bottom unless the operator has scrolled away.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QDockWidget,
    QListView,
    QWidget,
)

from capa.devices.records import DeviceEvent
from capa.ui.theme import (
    COLOR_FAIL,
    COLOR_OK,
    COLOR_WARN,
    monospace_font,
)

_SEVERITY_COLOR = {
    "info": COLOR_OK,
    "warning": COLOR_WARN,
    "error": COLOR_FAIL,
}

_MAX_ROWS = 5000
"""Cap dock size at 5000 rows; older entries scroll off the top. The bundle's
events.sqlite remains the source of truth for the full event log."""


class EventsDock(QDockWidget):
    """Append-only event list. Each row is one
    :class:`~capa.devices.records.DeviceEvent`."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Events", parent)
        self.setObjectName("dock_events")
        self.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea | Qt.DockWidgetArea.TopDockWidgetArea
        )

        self._model = QStandardItemModel(self)
        self._view = QListView(self)
        self._view.setModel(self._model)
        self._view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self._view.setUniformItemSizes(True)
        self._view.setFont(monospace_font(point_size=10))
        self.setWidget(self._view)

    # ------------------------------------------------------------------ slots

    def append_event(self, event: DeviceEvent) -> None:
        """Append one row. Connected to ``RunController.event_received``."""
        # Shows engine-relative seconds since run start, matching the plot axis.
        # Could be derived from the run clock for a wall-time display instead.
        text = (
            f"{event.t_mono_ns / 1e9:8.3f}s  "
            f"[{event.severity:7}] "
            f"{event.adapter}.{event.device} {event.kind}: {event.message}"
        )
        item = QStandardItem(text)
        color = _SEVERITY_COLOR.get(event.severity, COLOR_OK)
        item.setForeground(color)
        if event.severity == "error":
            f = QFont(self._view.font())
            f.setBold(True)
            item.setFont(f)
        self._model.appendRow(item)

        # Trim from the top once we exceed the row cap so the model size stays
        # bounded over a long run.
        excess = self._model.rowCount() - _MAX_ROWS
        if excess > 0:
            self._model.removeRows(0, excess)

        # Auto-scroll only when the user is already near the bottom — leave
        # them alone if they've scrolled up to inspect history.
        bar = self._view.verticalScrollBar()
        if bar is not None and bar.value() >= bar.maximum() - 4:
            self._view.scrollToBottom()

    def append_run_marker(self, text: str, *, color: QColor | None = None) -> None:
        """Append a synthetic separator row (run start/end, abort)."""
        item = QStandardItem(f"---- {text} ----")
        if color is not None:
            item.setForeground(color)
        f = QFont(self._view.font())
        f.setItalic(True)
        item.setFont(f)
        self._model.appendRow(item)
        self._view.scrollToBottom()

    def clear(self) -> None:
        """Clear the widget's contents in place."""
        self._model.clear()


__all__ = ["EventsDock"]
