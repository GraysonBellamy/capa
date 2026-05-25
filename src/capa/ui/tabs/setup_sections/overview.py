"""Overview section — read-only operational dashboard.

A landing pane that summarises the loaded setup in answer to "is this
rig set up correctly for what I'm running?" without making the operator
click through every tab. The CAPA-mapping readiness chips are backed
by the curated profile editor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
)

from capa.config.capa_profile import (
    CAPA_OPTIONAL_GROUPS,
    CAPA_REQUIRED_GROUPS,
    current_capa_mappings,
)
from capa.ui.tabs.setup_sections._base import SectionWidget

if TYPE_CHECKING:
    from capa.ui.tabs.setup_state import SetupDraft


class OverviewSection(SectionWidget):
    """Read-only summary of the current draft."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._draft: SetupDraft | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        title = QLabel("Overview", self)
        title.setStyleSheet("font-size: 14pt; font-weight: 600;")
        outer.addWidget(title)

        self._source_box = _bordered(self, "Source")
        self._source_form = QFormLayout()
        self._source_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._source_box.layout().addLayout(self._source_form)  # type: ignore[union-attr]
        self._experiment_path = QLabel("—", self)
        self._hardware_path = QLabel("—", self)
        self._method_path = QLabel("—", self)
        self._source_form.addRow("Experiment:", self._experiment_path)
        self._source_form.addRow("Hardware:", self._hardware_path)
        self._source_form.addRow("Method:", self._method_path)
        outer.addWidget(self._source_box)

        self._run_box = _bordered(self, "Run recipe")
        self._run_form = QFormLayout()
        self._run_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._run_box.layout().addLayout(self._run_form)  # type: ignore[union-attr]
        self._procedure = QLabel("—", self)
        self._profile = QLabel("—", self)
        self._operator = QLabel("—", self)
        self._sample = QLabel("—", self)
        self._run_form.addRow("Procedure:", self._procedure)
        self._run_form.addRow("Profile:", self._profile)
        self._run_form.addRow("Operator:", self._operator)
        self._run_form.addRow("Sample:", self._sample)
        outer.addWidget(self._run_box)

        self._hardware_box = _bordered(self, "Hardware")
        self._hardware_form = QFormLayout()
        self._hardware_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._hardware_box.layout().addLayout(self._hardware_form)  # type: ignore[union-attr]
        self._counts = QLabel("—", self)
        self._hardware_form.addRow("Counts:", self._counts)
        outer.addWidget(self._hardware_box)

        # CAPA mappings — one line per required+optional group, with the
        # currently-bound channel and a ✓/⚠/✗/– chip. Refilled on every
        # refresh; rows that don't change are cheap to recompose.
        self._capa_box = _bordered(self, "CAPA mappings")
        self._capa_form = QFormLayout()
        self._capa_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._capa_box.layout().addLayout(self._capa_form)  # type: ignore[union-attr]
        outer.addWidget(self._capa_box)

        self._validation = QLabel("Validation: (not yet validated)", self)
        self._validation.setStyleSheet("color: #555;")
        outer.addWidget(self._validation)

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
        doc = self._draft.document

        self._experiment_path.setText(_format_path(doc.experiment_path))
        hw_path_text = _format_path(doc.hardware_path)
        if doc.hardware_mode == "inline":
            hw_path_text = "(inline in experiment file)"
        self._hardware_path.setText(hw_path_text)
        if doc.method_mode == "none":
            self._method_path.setText("(no method — free run)")
        elif doc.method_mode == "inline":
            self._method_path.setText("(inline in experiment file)")
        else:
            self._method_path.setText(_format_path(doc.method_path))

        # Run recipe — best effort from the experiment payload (the
        # validated config may be ``None`` while edits are mid-flight).
        exp = doc.experiment_payload
        procedure = exp.get("procedure")
        if isinstance(procedure, dict):
            proc_id = procedure.get("id", "—")
            proc_ver = procedure.get("version", "")
            self._procedure.setText(f"{proc_id} {proc_ver}".strip() if proc_ver else str(proc_id))
        else:
            self._procedure.setText("—")

        profile = exp.get("domain_profile")
        if isinstance(profile, dict):
            self._profile.setText(str(profile.get("id", "—")))
        else:
            self._profile.setText("—")

        operator = exp.get("operator")
        if isinstance(operator, dict):
            op_id = operator.get("id", "—")
            op_name = operator.get("display_name", "")
            self._operator.setText(f"{op_id} ({op_name})" if op_name else str(op_id))
        else:
            self._operator.setText("—")

        sample = exp.get("sample")
        if isinstance(sample, dict):
            parts = [str(sample.get("id", "—"))]
            mat = sample.get("material")
            if mat:
                parts.append(str(mat))
            self._sample.setText(" · ".join(parts))
        else:
            self._sample.setText("—")

        # Hardware counts read directly off the (raw) hardware payload
        # so the dashboard is meaningful even before the first validate.
        hw = doc.hardware_payload
        devices = hw.get("devices", []) if isinstance(hw, dict) else []
        channels = hw.get("channels", []) if isinstance(hw, dict) else []
        cameras = hw.get("cameras", []) if isinstance(hw, dict) else []
        self._counts.setText(
            f"{len(devices)} device(s) · {len(channels)} channel(s) · {len(cameras)} camera(s)"
        )

        # CAPA mappings — clear and rebuild rows from the current
        # channel list. Skipped entirely when the profile isn't CAPA
        # pyrolysis (the section just shows "—") so non-CAPA configs
        # don't sprout a confusing block.
        self._refresh_capa_mappings()

        # Validation snapshot.
        problems = self._draft.problems
        if not problems:
            self._validation.setText("Validation: ✓ no problems")
            self._validation.setStyleSheet("color: #2a7;")
        else:
            errs = sum(1 for p in problems if p.severity == "error")
            warns = sum(1 for p in problems if p.severity == "warning")
            self._validation.setText(f"Validation: {errs} error(s), {warns} warning(s)")
            self._validation.setStyleSheet("color: #b33;" if errs else "color: #b80;")

    def _refresh_capa_mappings(self) -> None:
        # Wipe any existing rows; the form is rebuilt on every refresh
        # so toggling between CAPA / non-CAPA configs doesn't leave
        # stale rows behind.
        while self._capa_form.rowCount() > 0:
            self._capa_form.removeRow(0)

        if self._draft is None:
            return

        profile = self._draft.document.experiment_payload.get("domain_profile") or {}
        profile_id = profile.get("id") if isinstance(profile, dict) else None
        if profile_id != "capa.profiles.capa_pyrolysis":
            placeholder = QLabel("(no CAPA profile loaded)", self)
            placeholder.setStyleSheet("color: #888;")
            self._capa_form.addRow("Profile:", placeholder)
            return

        channels = self._draft.document.hardware_payload.get("channels") or []
        if not isinstance(channels, list):
            channels = []
        mappings = current_capa_mappings(channels)

        for group in CAPA_REQUIRED_GROUPS:
            owners = mappings.get(group) or []
            chip, text = ("✓", " · ".join(owners)) if owners else ("✗", "(no channel mapped)")
            colour = "#2a7" if owners else "#b33"
            value = QLabel(f"{chip}  {text}", self)
            value.setStyleSheet(f"color: {colour};")
            self._capa_form.addRow(f"{group} (required):", value)

        for group in CAPA_OPTIONAL_GROUPS:
            owners = mappings.get(group) or []
            if owners:
                value = QLabel(f"–  {' · '.join(owners)}", self)
                value.setStyleSheet("color: #555;")
            else:
                value = QLabel("(not mapped)", self)
                value.setStyleSheet("color: #888;")
            self._capa_form.addRow(f"{group} (optional):", value)


def _format_path(path: Any) -> str:
    if path is None:
        return "(unset)"
    return str(path)


def _bordered(parent: Any, title: str) -> QFrame:
    """A titled border frame holding an inner ``QVBoxLayout``."""
    frame = QFrame(parent)
    frame.setFrameShape(QFrame.Shape.StyledPanel)
    frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    box = QVBoxLayout(frame)
    box.setContentsMargins(8, 8, 8, 8)
    box.setSpacing(6)
    header = QLabel(title, frame)
    header.setStyleSheet("font-weight: 600;")
    box.addWidget(header)
    return frame


__all__ = ["OverviewSection"]
