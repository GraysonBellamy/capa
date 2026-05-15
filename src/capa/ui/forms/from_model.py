"""Build a Qt form from a Pydantic v2 ``BaseModel``.

The :func:`build_form` entry point walks
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

Field grouping (setup-editor disclosure): each pydantic field can opt
into a named, collapsible group via ``Field(json_schema_extra={...})``:

* ``"capa_group": "<name>"`` — bucket the field under this group. Fields
  with no annotation stay in the primary :class:`QFormLayout` at the top
  of the form, where they're always visible.
* ``"capa_group_open": True`` — override the group's default state. Any
  field in a group can set this; the *last* such declaration in field
  order wins, so the convention is to set it on the first field of the
  group.
* ``"capa_group_subtitle": "..."`` — muted header hint rendered next to
  the group title. Like ``capa_group_open``, last declaration wins.

Validation errors inside a collapsed group auto-open that group via
:meth:`~ModelForm.set_error_on_field` so errors are never hidden.
"""

from __future__ import annotations

import contextlib
from typing import Any

from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFormLayout, QLabel, QVBoxLayout, QWidget

from capa.ui.forms.widgets import CollapsibleGroup, FieldWidget, build_field_widget

# Discriminator-style fields that the form should never render — the
# enclosing tagged union picks the model class, not the user.
_HIDDEN_FIELD_NAMES: frozenset[str] = frozenset({"kind"})

# Primary-bucket name used internally for ungrouped fields. Reserved —
# pydantic fields that name themselves "primary" are still ungrouped
# because the resolution path checks for the json_schema_extra key, not
# the value's collision with this sentinel.
_PRIMARY_GROUP: str = "__primary__"


def _humanize(name: str) -> str:
    return name.replace("_", " ").strip().capitalize()


def _label_for(field_name: str, field: FieldInfo) -> str:
    return field.title or _humanize(field_name)


def _group_metadata(info: FieldInfo) -> tuple[str, bool | None, str | None]:
    """Read group-related ``json_schema_extra`` keys off one field.

    Returns ``(group_name, default_open_override, subtitle_override)``.
    Missing keys yield ``(_PRIMARY_GROUP, None, None)`` — the field is
    ungrouped and contributes no group-level metadata.
    """
    extra = getattr(info, "json_schema_extra", None)
    if not isinstance(extra, dict):
        return _PRIMARY_GROUP, None, None
    raw_group = extra.get("capa_group")
    group = str(raw_group) if isinstance(raw_group, str) and raw_group else _PRIMARY_GROUP
    raw_open = extra.get("capa_group_open")
    default_open = bool(raw_open) if isinstance(raw_open, bool) else None
    raw_subtitle = extra.get("capa_group_subtitle")
    subtitle = str(raw_subtitle) if isinstance(raw_subtitle, str) and raw_subtitle else None
    return group, default_open, subtitle


def _group_title(group_name: str) -> str:
    """Render an internal group key as the on-screen header title.

    ``"advanced"`` → ``"Advanced"``. Underscores become spaces; the first
    letter of each word is capitalised. Callers that want a different
    title should declare it via ``capa_group_subtitle`` (the subtitle is
    a hint, not a rename), or pick a more presentable group name.
    """
    return " ".join(part.capitalize() for part in group_name.split("_") if part)


class ModelForm(QWidget):
    """Composed widget — one row per non-hidden field.

    Construct via :func:`build_form`. The resulting widget owns its
    inner :class:`FieldWidget` instances and re-emits their changes
    coalesced as :attr:`valuesChanged`."""

    valuesChanged = Signal()  # noqa: N815 - Qt signal naming convention

    def __init__(
        self,
        model_cls: type[BaseModel],
        *,
        parent: QWidget | None = None,
        hidden_fields: frozenset[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self._model_cls = model_cls
        self._fields: dict[str, FieldWidget] = {}
        # Map field-name → CollapsibleGroup hosting it. Ungrouped fields
        # are absent. Used by :meth:`set_error_on_field` to auto-open
        # the right disclosure on validation failure so the operator
        # never has to hunt for a hidden error.
        self._field_group: dict[str, CollapsibleGroup] = {}
        # Per-form hidden field set, ORed with the global defaults.
        # The discriminated-union widget uses this to hide its
        # discriminator field (``"source"`` for SourceBinding, ``"kind"``
        # for Calibration) inside the variant subform — the operator
        # picks the variant from the parent combobox, not from inside.
        self._hidden_fields: frozenset[str] = _HIDDEN_FIELD_NAMES | (hidden_fields or frozenset())

        # Partition fields into ordered groups while preserving the
        # field-declaration order both within and across groups. The
        # primary bucket renders first, then each named group in the
        # order it was first seen. Group-level metadata (default-open,
        # subtitle) collects per group; last declaration wins.
        primary_fields: list[tuple[str, FieldInfo]] = []
        grouped: dict[str, list[tuple[str, FieldInfo]]] = {}
        group_open: dict[str, bool] = {}
        group_subtitle: dict[str, str | None] = {}
        for name, info in model_cls.model_fields.items():
            if name in self._hidden_fields:
                continue
            group, default_open, subtitle = _group_metadata(info)
            if group is _PRIMARY_GROUP:
                primary_fields.append((name, info))
            else:
                grouped.setdefault(group, []).append((name, info))
                if default_open is not None:
                    group_open[group] = default_open
                if subtitle is not None:
                    group_subtitle[group] = subtitle

        # Top-level layout: QVBoxLayout so the primary QFormLayout and
        # each CollapsibleGroup stack vertically.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        primary_form = QFormLayout()
        primary_form.setContentsMargins(0, 0, 0, 0)
        primary_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        outer.addLayout(primary_form)
        for name, info in primary_fields:
            self._add_field(primary_form, None, name, info)

        for group_name, members in grouped.items():
            # Default-closed for groups unless a field opted into open.
            group_widget = CollapsibleGroup(
                _group_title(group_name),
                subtitle=group_subtitle.get(group_name),
                default_open=group_open.get(group_name, False),
                parent=self,
            )
            outer.addWidget(group_widget)
            for name, info in members:
                self._add_field(None, group_widget, name, info)

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
        for hidden in self._hidden_fields & set(self._model_cls.model_fields):
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
        for name, widget in self._fields.items():
            widget.set_error(None)
            # Don't touch group open-state on the clear pass — collapsing
            # a group the operator opened to fix an error would be
            # disorienting. Open-state is sticky within a session and
            # only the validation-failure path forces a transition.
            _ = name
        try:
            self._model_cls.model_validate(self.values())
        except ValidationError as exc:
            errors = exc.errors()
            for err in errors:
                loc = err.get("loc", ())
                if loc and isinstance(loc[0], str):
                    self.set_error_on_field(str(loc[0]), str(err.get("msg", "invalid")))
            return [dict(err) for err in errors]
        return []

    def set_error_on_field(self, field_name: str, message: str | None) -> None:
        """Paint an error on one field and force-open any host group.

        Used by :meth:`validate` and by the Setup tab's Problems panel
        click-through — a problem whose ``ConfigProblem.path`` resolves
        to a single field name lands here, and the disclosure opens
        automatically. Passing ``None`` clears the error but does not
        close the group.
        """
        target = self._fields.get(field_name)
        if target is None:
            return
        target.set_error(message)
        if message:
            host = self._field_group.get(field_name)
            if host is not None:
                host.set_open(True)

    def group_for_field(self, field_name: str) -> CollapsibleGroup | None:
        """Return the :class:`CollapsibleGroup` hosting ``field_name``.

        ``None`` for primary (ungrouped) fields. Exposed so callers can
        drive open-state from outside (e.g. expand the group containing
        a focused field).
        """
        return self._field_group.get(field_name)

    # ---------------------------------------------------------------- internals

    def _add_field(
        self,
        primary_form: QFormLayout | None,
        group_widget: CollapsibleGroup | None,
        name: str,
        info: FieldInfo,
    ) -> None:
        """Build the field widget and add it to either the primary form
        layout or a collapsible group. Exactly one of ``primary_form`` /
        ``group_widget`` is non-None."""
        widget = build_field_widget(info.annotation, info, parent=self, field_name=name)
        label = QLabel(_label_for(name, info), self)
        if info.description:
            label.setToolTip(info.description)
        if primary_form is not None:
            primary_form.addRow(label, widget)
        else:
            assert group_widget is not None
            group_widget.add_row(label, widget)
            self._field_group[name] = group_widget
        widget.valueChanged.connect(self.valuesChanged)
        self._fields[name] = widget

    def _defaults_dict(self) -> dict[str, Any]:
        """Pydantic ``Field(default=...)`` values, expanded into a dict
        keyed by field name. Required fields with no default are skipped
        — those land in the widgets' zero/empty initial state and the
        operator fills them in."""
        defaults: dict[str, Any] = {}
        for name, info in self._model_cls.model_fields.items():
            if name in self._hidden_fields:
                continue
            if info.default_factory is not None:
                # Default factories can legitimately fail for required-
                # field models (a discriminated union variant whose
                # constructor needs values to validate). Suppress and
                # leave the widget at its zero state.
                with contextlib.suppress(Exception):
                    defaults[name] = info.default_factory()  # type: ignore[call-arg]
                continue
            if info.default is PydanticUndefined:
                continue
            defaults[name] = info.default
        return defaults


def build_form(
    model_cls: type[BaseModel],
    *,
    initial: BaseModel | dict[str, Any] | None = None,
    parent: QWidget | None = None,
    hidden_fields: frozenset[str] | None = None,
) -> ModelForm:
    """Build a :class:`ModelForm` for ``model_cls``.

    ``initial`` populates the widgets after construction; pass either a
    model instance or a plain dict shaped like ``model_dump()``.
    ``hidden_fields`` extends the default hide-set (``{"kind"}``) for
    callers that own a discriminator (the :class:`_DiscriminatedUnionField`
    widget passes its discriminator name so the subform doesn't repeat
    it).
    """
    form = ModelForm(model_cls, parent=parent, hidden_fields=hidden_fields)
    if initial is not None:
        form.set_values(initial)
    return form


__all__ = ["ModelForm", "build_form"]
