"""Cameras section — table + per-row CameraSpec editor.

Same shape as :mod:`capa.ui.tabs.setup_sections.devices` but
edits the broader :class:`~capa.devices.camera.base.CameraSpec`
envelope (kind, model_hint, serial, output_root, on_failure,
estimated_bps) alongside the per-adapter ``params`` sub-form. The
disk-fill estimate is recomputed from ``estimated_bps`` so the
operator sees the projected per-half-hour cost before they save.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSplitter,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from capa.devices.registry import (
    ADAPTERS,
    AdapterDescriptor,
    ensure_adapters_loaded,
)
from capa.ui.forms import build_form
from capa.ui.tabs.setup_sections._base import SectionWidget
from capa.ui.tabs.setup_sections._models import (
    fit_table_height,
    horizontal_header,
    unique_name,
)

if TYPE_CHECKING:
    from capa.ui.forms.from_model import ModelForm
    from capa.ui.tabs.setup_state import SetupDraft


# ---------------------------------------------------------------------------
# Table model.
# ---------------------------------------------------------------------------


class CameraTableModel(QAbstractTableModel):
    """List-of-dicts model over ``hardware_payload["cameras"]``."""

    camerasChanged = Signal()  # noqa: N815 — Qt signal naming convention

    HEADERS: tuple[str, ...] = ("name", "kind", "adapter", "estimated_bps")

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict[str, Any]] = []

    def cameras(self) -> list[dict[str, Any]]:
        """Tuple of camera entries managed by this section."""
        return [dict(row) for row in self._rows]

    def set_cameras(self, cameras: list[dict[str, Any]]) -> None:
        """Replace the section's camera list."""
        self.beginResetModel()
        self._rows = [dict(row) for row in cameras]
        self.endResetModel()

    def camera_at(self, row: int) -> dict[str, Any] | None:
        """Return the camera entry at the given row."""
        if 0 <= row < len(self._rows):
            return dict(self._rows[row])
        return None

    def update_camera(self, row: int, camera: dict[str, Any]) -> None:
        """Apply a partial update to one camera entry."""
        if not (0 <= row < len(self._rows)):
            return
        self._rows[row] = dict(camera)
        top_left = self.index(row, 0)
        bottom_right = self.index(row, len(self.HEADERS) - 1)
        self.dataChanged.emit(top_left, bottom_right)
        self.camerasChanged.emit()

    def add_camera(self, camera: dict[str, Any]) -> int:
        """Append a new camera entry to the section."""
        row = len(self._rows)
        self.beginInsertRows(QModelIndex(), row, row)
        self._rows.append(dict(camera))
        self.endInsertRows()
        self.camerasChanged.emit()
        return row

    def remove_camera(self, row: int) -> None:
        """Remove the camera entry at the given row."""
        if not (0 <= row < len(self._rows)):
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        self._rows.pop(row)
        self.endRemoveRows()
        self.camerasChanged.emit()

    # -- QAbstractTableModel -----------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        """Number of rows in the model. See :class:`PySide6.QtCore.QAbstractTableModel`."""
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        """Number of columns in the model. See :class:`PySide6.QtCore.QAbstractTableModel`."""
        if parent.isValid():
            return 0
        return len(self.HEADERS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        """Header label for the given section / orientation. See :class:`PySide6.QtCore.QAbstractItemModel`."""
        return horizontal_header(self.HEADERS, section, orientation, role)

    def data(  # type: ignore[override]
        self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        """Return the value at ``index`` for ``role``. See :class:`PySide6.QtCore.QAbstractItemModel`."""
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        row = index.row()
        rec = self._rows[row] if 0 <= row < len(self._rows) else None
        if rec is None:
            return None
        key = self.HEADERS[index.column()]
        value = rec.get(key, "")
        return str(value) if value is not None else ""


# ---------------------------------------------------------------------------
# Section widget.
# ---------------------------------------------------------------------------


def _camera_descriptors() -> list[AdapterDescriptor]:
    return [d for d in ADAPTERS.values() if d.family in ("camera_visible", "camera_ir")]


def _default_camera_for(descriptor: AdapterDescriptor, existing_names: list[str]) -> dict[str, Any]:
    kind = "visible" if descriptor.family == "camera_visible" else "ir"
    return {
        "name": unique_name(existing_names, kind),
        "adapter": descriptor.id,
        "kind": kind,
        "on_failure": "warn",
        "estimated_bps": 4_000_000,
        "params": dict(descriptor.default_params),
    }


def _human_disk_fill(estimated_bps: int, seconds: int) -> str:
    """Format ``bps × seconds`` as ``~MB / minutes``.

    Used by the detail pane's disk-fill estimate. Defaults
    to a 30-minute window because that's the typical CAPA run length;
    the operator sees the projected cost without leaving the editor.
    """
    bytes_total = int(estimated_bps) * seconds
    mb = bytes_total / (1024 * 1024)
    minutes = seconds // 60
    return f"~{mb:.1f} MB / {minutes} min"


class CamerasSection(SectionWidget):
    """Cameras table + detail editor."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        ensure_adapters_loaded()
        self._draft: SetupDraft | None = None
        self._suppress = False
        self._current_row: int = -1
        self._params_form: ModelForm | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        title = QLabel("Cameras", self)
        title.setStyleSheet("font-size: 14pt; font-weight: 600;")
        outer.addWidget(title)

        splitter = QSplitter(Qt.Orientation.Vertical, self)

        # -- Table region.
        table_region = QWidget(splitter)
        table_layout = QVBoxLayout(table_region)
        table_layout.setContentsMargins(0, 0, 0, 0)

        button_row = QHBoxLayout()
        self._add_btn = QToolButton(self)
        self._add_btn.setText("Add camera")
        self._add_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._add_menu = QMenu(self._add_btn)
        self._add_btn.setMenu(self._add_menu)
        self._duplicate_btn = QPushButton("Duplicate", self)
        self._remove_btn = QPushButton("Delete", self)
        button_row.addWidget(self._add_btn)
        button_row.addWidget(self._duplicate_btn)
        button_row.addWidget(self._remove_btn)
        button_row.addStretch(1)
        self._duplicate_btn.clicked.connect(self._on_duplicate)
        self._remove_btn.clicked.connect(self._on_remove)
        table_layout.addLayout(button_row)

        self._model = CameraTableModel()
        self._model.camerasChanged.connect(self._on_rows_changed)
        self._table = QTableView(self)
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        header = self._table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            header.setStretchLastSection(True)
        selection_model = self._table.selectionModel()
        if selection_model is not None:
            selection_model.selectionChanged.connect(self._on_row_changed)
        # Fit to row count — CAPA rigs run a small handful of cameras.
        self._model.rowsInserted.connect(lambda *_: self._update_table_height())
        self._model.rowsRemoved.connect(lambda *_: self._update_table_height())
        self._model.modelReset.connect(self._update_table_height)
        self._update_table_height()
        table_layout.addWidget(self._table)
        splitter.addWidget(table_region)

        # -- Detail region.
        self._detail = QWidget(splitter)
        detail_layout = QVBoxLayout(self._detail)
        detail_layout.setContentsMargins(0, 8, 0, 0)
        self._placeholder = QLabel("Select a camera to edit, or use Add camera ▾.", self._detail)
        self._placeholder.setStyleSheet("color: #888;")
        detail_layout.addWidget(self._placeholder)

        # Top-level CameraSpec editor.
        self._spec_widget = QWidget(self._detail)
        spec_form = QFormLayout(self._spec_widget)
        spec_form.setContentsMargins(0, 0, 0, 0)
        self._name_edit = QLineEdit(self._spec_widget)
        self._name_edit.textChanged.connect(self._on_name_changed)
        spec_form.addRow("Name:", self._name_edit)
        self._adapter_combo = QComboBox(self._spec_widget)
        self._adapter_combo.currentIndexChanged.connect(self._on_adapter_changed)
        spec_form.addRow("Adapter:", self._adapter_combo)
        self._kind_combo = QComboBox(self._spec_widget)
        for kind in ("visible", "ir"):
            self._kind_combo.addItem(kind, kind)
        self._kind_combo.currentIndexChanged.connect(self._on_kind_changed)
        spec_form.addRow("Kind:", self._kind_combo)
        self._model_hint_edit = QLineEdit(self._spec_widget)
        self._model_hint_edit.textChanged.connect(
            lambda t: self._on_top_field_changed("model_hint", t)
        )
        spec_form.addRow("Model hint:", self._model_hint_edit)
        self._serial_edit = QLineEdit(self._spec_widget)
        self._serial_edit.textChanged.connect(lambda t: self._on_top_field_changed("serial", t))
        spec_form.addRow("Serial:", self._serial_edit)
        self._output_root_edit = QLineEdit(self._spec_widget)
        self._output_root_edit.setPlaceholderText("blank = inside bundle")
        self._output_root_edit.textChanged.connect(
            lambda t: self._on_top_field_changed("output_root", t)
        )
        spec_form.addRow("Output root:", self._output_root_edit)
        self._on_failure_combo = QComboBox(self._spec_widget)
        for value in ("warn", "abort_run", "safe_shutdown"):
            self._on_failure_combo.addItem(value, value)
        self._on_failure_combo.currentIndexChanged.connect(self._on_failure_changed)
        spec_form.addRow("On failure:", self._on_failure_combo)
        self._estimated_bps_edit = QLineEdit(self._spec_widget)
        self._estimated_bps_edit.textChanged.connect(self._on_estimated_bps_changed)
        spec_form.addRow("Estimated bps:", self._estimated_bps_edit)
        self._disk_fill_label = QLabel("—", self._spec_widget)
        spec_form.addRow("Disk fill (30 min):", self._disk_fill_label)
        self._spec_widget.hide()
        detail_layout.addWidget(self._spec_widget)

        # Params form host.
        self._params_host = QWidget(self._detail)
        host_layout = QVBoxLayout(self._params_host)
        host_layout.setContentsMargins(0, 8, 0, 0)
        params_header = QLabel("Adapter params", self._params_host)
        params_header.setStyleSheet("font-weight: 600;")
        host_layout.addWidget(params_header)
        self._params_host.hide()
        detail_layout.addWidget(self._params_host)

        detail_layout.addStretch(1)
        splitter.addWidget(self._detail)
        # Table region claims its sizeHint; detail soaks up the rest.
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        outer.addWidget(splitter, stretch=1)

        self._rebuild_add_menu()

    # -- SectionWidget API --------------------------------------------------

    def set_draft(self, draft: SetupDraft) -> None:
        """Replace the in-progress draft."""
        self._draft = draft
        self.refresh()

    def refresh(self) -> None:
        """Recompute the form from the current draft."""
        if self._draft is None:
            return
        hw = self._draft.document.hardware_payload
        cams = hw.get("cameras") if isinstance(hw, dict) else None
        rows: list[dict[str, Any]] = []
        if isinstance(cams, list):
            for entry in cams:
                if isinstance(entry, dict):
                    rows.append(dict(entry))
        self._suppress = True
        try:
            self._model.set_cameras(rows)
        finally:
            self._suppress = False
        self._reset_detail()

    def payload(self) -> dict[str, object]:
        """Build the section's serialized payload from current widget state."""
        return {"cameras": self._model.cameras()}

    def _update_table_height(self) -> None:
        fit_table_height(self._table)

    # -- slots --------------------------------------------------------------

    def _on_rows_changed(self) -> None:
        if self._suppress:
            return
        self.valuesChanged.emit()

    def _on_duplicate(self) -> None:
        if self._current_row < 0:
            return
        src = self._model.camera_at(self._current_row)
        if src is None:
            return
        existing = [c.get("name", "") for c in self._model.cameras()]
        dup = dict(src)
        dup["name"] = unique_name(existing, f"{src.get('name', 'camera')}_copy")
        params = dup.get("params")
        if isinstance(params, dict):
            dup["params"] = dict(params)
        new_row = self._model.add_camera(dup)
        self._table.selectRow(new_row)

    def _on_remove(self) -> None:
        if self._current_row < 0:
            return
        self._model.remove_camera(self._current_row)
        self._reset_detail()

    def _on_row_changed(self) -> None:
        sel = self._table.selectionModel()
        rows = sel.selectedRows() if sel is not None else []
        if not rows:
            self._reset_detail()
            return
        row = rows[0].row()
        self._current_row = row
        camera = self._model.camera_at(row)
        if camera is None:
            self._reset_detail()
            return
        self._build_detail(camera)

    def _on_name_changed(self, text: str) -> None:
        self._mutate(lambda c: c.update({"name": text.strip()}))

    def _on_adapter_changed(self, _idx: int) -> None:
        if self._suppress or self._current_row < 0:
            return
        adapter_id = self._adapter_combo.currentData()
        camera = self._model.camera_at(self._current_row)
        if camera is None or not isinstance(adapter_id, str):
            return
        camera["adapter"] = adapter_id
        descriptor = ADAPTERS.get(adapter_id)
        if descriptor is not None:
            # Pre-fill kind from family and seed params.
            if descriptor.family == "camera_visible":
                camera["kind"] = "visible"
            elif descriptor.family == "camera_ir":
                camera["kind"] = "ir"
            camera["params"] = dict(descriptor.default_params)
        self._model.update_camera(self._current_row, camera)
        self._rebuild_params_form(camera)
        # Re-populate kind combo to reflect the new value.
        self._suppress = True
        try:
            idx = self._kind_combo.findData(camera.get("kind", "visible"))
            if idx >= 0:
                self._kind_combo.setCurrentIndex(idx)
        finally:
            self._suppress = False

    def _on_kind_changed(self, _idx: int) -> None:
        kind = self._kind_combo.currentData()
        if not isinstance(kind, str):
            return
        self._mutate(lambda c: c.update({"kind": kind}))

    def _on_top_field_changed(self, key: str, text: str) -> None:
        value = text.strip()

        def _apply(camera: dict[str, Any]) -> None:
            if value:
                camera[key] = value
            else:
                camera.pop(key, None)

        self._mutate(_apply)

    def _on_failure_changed(self, _idx: int) -> None:
        value = self._on_failure_combo.currentData()
        if not isinstance(value, str):
            return
        self._mutate(lambda c: c.update({"on_failure": value}))

    def _on_estimated_bps_changed(self, text: str) -> None:
        try:
            bps = int(text.strip())
        except ValueError:
            self._disk_fill_label.setText("—")
            return
        if bps <= 0:
            self._disk_fill_label.setText("—")
            return
        self._mutate(lambda c: c.update({"estimated_bps": bps}))
        self._disk_fill_label.setText(_human_disk_fill(bps, seconds=30 * 60))

    def _on_params_changed(self) -> None:
        if self._suppress or self._current_row < 0 or self._params_form is None:
            return
        camera = self._model.camera_at(self._current_row)
        if camera is None:
            return
        camera["params"] = self._params_form.values()
        self._model.update_camera(self._current_row, camera)

    # -- internals ----------------------------------------------------------

    def _mutate(self, mutator: Callable[[dict[str, Any]], None]) -> None:
        if self._suppress or self._current_row < 0:
            return
        camera = self._model.camera_at(self._current_row)
        if camera is None:
            return
        mutator(camera)
        self._model.update_camera(self._current_row, camera)

    def _rebuild_add_menu(self) -> None:
        self._add_menu.clear()
        descriptors = _camera_descriptors()
        if not descriptors:
            placeholder = self._add_menu.addAction("(no camera descriptors loaded)")
            placeholder.setEnabled(False)
            return
        # Group: visible first, then IR (CAPA rigs typically have one of each).
        order = sorted(descriptors, key=lambda d: (d.family != "camera_visible", d.label))
        for descriptor in order:
            action = self._add_menu.addAction(descriptor.label)
            action.triggered.connect(lambda _checked=False, d=descriptor: self._on_add_camera(d))

    def _on_add_camera(self, descriptor: AdapterDescriptor) -> None:
        existing = [c.get("name", "") for c in self._model.cameras()]
        camera = _default_camera_for(descriptor, existing)
        new_row = self._model.add_camera(camera)
        self._table.selectRow(new_row)

    def _reset_detail(self) -> None:
        self._current_row = -1
        self._spec_widget.hide()
        self._params_host.hide()
        if self._params_form is not None:
            self._params_form.deleteLater()
            self._params_form = None
        self._placeholder.show()

    def _build_detail(self, camera: dict[str, Any]) -> None:
        self._placeholder.hide()
        self._spec_widget.show()
        self._suppress = True
        try:
            self._name_edit.setText(str(camera.get("name", "")))
            self._populate_adapter_combo(str(camera.get("adapter", "")))
            kind_idx = self._kind_combo.findData(camera.get("kind", "visible"))
            self._kind_combo.setCurrentIndex(max(0, kind_idx))
            self._model_hint_edit.setText(str(camera.get("model_hint") or ""))
            self._serial_edit.setText(str(camera.get("serial") or ""))
            self._output_root_edit.setText(str(camera.get("output_root") or ""))
            failure_idx = self._on_failure_combo.findData(camera.get("on_failure", "warn"))
            self._on_failure_combo.setCurrentIndex(max(0, failure_idx))
            bps = camera.get("estimated_bps", 4_000_000)
            self._estimated_bps_edit.setText(str(bps))
            if isinstance(bps, int) and bps > 0:
                self._disk_fill_label.setText(_human_disk_fill(bps, seconds=30 * 60))
            else:
                self._disk_fill_label.setText("—")
        finally:
            self._suppress = False
        self._rebuild_params_form(camera)

    def _populate_adapter_combo(self, current_adapter: str) -> None:
        self._adapter_combo.clear()
        descriptors = _camera_descriptors()
        idx_to_select = 0
        for i, descriptor in enumerate(descriptors):
            self._adapter_combo.addItem(descriptor.label, descriptor.id)
            if descriptor.id == current_adapter:
                idx_to_select = i
        if current_adapter and not any(d.id == current_adapter for d in descriptors):
            self._adapter_combo.addItem(f"{current_adapter} (no descriptor)", current_adapter)
            idx_to_select = self._adapter_combo.count() - 1
        if self._adapter_combo.count() > 0:
            self._adapter_combo.setCurrentIndex(idx_to_select)

    def _rebuild_params_form(self, camera: dict[str, Any]) -> None:
        if self._params_form is not None:
            self._params_form.deleteLater()
            self._params_form = None

        layout = self._params_host.layout()
        if layout is None:
            return
        adapter_id = camera.get("adapter", "")
        descriptor = ADAPTERS.get(adapter_id) if isinstance(adapter_id, str) else None
        if descriptor is not None and descriptor.params_model is not None:
            self._params_form = build_form(descriptor.params_model, parent=self._params_host)
            params = camera.get("params") or {}
            if isinstance(params, dict):
                with contextlib.suppress(Exception):
                    self._params_form.set_values(params)
            self._params_form.valuesChanged.connect(self._on_params_changed)
            layout.addWidget(self._params_form)
            self._params_host.show()
        else:
            placeholder = QLabel("(no params schema for this adapter)", self._params_host)
            placeholder.setStyleSheet("color: #888;")
            layout.addWidget(placeholder)
            self._params_host.show()


__all__ = ["CameraTableModel", "CamerasSection"]
