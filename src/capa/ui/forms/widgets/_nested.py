"""Nested-model and discriminated-union field widgets.

These widgets recurse into :class:`~capa.ui.forms.from_model.ModelForm`
to render nested ``BaseModel`` instances; the import is late to break a
cycle (``from_model`` instantiates these field widgets).
"""

from __future__ import annotations

import contextlib
import types
import typing
from typing import TYPE_CHECKING, Any, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo
from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QVBoxLayout,
    QWidget,
)

from capa.ui.forms.widgets._base import FieldWidget
from capa.ui.forms.widgets._helpers import _humanize

if TYPE_CHECKING:
    from capa.ui.forms.from_model import ModelForm


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
        """Current value held by this widget, coerced to the model-side type."""
        return self._inner.values()

    def set_value(self, v: Any) -> None:
        """Set this widget's value from a model-side value."""
        self._inner.set_values(v if v is not None else {})


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
        """Current value held by this widget, coerced to the model-side type."""
        out: dict[str, Any] = {}
        if self._current_form is not None:
            out.update(self._current_form.values())
        # Always emit the canonical discriminator value, even when the
        # form chooses to hide it.
        out[self._discriminator] = self._selected_variant_value()
        return out

    def set_value(self, v: Any) -> None:
        """Set this widget's value from a model-side value."""
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
