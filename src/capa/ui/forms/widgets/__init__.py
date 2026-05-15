"""Per-annotation Qt field widgets used by :class:`ModelForm`.

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

from capa.ui.forms.widgets._base import FieldWidget
from capa.ui.forms.widgets._collapsible import CollapsibleGroup
from capa.ui.forms.widgets._factory import build_field_widget
from capa.ui.forms.widgets._nested import (
    _DiscriminatedUnionField,
    _is_discriminated_union,
)
from capa.ui.forms.widgets._scalar import _ComboBoxField, _PathField

__all__ = [
    "CollapsibleGroup",
    "FieldWidget",
    "_ComboBoxField",
    "_DiscriminatedUnionField",
    "_PathField",
    "_is_discriminated_union",
    "build_field_widget",
]
