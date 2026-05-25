"""Files section — source-layout editor.

Edits the on-disk shape of the draft: which file the experiment lives
in, whether hardware/method are inline or external, and where their
files sit. Distinct from the other sections in that it manipulates
:class:`ConfigDocument` attributes rather than payload slices.

Two transition actions live here too:

* **Extract** — convert an inline hardware block into an external TOML.
  Records the new path; the actual write happens on Save.
* **Inline** — bring an external file's contents inside the experiment
  payload. Drops the path; the next Save writes a single file.

The buttons make the transitions explicit: Save never silently
changes inline vs external.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from capa.ui.tabs.setup_sections._base import SectionWidget

if TYPE_CHECKING:
    from capa.ui.tabs.setup_state import SetupDraft


class FilesSection(SectionWidget):
    """Source-layout editor.

    Emits :attr:`valuesChanged` whenever the operator changes a mode or
    path; the Setup tab uses that to mark the draft dirty. Method-ref
    changes additionally fire :attr:`methodRefChanged` so the
    :class:`DocumentCoordinator` can re-sync the Method tab.
    """

    methodRefChanged = Signal()  # noqa: N815 — Qt signal naming convention
    """Fires when the method mode or method path changes — the
    :class:`DocumentCoordinator` listens for this to swap the Method
    tab's loaded method."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._draft: SetupDraft | None = None
        self._suppress_signals = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        title = QLabel("Files", self)
        title.setStyleSheet("font-size: 14pt; font-weight: 600;")
        outer.addWidget(title)

        # Experiment file.
        exp_frame, exp_box = _bordered(self, "Experiment")
        exp_form = QFormLayout()
        exp_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        exp_box.addLayout(exp_form)
        self._experiment_path_edit, exp_row = _path_row(self, "experiment")
        self._experiment_path_edit.textChanged.connect(self._on_experiment_path_changed)
        exp_form.addRow("Path:", exp_row)
        self._experiment_format_label = QLabel("—", self)
        exp_form.addRow("Format:", self._experiment_format_label)
        outer.addWidget(exp_frame)

        # Hardware file.
        hw_frame, hw_box = _bordered(self, "Hardware")
        self._hardware_external = QRadioButton("External file", self)
        self._hardware_inline = QRadioButton("Inline (in experiment)", self)
        self._hardware_mode_group = QButtonGroup(self)
        self._hardware_mode_group.addButton(self._hardware_external, 0)
        self._hardware_mode_group.addButton(self._hardware_inline, 1)
        self._hardware_external.setChecked(True)
        hw_mode_row = QHBoxLayout()
        hw_mode_row.addWidget(self._hardware_external)
        hw_mode_row.addWidget(self._hardware_inline)
        hw_mode_row.addStretch(1)
        hw_box.addLayout(hw_mode_row)
        self._hardware_external.toggled.connect(self._on_hardware_mode_changed)

        hw_form = QFormLayout()
        hw_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        hw_box.addLayout(hw_form)
        self._hardware_path_edit, hw_row = _path_row(self, "hardware")
        self._hardware_path_edit.textChanged.connect(self._on_hardware_path_changed)
        hw_form.addRow("Path:", hw_row)
        hw_actions = QHBoxLayout()
        self._hardware_extract_btn = QPushButton("Extract to file…", self)
        self._hardware_extract_btn.clicked.connect(self._on_extract_hardware)
        self._hardware_inline_btn = QPushButton("Inline into experiment", self)
        self._hardware_inline_btn.clicked.connect(self._on_inline_hardware)
        hw_actions.addWidget(self._hardware_extract_btn)
        hw_actions.addWidget(self._hardware_inline_btn)
        hw_actions.addStretch(1)
        hw_box.addLayout(hw_actions)
        outer.addWidget(hw_frame)

        # Method file.
        m_frame, m_box = _bordered(self, "Method")
        self._method_external = QRadioButton("External file", self)
        self._method_inline = QRadioButton("Inline (in experiment)", self)
        self._method_none = QRadioButton("None (free run)", self)
        self._method_mode_group = QButtonGroup(self)
        self._method_mode_group.addButton(self._method_external, 0)
        self._method_mode_group.addButton(self._method_inline, 1)
        self._method_mode_group.addButton(self._method_none, 2)
        self._method_none.setChecked(True)
        m_mode_row = QHBoxLayout()
        m_mode_row.addWidget(self._method_external)
        m_mode_row.addWidget(self._method_inline)
        m_mode_row.addWidget(self._method_none)
        m_mode_row.addStretch(1)
        m_box.addLayout(m_mode_row)
        self._method_external.toggled.connect(self._on_method_mode_changed)
        self._method_inline.toggled.connect(self._on_method_mode_changed)
        self._method_none.toggled.connect(self._on_method_mode_changed)

        m_form = QFormLayout()
        m_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        m_box.addLayout(m_form)
        self._method_path_edit, m_row = _path_row(self, "method")
        self._method_path_edit.textChanged.connect(self._on_method_path_changed)
        m_form.addRow("Path:", m_row)
        self._method_status = QLabel("(no method)", self)
        m_form.addRow("Status:", self._method_status)
        outer.addWidget(m_frame)

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
        self._suppress_signals = True
        try:
            self._experiment_path_edit.setText(
                str(doc.experiment_path) if doc.experiment_path else ""
            )
            self._experiment_format_label.setText(doc.experiment_format or "(unset)")

            if doc.hardware_mode == "inline":
                self._hardware_inline.setChecked(True)
                self._hardware_path_edit.setEnabled(False)
                self._hardware_path_edit.setText("")
                self._hardware_extract_btn.setEnabled(True)
                self._hardware_inline_btn.setEnabled(False)
            else:
                self._hardware_external.setChecked(True)
                self._hardware_path_edit.setEnabled(True)
                self._hardware_path_edit.setText(
                    str(doc.hardware_path) if doc.hardware_path else ""
                )
                self._hardware_extract_btn.setEnabled(False)
                self._hardware_inline_btn.setEnabled(True)

            if doc.method_mode == "external":
                self._method_external.setChecked(True)
                self._method_path_edit.setEnabled(True)
                self._method_path_edit.setText(str(doc.method_path) if doc.method_path else "")
                self._method_status.setText(
                    f"✓ attached — {doc.method_path.name}" if doc.method_path else "(path not set)"
                )
            elif doc.method_mode == "inline":
                self._method_inline.setChecked(True)
                self._method_path_edit.setEnabled(False)
                self._method_path_edit.setText("")
                self._method_status.setText("inline" if doc.method_payload else "inline (empty)")
            else:
                self._method_none.setChecked(True)
                self._method_path_edit.setEnabled(False)
                self._method_path_edit.setText("")
                self._method_status.setText("(no method)")
        finally:
            self._suppress_signals = False

    # -- slots --------------------------------------------------------------

    def _on_experiment_path_changed(self, text: str) -> None:
        if self._suppress_signals or self._draft is None:
            return
        path_text = text.strip()
        self._draft.document.experiment_path = Path(path_text).resolve() if path_text else None
        # Re-detect format from suffix; preserves YAML vs TOML across edits.
        if self._draft.document.experiment_path is not None:
            suffix = self._draft.document.experiment_path.suffix.lower()
            if suffix in (".yaml", ".yml"):
                self._draft.document.experiment_format = "yaml"
            elif suffix == ".toml":
                self._draft.document.experiment_format = "toml"
            self._experiment_format_label.setText(
                self._draft.document.experiment_format or "(unset)"
            )
        self.valuesChanged.emit()

    def _on_hardware_mode_changed(self, _checked: bool) -> None:
        if self._suppress_signals or self._draft is None:
            return
        doc = self._draft.document
        if self._hardware_inline.isChecked() and doc.hardware_mode == "external":
            # Pull external file contents into the inline payload and
            # drop the path. Mirrors ConfigDocument.inline_hardware_from_file
            # but skips the "already inline" check so radio toggling is
            # idempotent.
            doc.hardware_mode = "inline"
            doc.hardware_path = None
            doc.hardware_format = None
            self.refresh()
            self.valuesChanged.emit()
        elif self._hardware_external.isChecked() and doc.hardware_mode == "inline":
            # Switching inline → external without a path is allowed; Save
            # will refuse with a clear error until a path is set. The
            # Extract button is the ergonomic path.
            doc.hardware_mode = "external"
            doc.hardware_format = "toml"
            self.refresh()
            self.valuesChanged.emit()

    def _on_hardware_path_changed(self, text: str) -> None:
        if self._suppress_signals or self._draft is None:
            return
        path_text = text.strip()
        self._draft.document.hardware_path = Path(path_text).resolve() if path_text else None
        self.valuesChanged.emit()

    def _on_method_mode_changed(self, _checked: bool) -> None:
        if self._suppress_signals or self._draft is None:
            return
        doc = self._draft.document
        if self._method_external.isChecked():
            if doc.method_mode != "external":
                doc.method_mode = "external"
                doc.method_format = "toml"
                if doc.method_payload is None:
                    doc.method_payload = {}
                self.refresh()
                self.valuesChanged.emit()
                self.methodRefChanged.emit()
        elif self._method_inline.isChecked():
            if doc.method_mode != "inline":
                doc.method_mode = "inline"
                doc.method_path = None
                doc.method_format = None
                if doc.method_payload is None:
                    doc.method_payload = {}
                self.refresh()
                self.valuesChanged.emit()
                self.methodRefChanged.emit()
        elif self._method_none.isChecked() and doc.method_mode != "none":
            doc.method_mode = "none"
            doc.method_path = None
            doc.method_format = None
            doc.method_payload = None
            self.refresh()
            self.valuesChanged.emit()
            self.methodRefChanged.emit()

    def _on_method_path_changed(self, text: str) -> None:
        if self._suppress_signals or self._draft is None:
            return
        path_text = text.strip()
        new_path = Path(path_text).resolve() if path_text else None
        if new_path != self._draft.document.method_path:
            self._draft.document.method_path = new_path
            self.valuesChanged.emit()
            self.methodRefChanged.emit()

    def _on_extract_hardware(self) -> None:
        if self._draft is None:
            return
        if self._draft.document.hardware_mode != "inline":
            return
        # Suggest a sensible default location next to the experiment file.
        suggested = "hardware.toml"
        if self._draft.document.experiment_path is not None:
            suggested = str(self._draft.document.experiment_path.parent / "hardware.toml")
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Extract hardware to file", suggested, "TOML (*.toml)"
        )
        if not path_str:
            return
        path = Path(path_str)
        if path.suffix.lower() != ".toml":
            path = path.with_suffix(".toml")
        self._draft.document.extract_hardware_inline_to_file(path)
        self.refresh()
        self.valuesChanged.emit()

    def _on_inline_hardware(self) -> None:
        if self._draft is None:
            return
        if self._draft.document.hardware_mode != "external":
            return
        # Note: ConfigDocument.inline_hardware_from_file only flips the
        # mode; the payload is already loaded in memory (we did so at
        # load time), so no I/O needed here.
        self._draft.document.inline_hardware_from_file()
        self.refresh()
        self.valuesChanged.emit()


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _path_row(parent: QWidget, kind: str) -> tuple[QLineEdit, QWidget]:
    """A line-edit + Browse button row, returned as ``(edit, container)``."""
    container = QWidget(parent)
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    edit = QLineEdit(container)
    layout.addWidget(edit)
    browse = QPushButton("Browse…", container)
    layout.addWidget(browse)

    def _on_browse() -> None:
        if kind == "experiment":
            chosen, _ = QFileDialog.getOpenFileName(
                container,
                "Choose experiment file",
                edit.text(),
                "Config (*.yaml *.yml *.toml);;All files (*)",
            )
        else:
            chosen, _ = QFileDialog.getOpenFileName(
                container,
                f"Choose {kind} file",
                edit.text(),
                "TOML (*.toml);;All files (*)",
            )
        if chosen:
            edit.setText(chosen)

    browse.clicked.connect(_on_browse)
    return edit, container


def _bordered(parent: QWidget, title: str) -> tuple[QFrame, QVBoxLayout]:
    """Build a titled panel; return both the frame and its inner box.

    Callers that need to add layouts (forms, row layouts) want the
    concrete :class:`QVBoxLayout` rather than the union-typed
    :meth:`QFrame.layout()` result.
    """
    frame = QFrame(parent)
    frame.setFrameShape(QFrame.Shape.StyledPanel)
    frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    box = QVBoxLayout(frame)
    box.setContentsMargins(8, 8, 8, 8)
    box.setSpacing(6)
    header = QLabel(title, frame)
    header.setStyleSheet("font-weight: 600;")
    box.addWidget(header)
    return frame, box


__all__ = ["FilesSection"]
