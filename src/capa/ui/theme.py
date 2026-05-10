"""Minimal palette and font helpers. Plan §10 — all UI theming centralizes
here so a future Simple/Expert mode preset (P6) can override one place."""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QFontDatabase

# Status colors used across the run header, status bar pills, and event
# severity rendering.
COLOR_OK = QColor(54, 159, 95)
COLOR_WARN = QColor(214, 158, 46)
COLOR_FAIL = QColor(208, 64, 64)
COLOR_IDLE = QColor(140, 140, 140)
COLOR_RUNNING = QColor(54, 130, 220)


def monospace_font(*, point_size: int = 10) -> QFont:
    """Numeric readouts use a fixed-pitch font for stable column widths."""
    family = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()
    f = QFont(family)
    f.setPointSize(point_size)
    return f


def numeric_display_font() -> QFont:
    """Larger fixed-pitch font for the numerics dock."""
    f = monospace_font(point_size=18)
    f.setBold(True)
    return f


__all__ = [
    "COLOR_FAIL",
    "COLOR_IDLE",
    "COLOR_OK",
    "COLOR_RUNNING",
    "COLOR_WARN",
    "monospace_font",
    "numeric_display_font",
]
