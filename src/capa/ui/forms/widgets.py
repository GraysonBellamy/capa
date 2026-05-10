"""Per-annotation widget builders.

Each builder accepts a Pydantic ``FieldInfo`` (carrying annotation,
default, validation constraints, title, description) and returns a
:class:`FieldWidget` — a small adapter that owns one Qt widget and
exposes a uniform ``value()`` / ``set_value()`` / ``set_error()`` /
``changed`` interface. The form module composes these without caring
about widget specifics.

MVP coverage (the 90/10 rule): plain scalars, Literal, nested
``BaseModel``, ``X | None``, ``tuple[X, ...]`` / ``list[X]``,
``dict[str, float]``. Anything richer (free-form ``dict[str, Any]``,
unions of multiple models) is left to a fallback ``QLineEdit`` that
serializes the value as JSON — surface for the rare case, not pretty.
"""

from __future__ import annotations

import json
import types
import typing
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, get_args, get_origin

from PyQt6.QtCore import QSignalBlocker, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDoubleSpinBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from pydantic import BaseModel
from pydantic.fields import FieldInfo

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_ERROR_STYLE = "QWidget { border: 1px solid #d33; }"
"""Painted on the offending widget when ``set_error()`` fires.

A red border is the cheap, theme-agnostic signal; the full message lands
in the widget's tooltip. If a future theme overrides ``QLineEdit`` borders
this won't shadow the theme's other state styling because we set it on
the wrapper, not the inner widget."""


def _humanize(name: str) -> str:
    """``"duration_s"`` → ``"Duration s"``. Used when ``Field(title=...)``
    is absent."""
    return name.replace("_", " ").strip().capitalize()


def _is_optional(annotation: Any) -> tuple[bool, Any]:
    """Inspect ``X | None`` / ``Optional[X]``. Returns ``(True, X)`` if
    ``annotation`` admits ``None``, else ``(False, annotation)``.

    Pydantic v2 represents both as ``X | None`` (PEP 604) under the hood,
    so the check is just "is None one of the union args?"."""
    origin = get_origin(annotation)
    if origin not in (typing.Union, types.UnionType):
        return False, annotation
    args = [a for a in get_args(annotation) if a is not type(None)]
    if len(args) == 1 and len(get_args(annotation)) == 2:
        return True, args[0]
    return False, annotation


def _numeric_constraints(field: FieldInfo) -> dict[str, float]:
    """Pull ``Field(gt=, ge=, lt=, le=)`` numeric constraints into a
    dict the spinbox factories can apply directly. Strict ``gt`` / ``lt``
    are approximated by clamping to ge=gt+epsilon; tests should pass.
    """
    out: dict[str, float] = {}
    for meta in getattr(field, "metadata", ()) or ():
        for attr in ("gt", "ge", "lt", "le"):
            v = getattr(meta, attr, None)
            if v is not None:
                out[attr] = float(v)
    return out


# ---------------------------------------------------------------------------
# FieldWidget base — uniform interface for ModelForm
# ---------------------------------------------------------------------------


class FieldWidget(QWidget):
    """Adapter wrapping one or more Qt widgets with a uniform field API.

    Each subclass owns its inner widget(s) and forwards their change
    signal to ``valueChanged``. ``ModelForm`` only ever calls these
    methods; it does not poke at the inner widgets directly."""

    valueChanged = pyqtSignal()

    def value(self) -> Any:
        raise NotImplementedError

    def set_value(self, v: Any) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def set_error(self, msg: str | None) -> None:
        if msg:
            self.setStyleSheet(_ERROR_STYLE)
            self.setToolTip(msg)
        else:
            self.setStyleSheet("")
            self.setToolTip(self._description or "")

    _description: str = ""


# ---------------------------------------------------------------------------
# Concrete widgets
# ---------------------------------------------------------------------------


class _LineEditField(FieldWidget):
    def __init__(self, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._edit = QLineEdit(self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._edit)
        self._edit.textChanged.connect(self.valueChanged)  # type: ignore[arg-type]

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
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._spin)
        self._spin.valueChanged.connect(self.valueChanged)  # type: ignore[arg-type]

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
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._spin = QDoubleSpinBox(self)
        # Default to 6 decimals — finer than any production value we'll
        # see (heat flux kW/m², flow sccm, temperature °C). The strict-
        # inequality nudge below uses 10**-decimals so it round-trips at
        # the configured precision; smaller eps would silently round to
        # the bound.
        decimals = 6
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
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._spin)
        self._spin.valueChanged.connect(self.valueChanged)  # type: ignore[arg-type]

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
        self._check.toggled.connect(self.valueChanged)  # type: ignore[arg-type]

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
        self._combo.currentIndexChanged.connect(self.valueChanged)  # type: ignore[arg-type]

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
        self._edit.dateTimeChanged.connect(self.valueChanged)  # type: ignore[arg-type]

    def value(self) -> datetime:
        return self._edit.dateTime().toPyDateTime()

    def set_value(self, v: Any) -> None:
        from PyQt6.QtCore import QDateTime  # noqa: PLC0415

        with QSignalBlocker(self._edit):
            if isinstance(v, datetime):
                self._edit.setDateTime(QDateTime(v))
            elif isinstance(v, str):
                try:
                    self._edit.setDateTime(QDateTime(datetime.fromisoformat(v)))
                except ValueError:
                    pass


class _PathField(FieldWidget):
    def __init__(self, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._edit = QLineEdit(self)
        self._browse = QPushButton("Browse…", self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._edit)
        layout.addWidget(self._browse)
        self._edit.textChanged.connect(self.valueChanged)  # type: ignore[arg-type]
        self._browse.clicked.connect(self._on_browse)  # type: ignore[arg-type]

    def value(self) -> Path:
        return Path(self._edit.text())

    def set_value(self, v: Any) -> None:
        with QSignalBlocker(self._edit):
            self._edit.setText(str(v) if v is not None else "")

    def _on_browse(self) -> None:
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
        self._enable.toggled.connect(self._on_toggle)  # type: ignore[arg-type]
        self._enable.toggled.connect(self.valueChanged)  # type: ignore[arg-type]
        self._inner.valueChanged.connect(self.valueChanged)  # type: ignore[arg-type]

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
        self._add.clicked.connect(self._on_add)  # type: ignore[arg-type]
        self._remove.clicked.connect(self._on_remove)  # type: ignore[arg-type]
        self._list.itemChanged.connect(self.valueChanged)  # type: ignore[arg-type]

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
        return tuple(self._list.item(i).text() for i in range(self._list.count()))

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
        self._add.clicked.connect(self._on_add)  # type: ignore[arg-type]
        self._remove.clicked.connect(self._on_remove)  # type: ignore[arg-type]

    def _on_add(self, *, key: str = "", val: float = 0.0) -> None:
        row_widget = QWidget(self)
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        key_edit = QLineEdit(key, row_widget)
        val_spin = QDoubleSpinBox(row_widget)
        val_spin.setDecimals(6)
        val_spin.setRange(-1e12, 1e12)
        val_spin.setValue(float(val))
        row_layout.addWidget(key_edit)
        row_layout.addWidget(val_spin)
        self._rows_layout.addWidget(row_widget)
        self._rows.append((key_edit, val_spin))
        key_edit.textChanged.connect(self.valueChanged)  # type: ignore[arg-type]
        val_spin.valueChanged.connect(self.valueChanged)  # type: ignore[arg-type]
        self.valueChanged.emit()

    def _on_remove(self) -> None:
        if not self._rows:
            return
        key_edit, val_spin = self._rows.pop()
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
        self._edit.textChanged.connect(self.valueChanged)  # type: ignore[arg-type]

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


# ---------------------------------------------------------------------------
# Nested-model widget — built lazily to avoid an import cycle with
# from_model.py.
# ---------------------------------------------------------------------------


class _NestedModelField(FieldWidget):
    """Wrap a recursive :class:`ModelForm` for nested ``BaseModel`` fields.

    The inner form is wrapped in a :class:`QGroupBox` titled with the
    field's display label; that gives nested forms a clear visual nest
    without consuming horizontal space."""

    def __init__(
        self,
        *,
        model_cls: type[BaseModel],
        title: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # Late import to break the cycle.
        from capa.ui.forms.from_model import build_form  # noqa: PLC0415

        self._model_cls = model_cls
        self._inner = build_form(model_cls)
        group = QGroupBox(title, self)
        layout = QVBoxLayout(group)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self._inner)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(group)
        self._inner.valuesChanged.connect(self.valueChanged)  # type: ignore[arg-type]

    def value(self) -> dict[str, Any]:
        return self._inner.values()

    def set_value(self, v: Any) -> None:
        self._inner.set_values(v if v is not None else {})


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
        from capa.ui.forms.from_model import ModelForm, build_form  # noqa: PLC0415

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
        self._add.clicked.connect(self._on_add)  # type: ignore[arg-type]
        self._remove.clicked.connect(self._on_remove)  # type: ignore[arg-type]

    def _on_add(self, *, initial: BaseModel | dict | None = None) -> None:
        form = self._build_form()
        if initial is not None:
            form.set_values(initial)
        self._rows_layout.addWidget(form)
        self._rows.append(form)
        form.valuesChanged.connect(self.valueChanged)  # type: ignore[arg-type]
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


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def build_field_widget(
    annotation: Any,
    field: FieldInfo,
    *,
    parent: QWidget | None = None,
) -> FieldWidget:
    """Pick a :class:`FieldWidget` subclass based on the annotation.

    Optional / union-with-None annotations are unwrapped first and
    wrapped in :class:`_OptionalField`. The dispatcher is intentionally
    flat — extending it means adding one more branch, not subclassing a
    visitor."""
    is_optional, inner_annotation = _is_optional(annotation)
    widget = _build_inner(inner_annotation, field, parent=parent)
    if is_optional:
        widget = _OptionalField(inner=widget, parent=parent)
    widget._description = field.description or ""  # noqa: SLF001 - intentional internal field
    if field.description:
        widget.setToolTip(field.description)
    return widget


def _build_inner(annotation: Any, field: FieldInfo, *, parent: QWidget | None) -> FieldWidget:
    origin = get_origin(annotation)
    args = get_args(annotation)

    # Literal[...] → combobox
    if origin is typing.Literal:
        return _ComboBoxField(choices=args, parent=parent)

    # Plain primitives.
    if annotation is str:
        return _LineEditField(parent=parent)
    if annotation is bool:
        return _CheckBoxField(parent=parent)
    if annotation is int:
        return _SpinBoxField(constraints=_numeric_constraints(field), parent=parent)
    if annotation is float:
        return _DoubleSpinBoxField(constraints=_numeric_constraints(field), parent=parent)
    if annotation is datetime:
        return _DateTimeField(parent=parent)
    if annotation is Path:
        return _PathField(parent=parent)

    # Nested BaseModel.
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        title = field.title or _humanize(field.alias or "")
        return _NestedModelField(model_cls=annotation, title=title, parent=parent)

    # Container types.
    if origin in (tuple, list):
        if not args:
            return _JsonFallbackField(parent=parent)
        # Drop the trailing Ellipsis from `tuple[X, ...]`.
        elem_type = args[0]
        if isinstance(elem_type, type) and issubclass(elem_type, BaseModel):
            return _ModelTupleField(model_cls=elem_type, parent=parent)
        if elem_type is str:
            return _StrTupleField(parent=parent)
        # Fall through to JSON fallback for tuple[float] etc.
        return _JsonFallbackField(parent=parent)

    if origin is dict:
        if args == (str, float):
            return _DictStrFloatField(parent=parent)
        return _JsonFallbackField(parent=parent)

    # Unknown annotation — JSON fallback. Keeps the form usable for
    # plugin-defined fields where the schema isn't a stock pydantic shape.
    return _JsonFallbackField(parent=parent)


__all__ = [
    "FieldWidget",
    "build_field_widget",
]
