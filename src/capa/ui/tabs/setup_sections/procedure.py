"""Procedure section — plugin-aware editor.

The section renders:

* A combobox over registered procedure ids.
* A version field (free-form string; PEP 440 is enforced at validate
  time).
* A nested auto-form against the chosen procedure's ``config_model``
  when one is available; a JSON-fallback editor when the plugin
  doesn't declare a model or fails to load.

Procedure discovery degrades gracefully: if
:class:`ProcedureRegistry.discover` raises, the section falls back to
a free-form text edit for ``procedure.id`` so the draft remains
editable.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from capa.ui.forms import build_form
from capa.ui.tabs.setup_sections._base import SectionWidget

if TYPE_CHECKING:
    from capa.ui.forms.from_model import ModelForm
    from capa.ui.tabs.setup_state import SetupDraft


class ProcedureSection(SectionWidget):
    """Procedure selector + config editor."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._draft: SetupDraft | None = None
        self._suppress_signals = False
        self._procedure_ids: tuple[str, ...] = ()
        self._config_models: dict[str, type[BaseModel] | None] = {}
        self._current_config_form: ModelForm | None = None
        self._fallback_editor: QPlainTextEdit | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        title = QLabel("Procedure", self)
        title.setStyleSheet("font-size: 14pt; font-weight: 600;")
        outer.addWidget(title)

        # Selector row.
        selector_form = QFormLayout()
        self._id_combo = QComboBox(self)
        self._id_combo.setEditable(True)
        # Editable so plugin-contributed ids the registry can't resolve
        # (broken plugins, lockfile-only entries) remain authorable.
        selector_form.addRow("ID:", self._id_combo)
        self._version_edit = QLineEdit(self)
        self._version_edit.setPlaceholderText("(any installed version)")
        selector_form.addRow("Version:", self._version_edit)
        self._description_label = QLabel("—", self)
        self._description_label.setWordWrap(True)
        self._description_label.setStyleSheet("color: #555;")
        selector_form.addRow("About:", self._description_label)
        outer.addLayout(selector_form)

        outer.addWidget(_separator(self))

        # Config sub-form container.
        config_header = QLabel("Procedure config", self)
        config_header.setStyleSheet("font-weight: 600;")
        outer.addWidget(config_header)
        self._config_container = QFrame(self)
        self._config_container.setFrameShape(QFrame.Shape.StyledPanel)
        self._config_container_layout = QVBoxLayout(self._config_container)
        self._config_container_layout.setContentsMargins(8, 8, 8, 8)
        self._config_placeholder = QLabel(
            "Choose a procedure to edit its configuration.", self._config_container
        )
        self._config_placeholder.setStyleSheet("color: #888;")
        self._config_container_layout.addWidget(self._config_placeholder)
        outer.addWidget(self._config_container)

        outer.addStretch(1)

        # Populate the combobox lazily (it may discover plugins that
        # imports add latency to) — but it's cheap enough at construction.
        self._populate_procedure_choices()

        # Wire signals AFTER the initial population so the combobox
        # change-on-fill doesn't fire valuesChanged from the empty state.
        self._id_combo.currentIndexChanged.connect(self._on_id_changed)
        self._id_combo.editTextChanged.connect(self._on_id_text_changed)
        self._version_edit.textChanged.connect(self._on_field_changed)

    # -- SectionWidget API --------------------------------------------------

    def set_draft(self, draft: SetupDraft) -> None:
        self._draft = draft
        self.refresh()

    def refresh(self) -> None:
        if self._draft is None:
            return
        proc = self._draft.document.experiment_payload.get("procedure")
        if not isinstance(proc, dict):
            proc = {}
        self._suppress_signals = True
        try:
            proc_id = str(proc.get("id", "") or "")
            self._set_combo_to(proc_id)
            self._version_edit.setText(str(proc.get("version", "") or ""))
            config = proc.get("config", {})
            self._rebuild_config_editor(proc_id, initial=config if isinstance(config, dict) else {})
        finally:
            self._suppress_signals = False
        self._update_description(proc_id)

    def payload(self) -> dict[str, object]:
        return {"procedure": self._current_procedure_dict()}

    # -- internals ----------------------------------------------------------

    def _populate_procedure_choices(self) -> None:
        ids, models = _discover_procedures()
        self._procedure_ids = ids
        self._config_models = models
        with QSignalBlocker(self._id_combo):
            self._id_combo.clear()
            for proc_id in ids:
                self._id_combo.addItem(proc_id)
            if not ids:
                # Plugin discovery failed or returned nothing.
                self._id_combo.setEditable(True)

    def _set_combo_to(self, proc_id: str) -> None:
        for i in range(self._id_combo.count()):
            if self._id_combo.itemText(i) == proc_id:
                self._id_combo.setCurrentIndex(i)
                return
        # Not in the registry — keep the editable line edit's text.
        self._id_combo.setEditText(proc_id)

    def _rebuild_config_editor(self, proc_id: str, *, initial: dict[str, Any]) -> None:
        # Clear out anything we previously installed.
        if self._current_config_form is not None:
            self._current_config_form.deleteLater()
            self._current_config_form = None
        if self._fallback_editor is not None:
            self._fallback_editor.deleteLater()
            self._fallback_editor = None
        self._config_placeholder.hide()

        model = self._config_models.get(proc_id)
        if model is not None:
            form = build_form(model, parent=self._config_container)
            if initial:
                with contextlib.suppress(Exception):
                    form.set_values(initial)
            form.valuesChanged.connect(self._on_field_changed)
            self._config_container_layout.addWidget(form)
            self._current_config_form = form
            return
        # Fallback — JSON-line editor so plugin-contributed configs the
        # registry can't introspect remain editable.
        editor = QPlainTextEdit(self._config_container)
        editor.setPlaceholderText("Procedure config (JSON object)")
        if initial:
            try:
                editor.setPlainText(json.dumps(initial, indent=2))
            except (TypeError, ValueError):
                editor.setPlainText(str(initial))
        editor.textChanged.connect(self._on_field_changed)
        editor.setMaximumHeight(220)
        editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._config_container_layout.addWidget(editor)
        self._fallback_editor = editor

    def _current_config_value(self) -> dict[str, Any]:
        if self._current_config_form is not None:
            return self._current_config_form.values()
        if self._fallback_editor is not None:
            text = self._fallback_editor.toPlainText().strip()
            if not text:
                return {}
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                return {}
            return value if isinstance(value, dict) else {}
        return {}

    def _current_procedure_dict(self) -> dict[str, Any]:
        proc_id = self._id_combo.currentText().strip()
        version = self._version_edit.text().strip()
        out: dict[str, Any] = {"id": proc_id, "config": self._current_config_value()}
        if version:
            out["version"] = version
        return out

    def _update_description(self, proc_id: str) -> None:
        if proc_id in self._procedure_ids:
            self._description_label.setText("Loaded from the procedure registry.")
            self._description_label.setStyleSheet("color: #2a7;")
        elif not proc_id:
            self._description_label.setText("—")
            self._description_label.setStyleSheet("color: #555;")
        else:
            self._description_label.setText(
                "Not in the local registry — config may not validate at run start."
            )
            self._description_label.setStyleSheet("color: #b80;")

    # -- slots --------------------------------------------------------------

    def _on_id_changed(self, _index: int) -> None:
        if self._suppress_signals:
            return
        proc_id = self._id_combo.currentText().strip()
        # Carry forward the current config dict where keys overlap with
        # the new model — mirrors the _DiscriminatedUnionField pattern so
        # the operator doesn't lose typing when switching plugins.
        current = self._current_config_value()
        self._rebuild_config_editor(proc_id, initial=current)
        self._update_description(proc_id)
        self.valuesChanged.emit()

    def _on_id_text_changed(self, _text: str) -> None:
        # The combobox is editable; a fully typed-out id should also
        # trigger reconfig if it matches a registered procedure.
        if self._suppress_signals:
            return
        proc_id = self._id_combo.currentText().strip()
        if proc_id in self._procedure_ids and self._config_models.get(proc_id) is not None:
            current = self._current_config_value()
            self._rebuild_config_editor(proc_id, initial=current)
        self._update_description(proc_id)
        self.valuesChanged.emit()

    def _on_field_changed(self) -> None:
        if self._suppress_signals:
            return
        self.valuesChanged.emit()


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _discover_procedures() -> tuple[tuple[str, ...], dict[str, type[BaseModel] | None]]:
    """Return ``(ids, config_models)`` from the procedure registry.

    Discovery failures are swallowed — a broken plugin must not break
    the editor. The returned tuple is empty when nothing was found.
    """
    try:
        from capa.core.plugins_runtime import ProcedureRegistry, resolve_mode  # noqa: PLC0415

        registry = ProcedureRegistry.discover(mode=resolve_mode())
        ids = tuple(registry.ids())
    except Exception:
        return ((), {})
    models: dict[str, type[BaseModel] | None] = {}
    for proc_id in ids:
        try:
            # Procedures expose ``config_model`` as a class attribute on
            # their registered factory. ``LoadedProcedure.cls`` is the
            # class; the registry's ``instantiate`` path validates against
            # ``config_model``. We only need the type for form generation.
            loaded = registry.get(proc_id)
            cls = loaded.cls if loaded is not None else None
            model = getattr(cls, "config_model", None)
            models[proc_id] = (
                model if isinstance(model, type) and issubclass(model, BaseModel) else None
            )
        except Exception:
            models[proc_id] = None
    return (ids, models)


def _separator(parent: QWidget) -> QFrame:
    line = QFrame(parent)
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


__all__ = ["ProcedureSection"]
