"""Devices section — table + adapter-descriptor-driven detail.

Adapter combobox is driven
by :data:`capa.devices.registry.ADAPTERS`; the per-device params form is
built off ``descriptor.params_model`` so the same model that the runtime
uses for adapter construction is the one the operator edits.
"""

from __future__ import annotations

import contextlib
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

from capa.devices.registry import ADAPTERS, AdapterDescriptor, ensure_adapters_loaded
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


class DeviceTableModel(QAbstractTableModel):
    """List-of-dicts model over ``hardware_payload["devices"]``.

    Each row is the raw dict the operator edits; the section widget
    keeps the dicts mutable so a partial / pre-validation state is
    representable (drafts are dicts).
    """

    devicesChanged = Signal()  # noqa: N815 — Qt signal naming convention
    """Fires on any insert / remove / row edit."""

    HEADERS: tuple[str, ...] = ("name", "adapter", "rate", "status")

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict[str, Any]] = []
        self._status: dict[int, str] = {}

    def devices(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows]

    def set_devices(self, devices: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = [dict(row) for row in devices]
        self._status.clear()
        self.endResetModel()

    def device_at(self, row: int) -> dict[str, Any] | None:
        if 0 <= row < len(self._rows):
            return dict(self._rows[row])
        return None

    def update_device(self, row: int, device: dict[str, Any]) -> None:
        if not (0 <= row < len(self._rows)):
            return
        self._rows[row] = dict(device)
        top_left = self.index(row, 0)
        bottom_right = self.index(row, len(self.HEADERS) - 1)
        self.dataChanged.emit(top_left, bottom_right)
        self.devicesChanged.emit()

    def add_device(self, device: dict[str, Any]) -> int:
        row = len(self._rows)
        self.beginInsertRows(QModelIndex(), row, row)
        self._rows.append(dict(device))
        self.endInsertRows()
        self.devicesChanged.emit()
        return row

    def remove_device(self, row: int) -> None:
        if not (0 <= row < len(self._rows)):
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        self._rows.pop(row)
        self._status.pop(row, None)
        self.endRemoveRows()
        self.devicesChanged.emit()

    def set_status(self, row: int, status: str) -> None:
        if not (0 <= row < len(self._rows)):
            return
        self._status[row] = status
        idx = self.index(row, len(self.HEADERS) - 1)
        self.dataChanged.emit(idx, idx)

    # -- QAbstractTableModel -----------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        if parent.isValid():
            return 0
        return len(self.HEADERS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        return horizontal_header(self.HEADERS, section, orientation, role)

    def data(  # type: ignore[override]
        self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        row = index.row()
        col = index.column()
        rec = self._rows[row] if 0 <= row < len(self._rows) else None
        if rec is None:
            return None
        key = self.HEADERS[col]
        if key == "status":
            return self._status.get(row, "—")
        if key == "rate":
            # Read rate_hz from params if present so the table tells the
            # operator the cadence without diving into the detail pane.
            params = rec.get("params") or {}
            rate = params.get("rate_hz") if isinstance(params, dict) else None
            return f"{rate} Hz" if rate is not None else "—"
        value = rec.get(key, "")
        return str(value) if value is not None else ""


# ---------------------------------------------------------------------------
# Section widget.
# ---------------------------------------------------------------------------


def _default_device_for(descriptor: AdapterDescriptor, existing_names: list[str]) -> dict[str, Any]:
    base = descriptor.family if descriptor.family != "plugin" else "device"
    return {
        "name": unique_name(existing_names, base),
        "adapter": descriptor.id,
        "params": dict(descriptor.default_params),
    }


def _is_sim(descriptor: AdapterDescriptor) -> bool:
    # The "sim" family covers most simulators, but the FLIR IR sim
    # advertises family="camera_ir" so it pairs UI-side with the real
    # FLIR adapter. Detect sims by module-path prefix so neither
    # grouping nor sort order misses them.
    return descriptor.family == "sim" or descriptor.id.startswith("capa.devices.sim.")


class DevicesSection(SectionWidget):
    """Devices table + detail editor.

    The section owns one :class:`DeviceTableModel` and a detail pane
    that swaps its params form whenever the row's adapter changes.
    Edits flow back through :meth:`payload` as a single ``{"devices":
    [...]}`` slice; the Setup tab routes that into ``hardware_payload``
    via the key-based router in :mod:`capa.ui.tabs.setup`.
    """

    handshakeRequested = Signal(str)  # noqa: N815 — Qt signal naming convention
    """Operator clicked Test Connection on a device row."""

    deviceActionRequested = Signal(str)  # noqa: N815 — Qt signal naming convention
    """Operator chose "Open manual control" for a device row. Carries
    the device name; the SetupTab forwards to MainWindow."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        # Force descriptor population before the Add menu is built —
        # the runtime imports lazily so the registry is empty on a
        # fresh process otherwise.
        ensure_adapters_loaded()
        self._draft: SetupDraft | None = None
        self._suppress_signals = False
        self._current_row: int = -1
        self._params_form: ModelForm | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        title = QLabel("Devices", self)
        title.setStyleSheet("font-size: 14pt; font-weight: 600;")
        outer.addWidget(title)

        splitter = QSplitter(Qt.Orientation.Vertical, self)

        # -- Table region.
        table_region = QWidget(splitter)
        table_layout = QVBoxLayout(table_region)
        table_layout.setContentsMargins(0, 0, 0, 0)

        button_row = QHBoxLayout()
        self._add_btn = QToolButton(self)
        self._add_btn.setText("Add device")
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

        self._model = DeviceTableModel()
        self._model.devicesChanged.connect(self._on_rows_changed)
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
        # Grow the table to fit every device row — CAPA rigs run a handful
        # of devices, so we never need an inner scrollbar.
        self._model.rowsInserted.connect(lambda *_: self._update_table_height())
        self._model.rowsRemoved.connect(lambda *_: self._update_table_height())
        self._model.modelReset.connect(self._update_table_height)
        self._update_table_height()
        table_layout.addWidget(self._table)
        splitter.addWidget(table_region)

        # -- Detail region.
        self._detail_container = QWidget(splitter)
        detail_layout = QVBoxLayout(self._detail_container)
        detail_layout.setContentsMargins(0, 8, 0, 0)
        self._detail_placeholder = QLabel(
            "Select a device to edit, or use Add device ▾ to create one.",
            self._detail_container,
        )
        self._detail_placeholder.setStyleSheet("color: #888;")
        detail_layout.addWidget(self._detail_placeholder)

        # Identity / adapter row.
        self._identity_widget = QWidget(self._detail_container)
        identity_form = QFormLayout(self._identity_widget)
        identity_form.setContentsMargins(0, 0, 0, 0)
        self._name_edit = QLineEdit(self._identity_widget)
        self._name_edit.textChanged.connect(self._on_name_changed)
        identity_form.addRow("Name:", self._name_edit)
        self._adapter_combo = QComboBox(self._identity_widget)
        self._adapter_combo.currentIndexChanged.connect(self._on_adapter_changed)
        identity_form.addRow("Adapter:", self._adapter_combo)
        self._identity_widget.hide()
        detail_layout.addWidget(self._identity_widget)

        # Params form host — replaced whenever the adapter changes.
        self._params_host = QWidget(self._detail_container)
        host_layout = QVBoxLayout(self._params_host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        self._params_host.hide()
        detail_layout.addWidget(self._params_host)

        # Action row at the bottom of the detail pane.
        self._action_row = QWidget(self._detail_container)
        action_layout = QHBoxLayout(self._action_row)
        action_layout.setContentsMargins(0, 0, 0, 0)
        self._test_btn = QPushButton("Test connection", self._action_row)
        self._test_btn.clicked.connect(self._on_test_connection)
        self._test_btn.setToolTip("Runs the adapter's handshake without opening the run pool.")
        self._manual_btn = QPushButton("Open Manual Control", self._action_row)
        self._manual_btn.clicked.connect(self._on_open_manual)
        action_layout.addWidget(self._test_btn)
        action_layout.addWidget(self._manual_btn)
        action_layout.addStretch(1)
        self._status_label = QLabel("", self._action_row)
        self._status_label.setStyleSheet("color: #555;")
        action_layout.addWidget(self._status_label)
        self._action_row.hide()
        detail_layout.addWidget(self._action_row)
        detail_layout.addStretch(1)

        self._detail_layout = detail_layout
        splitter.addWidget(self._detail_container)
        # Table region uses its fixed size (driven by ``fit_table_height``);
        # detail collects any leftover height.
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        outer.addWidget(splitter, stretch=1)

        self._rebuild_add_menu()

    # -- SectionWidget API --------------------------------------------------

    def set_draft(self, draft: SetupDraft) -> None:
        self._draft = draft
        self.refresh()

    def refresh(self) -> None:
        if self._draft is None:
            return
        hw = self._draft.document.hardware_payload
        devices = hw.get("devices") if isinstance(hw, dict) else None
        rows: list[dict[str, Any]] = []
        if isinstance(devices, list):
            for entry in devices:
                if isinstance(entry, dict):
                    rows.append(dict(entry))
        self._suppress_signals = True
        try:
            self._model.set_devices(rows)
        finally:
            self._suppress_signals = False
        self._reset_detail()

    def payload(self) -> dict[str, object]:
        return {"devices": self._model.devices()}

    def _update_table_height(self) -> None:
        fit_table_height(self._table)

    # -- slots --------------------------------------------------------------

    def _on_rows_changed(self) -> None:
        if self._suppress_signals:
            return
        self.valuesChanged.emit()

    def _on_duplicate(self) -> None:
        if self._current_row < 0:
            return
        src = self._model.device_at(self._current_row)
        if src is None:
            return
        existing = [d.get("name", "") for d in self._model.devices()]
        dup = dict(src)
        dup["name"] = unique_name(existing, f"{src.get('name', 'device')}_copy")
        # Deep-copy params so subsequent edits don't shadow the source row.
        params = dup.get("params")
        if isinstance(params, dict):
            dup["params"] = dict(params)
        new_row = self._model.add_device(dup)
        self._table.selectRow(new_row)

    def _on_remove(self) -> None:
        if self._current_row < 0:
            return
        self._model.remove_device(self._current_row)
        self._reset_detail()

    def _on_row_changed(self) -> None:
        sel = self._table.selectionModel()
        rows = sel.selectedRows() if sel is not None else []
        if not rows:
            self._reset_detail()
            return
        row = rows[0].row()
        self._current_row = row
        device = self._model.device_at(row)
        if device is None:
            self._reset_detail()
            return
        self._build_detail(device)

    def _on_name_changed(self, text: str) -> None:
        if self._suppress_signals or self._current_row < 0:
            return
        device = self._model.device_at(self._current_row)
        if device is None:
            return
        device["name"] = text.strip()
        self._configure_nidaq_channels_widget(device)
        self._model.update_device(self._current_row, device)

    def _on_adapter_changed(self, _idx: int) -> None:
        if self._suppress_signals or self._current_row < 0:
            return
        adapter_id = self._adapter_combo.currentData()
        device = self._model.device_at(self._current_row)
        if device is None or not isinstance(adapter_id, str):
            return
        device["adapter"] = adapter_id
        # Pre-fill params from the new descriptor's defaults so the
        # operator never sees a half-empty form.
        descriptor = ADAPTERS.get(adapter_id)
        if descriptor is not None:
            device["params"] = dict(descriptor.default_params)
        self._model.update_device(self._current_row, device)
        self._rebuild_params_form(device)

    def _on_params_changed(self) -> None:
        if self._suppress_signals or self._current_row < 0 or self._params_form is None:
            return
        device = self._model.device_at(self._current_row)
        if device is None:
            return
        device["params"] = self._params_form.values()
        self._configure_nidaq_channels_widget(device)
        self._model.update_device(self._current_row, device)

    def _on_test_connection(self) -> None:
        if self._current_row < 0:
            return
        device = self._model.device_at(self._current_row)
        if device is None:
            return
        name = device.get("name", "")
        if isinstance(name, str) and name:
            self.handshakeRequested.emit(name)
            self._status_label.setText("(handshake pending)")

    def _on_open_manual(self) -> None:
        if self._current_row < 0:
            return
        device = self._model.device_at(self._current_row)
        if device is None:
            return
        name = device.get("name", "")
        if isinstance(name, str) and name:
            self.deviceActionRequested.emit(name)

    # -- internals ----------------------------------------------------------

    def _rebuild_add_menu(self) -> None:
        self._add_menu.clear()
        # Group real-hardware descriptors by family; collect ALL sim
        # descriptors (including camera sims like the FLIR IR sim, whose
        # family is "camera_ir") into a single bucket so they land at the
        # bottom under a clear "Simulated (testing only)" header. This is
        # production tooling — sims exist for tests and offline dev and
        # should not crowd out the real adapters.
        real_groups: dict[str, list[AdapterDescriptor]] = {}
        sim_descriptors: list[AdapterDescriptor] = []
        for descriptor in ADAPTERS.values():
            if _is_sim(descriptor):
                sim_descriptors.append(descriptor)
            else:
                real_groups.setdefault(descriptor.family, []).append(descriptor)

        for family in sorted(real_groups):
            section = self._add_menu.addAction(family.upper())
            section.setEnabled(False)
            for descriptor in sorted(real_groups[family], key=lambda d: d.label):
                action = self._add_menu.addAction(descriptor.label)
                action.triggered.connect(
                    lambda _checked=False, d=descriptor: self._on_add_device(d)
                )
            self._add_menu.addSeparator()

        if sim_descriptors:
            section = self._add_menu.addAction("SIMULATED (TESTING ONLY)")
            section.setEnabled(False)
            for descriptor in sorted(sim_descriptors, key=lambda d: d.label):
                action = self._add_menu.addAction(descriptor.label)
                action.triggered.connect(
                    lambda _checked=False, d=descriptor: self._on_add_device(d)
                )

    def _on_add_device(self, descriptor: AdapterDescriptor) -> None:
        existing = [d.get("name", "") for d in self._model.devices()]
        device = _default_device_for(descriptor, existing)
        new_row = self._model.add_device(device)
        self._table.selectRow(new_row)

    def _reset_detail(self) -> None:
        self._current_row = -1
        self._identity_widget.hide()
        self._params_host.hide()
        self._action_row.hide()
        self._status_label.setText("")
        if self._params_form is not None:
            self._params_form.deleteLater()
            self._params_form = None
        self._detail_placeholder.show()

    def _build_detail(self, device: dict[str, Any]) -> None:
        self._detail_placeholder.hide()
        self._identity_widget.show()
        self._action_row.show()
        self._status_label.setText("")
        # Populate identity + adapter combo without re-emitting signals.
        self._suppress_signals = True
        try:
            self._name_edit.setText(str(device.get("name", "")))
            self._populate_adapter_combo(str(device.get("adapter", "")))
        finally:
            self._suppress_signals = False
        self._rebuild_params_form(device)

    def _populate_adapter_combo(self, current_adapter: str) -> None:
        self._adapter_combo.clear()
        # Sims demoted to the bottom — real hardware comes first. The
        # sim flag is detected by module-path prefix so non-"sim"-family
        # simulators (e.g. the FLIR IR sim in family="camera_ir") are
        # also pushed down.
        descriptors = sorted(
            ADAPTERS.values(),
            key=lambda d: (_is_sim(d), d.family, d.label),
        )
        idx_to_select = 0
        for i, descriptor in enumerate(descriptors):
            label = f"{descriptor.label}  (sim)" if _is_sim(descriptor) else descriptor.label
            self._adapter_combo.addItem(label, descriptor.id)
            if descriptor.id == current_adapter:
                idx_to_select = i
        # An adapter id not in the registry is a hard error (Layer 2
        # validation surfaces it). Show the unknown id so the operator
        # can see/edit it without silently rewriting the row.
        if current_adapter and not any(d.id == current_adapter for d in descriptors):
            self._adapter_combo.addItem(f"{current_adapter} (unknown adapter)", current_adapter)
            idx_to_select = self._adapter_combo.count() - 1
        self._adapter_combo.setCurrentIndex(idx_to_select)

    def _rebuild_params_form(self, device: dict[str, Any]) -> None:
        # Drop the previous form.
        if self._params_form is not None:
            self._params_form.deleteLater()
            self._params_form = None

        adapter_id = device.get("adapter", "")
        descriptor = ADAPTERS.get(adapter_id) if isinstance(adapter_id, str) else None
        layout = self._params_host.layout()
        if layout is None:
            return

        if descriptor is not None and descriptor.params_model is not None:
            self._params_form = build_form(descriptor.params_model, parent=self._params_host)
            params = device.get("params") or {}
            if isinstance(params, dict):
                with contextlib.suppress(Exception):
                    self._params_form.set_values(params)
            self._configure_nidaq_channels_widget(device)
            self._params_form.valuesChanged.connect(self._on_params_changed)
            layout.addWidget(self._params_form)
            self._params_host.show()
        else:
            # Fallback for adapters without a params_model: show a tiny
            # read-only label rather than a misleading empty form. The
            # operator can still hand-edit via the underlying TOML; the
            # detail pane just doesn't curate it.
            placeholder = QLabel(
                "(no params schema — edit via TOML or supply an AdapterDescriptor)",
                self._params_host,
            )
            placeholder.setStyleSheet("color: #888;")
            layout.addWidget(placeholder)
            self._params_host.show()

    def _configure_nidaq_channels_widget(self, device: dict[str, Any]) -> None:
        """Give the specialised NI input editor its owning device/task.

        ``NIDAQChannelsField`` only edits ``params.channels``, but its
        cross-section requests need the full join key. The Devices section
        owns the row context, so it passes that context down after building
        or updating the params form.
        """
        if self._params_form is None:
            return
        field_widget = self._params_form.field_widget("channels")
        if field_widget is None:
            return
        from capa.ui.forms.widgets._nidaq_channels import (  # noqa: PLC0415
            NIDAQChannelsField,
        )

        if not isinstance(field_widget, NIDAQChannelsField):
            return
        params = device.get("params") if isinstance(device.get("params"), dict) else {}
        task_name = params.get("task_name") if isinstance(params, dict) else None
        field_widget.set_join_context(
            device_name=str(device.get("name") or ""),
            task_name=task_name if isinstance(task_name, str) else "",
        )


__all__ = ["DeviceTableModel", "DevicesSection"]
