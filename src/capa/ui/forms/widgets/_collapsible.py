""":class:`CollapsibleGroup` — header-button disclosure for rare-field groups.

Used by :class:`~capa.ui.forms.from_model.ModelForm` to render every
field group declared via ``Field(json_schema_extra={"capa_group": ...})``;
also usable directly by section widgets that need ad-hoc disclosure
outside the auto-form path.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


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
        """``True`` if this collapsible section is expanded."""
        return self._is_open

    def set_open(self, open_: bool) -> None:
        """Expand or collapse this collapsible section."""
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
