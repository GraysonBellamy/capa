"""Collection / sequence field widgets.

``tuple[str, ...]`` / ``list[str]``, ``dict[str, float]``, fixed-shape
``tuple[Model, ...]``, and the JSON-fallback widget for annotations the
factory does not recognize.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from capa.ui.forms.widgets._base import FieldWidget

if TYPE_CHECKING:
    from capa.ui.forms.from_model import ModelForm


class _StrTupleField(FieldWidget):
    """``tuple[str, ...]`` / ``list[str]`` editor — one row per item.

    MVP: a :class:`QListWidget` with Add/Remove buttons. Items are
    edited by double-click (default ``QListWidget`` behavior). Suitable
    for AcquireStep.channels and similar small lists; not optimized for
    long sequences."""

    def __init__(self, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._list = QListWidget(self)
        self._add = QPushButton("Add", self)
        self._remove = QPushButton("Remove", self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._list)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(self._add)
        buttons.addWidget(self._remove)
        outer.addLayout(buttons)
        self._add.clicked.connect(self._on_add)
        self._remove.clicked.connect(self._on_remove)
        self._list.itemChanged.connect(self.valueChanged)

    def _on_add(self) -> None:
        item = QListWidgetItem("")
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self._list.addItem(item)
        self._list.editItem(item)
        self.valueChanged.emit()

    def _on_remove(self) -> None:
        for item in self._list.selectedItems():
            self._list.takeItem(self._list.row(item))
        self.valueChanged.emit()

    def value(self) -> tuple[str, ...]:
        items = (self._list.item(i) for i in range(self._list.count()))
        return tuple(item.text() for item in items if item is not None)

    def set_value(self, v: Any) -> None:
        with QSignalBlocker(self._list):
            self._list.clear()
            for entry in v or ():
                item = QListWidgetItem(str(entry))
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                self._list.addItem(item)


class _DictStrFloatField(FieldWidget):
    """``dict[str, float]`` editor — supports ``SafeShutdownStep.cool_target``.

    Each row is a key/value pair; Add appends an empty pair, Remove
    drops selected. Keys edited inline; values via a small spinbox.
    Implementation note: keep this simple — the 10% case for
    SafeShutdownStep doesn't justify a fancy table model."""

    def __init__(self, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[tuple[QLineEdit, QDoubleSpinBox]] = []
        self._rows_layout = QVBoxLayout()
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(self._rows_layout)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        self._add = QPushButton("Add row", self)
        self._remove = QPushButton("Remove last", self)
        buttons.addWidget(self._add)
        buttons.addWidget(self._remove)
        outer.addLayout(buttons)
        self._add.clicked.connect(self._on_add)
        self._remove.clicked.connect(self._on_remove)

    def _on_add(self, *, key: str = "", val: float = 0.0) -> None:
        row_widget = QWidget(self)
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        key_edit = QLineEdit(key, row_widget)
        val_spin = QDoubleSpinBox(row_widget)
        # Free-form dict — we don't know the unit, so 3 decimals is a
        # cleaner compromise than the previous noisy 6.
        val_spin.setDecimals(3)
        val_spin.setRange(-1e12, 1e12)
        val_spin.setValue(float(val))
        row_layout.addWidget(key_edit)
        row_layout.addWidget(val_spin)
        self._rows_layout.addWidget(row_widget)
        self._rows.append((key_edit, val_spin))
        key_edit.textChanged.connect(self.valueChanged)
        val_spin.valueChanged.connect(self.valueChanged)
        self.valueChanged.emit()

    def _on_remove(self) -> None:
        if not self._rows:
            return
        key_edit, _val_spin = self._rows.pop()
        widget = key_edit.parentWidget()
        if widget is not None:
            widget.deleteLater()
        self.valueChanged.emit()

    def value(self) -> dict[str, float]:
        return {k.text(): float(v.value()) for k, v in self._rows if k.text()}

    def set_value(self, v: Any) -> None:
        # Reset the rows list and lay out new ones.
        for key_edit, _ in self._rows:
            widget = key_edit.parentWidget()
            if widget is not None:
                widget.deleteLater()
        self._rows.clear()
        for key, val in (v or {}).items():
            self._on_add(key=str(key), val=float(val))


class _JsonFallbackField(FieldWidget):
    """Last resort: a ``QLineEdit`` that round-trips the value as JSON.

    Used for annotations the dispatcher doesn't recognize (free-form
    ``dict[str, Any]``, complex unions). Operators can still author
    things by typing JSON; not pretty, but doesn't block the form from
    rendering."""

    def __init__(self, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._edit = QLineEdit(self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._edit)
        self._edit.textChanged.connect(self.valueChanged)

    def value(self) -> Any:
        text = self._edit.text().strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text  # let validation surface the error

    def set_value(self, v: Any) -> None:
        with QSignalBlocker(self._edit):
            self._edit.setText("" if v is None else json.dumps(v))


class _ModelTupleField(FieldWidget):
    """``tuple[Model, ...]`` editor — fixed-shape rows of nested model forms.

    Operators add or remove a row via Add/Remove; each row is a nested
    :class:`ModelForm` for the element type. Used by
    ``HoldStep.safety_overrides`` (``tuple[AlarmOverride, ...]``)."""

    def __init__(
        self,
        *,
        model_cls: type[BaseModel],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        from capa.ui.forms.from_model import build_form  # noqa: PLC0415

        self._model_cls = model_cls
        self._build_form: Callable[[], ModelForm] = lambda: build_form(model_cls)
        self._rows: list[ModelForm] = []
        self._rows_layout = QVBoxLayout()
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(self._rows_layout)
        buttons = QHBoxLayout()
        self._add = QPushButton(f"Add {model_cls.__name__}", self)
        self._remove = QPushButton("Remove last", self)
        buttons.addWidget(self._add)
        buttons.addWidget(self._remove)
        outer.addLayout(buttons)
        self._add.clicked.connect(self._on_add)
        self._remove.clicked.connect(self._on_remove)

    def _on_add(self, *, initial: BaseModel | dict[str, Any] | None = None) -> None:
        form = self._build_form()
        if initial is not None:
            form.set_values(initial)
        self._rows_layout.addWidget(form)
        self._rows.append(form)
        form.valuesChanged.connect(self.valueChanged)
        self.valueChanged.emit()

    def _on_remove(self) -> None:
        if not self._rows:
            return
        form = self._rows.pop()
        form.deleteLater()
        self.valueChanged.emit()

    def value(self) -> list[dict[str, Any]]:
        return [form.values() for form in self._rows]

    def set_value(self, v: Any) -> None:
        for form in self._rows:
            form.deleteLater()
        self._rows.clear()
        for entry in v or ():
            self._on_add(initial=entry)
