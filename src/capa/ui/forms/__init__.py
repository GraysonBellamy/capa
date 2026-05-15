"""Pydantic-driven Qt form generator.

Walks a :class:`pydantic.BaseModel` subclass and produces a
:class:`QWidget` with one field per model attribute, choosing the widget
from the field annotation. Validation errors paint inline on the
offending widget. Used by the Method editor's per-step detail panel and
by any future profile/procedure config UI.

Public API:

* :func:`build_form` — entry point; returns a :class:`ModelForm`.
* :class:`ModelForm` — composed widget; exposes ``values()``,
  ``set_values()``, ``validate()``, and a ``valuesChanged`` Qt signal.
"""

from __future__ import annotations

from capa.ui.forms.from_model import ModelForm, build_form

__all__ = ["ModelForm", "build_form"]
