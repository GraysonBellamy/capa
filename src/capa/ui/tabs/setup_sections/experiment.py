"""Experiment section — operator / sample / tags / calibration / custom.

A single auto-form over a small view-model that mirrors the
operator-editable slice of :class:`ExperimentConfig`. The section
returns its current values as a payload dict; the Setup tab merges
them into ``document.experiment_payload`` and re-validates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field
from PySide6.QtWidgets import QLabel, QVBoxLayout

from capa.experiment.config import CalibrationSetRef, OperatorRef, SampleInfo
from capa.ui.forms import build_form
from capa.ui.tabs.setup_sections._base import SectionWidget

if TYPE_CHECKING:
    from capa.ui.tabs.setup_state import SetupDraft


class _ExperimentMetadataView(BaseModel):
    """View model for the Experiment section's auto-form.

    Mirrors only the editable top-level fields the section is
    responsible for: operator, sample, calibration_set, tags, custom.
    Everything else (hardware, method, procedure, domain_profile,
    storage, safety) lives in its own section.
    """

    model_config = ConfigDict(extra="forbid")

    operator: OperatorRef = Field(default_factory=lambda: OperatorRef(id=""))
    sample: SampleInfo = Field(default_factory=lambda: SampleInfo(id=""))
    calibration_set: CalibrationSetRef = Field(
        default_factory=lambda: CalibrationSetRef(name="default")
    )
    tags: tuple[str, ...] = Field(
        default_factory=tuple,
        json_schema_extra={
            "capa_group": "metadata",
            "capa_group_subtitle": "Tags and free-form custom fields",
        },
    )
    custom: dict[str, Any] = Field(
        default_factory=dict,
        json_schema_extra={"capa_group": "metadata"},
    )


_OWNED_KEYS: tuple[str, ...] = (
    "operator",
    "sample",
    "calibration_set",
    "tags",
    "custom",
)


class ExperimentSection(SectionWidget):
    """Operator / sample / tags / calibration / custom editor."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._draft: SetupDraft | None = None
        self._suppress_signals = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        title = QLabel("Experiment", self)
        title.setStyleSheet("font-size: 14pt; font-weight: 600;")
        outer.addWidget(title)

        self._form = build_form(_ExperimentMetadataView, parent=self)
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
        initial: dict[str, Any] = {}
        for key in _OWNED_KEYS:
            if key in payload:
                initial[key] = payload[key]
        self._suppress_signals = True
        try:
            self._form.set_values(initial)
        finally:
            self._suppress_signals = False

    def payload(self) -> dict[str, object]:
        """Return only the keys this section owns, in canonical shape."""
        return {key: value for key, value in self._form.values().items() if key in _OWNED_KEYS}

    # -- slots --------------------------------------------------------------

    def _on_form_changed(self) -> None:
        if self._suppress_signals:
            return
        self.valuesChanged.emit()


__all__ = ["ExperimentSection"]
