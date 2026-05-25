"""Storage section — auto-form over :class:`StoragePolicy`.

Plain ``build_form`` of the :class:`StoragePolicy` model. The
``bundle_root`` field is already annotated with a directory-picker hint
(``Field(json_schema_extra={"capa_path_mode": "dir"})``) so the form
generator renders the right widget.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtWidgets import QLabel, QVBoxLayout

from capa.experiment.config import StoragePolicy
from capa.ui.forms import build_form
from capa.ui.tabs.setup_sections._base import SectionWidget

if TYPE_CHECKING:
    from capa.ui.tabs.setup_state import SetupDraft


class StorageSection(SectionWidget):
    """Editor for :class:`StoragePolicy`."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._draft: SetupDraft | None = None
        self._suppress_signals = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        title = QLabel("Storage", self)
        title.setStyleSheet("font-size: 14pt; font-weight: 600;")
        outer.addWidget(title)

        self._form = build_form(StoragePolicy, parent=self)
        self._form.valuesChanged.connect(self._on_form_changed)
        outer.addWidget(self._form)

        outer.addStretch(1)

    # -- SectionWidget API --------------------------------------------------

    def set_draft(self, draft: SetupDraft) -> None:
        """Replace the in-progress draft."""
        self._draft = draft
        self.refresh()

    def refresh(self) -> None:
        """Recompute the form from the current draft."""
        if self._draft is None:
            return
        payload = self._draft.document.experiment_payload
        storage = payload.get("storage")
        if isinstance(storage, dict):
            self._suppress_signals = True
            try:
                self._form.set_values(storage)
            finally:
                self._suppress_signals = False

    def payload(self) -> dict[str, object]:
        """Return ``{"storage": {...}}`` for the Setup tab to merge."""
        return {"storage": self._form.values()}

    # -- slots --------------------------------------------------------------

    def _on_form_changed(self) -> None:
        if self._suppress_signals:
            return
        self.valuesChanged.emit()


__all__ = ["StorageSection"]
