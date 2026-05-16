"""Scalar field widgets — line edit, spin boxes, checkbox, combo, date-time,
file/dir picker, and the ``Optional[…]`` wrapper.

Each subclass owns one inner Qt widget and forwards its change signal to
:attr:`FieldWidget.valueChanged`. Container/nested widgets live in the
sibling ``_collection`` and ``_nested`` modules.
"""

from __future__ import annotations

import contextlib
import typing
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)

from capa.ui.forms.widgets._base import FieldWidget


class _LineEditField(FieldWidget):
    def __init__(self, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._edit = QLineEdit(self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._edit)
        self._edit.textChanged.connect(self.valueChanged)

    def value(self) -> str:
        return self._edit.text()

    def set_value(self, v: Any) -> None:
        with QSignalBlocker(self._edit):
            self._edit.setText("" if v is None else str(v))


class _SpinBoxField(FieldWidget):
    def __init__(
        self,
        *,
        constraints: dict[str, float],
        unit: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._spin = QSpinBox(self)
        self._spin.setRange(-(2**31), 2**31 - 1)
        if "ge" in constraints:
            self._spin.setMinimum(int(constraints["ge"]))
        if "gt" in constraints:
            self._spin.setMinimum(int(constraints["gt"]) + 1)
        if "le" in constraints:
            self._spin.setMaximum(int(constraints["le"]))
        if "lt" in constraints:
            self._spin.setMaximum(int(constraints["lt"]) - 1)
        if unit:
            self._spin.setSuffix(f" {unit}")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._spin)
        self._spin.valueChanged.connect(self.valueChanged)

    def value(self) -> int:
        return int(self._spin.value())

    def set_value(self, v: Any) -> None:
        with QSignalBlocker(self._spin):
            self._spin.setValue(int(v) if v is not None else 0)


class _DoubleSpinBoxField(FieldWidget):
    def __init__(
        self,
        *,
        constraints: dict[str, float],
        decimals: int = 3,
        unit: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._spin = QDoubleSpinBox(self)
        # ``decimals`` is picked per-field by :func:`_decimals_for_field`
        # so an acquisition rate doesn't masquerade as 2.000000 Hz. The
        # strict-inequality eps tracks the configured precision so a
        # ``gt=0`` field clamps to the smallest representable positive
        # number rather than silently rounding to the bound.
        eps = 10.0**-decimals
        self._spin.setDecimals(decimals)
        self._spin.setRange(-1e12, 1e12)
        if "ge" in constraints:
            self._spin.setMinimum(constraints["ge"])
        if "gt" in constraints:
            self._spin.setMinimum(constraints["gt"] + eps)
        if "le" in constraints:
            self._spin.setMaximum(constraints["le"])
        if "lt" in constraints:
            self._spin.setMaximum(constraints["lt"] - eps)
        if unit:
            self._spin.setSuffix(f" {unit}")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._spin)
        self._spin.valueChanged.connect(self.valueChanged)

    def value(self) -> float:
        return float(self._spin.value())

    def set_value(self, v: Any) -> None:
        with QSignalBlocker(self._spin):
            self._spin.setValue(float(v) if v is not None else 0.0)


class _CheckBoxField(FieldWidget):
    def __init__(self, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._check = QCheckBox(self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._check)
        self._check.toggled.connect(self.valueChanged)

    def value(self) -> bool:
        return self._check.isChecked()

    def set_value(self, v: Any) -> None:
        with QSignalBlocker(self._check):
            self._check.setChecked(bool(v))


class _ComboBoxField(FieldWidget):
    def __init__(self, *, choices: tuple[Any, ...], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._combo = QComboBox(self)
        self._choices = choices
        for choice in choices:
            self._combo.addItem(str(choice), userData=choice)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._combo)
        self._combo.currentIndexChanged.connect(self.valueChanged)

    def value(self) -> Any:
        idx = self._combo.currentIndex()
        return self._choices[idx] if 0 <= idx < len(self._choices) else None

    def set_value(self, v: Any) -> None:
        with QSignalBlocker(self._combo):
            for i, choice in enumerate(self._choices):
                if choice == v:
                    self._combo.setCurrentIndex(i)
                    return
            self._combo.setCurrentIndex(0)


class _DateTimeField(FieldWidget):
    def __init__(self, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._edit = QDateTimeEdit(self)
        self._edit.setCalendarPopup(True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._edit)
        self._edit.dateTimeChanged.connect(self.valueChanged)

    def value(self) -> datetime:
        return cast(datetime, self._edit.dateTime().toPython())

    def set_value(self, v: Any) -> None:
        from PySide6.QtCore import QDate, QDateTime, QTime  # noqa: PLC0415

        def _to_qdatetime(dt: datetime) -> QDateTime:
            return QDateTime(
                QDate(dt.year, dt.month, dt.day),
                QTime(dt.hour, dt.minute, dt.second, dt.microsecond // 1000),
            )

        with QSignalBlocker(self._edit):
            if isinstance(v, datetime):
                self._edit.setDateTime(_to_qdatetime(v))
            elif isinstance(v, str):
                with contextlib.suppress(ValueError):
                    self._edit.setDateTime(_to_qdatetime(datetime.fromisoformat(v)))


class _PathField(FieldWidget):
    """File or directory picker.

    ``mode`` defaults to ``"file"``; set via
    ``Field(json_schema_extra={"capa_path_mode": "dir"})`` to switch the
    browse button to a directory picker. ``StoragePolicy.bundle_root`` is
    the canonical caller of the directory mode.
    """

    def __init__(
        self,
        *,
        mode: typing.Literal["file", "dir"] = "file",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._mode = mode
        self._edit = QLineEdit(self)
        self._browse = QPushButton("Browse…", self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._edit)
        layout.addWidget(self._browse)
        self._edit.textChanged.connect(self.valueChanged)
        self._browse.clicked.connect(self._on_browse)

    def value(self) -> Path:
        return Path(self._edit.text())

    def set_value(self, v: Any) -> None:
        with QSignalBlocker(self._edit):
            self._edit.setText(str(v) if v is not None else "")

    def _on_browse(self) -> None:
        if self._mode == "dir":
            chosen = QFileDialog.getExistingDirectory(self, "Choose directory", self._edit.text())
            if chosen:
                self._edit.setText(chosen)
        else:
            chosen, _ = QFileDialog.getOpenFileName(self, "Choose file", self._edit.text())
            if chosen:
                self._edit.setText(chosen)


class _OptionalField(FieldWidget):
    """Wrap an inner :class:`FieldWidget` with a "use default" checkbox.

    Unchecked → field reports ``None`` (the form treats absence as
    "use default"); checked → field reports the inner widget's value.
    The inner widget is hidden while unchecked so operators don't
    accidentally edit a value that isn't being submitted."""

    def __init__(self, *, inner: FieldWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._inner = inner
        self._enable = QCheckBox("Set", self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._enable)
        layout.addWidget(self._inner)
        self._inner.setEnabled(False)
        self._enable.toggled.connect(self._on_toggle)
        self._enable.toggled.connect(self.valueChanged)
        self._inner.valueChanged.connect(self.valueChanged)

    def _on_toggle(self, checked: bool) -> None:
        self._inner.setEnabled(checked)

    def value(self) -> Any:
        return self._inner.value() if self._enable.isChecked() else None

    def set_value(self, v: Any) -> None:
        with QSignalBlocker(self._enable):
            self._enable.setChecked(v is not None)
        self._inner.setEnabled(v is not None)
        if v is not None:
            self._inner.set_value(v)
