"""The wheel guard: scrolling past a spin box, combo box or slider leaves its
value alone, while scroll bars and other widgets still get the wheel."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDateTimeEdit,
    QDoubleSpinBox,
    QLineEdit,
    QScrollBar,
    QSlider,
    QSpinBox,
    QWidget,
)

from capa.ui.wheel_guard import WheelGuard, install_wheel_guard, is_value_widget


def _wheel_up(widget: QWidget) -> QWheelEvent:
    """One notch of the wheel rolled away from the user, over ``widget``."""
    centre = QPointF(widget.rect().center())
    return QWheelEvent(
        centre,
        QPointF(widget.mapToGlobal(widget.rect().center())),
        QPoint(0, 0),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )


@pytest.fixture
def guard(qapp: QApplication) -> Iterator[WheelGuard]:
    installed = install_wheel_guard(qapp)
    yield installed
    qapp.removeEventFilter(installed)
    installed.deleteLater()


def _spin(qtbot: Any) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(0, 100)
    spin.setValue(50)
    qtbot.addWidget(spin)
    spin.show()
    return spin


def test_wheel_changes_a_spin_box_without_the_guard(qtbot: Any) -> None:
    """The control: the wheel event used here does step a bare spin box."""
    spin = _spin(qtbot)
    QApplication.sendEvent(spin, _wheel_up(spin))
    assert spin.value() == 51


def test_guard_keeps_the_wheel_off_a_spin_box(qtbot: Any, guard: WheelGuard) -> None:
    spin = _spin(qtbot)
    event = _wheel_up(spin)
    QApplication.sendEvent(spin, event)
    assert spin.value() == 50
    # Left ignored, so the application offers it to the parent scroll area.
    assert not event.isAccepted()


def test_guard_keeps_the_wheel_off_a_combo_box(qtbot: Any, guard: WheelGuard) -> None:
    combo = QComboBox()
    combo.addItems(["lookup", "operator", "sigma_t4"])
    qtbot.addWidget(combo)
    combo.show()
    QApplication.sendEvent(combo, _wheel_up(combo))
    QApplication.sendEvent(combo, _wheel_up(combo))
    assert combo.currentIndex() == 0


@pytest.mark.parametrize(
    "factory",
    [QSpinBox, QDoubleSpinBox, QDateTimeEdit, QComboBox, lambda: QSlider(Qt.Orientation.Vertical)],
)
def test_value_widgets(qtbot: Any, factory: Any) -> None:
    widget = factory()
    qtbot.addWidget(widget)
    assert is_value_widget(widget)


@pytest.mark.parametrize("factory", [QScrollBar, QLineEdit, QWidget])
def test_other_widgets_keep_the_wheel(qtbot: Any, factory: Any) -> None:
    widget = factory()
    qtbot.addWidget(widget)
    assert not is_value_widget(widget)
