"""``SaveAsDialog`` — operator-facing source-layout picker.

Save As never silently inlines or extracts. This dialog forces the
operator to choose one of the four canonical layouts up front:

1. Experiment YAML + external hardware TOML (recommended default).
2. Experiment TOML + external hardware TOML.
3. Single inline experiment YAML.
4. Single inline experiment TOML.

Method layout (none / inline / external) is preserved from the current
document — :class:`FilesSection` is the right place to change that —
and the dialog refuses to commit until the chosen paths point at
write-targets.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from capa.config import ConfigDocument, SourceLayout

_LayoutChoice = Literal["yaml_ext", "toml_ext", "yaml_inline", "toml_inline"]


class SaveAsDialog(QDialog):
    """Modal source-layout chooser used by ``SetupTab._on_save_as``."""

    def __init__(self, *, document: ConfigDocument, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Save Setup As")
        self.setModal(True)
        self._document = document
        self._chosen_layout: SourceLayout | None = None

        outer = QVBoxLayout(self)

        outer.addWidget(QLabel("<b>Source layout</b>", self))
        self._yaml_ext = QRadioButton(
            "Experiment YAML + external hardware TOML  (recommended)", self
        )
        self._toml_ext = QRadioButton("Experiment TOML + external hardware TOML", self)
        self._yaml_inline = QRadioButton("Single inline experiment YAML", self)
        self._toml_inline = QRadioButton("Single inline experiment TOML", self)
        self._group = QButtonGroup(self)
        self._group.addButton(self._yaml_ext, 0)
        self._group.addButton(self._toml_ext, 1)
        self._group.addButton(self._yaml_inline, 2)
        self._group.addButton(self._toml_inline, 3)
        self._yaml_ext.setChecked(True)
        for btn in (self._yaml_ext, self._toml_ext, self._yaml_inline, self._toml_inline):
            outer.addWidget(btn)
            btn.toggled.connect(self._on_layout_changed)

        outer.addWidget(QLabel("<b>Paths</b>", self))
        form = QFormLayout()
        self._experiment_edit, exp_row = _path_row(self, "experiment")
        self._hardware_edit, hw_row = _path_row(self, "hardware")
        form.addRow("Experiment:", exp_row)
        form.addRow("Hardware:", hw_row)
        outer.addLayout(form)

        # Seed paths from the current document where available so a
        # plain Save As against an already-loaded draft pre-fills the
        # likely answers.
        if document.experiment_path is not None:
            self._experiment_edit.setText(str(document.experiment_path))
        if document.hardware_path is not None:
            self._hardware_edit.setText(str(document.hardware_path))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        self._on_layout_changed()  # initial enable-state for hardware row

    # -- API ----------------------------------------------------------------

    def chosen_layout(self) -> SourceLayout | None:
        return self._chosen_layout

    # -- internals ----------------------------------------------------------

    def _layout_choice(self) -> _LayoutChoice:
        if self._toml_ext.isChecked():
            return "toml_ext"
        if self._yaml_inline.isChecked():
            return "yaml_inline"
        if self._toml_inline.isChecked():
            return "toml_inline"
        return "yaml_ext"

    def _on_layout_changed(self) -> None:
        # The hardware path field is disabled for inline layouts because
        # the hardware payload goes into the experiment file.
        inline = self._layout_choice() in ("yaml_inline", "toml_inline")
        self._hardware_edit.setEnabled(not inline)

    def _on_accept(self) -> None:
        choice = self._layout_choice()
        exp_text = self._experiment_edit.text().strip()
        if not exp_text:
            QMessageBox.warning(
                self, "Experiment path required", "Choose where to save the experiment file."
            )
            return
        exp_path = Path(exp_text).resolve()
        if choice in ("yaml_ext", "yaml_inline") and exp_path.suffix.lower() not in (
            ".yaml",
            ".yml",
        ):
            exp_path = exp_path.with_suffix(".yaml")
        if choice in ("toml_ext", "toml_inline") and exp_path.suffix.lower() != ".toml":
            exp_path = exp_path.with_suffix(".toml")

        hardware_path: Path | None = None
        hardware_mode: Literal["external", "inline"] = "inline"
        hardware_format: Literal["toml"] | None = None
        if choice in ("yaml_ext", "toml_ext"):
            hw_text = self._hardware_edit.text().strip()
            if not hw_text:
                QMessageBox.warning(
                    self,
                    "Hardware path required",
                    "External hardware layout selected — choose a TOML target for the hardware file.",
                )
                return
            hardware_path = Path(hw_text).resolve()
            if hardware_path.suffix.lower() != ".toml":
                hardware_path = hardware_path.with_suffix(".toml")
            hardware_mode = "external"
            hardware_format = "toml"

        exp_format: Literal["yaml", "toml"] = (
            "yaml" if choice in ("yaml_ext", "yaml_inline") else "toml"
        )

        self._chosen_layout = SourceLayout(
            experiment_path=exp_path,
            experiment_format=exp_format,
            hardware_path=hardware_path,
            hardware_format=hardware_format,
            hardware_mode=hardware_mode,
            # Method layout: preserve the loaded document's method shape.
            # FilesSection is the place to change it.
            method_path=self._document.method_path,
            method_format=self._document.method_format,
            method_mode=self._document.method_mode,
        )
        self.accept()


def _path_row(parent: QWidget, kind: str) -> tuple[QLineEdit, QWidget]:
    container = QWidget(parent)
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    edit = QLineEdit(container)
    layout.addWidget(edit)
    browse = QPushButton("Browse…", container)
    layout.addWidget(browse)

    def _on_browse() -> None:
        if kind == "experiment":
            chosen, _ = QFileDialog.getSaveFileName(
                container,
                "Save experiment as",
                edit.text(),
                "Config (*.yaml *.yml *.toml);;All files (*)",
            )
        else:
            chosen, _ = QFileDialog.getSaveFileName(
                container,
                f"Save {kind} as",
                edit.text(),
                "TOML (*.toml);;All files (*)",
            )
        if chosen:
            edit.setText(chosen)

    browse.clicked.connect(_on_browse)
    return edit, container


__all__ = ["SaveAsDialog"]
