""":class:`FieldWidget` base — uniform interface every ``ModelForm`` field uses."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

from capa.ui.forms.widgets._helpers import _ERROR_STYLE

if TYPE_CHECKING:
    from capa.ui.forms.widgets._channels import ChannelOption


class FieldWidget(QWidget):
    """Adapter wrapping one or more Qt widgets with a uniform field API.

    Each subclass owns its inner widget(s) and forwards their change
    signal to ``valueChanged``. ``ModelForm`` only ever calls these
    methods; it does not poke at the inner widgets directly."""

    valueChanged = Signal()  # noqa: N815 - Qt signal naming convention

    def value(self) -> Any:
        """Current value held by this widget, coerced to the model-side type."""
        raise NotImplementedError

    def set_value(self, v: Any) -> None:  # pragma: no cover - abstract
        """Set this widget's value from a model-side value."""
        raise NotImplementedError

    def set_error(self, msg: str | None) -> None:
        """Display ``message`` as the widget's inline error (or clear if ``None``)."""
        if msg:
            self.setStyleSheet(_ERROR_STYLE)
            self.setToolTip(msg)
        else:
            self.setStyleSheet("")
            self.setToolTip(self._description or "")

    def show_errors(self, errors: Mapping[tuple[str | int, ...], str]) -> None:
        """Paint the errors addressed to this field, or clear them if none.

        Paths are relative to this field: ``()`` is the field itself,
        anything longer points inside it (a tuple row, a nested model's
        sub-field). A widget without sub-fields of its own shows every
        message on itself; :class:`_NestedModelField` routes them down.
        """
        self.set_error("\n".join(errors.values()) or None)

    def set_channel_options(self, options: Sequence[ChannelOption]) -> None:
        """Offer ``options`` to a field that names channels; no-op elsewhere."""

    _description: str = ""
