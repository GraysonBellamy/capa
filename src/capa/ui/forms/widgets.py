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

import contextlib
import json
import types
import typing
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum as _StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo
from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from capa.ui.forms.from_model import ModelForm

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


def _path_mode_from_field(field: FieldInfo) -> typing.Literal["file", "dir"]:
    """Read ``Field(json_schema_extra={"capa_path_mode": "dir"})``.

    The default is ``"file"``; only callers that explicitly opt into a
    directory picker get one. Used by ``StoragePolicy.bundle_root``."""
    extra = getattr(field, "json_schema_extra", None)
    if isinstance(extra, dict) and extra.get("capa_path_mode") == "dir":
        return "dir"
    return "file"


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


# Unit-suffix → decimal-place table for float spinboxes. Order matters:
# longer suffixes must come first so ``_mm`` doesn't lose to ``_m``.
# Reasoning per group: a heat flux of 12.3 kW/m² doesn't need .000123; a
# sample mass of 1.2345 g does (sub-mg matters). When in doubt the
# default below is 3 — fine for most operator-facing values, easy to
# override per-field via ``Field(json_schema_extra={"capa_decimals": N})``.
_DECIMALS_BY_SUFFIX: tuple[tuple[str, int], ...] = (
    # Time
    ("_ns", 0),
    ("_us", 0),
    ("_ms", 1),
    ("_seconds", 2),
    ("_minutes", 2),
    ("_hours", 2),
    ("_s", 2),
    # Frequency
    ("_khz", 2),
    ("_hz", 1),
    # Length
    ("_nm", 1),
    ("_um", 1),
    ("_mm", 2),
    ("_cm", 2),
    ("_inches", 3),
    ("_in", 3),
    ("_meters", 4),
    # Area
    ("_mm2", 2),
    ("_cm2", 2),
    ("_m2", 3),
    # Mass
    ("_mg", 3),
    ("_kg", 4),
    ("_g", 4),
    # Flow
    ("_sccm", 2),
    ("_slpm", 2),
    ("_slm", 2),
    ("_lpm", 2),
    ("_mlpm", 2),
    # Temperature
    ("_celsius", 1),
    ("_kelvin", 1),
    ("_fahrenheit", 1),
    ("_degc", 1),
    ("_degf", 1),
    ("_c", 1),
    ("_f", 1),
    ("_k", 1),
    # Pressure
    ("_pascal", 1),
    ("_pascals", 1),
    ("_kpa", 2),
    ("_mpa", 3),
    ("_bar", 3),
    ("_psi", 2),
    ("_torr", 2),
    ("_mbar", 2),
    ("_atm", 3),
    ("_pa", 1),
    # Heat flux / power
    ("_kw_m2", 1),
    ("_kw_per_m2", 1),
    ("_w_m2", 1),
    ("_w_per_m2", 1),
    ("_kw", 2),
    ("_mw", 1),
    ("_w", 1),
    # Energy
    ("_kj", 2),
    ("_mj", 3),
    ("_j", 2),
    # Electrical
    ("_mv", 2),
    ("_volts", 4),
    ("_v", 4),
    ("_ma", 2),
    ("_amperes", 4),
    ("_amps", 4),
    ("_a", 4),
    ("_ohms", 2),
    ("_ohm", 2),
    # Ratios / dimensionless
    ("_percent", 1),
    ("_pct", 1),
    ("_fraction", 4),
    ("_frac", 4),
    ("_ratio", 4),
)


def _decimals_for_field(field_name: str | None, field: FieldInfo) -> int:
    """Pick a decimal count for a ``QDoubleSpinBox``.

    Priority:

    1. Explicit ``Field(json_schema_extra={"capa_decimals": N})``.
    2. Suffix lookup against :data:`_DECIMALS_BY_SUFFIX`. A trailing
       ``_per_min`` / ``_per_s`` is treated as a rate and stripped before
       the suffix match so ``ramp_rate_c_per_min`` reads as a per-minute
       temperature rate (2 decimals on the °C side).
    3. Default ``3`` — tighter than the operator-frustrating ``6`` and
       loose enough that nobody-cares fields read cleanly.

    A non-recognised field can opt back into more precision via the
    explicit override.
    """
    extra = getattr(field, "json_schema_extra", None)
    if isinstance(extra, dict):
        override = extra.get("capa_decimals")
        if isinstance(override, int) and 0 <= override <= 12:
            return override
    if field_name:
        lowered = field_name.lower()
        # Rate-per-time fields like ``ramp_rate_c_per_min`` —— strip
        # the ``_per_<unit>`` tail before suffix matching so the base
        # unit (``_c``) drives precision rather than ``_min``.
        for tail in ("_per_min", "_per_minute", "_per_s", "_per_sec", "_per_second", "_per_hour"):
            if lowered.endswith(tail):
                lowered = lowered[: -len(tail)]
                break
        for suffix, decimals in _DECIMALS_BY_SUFFIX:
            if lowered.endswith(suffix):
                return decimals
    return 3


# ---------------------------------------------------------------------------
# CollapsibleGroup — disclosure widget for grouping rare fields under a
# header that defaults to closed. Used by ``ModelForm`` to render every
# field group declared via ``Field(json_schema_extra={"capa_group": ...})``;
# also usable directly by section widgets that need ad-hoc disclosure
# outside the auto-form path.
# ---------------------------------------------------------------------------


class CollapsibleGroup(QWidget):
    """A header-button + content-area pair with toggleable visibility.

    The header is a :class:`QToolButton` styled flat with a chevron that
    flips between right (closed) and down (open). An optional subtitle
    renders to the right of the title in muted text, useful for hinting
    at what lives inside without forcing the operator to expand. The
    content area is a plain :class:`QWidget`; callers add to it via
    :meth:`add_row` (label + widget, ``QFormLayout`` semantics) or
    :meth:`add_widget` (full-width widget).

    Open/closed state is held on the instance; :meth:`set_open` is
    idempotent and emits :attr:`toggled` only on transitions. Visibility
    is toggled with ``setVisible`` — no animation, deliberate. Animation
    flickers with QSS, slows reveal of validation errors, and adds no
    information.
    """

    toggled = Signal(bool)
    """Emitted with the new open state on every transition."""

    def __init__(
        self,
        title: str,
        *,
        subtitle: str | None = None,
        default_open: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._is_open = bool(default_open)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 4)
        outer.setSpacing(4)

        # Header row: chevron + title (+ optional muted subtitle).
        header_row = QWidget(self)
        header_layout = QHBoxLayout(header_row)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(4)

        self._button = QToolButton(header_row)
        self._button.setText(title)
        self._button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._button.setArrowType(
            Qt.ArrowType.DownArrow if self._is_open else Qt.ArrowType.RightArrow
        )
        self._button.setAutoRaise(True)
        self._button.setCheckable(True)
        self._button.setChecked(self._is_open)
        self._button.setStyleSheet("QToolButton { font-weight: 600; }")
        self._button.clicked.connect(self._on_button_clicked)
        header_layout.addWidget(self._button)

        if subtitle:
            self._subtitle_label: QLabel | None = QLabel(subtitle, header_row)
            self._subtitle_label.setStyleSheet("color: #777;")
            header_layout.addWidget(self._subtitle_label, 1)
        else:
            self._subtitle_label = None
            header_layout.addStretch(1)

        outer.addWidget(header_row)

        # Content area: hosts a QFormLayout so ``add_row`` mirrors the
        # parent form's row shape. Callers that need a free-form layout
        # use :meth:`add_widget`, which appends below the form rows.
        self._content = QFrame(self)
        self._content.setFrameShape(QFrame.Shape.NoFrame)
        # Indent the content slightly so the disclosure hierarchy reads.
        content_layout = QVBoxLayout(self._content)
        content_layout.setContentsMargins(16, 0, 0, 0)
        content_layout.setSpacing(4)

        self._form_layout = QFormLayout()
        self._form_layout.setContentsMargins(0, 0, 0, 0)
        self._form_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        content_layout.addLayout(self._form_layout)

        outer.addWidget(self._content)
        self._content.setVisible(self._is_open)

    # ------------------------------------------------------------------ API

    def is_open(self) -> bool:
        return self._is_open

    def set_open(self, open_: bool) -> None:
        open_ = bool(open_)
        if open_ == self._is_open:
            return
        self._is_open = open_
        self._button.setChecked(open_)
        self._button.setArrowType(Qt.ArrowType.DownArrow if open_ else Qt.ArrowType.RightArrow)
        self._content.setVisible(open_)
        self.toggled.emit(open_)

    def add_row(self, label: str | QWidget, widget: QWidget) -> None:
        """Mirror ``QFormLayout.addRow`` for the group's content area."""
        if isinstance(label, str):
            self._form_layout.addRow(QLabel(label, self._content), widget)
        else:
            self._form_layout.addRow(label, widget)

    def add_widget(self, widget: QWidget) -> None:
        """Append a full-width widget below the group's form rows."""
        layout = self._content.layout()
        if layout is not None:
            layout.addWidget(widget)

    # ---------------------------------------------------------------- slots

    def _on_button_clicked(self) -> None:
        # ``QToolButton.toggled`` would race with our own state; drive
        # everything through ``set_open`` so the chevron, content
        # visibility, and the toggled signal stay in lockstep.
        self.set_open(not self._is_open)


# ---------------------------------------------------------------------------
# FieldWidget base — uniform interface for ModelForm
# ---------------------------------------------------------------------------


class FieldWidget(QWidget):
    """Adapter wrapping one or more Qt widgets with a uniform field API.

    Each subclass owns its inner widget(s) and forwards their change
    signal to ``valueChanged``. ``ModelForm`` only ever calls these
    methods; it does not poke at the inner widgets directly."""

    valueChanged = Signal()  # noqa: N815 - Qt signal naming convention

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
        self._inner.valuesChanged.connect(self.valueChanged)

    def value(self) -> dict[str, Any]:
        return self._inner.values()

    def set_value(self, v: Any) -> None:
        self._inner.set_values(v if v is not None else {})


# ---------------------------------------------------------------------------
# Discriminated union widget.
# ---------------------------------------------------------------------------


def _is_discriminated_union(annotation: Any, field: FieldInfo | None = None) -> bool:
    """Detect a Pydantic discriminated tagged union.

    Pydantic v2 strips the ``Annotated[..., Field(discriminator=...)]``
    wrapper when it builds :class:`FieldInfo`: the annotation becomes a
    plain ``X | Y | …`` union and the discriminator name lands on
    ``FieldInfo.discriminator``. We accept either shape so the helper
    can be called both from the dispatcher (with the field handy) and
    from tests on a bare ``Annotated[…]`` symbol.
    """
    # Path 1: FieldInfo has the discriminator (Pydantic-stripped form).
    if field is not None and getattr(field, "discriminator", None):
        ann = annotation
        if get_origin(ann) is typing.Annotated:
            ann = get_args(ann)[0]
        if get_origin(ann) in (typing.Union, types.UnionType):
            return True
    # Path 2: raw Annotated[…, FieldInfo(discriminator=…)] (test idiom).
    if get_origin(annotation) is typing.Annotated:
        for meta in getattr(annotation, "__metadata__", ()):
            if getattr(meta, "discriminator", None) is not None:
                return True
    return False


def _extract_union_variants(annotation: Any) -> tuple[type[BaseModel], ...]:
    """Pull the union members from either ``Annotated[X | Y, …]`` or ``X | Y``."""
    if get_origin(annotation) is typing.Annotated:
        annotation = get_args(annotation)[0]
    args = get_args(annotation)
    return tuple(a for a in args if isinstance(a, type) and issubclass(a, BaseModel))


def _get_discriminator_name(annotation: Any, field: FieldInfo | None = None) -> str:
    """Return the field name the union dispatches on (``"kind"``, ``"source"``)."""
    if field is not None:
        disc = getattr(field, "discriminator", None)
        if disc:
            return str(disc)
    if get_origin(annotation) is typing.Annotated:
        for meta in getattr(annotation, "__metadata__", ()):
            disc = getattr(meta, "discriminator", None)
            if disc is not None:
                return str(disc)
    return "type"  # pragma: no cover - defensive


def _variant_discriminator_value(variant: type[BaseModel], discriminator: str) -> str:
    """Read the Literal default for a variant's discriminator field.

    Each tagged-union variant declares
    ``discriminator: Literal["foo"] = "foo"`` — we read that default so
    the combobox can label and route by the canonical string value.
    """
    field_info = variant.model_fields.get(discriminator)
    if field_info is None:
        return variant.__name__
    default = field_info.default
    if default is None or default is ...:
        # Fall back to the Literal annotation if no default was provided.
        ann = field_info.annotation
        ann_args = get_args(ann)
        if ann_args:
            return str(ann_args[0])
        return variant.__name__
    return str(default)


class _DiscriminatedUnionField(FieldWidget):
    """Combobox-driven editor for ``Annotated[A | B | ..., discriminator=…]``.

    Replaces the JSON-fallback widget for tagged unions like
    :data:`capa.channels.spec.SourceBinding` and
    :data:`capa.channels.calibration.Calibration`. Switching the combobox
    rebuilds the variant subform, preserving values for field names that
    exist in both variants (the operator's ``input_unit`` survives a
    flip from Identity to LinearTwoPoint).
    """

    def __init__(
        self,
        *,
        union: Any,
        field: FieldInfo,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._union = union
        self._field = field
        self._variants = _extract_union_variants(union)
        self._discriminator = _get_discriminator_name(union, field)
        # discriminator value -> variant class, e.g. "identity" -> Identity.
        self._variant_by_value: dict[str, type[BaseModel]] = {
            _variant_discriminator_value(v, self._discriminator): v for v in self._variants
        }
        # Cross-variant buffer: field-name -> last-seen value across
        # variant switches. Lets operators flip variant without losing
        # the ``input_unit`` / ``output_unit`` / ``uncertainty`` they
        # already typed.
        self._buffer: dict[str, Any] = {}
        self._current_form: ModelForm | None = None
        self._current_variant: type[BaseModel] | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._combo = QComboBox(self)
        for value in self._variant_by_value:
            self._combo.addItem(_humanize(value), userData=value)
        outer.addWidget(self._combo)
        self._subform_holder = QWidget(self)
        self._subform_layout = QVBoxLayout(self._subform_holder)
        self._subform_layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._subform_holder)

        self._combo.currentIndexChanged.connect(self._on_variant_changed)
        # Build the first subform.
        self._rebuild_subform()

    # -- internals ----------------------------------------------------------

    def _selected_variant_value(self) -> str:
        data = self._combo.currentData()
        if isinstance(data, str):
            return data
        # Fallback for old-style addItem without userData.
        return self._combo.currentText().lower().replace(" ", "_")

    def _on_variant_changed(self) -> None:
        # Capture current subform values into the cross-variant buffer
        # before tearing it down.
        if self._current_form is not None:
            with contextlib.suppress(Exception):
                current_values = self._current_form.values()
                self._buffer.update(current_values)
        self._rebuild_subform()
        self.valueChanged.emit()

    def _rebuild_subform(self) -> None:
        """Tear down the existing subform; build a new one for the picked variant."""
        # Late import — same cycle-break trick _NestedModelField uses.
        from capa.ui.forms.from_model import build_form  # noqa: PLC0415

        value = self._selected_variant_value()
        variant = self._variant_by_value.get(value)
        if variant is None:
            return
        # Clear existing subform.
        if self._current_form is not None:
            self._current_form.deleteLater()
            self._current_form = None
        self._current_variant = variant
        # Build the new subform; hide the discriminator field within it
        # so the operator only sees the combobox above.
        new_form = build_form(variant, hidden_fields=frozenset({self._discriminator}))
        # Replay buffered values for overlapping field names.
        replay: dict[str, Any] = {}
        for name in variant.model_fields:
            if name == self._discriminator:
                continue
            if name in self._buffer:
                replay[name] = self._buffer[name]
        if replay:
            with contextlib.suppress(Exception):
                new_form.set_values(replay)
        new_form.valuesChanged.connect(self.valueChanged)
        self._subform_layout.addWidget(new_form)
        self._current_form = new_form

    # -- FieldWidget API ---------------------------------------------------

    def value(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self._current_form is not None:
            out.update(self._current_form.values())
        # Always emit the canonical discriminator value, even when the
        # form chooses to hide it.
        out[self._discriminator] = self._selected_variant_value()
        return out

    def set_value(self, v: Any) -> None:
        if isinstance(v, BaseModel):
            v = v.model_dump()
        if not isinstance(v, dict):
            return
        target = v.get(self._discriminator)
        if target is not None and target in self._variant_by_value:
            with QSignalBlocker(self._combo):
                # Find the index of the target variant value.
                for i in range(self._combo.count()):
                    if self._combo.itemData(i) == target:
                        self._combo.setCurrentIndex(i)
                        break
            self._rebuild_subform()
        # Populate the subform with the remaining fields.
        if self._current_form is not None:
            payload = {k: val for k, val in v.items() if k != self._discriminator}
            with contextlib.suppress(Exception):
                self._current_form.set_values(payload)
            # Refresh the buffer so future variant flips see these values.
            self._buffer.update(payload)


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


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def build_field_widget(
    annotation: Any,
    field: FieldInfo,
    *,
    parent: QWidget | None = None,
    field_name: str | None = None,
) -> FieldWidget:
    """Pick a :class:`FieldWidget` subclass based on the annotation.

    Optional / union-with-None annotations are unwrapped first and
    wrapped in :class:`_OptionalField`. ``field_name`` (passed by
    :class:`ModelForm`) is used to infer per-field spinbox decimal
    precision via :func:`_decimals_for_field`; callers that don't have
    a name (e.g. ad-hoc widgets) may omit it.
    """
    is_optional, inner_annotation = _is_optional(annotation)
    widget = _build_inner(inner_annotation, field, parent=parent, field_name=field_name)
    if is_optional:
        widget = _OptionalField(inner=widget, parent=parent)
    widget._description = field.description or ""
    if field.description:
        widget.setToolTip(field.description)
    return widget


def _build_inner(
    annotation: Any,
    field: FieldInfo,
    *,
    parent: QWidget | None,
    field_name: str | None = None,
) -> FieldWidget:
    # Discriminated unions take priority over generic origin/args probes:
    # ``Annotated[A | B, Field(discriminator=...)]`` would otherwise fall
    # through to the JSON fallback.
    if _is_discriminated_union(annotation, field):
        return _DiscriminatedUnionField(union=annotation, field=field, parent=parent)

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
        return _DoubleSpinBoxField(
            constraints=_numeric_constraints(field),
            decimals=_decimals_for_field(field_name, field),
            parent=parent,
        )
    if annotation is datetime:
        return _DateTimeField(parent=parent)
    if annotation is Path:
        mode = _path_mode_from_field(field)
        return _PathField(mode=mode, parent=parent)

    # StrEnum subclass → combobox over members. ``Literal`` already
    # covers explicit choice tuples; this branch handles the cleaner
    # ``class Mode(StrEnum): ...`` declaration the operator-facing
    # config models prefer.
    if (
        isinstance(annotation, type)
        and issubclass(annotation, str)
        and issubclass(annotation, _StrEnum)
    ):
        return _ComboBoxField(choices=tuple(annotation), parent=parent)

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
    "CollapsibleGroup",
    "FieldWidget",
    "build_field_widget",
]


# Re-export the discriminated-union detector so tests and the form
# generator can introspect it without importing the private helpers.
_DiscriminatedUnion_detect = _is_discriminated_union
