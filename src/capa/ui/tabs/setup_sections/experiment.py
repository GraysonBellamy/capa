"""Experiment section — operator / sample / tags / custom.

A single auto-form over a small view-model that mirrors the
operator-editable slice of :class:`ExperimentConfig`. The section
returns its current values as a payload dict; the Setup tab merges
them into ``document.experiment_payload`` and re-validates.

When the experiment carries the CAPA profile, ``sample`` belongs to the
CAPA Profile section (it is filled from the profile's specimen), so the
sample fields here are read-only and left out of the payload.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from capa.config.capa_profile import is_capa_profile
from capa.experiment.config import OperatorRef, SampleInfo
from capa.ui.forms import build_form
from capa.ui.tabs.setup_sections._base import SectionWidget

if TYPE_CHECKING:
    from capa.ui.tabs.setup_state import SetupDraft


class _ExperimentMetadataView(BaseModel):
    """View model for the Experiment section's auto-form.

    Mirrors only the editable top-level fields the section is
    responsible for: operator, sample, tags, custom.
    Everything else (hardware, method, procedure, domain_profile,
    storage, safety) lives in its own section.
    """

    model_config = ConfigDict(extra="forbid")

    operator: OperatorRef
    sample: SampleInfo = Field(default_factory=lambda: SampleInfo(id=""))
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
    "tags",
    "custom",
)


class ExperimentSection(SectionWidget):
    """Operator / sample / tags / custom editor."""

    editSectionRequested = Signal(str)  # noqa: N815 — Qt signal naming convention
    """Section id the operator asked to jump to (``"capa_profile"`` from
    the read-only sample notice)."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._draft: SetupDraft | None = None
        self._suppress_signals = False
        self._sample_from_profile = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        title = QLabel("Experiment", self)
        title.setStyleSheet("font-size: 14pt; font-weight: 600;")
        outer.addWidget(title)

        self._profile_notice = QWidget(self)
        notice_row = QHBoxLayout(self._profile_notice)
        notice_row.setContentsMargins(0, 0, 0, 0)
        notice_label = QLabel(
            "The sample is filled from the CAPA profile's specimen and is read-only here.",
            self._profile_notice,
        )
        notice_label.setWordWrap(True)
        notice_label.setStyleSheet("color: #555;")
        notice_row.addWidget(notice_label, stretch=1)
        edit_btn = QPushButton("Edit specimen", self._profile_notice)
        edit_btn.clicked.connect(lambda: self.editSectionRequested.emit("capa_profile"))
        notice_row.addWidget(edit_btn, alignment=Qt.AlignmentFlag.AlignRight)
        self._profile_notice.setVisible(False)
        outer.addWidget(self._profile_notice)

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
            self._form.set_values(initial, replace=True)
        finally:
            self._suppress_signals = False
        self._sample_from_profile = is_capa_profile(payload)
        self._profile_notice.setVisible(self._sample_from_profile)
        sample_widget = self._form.field_widget("sample")
        if sample_widget is not None:
            sample_widget.setEnabled(not self._sample_from_profile)

    def payload(self) -> dict[str, object]:
        """Return only the keys this section owns, in canonical shape."""
        owned = _OWNED_KEYS
        if self._sample_from_profile:
            owned = tuple(key for key in owned if key != "sample")
        return {key: value for key, value in self._form.values().items() if key in owned}

    # -- slots --------------------------------------------------------------

    def _on_form_changed(self) -> None:
        if self._suppress_signals:
            return
        self.valuesChanged.emit()


__all__ = ["ExperimentSection"]
