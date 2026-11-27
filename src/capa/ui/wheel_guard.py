"""Keep the mouse wheel from changing value widgets.

Scrolling a long pane with the wheel would otherwise change whichever spin
box, combo box or slider passed under the pointer. :class:`WheelGuard`,
installed on the :class:`~PySide6.QtWidgets.QApplication`, turns those wheel events away
from the value widget and lets them carry on to its parents, so the
enclosing scroll area scrolls instead. Typing, the arrow keys, the spin
buttons and the drop-down list still change the value.
"""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QEvent, QObject
from PySide6.QtWidgets import (
    QAbstractSlider,
    QAbstractSpinBox,
    QComboBox,
    QScrollBar,
)


def is_value_widget(obj: QObject) -> bool:
    """``True`` for a widget the wheel would change the value of: a spin box
    (date and time edits included), a combo box or a slider. Scroll bars
    are sliders too, but scrolling is their job."""
    if isinstance(obj, QAbstractSpinBox | QComboBox):
        return True
    return isinstance(obj, QAbstractSlider) and not isinstance(obj, QScrollBar)


class WheelGuard(QObject):
    """Application event filter that passes value widgets' wheel events on."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        """Qt event filter — see :meth:`PySide6.QtCore.QObject.eventFilter`.

        Filtering the event (``True``) keeps the value widget from seeing
        it; leaving it ignored makes the application offer it to the
        widget's parent next, up to the scroll area.
        """
        if event.type() == QEvent.Type.Wheel and is_value_widget(watched):
            event.ignore()
            return True
        return False


def install_wheel_guard(app: QCoreApplication) -> WheelGuard:
    """Install a :class:`WheelGuard` on ``app``; it lives as long as ``app``."""
    guard = WheelGuard(app)
    app.installEventFilter(guard)
    return guard


__all__ = ["WheelGuard", "install_wheel_guard", "is_value_widget"]
