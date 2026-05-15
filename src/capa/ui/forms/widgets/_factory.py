""":func:`build_field_widget` — annotation-driven dispatcher.

Picks a concrete :class:`FieldWidget` subclass from the surrounding
modules based on the Pydantic ``FieldInfo`` annotation. Discriminated
unions take priority over generic origin/args probes; ``Optional[…]``
is unwrapped once at the outer layer and re-wrapped in
:class:`_OptionalField`.
"""

from __future__ import annotations

import typing
from datetime import datetime
from enum import StrEnum as _StrEnum
from pathlib import Path
from typing import Any, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo
from PySide6.QtWidgets import QWidget

from capa.ui.forms.widgets._base import FieldWidget
from capa.ui.forms.widgets._collection import (
    _DictStrFloatField,
    _JsonFallbackField,
    _ModelTupleField,
    _StrTupleField,
)
from capa.ui.forms.widgets._helpers import (
    _decimals_for_field,
    _humanize,
    _is_optional,
    _numeric_constraints,
    _path_mode_from_field,
)
from capa.ui.forms.widgets._nested import (
    _DiscriminatedUnionField,
    _is_discriminated_union,
    _NestedModelField,
)
from capa.ui.forms.widgets._scalar import (
    _CheckBoxField,
    _ComboBoxField,
    _DateTimeField,
    _DoubleSpinBoxField,
    _LineEditField,
    _OptionalField,
    _PathField,
    _SpinBoxField,
)


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
