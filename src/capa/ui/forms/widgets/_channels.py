"""Channel-name pickers — editable dropdowns over the loaded hardware's channels.

A pydantic field opts in with ``Field(json_schema_extra={"capa_widget": ...})``:

* ``"command_channel"`` — lists only the channels a method step can
  drive (setpoints, MFC flows, analog/digital outputs).
* ``"channel"`` — lists every channel (e.g. an end-condition's input).

A ``str`` field renders as one picker; a ``dict[str, float]`` field gets
a picker for each key. Forms carry no hardware of their own: the owner
pushes the current list via :meth:`ModelForm.set_channel_options`. The
combo stays editable, so a name the list doesn't hold — no hardware
loaded yet, or a channel kind the filter doesn't anticipate — can still
be typed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import QComboBox, QCompleter, QHBoxLayout, QWidget

from capa.ui.forms.widgets._base import FieldWidget

ChannelRole = Literal["command", "any"]

CHANNEL_WIDGET_ROLES: dict[str, ChannelRole] = {
    "command_channel": "command",
    "channel": "any",
}
"""``capa_widget`` id → which channels the picker lists."""

COMMAND_KINDS: frozenset[str] = frozenset({"setpoint", "mfc_flow", "ao", "do"})
"""Channel kinds a method step can command. ``mfc_flow`` is in because a
purge MFC is commonly bound only through its flow channel."""


@dataclass(frozen=True)
class ChannelOption:
    """One channel offered by a picker."""

    name: str
    kind: str | None = None
    unit: str | None = None


def channel_options_from_hardware(hardware: Mapping[str, Any]) -> tuple[ChannelOption, ...]:
    """Channel options from a hardware payload, in declaration order.

    Reads the raw draft payload rather than a validated profile so the
    pickers keep working while the Setup draft has unrelated errors.
    Rows without a usable name are skipped.
    """
    channels = hardware.get("channels")
    if not isinstance(channels, Sequence) or isinstance(channels, str):
        return ()
    options: list[ChannelOption] = []
    for row in channels:
        if not isinstance(row, Mapping):
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name:
            continue
        kind = row.get("kind")
        unit = row.get("unit")
        options.append(
            ChannelOption(
                name=name,
                kind=None if kind is None else str(kind),
                unit=None if unit is None else str(unit),
            )
        )
    return tuple(options)


class ChannelCombo(QComboBox):
    """Editable combo listing channel names for one :data:`ChannelRole`.

    Typing filters the list (case-insensitive substring match); each
    item's tooltip shows the channel's kind and unit.
    """

    def __init__(self, *, role: ChannelRole, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._role = role
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        completer = self.completer()
        if completer is not None:
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)

    def set_options(self, options: Sequence[ChannelOption]) -> None:
        """Replace the listed channels, keeping the typed name."""
        text = self.currentText()
        with QSignalBlocker(self):
            self.clear()
            for option in options:
                if self._role == "command" and option.kind not in COMMAND_KINDS:
                    continue
                self.addItem(option.name)
                details = " · ".join(part for part in (option.kind, option.unit) if part)
                if details:
                    self.setItemData(self.count() - 1, details, Qt.ItemDataRole.ToolTipRole)
            self.setEditText(text)


class _ChannelField(FieldWidget):
    """``str`` field holding a channel name."""

    def __init__(self, *, role: ChannelRole, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._combo = ChannelCombo(role=role, parent=self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._combo)
        self._combo.editTextChanged.connect(self.valueChanged)

    def value(self) -> str:
        """Current value held by this widget, coerced to the model-side type."""
        return self._combo.currentText()

    def set_value(self, v: Any) -> None:
        """Set this widget's value from a model-side value."""
        with QSignalBlocker(self._combo):
            self._combo.setEditText("" if v is None else str(v))

    def set_channel_options(self, options: Sequence[ChannelOption]) -> None:
        """List ``options`` in the dropdown."""
        self._combo.set_options(options)


__all__ = [
    "CHANNEL_WIDGET_ROLES",
    "COMMAND_KINDS",
    "ChannelCombo",
    "ChannelOption",
    "ChannelRole",
    "channel_options_from_hardware",
]
