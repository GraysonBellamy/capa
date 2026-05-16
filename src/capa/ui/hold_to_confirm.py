""":class:`HoldToConfirmButton` — destructive action with a 1-second arm.

Click is not enough for emergency-but-clickable controls: an accidental
brush against the button shouldn't kill a 30-minute calorimeter run.
The operator must press and hold for :data:`HOLD_DURATION_MS` while a
progress fill animates inside the button. Releasing early cancels.

This widget is intentionally tiny: no theme integration beyond a single
color argument, no per-platform behavior. The Run tab is the only
consumer today.
"""

from __future__ import annotations

from typing import Final

from PySide6.QtCore import QElapsedTimer, QEvent, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import QPushButton, QWidget

HOLD_DURATION_MS: Final[int] = 1000
"""Time the operator must keep the button pressed before ``confirmed``
fires. One second is long enough that an accidental click can't trigger
an emergency stop, short enough that a deliberate operator response
isn't frustrated."""

_TICK_INTERVAL_MS: Final[int] = 32  # ~30 FPS progress repaint


class HoldToConfirmButton(QPushButton):
    """Push button that emits :attr:`confirmed` after a 1-second press.

    Releasing the mouse / leaving the widget before the timer elapses
    cancels the in-progress hold and re-arms for the next press.
    """

    confirmed = Signal()

    def __init__(
        self,
        text: str,
        *,
        accent: QColor,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(text, parent)
        self._accent = QColor(accent)
        self._held: bool = False
        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_INTERVAL_MS)
        self._timer.timeout.connect(self._on_tick)
        self._elapsed = QElapsedTimer()

        # Visual: the button takes its accent from the destructive
        # color. Clicked() is suppressed — we drive the confirmation
        # signal from the timer.
        self.setStyleSheet(
            f"QPushButton {{ background: {accent.name()}; color: white; "
            f"font-weight: 600; padding: 6px 12px; border-radius: 4px; }}"
            f"QPushButton:disabled {{ background: #d0d5dd; color: #98a2b3; }}"
        )

    # -- mouse handling -----------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if not self.isEnabled():
            return
        self._held = True
        self._elapsed.start()
        self._timer.start()
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._held:
            self._cancel()

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802
        if self._held:
            self._cancel()

    # -- internal -----------------------------------------------------------

    def _on_tick(self) -> None:
        if not self._held:
            self._timer.stop()
            return
        if self._elapsed.elapsed() >= HOLD_DURATION_MS:
            self._held = False
            self._timer.stop()
            self.update()
            self.confirmed.emit()
            return
        self.update()

    def _cancel(self) -> None:
        self._held = False
        self._timer.stop()
        self.update()

    def progress(self) -> float:
        """Current hold fraction in ``[0, 1]``. Exposed for tests."""
        if not self._held:
            return 0.0
        return min(1.0, self._elapsed.elapsed() / HOLD_DURATION_MS)

    # -- paint --------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        super().paintEvent(event)
        if not self._held:
            return
        fraction = self.progress()
        if fraction <= 0:
            return
        painter = QPainter(self)
        painter.setOpacity(0.35)
        fill_width = int(self.width() * fraction)
        painter.fillRect(QRect(0, 0, fill_width, self.height()), Qt.GlobalColor.white)
        painter.end()


__all__ = ["HOLD_DURATION_MS", "HoldToConfirmButton"]
