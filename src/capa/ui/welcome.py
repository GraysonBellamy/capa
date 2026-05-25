""":class:`WelcomeHero` — first-launch landing surface.

Shown by :class:`MainWindow` in place of the empty Setup tab while no
config is loaded. Three primary action cards (New / Open / Try
simulator) plus a list of recently-opened configs.

The hero replaces only the central body. The menu bar, status bar, and
docks remain visible so the operator can find Help → Quick Start and
discover the docking surface.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import structlog
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from capa.ui.recents import RecentEntry, load_recents
from capa.ui.theme import LINK_TEXT_QSS, MUTED_TEXT_QSS

_logger = structlog.get_logger("capa.ui.welcome")

SIMULATOR_CONFIG: Final[Path] = Path("configs/experiments/sim_freerun.yaml")
"""Bundled simulator config. Resolved relative to the repo root by
:class:`MainWindow` before the path is opened."""


class _ActionCard(QFrame):
    """Large clickable card with an icon glyph + title + subtitle."""

    clicked = Signal()

    def __init__(
        self,
        *,
        glyph: str,
        title: str,
        subtitle: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumSize(220, 160)
        # Palette-driven so cards adapt to dark/light themes. `mid` and
        # `highlight` give a visible-but-quiet border with a clear hover
        # accent; `alternate-base` is the standard "subtle row tint" role
        # and contrasts cleanly with `text` in both modes.
        self.setStyleSheet(
            "_ActionCard { border: 1px solid palette(mid); border-radius: 8px; padding: 16px; }"
            "_ActionCard:hover { border-color: palette(highlight); background: palette(alternate-base); }"
        )
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        glyph_label = QLabel(glyph, self)
        glyph_label.setStyleSheet("font-size: 28pt; font-weight: 600;")
        layout.addWidget(glyph_label)

        title_label = QLabel(title, self)
        title_label.setStyleSheet("font-size: 14pt; font-weight: 600;")
        layout.addWidget(title_label)

        subtitle_label = QLabel(subtitle, self)
        subtitle_label.setWordWrap(True)
        subtitle_label.setStyleSheet(MUTED_TEXT_QSS)
        layout.addWidget(subtitle_label)
        layout.addStretch(1)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Qt event handler — see :class:`PySide6.QtWidgets.QWidget`."""
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class WelcomeHero(QWidget):
    """First-launch landing widget.

    Emits one of :attr:`newRequested`, :attr:`openRequested`,
    :attr:`simulatorRequested`, or :attr:`recentRequested` when the
    operator picks an action. :class:`MainWindow` consumes these to
    drive the same code paths File → Open and File → New use.
    """

    newRequested = Signal()  # noqa: N815
    openRequested = Signal()  # noqa: N815
    simulatorRequested = Signal()  # noqa: N815
    recentRequested = Signal(object)  # noqa: N815 — argument is Path

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(48, 48, 48, 48)
        outer.setSpacing(24)
        outer.setAlignment(Qt.AlignmentFlag.AlignTop)

        title = QLabel("Welcome to capa", self)
        title.setStyleSheet("font-size: 24pt; font-weight: 600;")
        outer.addWidget(title)
        subtitle = QLabel("Controlled-Atmosphere Cone Calorimeter", self)
        subtitle.setStyleSheet(f"font-size: 13pt; {MUTED_TEXT_QSS}")
        outer.addWidget(subtitle)

        cards_row = QHBoxLayout()
        cards_row.setSpacing(16)

        new_card = _ActionCard(
            glyph="+",
            title="New setup",
            subtitle="Start from a template — pick a procedure and capa builds the scaffold.",
            parent=self,
        )
        new_card.clicked.connect(self.newRequested)
        cards_row.addWidget(new_card)

        open_card = _ActionCard(
            glyph="📂",
            title="Open config",
            subtitle="Load an existing .yaml or .toml experiment config from disk.",
            parent=self,
        )
        open_card.clicked.connect(self.openRequested)
        cards_row.addWidget(open_card)

        sim_card = _ActionCard(
            glyph="🧪",
            title="Try a simulator",
            subtitle="No hardware required — runs against built-in device simulators.",
            parent=self,
        )
        sim_card.clicked.connect(self.simulatorRequested)
        cards_row.addWidget(sim_card)

        cards_row.addStretch(1)
        outer.addLayout(cards_row)

        # Recents.
        self._recents_container = QWidget(self)
        recents_layout = QVBoxLayout(self._recents_container)
        recents_layout.setContentsMargins(0, 0, 0, 0)
        recents_layout.setSpacing(4)
        recents_header = QLabel("Recent", self._recents_container)
        recents_header.setStyleSheet("font-size: 11pt; font-weight: 600;")
        recents_layout.addWidget(recents_header)
        self._recents_body = QVBoxLayout()
        self._recents_body.setSpacing(2)
        recents_layout.addLayout(self._recents_body)
        outer.addWidget(self._recents_container)

        # Footer hint.
        hint = QLabel(
            "New here? Open the Quick Start guide via <b>Help → Quick Start</b>.",
            self,
        )
        hint.setStyleSheet(MUTED_TEXT_QSS)
        hint.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        outer.addStretch(1)
        outer.addWidget(hint)

        self.refresh_recents()

    def refresh_recents(self) -> None:
        """Re-read ``~/.capa/recents.json`` and rebuild the row.

        Called on construction and whenever the operator returns to the
        welcome screen (e.g. after closing a config). Keeps the list in
        sync with what other capa sessions have written.
        """
        # Clear existing rows.
        while self._recents_body.count():
            item = self._recents_body.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()

        entries = load_recents()
        if not entries:
            empty = QLabel("(no recent configs)", self._recents_container)
            empty.setStyleSheet(MUTED_TEXT_QSS)
            self._recents_body.addWidget(empty)
            self._recents_container.setVisible(True)
            return

        for entry in entries[:5]:
            self._recents_body.addWidget(_RecentRow(entry, self._on_recent_clicked, self))
        self._recents_container.setVisible(True)

    def _on_recent_clicked(self, path: Path) -> None:
        self.recentRequested.emit(path)


class _RecentRow(QWidget):
    """One row in the recents list — clickable file path + relative time."""

    def __init__(
        self,
        entry: RecentEntry,
        on_click: Callable[[Path], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._entry = entry
        self._on_click = on_click
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        path_label = QLabel(str(entry.path), self)
        path_label.setStyleSheet(LINK_TEXT_QSS)
        layout.addWidget(path_label)

        time_label = QLabel(_format_relative(entry.opened_at), self)
        time_label.setStyleSheet(MUTED_TEXT_QSS)
        layout.addWidget(time_label)
        layout.addStretch(1)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Qt event handler — see :class:`PySide6.QtWidgets.QWidget`."""
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_click(self._entry.path)
        super().mousePressEvent(event)


def _format_relative(ts: datetime) -> str:
    """``"2 hours ago"``-style relative time. Tolerant of naive datetimes."""
    now = datetime.now(UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    delta = now - ts
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} hour(s) ago"
    return f"{seconds // 86400} day(s) ago"


__all__ = ["SIMULATOR_CONFIG", "WelcomeHero"]
