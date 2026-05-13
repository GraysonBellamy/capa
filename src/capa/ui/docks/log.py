"""Log dock — append-only stream of structlog / stdlib log lines.

Mirrors what the structlog console handler writes to stdout into a Qt
panel so the operator can diagnose what's going on without watching the
terminal. Unlike :class:`~capa.ui.docks.events.EventsDock` (which shows
adapter-emitted :class:`DeviceEvent` objects — the hardware audit trail),
this dock surfaces app-internal activity: pool open/close, conductor
state transitions, config loads, dispatch failures, warnings and errors.

A :class:`logging.Handler` is installed on the root logger when the dock
is constructed and removed when it is torn down. The handler marshals
each record onto the Qt main thread via a :class:`Signal` so log calls
from worker / conductor threads land safely.
"""

from __future__ import annotations

import logging
from typing import Final

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QColor, QFont, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from capa.ui.theme import (
    COLOR_FAIL,
    COLOR_IDLE,
    COLOR_OK,
    COLOR_WARN,
    monospace_font,
)

_LEVEL_COLOR: Final[dict[int, QColor]] = {
    logging.DEBUG: COLOR_IDLE,
    logging.INFO: COLOR_OK,
    logging.WARNING: COLOR_WARN,
    logging.ERROR: COLOR_FAIL,
    logging.CRITICAL: COLOR_FAIL,
}

_MAX_ROWS: Final[int] = 5000
"""Cap dock size at 5000 rows; older entries scroll off the top. The
bundle's ``run.log`` remains the source of truth for the full log."""

_LEVEL_CHOICES: Final[tuple[tuple[str, int], ...]] = (
    ("DEBUG", logging.DEBUG),
    ("INFO", logging.INFO),
    ("WARNING", logging.WARNING),
    ("ERROR", logging.ERROR),
)


class _QtLogBridge(QObject):
    """Thread-safe relay from a :class:`logging.Handler` to the dock.

    The handler's :meth:`emit` runs on whatever thread produced the log
    record (worker pool threads, the conductor thread, the asyncio
    qasync loop). Emitting a :class:`Signal` with a queued connection
    marshals each formatted line onto the Qt main thread, where the
    dock's slot is free to touch the :class:`QStandardItemModel`.
    """

    line = Signal(str, int)


class _QtLogHandler(logging.Handler):
    """``logging.Handler`` that forwards rendered records through a
    :class:`_QtLogBridge`. One per :class:`LogDock`."""

    def __init__(self, bridge: _QtLogBridge, level: int) -> None:
        super().__init__(level=level)
        self._bridge = bridge

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:
            self.handleError(record)
            return
        try:
            self._bridge.line.emit(text, record.levelno)
        except RuntimeError:
            # Bridge QObject already destroyed (shutdown race). Drop.
            return


class _PlainFormatter(logging.Formatter):
    """Compact formatter for the dock.

    structlog's :class:`ProcessorFormatter` already rendered the event
    dict into ``record.msg`` (via :class:`ConsoleRenderer`), so we just
    prepend a wall-clock timestamp + level tag and let the message
    speak for itself. ConsoleRenderer ANSI escapes are stripped so they
    don't leak into the Qt view.
    """

    _ANSI_RE = None  # lazy

    def format(self, record: logging.LogRecord) -> str:
        import re

        if _PlainFormatter._ANSI_RE is None:
            _PlainFormatter._ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
        ts = self.formatTime(record, datefmt="%H:%M:%S")
        ms = int(record.msecs)
        message = record.getMessage()
        message = _PlainFormatter._ANSI_RE.sub("", message)
        level = record.levelname.ljust(5)[:5]
        return f"{ts}.{ms:03d}  [{level}] {message}"


class LogDock(QDockWidget):
    """Append-only log stream from the root logger."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Log", parent)
        self.setObjectName("dock_log")
        self.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea | Qt.DockWidgetArea.TopDockWidgetArea
        )

        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        # Header row: level dropdown + clear button. Cheap controls so
        # the operator can flip to DEBUG when chasing a bug without
        # leaving the GUI.
        header = QHBoxLayout()
        header.setContentsMargins(2, 0, 2, 0)
        header.addWidget(QLabel("Level:", container))
        self._level_combo = QComboBox(container)
        for label, _ in _LEVEL_CHOICES:
            self._level_combo.addItem(label)
        self._level_combo.setCurrentText("INFO")
        self._level_combo.currentTextChanged.connect(self._on_level_changed)
        header.addWidget(self._level_combo)
        header.addStretch(1)
        clear_btn = QPushButton("Clear", container)
        clear_btn.clicked.connect(self.clear)
        header.addWidget(clear_btn)
        layout.addLayout(header)

        self._model = QStandardItemModel(container)
        self._view = QListView(container)
        self._view.setModel(self._model)
        self._view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self._view.setUniformItemSizes(True)
        self._view.setFont(monospace_font(point_size=9))
        layout.addWidget(self._view, 1)

        self.setWidget(container)

        # Bridge + handler. Queued connection so emits from non-Qt
        # threads marshal onto the main thread.
        self._bridge = _QtLogBridge(self)
        self._bridge.line.connect(self._append, Qt.ConnectionType.QueuedConnection)
        self._handler = _QtLogHandler(self._bridge, level=logging.INFO)
        self._handler.setFormatter(_PlainFormatter())
        self._handler._capa_owned = True  # type: ignore[attr-defined]
        logging.getLogger().addHandler(self._handler)

    # ------------------------------------------------------------------ slots

    @Slot(str, int)
    def _append(self, text: str, levelno: int) -> None:
        item = QStandardItem(text)
        color = _LEVEL_COLOR.get(levelno, COLOR_OK)
        item.setForeground(color)
        if levelno >= logging.ERROR:
            f = QFont(self._view.font())
            f.setBold(True)
            item.setFont(f)
        self._model.appendRow(item)

        excess = self._model.rowCount() - _MAX_ROWS
        if excess > 0:
            self._model.removeRows(0, excess)

        bar = self._view.verticalScrollBar()
        if bar is not None and bar.value() >= bar.maximum() - 4:
            self._view.scrollToBottom()

    def _on_level_changed(self, label: str) -> None:
        level = dict(_LEVEL_CHOICES).get(label, logging.INFO)
        self._handler.setLevel(level)
        # Root level must be permissive enough to deliver the chosen
        # level to our handler — other capa handlers manage their own
        # thresholds (file handler at DEBUG, console at INFO), so
        # lowering the root only widens what reaches our filter.
        root = logging.getLogger()
        if root.level == logging.NOTSET or root.level > level:
            root.setLevel(min(root.level if root.level else level, level))

    def clear(self) -> None:
        self._model.clear()

    # ------------------------------------------------------------------ teardown

    def closeEvent(self, event: object) -> None:  # type: ignore[override]
        self._detach_handler()
        super().closeEvent(event)  # type: ignore[arg-type]

    def deleteLater(self) -> None:  # type: ignore[override]
        self._detach_handler()
        super().deleteLater()

    def _detach_handler(self) -> None:
        handler = self._handler
        if handler is None:
            return
        try:
            logging.getLogger().removeHandler(handler)
            handler.close()
        except Exception:
            pass
        self._handler = None  # type: ignore[assignment]


__all__ = ["LogDock"]
