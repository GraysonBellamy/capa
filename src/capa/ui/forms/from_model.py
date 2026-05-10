"""Build a Qt form from a Pydantic v2 ``BaseModel``.

Plan §10.5. The :func:`build_form` entry point walks
``model_cls.model_fields`` and pairs each field with a
:class:`~capa.ui.forms.widgets.FieldWidget` chosen by annotation. The
resulting :class:`ModelForm` is a thin :class:`QWidget` that exposes:

* :meth:`~ModelForm.values` — current state as a plain dict, ready for
  ``model_cls.model_validate(...)``;
* :meth:`~ModelForm.set_values` — populate from a dict or a
  ``BaseModel`` instance;
* :meth:`~ModelForm.validate` — run validation, paint inline errors,
  return the list of :class:`pydantic.ValidationError.errors()` dicts.

The form does *not* round-trip a fully-typed ``BaseModel`` instance via
``values()`` — it returns the dict shape, leaving validation /
construction to the caller. This keeps ``ModelForm`` usable for partial
or in-progress data without forcing every read to succeed Pydantic
validation.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFormLayout, QLabel, QWidget

from capa.ui.forms.widgets import FieldWidget, build_field_widget

# Discriminator-style fields that the form should never render — the
# enclosing tagged union picks the model class, not the user.
_HIDDEN_FIELD_NAMES: frozenset[str] = frozenset({"kind"})


def _humanize(name: str) -> str:
    return name.replace("_", " ").strip().capitalize()


def _label_for(field_name: str, field: FieldInfo) -> str:
    return field.title or _humanize(field_name)


class ModelForm(QWidget):
    """Composed widget — one row per non-hidden field.

    Construct via :func:`build_form`. The resulting widget owns its
    inner :class:`FieldWidget` instances and re-emits their changes
    coalesced as :attr:`valuesChanged`."""

    valuesChanged = Signal()

    def __init__(
        self,
        model_cls: type[BaseModel],
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._model_cls = model_cls
        self._fields: dict[str, FieldWidget] = {}

        layout = QFormLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        for name, info in model_cls.model_fields.items():
            if name in _HIDDEN_FIELD_NAMES:
                continue
            widget = build_field_widget(info.annotation, info, parent=self)
            label = QLabel(_label_for(name, info), self)
            if info.description:
                label.setToolTip(info.description)
            layout.addRow(label, widget)
            widget.valueChanged.connect(self.valuesChanged)
            self._fields[name] = widget

        self.set_values(self._defaults_dict())

    # ------------------------------------------------------------------ API

    def values(self) -> dict[str, Any]:
        """Return current form state as a plain dict.

        Hidden fields (the ``kind`` discriminator) are reinjected from
        the model class so the dict round-trips through
        ``model_cls.model_validate(...)`` cleanly."""
        out: dict[str, Any] = {}
        for name, widget in self._fields.items():
            out[name] = widget.value()
        for hidden in _HIDDEN_FIELD_NAMES & set(self._model_cls.model_fields):
            default = self._model_cls.model_fields[hidden].default
            if default is not None:
                out[hidden] = default
        return out

    def set_values(self, data: dict[str, Any] | BaseModel) -> None:
        """Populate the form from a dict or a model instance.

        Unknown keys are ignored; missing keys leave the field at its
        current value. Use this from the Method editor's row-selection
        handler to swap one step's values for another's."""
        if isinstance(data, BaseModel):
            data = data.model_dump(mode="python")
        for name, widget in self._fields.items():
            if name in data:
                widget.set_value(data[name])

    def validate(self) -> list[dict[str, Any]]:
        """Validate current state. Returns the Pydantic
        ``e.errors()`` list (empty if validation passed) and paints
        inline error indicators on the offending widgets."""
        # Clear previous errors first so passing fields drop their style.
        for widget in self._fields.values():
            widget.set_error(None)
        try:
            self._model_cls.model_validate(self.values())
        except ValidationError as exc:
            errors = exc.errors()
            for err in errors:
                loc = err.get("loc", ())
                if loc and isinstance(loc[0], str):
                    target = self._fields.get(loc[0])
                    if target is not None:
                        target.set_error(err.get("msg", "invalid"))
            return [dict(err) for err in errors]
        return []

    # ---------------------------------------------------------------- internals

    def _defaults_dict(self) -> dict[str, Any]:
        """Pydantic ``Field(default=...)`` values, expanded into a dict
        keyed by field name. Required fields with no default are skipped
        — those land in the widgets' zero/empty initial state and the
        operator fills them in."""
        defaults: dict[str, Any] = {}
        for name, info in self._model_cls.model_fields.items():
            if name in _HIDDEN_FIELD_NAMES:
                continue
            if info.default_factory is not None:
                try:
                    defaults[name] = info.default_factory()  # type: ignore[call-arg]
                except TypeError:
                    pass
                continue
            # `info.default` is `PydanticUndefined` for required fields,
            # `Ellipsis` for legacy `Field(...)`, and the actual default
            # otherwise. Treat all three "no default" sentinels the same.
            if info.default is PydanticUndefined or info.default is ...:
                continue
            defaults[name] = info.default
        return defaults


def build_form(
    model_cls: type[BaseModel],
    *,
    initial: BaseModel | dict[str, Any] | None = None,
    parent: QWidget | None = None,
) -> ModelForm:
    """Build a :class:`ModelForm` for ``model_cls``.

    ``initial`` populates the widgets after construction; pass either a
    model instance or a plain dict shaped like ``model_dump()``."""
    form = ModelForm(model_cls, parent=parent)
    if initial is not None:
        form.set_values(initial)
    return form


__all__ = ["ModelForm", "build_form"]
