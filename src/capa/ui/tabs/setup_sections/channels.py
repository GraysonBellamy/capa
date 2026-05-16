"""Channels section — table + binding editor + calibration sub-form.

The detail pane is hand-composed rather than auto-formed
because the section has variant-aware filtering: the binding-type
combobox is ordered by :data:`KIND_TO_PREFERRED_BINDINGS` for the
channel's kind and filtered by the selected device's adapter family.
The calibration sub-form drops in :class:`_DiscriminatedUnionField`
directly — that widget already preserves overlapping field values
across variant switches.
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
    QAbstractScrollArea,
    QComboBox,
    QDoubleSpinBox,
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

from capa.channels.calibration import Calibration
from capa.channels.spec import ChannelKind
from capa.config.binding_policy import (
    filter_bindings_for_family,
    ordered_bindings_for_kind,
)
from capa.devices.registry import (
    ADAPTERS,
    AdapterDescriptor,
    ensure_adapters_loaded,
    get_descriptor,
)
from capa.ui.forms.widgets._nested import _DiscriminatedUnionField
from capa.ui.tabs.setup_sections._base import SectionWidget
from capa.ui.tabs.setup_sections._models import horizontal_header, unique_name

if TYPE_CHECKING:
    from capa.devices.registry import ChannelTemplate
    from capa.ui.tabs.setup_state import SetupDraft


# ---------------------------------------------------------------------------
# Variant-field metadata.
# ---------------------------------------------------------------------------


# Variant value -> human label. Used by the binding-type combobox.
_VARIANT_LABELS: dict[str, str] = {
    "watlow_parameter": "Watlow parameter",
    "alicat_frame_field": "Alicat frame field",
    "sartorius_reading": "Sartorius reading",
    "nidaq_reading_field": "NI-DAQ reading field",
    "nidaq_block_channel": "NI-DAQ block channel",
    "derived": "Derived expression",
}


# Each variant exposes one or two scalar fields beyond ``device``.
# We render them by hand rather than running build_form on the variant
# class because the section wants tight control over field ordering,
# tooltips, and field-level catalogues.
_VARIANT_FIELDS: dict[str, tuple[tuple[str, str, type], ...]] = {
    "watlow_parameter": (
        ("parameter", "Parameter", str),
        ("instance", "Instance", int),
    ),
    "alicat_frame_field": (("field", "Field", str),),
    "sartorius_reading": (("field", "Field", str),),
    "nidaq_reading_field": (
        ("task", "Task", str),
        ("field", "Field", str),
    ),
    "nidaq_block_channel": (
        ("task", "Task", str),
        ("channel", "Channel", str),
    ),
    "derived": (
        ("expression", "Expression", str),
        ("inputs", "Inputs (comma-separated)", str),
    ),
}


# ---------------------------------------------------------------------------
# Table model.
# ---------------------------------------------------------------------------


class ChannelTableModel(QAbstractTableModel):
    """List-of-dicts model over ``hardware_payload["channels"]``."""

    channelsChanged = Signal()  # noqa: N815 — Qt signal naming convention

    HEADERS: tuple[str, ...] = ("name", "kind", "device", "binding", "unit")

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict[str, Any]] = []

    def channels(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows]

    def set_channels(self, channels: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = [dict(row) for row in channels]
        self.endResetModel()

    def channel_at(self, row: int) -> dict[str, Any] | None:
        if 0 <= row < len(self._rows):
            return dict(self._rows[row])
        return None

    def update_channel(self, row: int, channel: dict[str, Any]) -> None:
        if not (0 <= row < len(self._rows)):
            return
        self._rows[row] = dict(channel)
        top_left = self.index(row, 0)
        bottom_right = self.index(row, len(self.HEADERS) - 1)
        self.dataChanged.emit(top_left, bottom_right)
        self.channelsChanged.emit()

    def add_channel(self, channel: dict[str, Any]) -> int:
        row = len(self._rows)
        self.beginInsertRows(QModelIndex(), row, row)
        self._rows.append(dict(channel))
        self.endInsertRows()
        self.channelsChanged.emit()
        return row

    def remove_channel(self, row: int) -> None:
        if not (0 <= row < len(self._rows)):
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        self._rows.pop(row)
        self.endRemoveRows()
        self.channelsChanged.emit()

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
        rec = self._rows[row] if 0 <= row < len(self._rows) else None
        if rec is None:
            return None
        key = self.HEADERS[index.column()]
        if key == "device":
            source = rec.get("source") or {}
            return str(source.get("device", "—")) if isinstance(source, dict) else "—"
        if key == "binding":
            source = rec.get("source") or {}
            return str(source.get("source", "—")) if isinstance(source, dict) else "—"
        value = rec.get(key, "")
        return str(value) if value is not None else ""


# ---------------------------------------------------------------------------
# Section widget.
# ---------------------------------------------------------------------------


def _channel_from_template(
    template: ChannelTemplate,
    existing_names: list[str],
    device_name: str | None,
) -> dict[str, Any]:
    """Build a channel dict from a :class:`ChannelTemplate`.

    The template's ``source_factory`` takes a device name; when no
    device is selected yet (operator hasn't chosen one), we still
    produce a placeholder dict that fails Layer-1 validation cleanly so
    the Problems panel guides the operator to fill it in.
    """
    base_name = template.id.split(".")[-1]
    name = unique_name(existing_names, base_name)
    source = template.source_factory(device_name or "")
    record: dict[str, Any] = {
        "name": name,
        "kind": template.kind,
        "unit": template.default_unit,
        "source": source,
    }
    if template.default_derived_unit:
        record["derived_unit"] = template.default_derived_unit
    if template.default_calibration:
        record["calibration"] = dict(template.default_calibration)
    if template.plot_group:
        record["plot_group"] = template.plot_group
    metadata = dict(template.metadata_defaults)
    if template.capa_group:
        metadata["capa_group"] = template.capa_group
    if metadata:
        record["metadata"] = metadata
    return record


def _blank_channel(existing_names: list[str]) -> dict[str, Any]:
    return {
        "name": unique_name(existing_names, "channel"),
        "kind": ChannelKind.PROCESS_VAR.value,
        "unit": "",
        "source": {"source": "watlow_parameter", "device": "", "parameter": "", "instance": 1},
    }


class _SourceBindingEditor(QWidget):
    """Device + binding-type + variant-fields editor.

    Drives the discriminated-union shape of :data:`SourceBinding`
    without exposing the raw ``source`` discriminator to the operator.
    Filters available binding types by the selected device's adapter
    family and reorders by the channel's kind.
    """

    valueChanged = Signal()  # noqa: N815 — Qt signal naming convention

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._suppress = False
        self._devices: list[dict[str, Any]] = []
        self._kind: str | None = None
        self._current: dict[str, Any] = {}
        self._variant_fields: dict[str, QWidget] = {}

        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)

        self._device_combo = QComboBox(self)
        self._device_combo.currentIndexChanged.connect(self._on_device_changed)
        form.addRow("Device:", self._device_combo)

        self._variant_combo = QComboBox(self)
        self._variant_combo.currentIndexChanged.connect(self._on_variant_changed)
        form.addRow("Binding:", self._variant_combo)

        self._fields_host = QWidget(self)
        self._fields_layout = QFormLayout(self._fields_host)
        self._fields_layout.setContentsMargins(0, 0, 0, 0)
        form.addRow(self._fields_host)

    # -- API ----------------------------------------------------------------

    def set_context(
        self,
        *,
        devices: list[dict[str, Any]],
        kind: str | None,
    ) -> None:
        """Refresh the device list and channel-kind ordering hint.

        Called whenever the operator switches rows or edits the kind.
        Keeps the current selection if still valid.
        """
        self._devices = devices
        self._kind = kind
        self._repopulate_combos()

    def value(self) -> dict[str, Any]:
        """Compose the current source dict from combo + variant fields."""
        device = self._device_combo.currentData() or ""
        variant = self._variant_combo.currentData() or ""
        out: dict[str, Any] = {"source": variant, "device": device}
        # Pull variant-specific fields.
        for field_name, widget in self._variant_fields.items():
            if isinstance(widget, QLineEdit):
                text = widget.text().strip()
                if field_name == "inputs":
                    # tuple[str, ...] field — store as list of stripped names.
                    out[field_name] = [p.strip() for p in text.split(",") if p.strip()]
                else:
                    out[field_name] = text
            elif isinstance(widget, QDoubleSpinBox):
                # No double-spin fields today; placeholder for later use.
                out[field_name] = widget.value()
            elif isinstance(widget, QComboBox):
                out[field_name] = widget.currentText()
        # Derived bindings have no device — drop the key so the schema
        # validator picks the right variant.
        if variant == "derived":
            out.pop("device", None)
        return out

    def set_value(self, source: dict[str, Any]) -> None:
        self._suppress = True
        try:
            self._current = dict(source or {})
            self._repopulate_combos()
        finally:
            self._suppress = False

    # -- internals ----------------------------------------------------------

    def _repopulate_combos(self) -> None:
        # Device combobox.
        self._device_combo.blockSignals(True)
        self._device_combo.clear()
        for device in self._devices:
            name = device.get("name", "")
            if isinstance(name, str):
                self._device_combo.addItem(name, name)
        self._device_combo.addItem("(derived — no device)", "")
        current_device = self._current.get("device", "")
        idx = self._device_combo.findData(current_device)
        if idx >= 0:
            self._device_combo.setCurrentIndex(idx)
        self._device_combo.blockSignals(False)

        # Variant combobox — order by kind, filter by selected device's family.
        self._populate_variant_combo()

        # Variant fields.
        self._rebuild_variant_fields()

    def _selected_device_descriptor(self) -> AdapterDescriptor | None:
        device_name = self._device_combo.currentData()
        if not device_name:
            return None
        for dev in self._devices:
            if dev.get("name") == device_name:
                adapter_id = dev.get("adapter")
                if isinstance(adapter_id, str):
                    return get_descriptor(adapter_id)
        return None

    def _populate_variant_combo(self) -> None:
        self._variant_combo.blockSignals(True)
        self._variant_combo.clear()
        ordered = ordered_bindings_for_kind(self._kind)
        descriptor = self._selected_device_descriptor()
        supported = descriptor.supported_binding_sources if descriptor is not None else None
        # If no device or no descriptor, show every variant.
        device_name = self._device_combo.currentData()
        if not device_name:
            allowed = ordered
        else:
            allowed = filter_bindings_for_family(ordered, supported)
            if not allowed:
                # Plugin / unknown adapter — show everything so the operator
                # isn't blocked.
                allowed = ordered
        for variant in allowed:
            self._variant_combo.addItem(_VARIANT_LABELS.get(variant, variant), variant)
        current = self._current.get("source", "")
        idx = self._variant_combo.findData(current)
        if idx < 0 and self._variant_combo.count() > 0:
            idx = 0
        if idx >= 0:
            self._variant_combo.setCurrentIndex(idx)
        self._variant_combo.blockSignals(False)

    def _rebuild_variant_fields(self) -> None:
        # Tear down old widgets.
        while self._fields_layout.rowCount() > 0:
            self._fields_layout.removeRow(0)
        self._variant_fields = {}
        variant = self._variant_combo.currentData()
        if not isinstance(variant, str):
            return
        spec = _VARIANT_FIELDS.get(variant, ())
        for field_name, label, dtype in spec:
            widget: QWidget
            current_val = self._current.get(field_name)
            if dtype is int:
                line = QLineEdit(str(current_val) if current_val is not None else "1")
                line.setPlaceholderText("integer")
                widget = line
            else:
                line = QLineEdit("")
                if field_name == "inputs" and isinstance(current_val, list):
                    line.setText(", ".join(str(p) for p in current_val))
                elif current_val is not None:
                    line.setText(str(current_val))
                widget = line
            if isinstance(widget, QLineEdit):
                widget.textChanged.connect(self._on_inner_changed)
            self._fields_layout.addRow(label + ":", widget)
            self._variant_fields[field_name] = widget

    def _on_device_changed(self) -> None:
        if self._suppress:
            return
        # Re-filter the variant combo for the new device's family.
        # Capture the current source state so the variant fields keep
        # in-progress values where compatible.
        self._current = self.value()
        self._populate_variant_combo()
        self._rebuild_variant_fields()
        self.valueChanged.emit()

    def _on_variant_changed(self) -> None:
        if self._suppress:
            return
        self._current = self.value()
        self._rebuild_variant_fields()
        self.valueChanged.emit()

    def _on_inner_changed(self, _text: str) -> None:
        if self._suppress:
            return
        self.valueChanged.emit()


class ChannelsSection(SectionWidget):
    """Channels table + detail editor."""

    plotCalibrationRequested = Signal(str)  # noqa: N815 — Qt signal naming convention
    """Operator clicked "Plot calibration". The
    :class:`CalibrationPlotDialog` is the connected listener."""

    applyCalibrationRequested = Signal(str)  # noqa: N815 — Qt signal naming convention
    """Operator clicked "Apply to other channels…". The Setup tab opens
    :class:`ApplyCalibrationDialog` with the source channel's
    calibration pre-filled."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        ensure_adapters_loaded()
        self._draft: SetupDraft | None = None
        self._suppress = False
        self._current_row: int = -1
        self._calibration_field: _DiscriminatedUnionField | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        title = QLabel("Channels", self)
        title.setStyleSheet("font-size: 14pt; font-weight: 600;")
        outer.addWidget(title)

        splitter = QSplitter(Qt.Orientation.Vertical, self)

        # -- Table region.
        table_region = QWidget(splitter)
        table_layout = QVBoxLayout(table_region)
        table_layout.setContentsMargins(0, 0, 0, 0)

        button_row = QHBoxLayout()
        self._add_btn = QToolButton(self)
        self._add_btn.setText("Add channel")
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

        self._model = ChannelTableModel()
        self._model.channelsChanged.connect(self._on_rows_changed)
        self._table = QTableView(self)
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        # Grow the table viewport to fit its rows (capped) so configs with
        # only a few channels don't waste space, and the splitter pane
        # below stays usable when there are many.
        self._table.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)
        self._table.setMinimumHeight(120)
        self._table.setMaximumHeight(500)
        header = self._table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            header.setStretchLastSection(True)
        selection_model = self._table.selectionModel()
        if selection_model is not None:
            selection_model.selectionChanged.connect(self._on_row_changed)
        table_layout.addWidget(self._table)
        splitter.addWidget(table_region)

        # -- Detail region.
        self._detail = QWidget(splitter)
        detail_layout = QVBoxLayout(self._detail)
        detail_layout.setContentsMargins(0, 8, 0, 0)
        self._detail_placeholder = QLabel(
            "Select a channel to edit, or use Add channel ▾.",
            self._detail,
        )
        self._detail_placeholder.setStyleSheet("color: #888;")
        detail_layout.addWidget(self._detail_placeholder)

        # Identity block.
        self._identity_widget = QWidget(self._detail)
        identity_form = QFormLayout(self._identity_widget)
        identity_form.setContentsMargins(0, 0, 0, 0)
        self._name_edit = QLineEdit(self._identity_widget)
        self._name_edit.textChanged.connect(self._on_name_changed)
        identity_form.addRow("Name:", self._name_edit)
        self._kind_combo = QComboBox(self._identity_widget)
        for kind in ChannelKind:
            self._kind_combo.addItem(kind.value, kind.value)
        self._kind_combo.currentIndexChanged.connect(self._on_kind_changed)
        identity_form.addRow("Kind:", self._kind_combo)
        self._plot_group_edit = QLineEdit(self._identity_widget)
        self._plot_group_edit.textChanged.connect(self._on_plot_group_changed)
        identity_form.addRow("Plot group:", self._plot_group_edit)
        self._capa_group_edit = QLineEdit(self._identity_widget)
        self._capa_group_edit.setPlaceholderText("e.g. heater_pv (CAPA profile mapping)")
        self._capa_group_edit.textChanged.connect(self._on_capa_group_changed)
        identity_form.addRow("CAPA group:", self._capa_group_edit)
        self._identity_widget.hide()
        detail_layout.addWidget(self._identity_widget)

        # Source binding block.
        self._source_box = QWidget(self._detail)
        source_outer = QVBoxLayout(self._source_box)
        source_outer.setContentsMargins(0, 8, 0, 0)
        source_header = QLabel("Source binding", self._source_box)
        source_header.setStyleSheet("font-weight: 600;")
        source_outer.addWidget(source_header)
        self._source_editor = _SourceBindingEditor(self._source_box)
        self._source_editor.valueChanged.connect(self._on_source_changed)
        source_outer.addWidget(self._source_editor)
        self._source_box.hide()
        detail_layout.addWidget(self._source_box)

        # Units block.
        self._units_widget = QWidget(self._detail)
        units_form = QFormLayout(self._units_widget)
        units_form.setContentsMargins(0, 8, 0, 0)
        self._unit_edit = QLineEdit(self._units_widget)
        self._unit_edit.textChanged.connect(self._on_unit_changed)
        units_form.addRow("Raw unit:", self._unit_edit)
        self._derived_unit_edit = QLineEdit(self._units_widget)
        self._derived_unit_edit.setPlaceholderText("blank = same as raw unit")
        self._derived_unit_edit.textChanged.connect(self._on_derived_unit_changed)
        units_form.addRow("Derived unit:", self._derived_unit_edit)
        self._sample_rate_edit = QLineEdit(self._units_widget)
        self._sample_rate_edit.setPlaceholderText("blank = adapter-driven cadence")
        self._sample_rate_edit.textChanged.connect(self._on_sample_rate_changed)
        units_form.addRow("Sample rate (Hz):", self._sample_rate_edit)
        self._units_widget.hide()
        detail_layout.addWidget(self._units_widget)

        # Calibration block.
        self._calibration_box = QWidget(self._detail)
        cal_outer = QVBoxLayout(self._calibration_box)
        cal_outer.setContentsMargins(0, 8, 0, 0)
        cal_header_row = QHBoxLayout()
        cal_header = QLabel("Calibration", self._calibration_box)
        cal_header.setStyleSheet("font-weight: 600;")
        cal_header_row.addWidget(cal_header)
        cal_header_row.addStretch(1)
        self._plot_btn = QPushButton("Plot…", self._calibration_box)
        self._plot_btn.setToolTip("Plot value(raw) for this channel's calibration.")
        self._plot_btn.clicked.connect(self._on_plot_calibration)
        cal_header_row.addWidget(self._plot_btn)
        self._apply_to_others_btn = QPushButton("Apply to others…", self._calibration_box)
        self._apply_to_others_btn.setToolTip(
            "Clone this channel's calibration to other channels"
            " (e.g. share one TC curve across six thermocouples)."
        )
        self._apply_to_others_btn.clicked.connect(self._on_apply_to_others)
        cal_header_row.addWidget(self._apply_to_others_btn)
        cal_outer.addLayout(cal_header_row)
        self._cal_host = QWidget(self._calibration_box)
        self._cal_host_layout = QVBoxLayout(self._cal_host)
        self._cal_host_layout.setContentsMargins(0, 0, 0, 0)
        cal_outer.addWidget(self._cal_host)
        self._calibration_box.hide()
        detail_layout.addWidget(self._calibration_box)

        detail_layout.addStretch(1)
        splitter.addWidget(self._detail)
        # Table region claims its sizeHint (driven by AdjustToContents on
        # the QTableView); detail collects the remaining space. Replaces
        # the previous fixed [220, 400] which pinned the table small
        # regardless of how many channels were defined.
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
        channels = hw.get("channels") if isinstance(hw, dict) else None
        rows: list[dict[str, Any]] = []
        if isinstance(channels, list):
            for entry in channels:
                if isinstance(entry, dict):
                    rows.append(dict(entry))
        self._suppress = True
        try:
            self._model.set_channels(rows)
        finally:
            self._suppress = False
        self._reset_detail()
        # Repopulate the add menu in case the device list changed (new
        # adapter family unlocks new templates).
        self._rebuild_add_menu()

    def payload(self) -> dict[str, object]:
        return {"channels": self._model.channels()}

    # -- slots: table -------------------------------------------------------

    def _on_rows_changed(self) -> None:
        if self._suppress:
            return
        self.valuesChanged.emit()

    def _on_duplicate(self) -> None:
        if self._current_row < 0:
            return
        src = self._model.channel_at(self._current_row)
        if src is None:
            return
        existing = [c.get("name", "") for c in self._model.channels()]
        dup = _deep_dict_copy(src)
        dup["name"] = unique_name(existing, f"{src.get('name', 'channel')}_copy")
        new_row = self._model.add_channel(dup)
        self._table.selectRow(new_row)

    def _on_remove(self) -> None:
        if self._current_row < 0:
            return
        self._model.remove_channel(self._current_row)
        self._reset_detail()

    def _on_row_changed(self) -> None:
        sel = self._table.selectionModel()
        rows = sel.selectedRows() if sel is not None else []
        if not rows:
            self._reset_detail()
            return
        row = rows[0].row()
        self._current_row = row
        channel = self._model.channel_at(row)
        if channel is None:
            self._reset_detail()
            return
        self._build_detail(channel)

    # -- slots: detail edits ------------------------------------------------

    def _on_name_changed(self, text: str) -> None:
        self._mutate_current(lambda ch: ch.update({"name": text.strip()}))

    def _on_kind_changed(self, _idx: int) -> None:
        kind = self._kind_combo.currentData()
        if not isinstance(kind, str):
            return
        self._mutate_current(lambda ch: ch.update({"kind": kind}))
        # Reorder the source-binding combo to match the new kind.
        if self._current_row >= 0:
            channel = self._model.channel_at(self._current_row)
            if channel is not None:
                self._source_editor.set_context(
                    devices=self._current_devices(),
                    kind=kind,
                )
                source = channel.get("source") or {}
                if isinstance(source, dict):
                    self._source_editor.set_value(source)

    def _on_plot_group_changed(self, text: str) -> None:
        value = text.strip()

        def _apply(ch: dict[str, Any]) -> None:
            if value:
                ch["plot_group"] = value
            else:
                ch.pop("plot_group", None)

        self._mutate_current(_apply)

    def _on_capa_group_changed(self, text: str) -> None:
        value = text.strip()

        def _apply(ch: dict[str, Any]) -> None:
            metadata = dict(ch.get("metadata") or {})
            if value:
                metadata["capa_group"] = value
            else:
                metadata.pop("capa_group", None)
            if metadata:
                ch["metadata"] = metadata
            else:
                ch.pop("metadata", None)

        self._mutate_current(_apply)

    def _on_source_changed(self) -> None:
        source = self._source_editor.value()
        self._mutate_current(lambda ch: ch.update({"source": source}))

    def _on_unit_changed(self, text: str) -> None:
        self._mutate_current(lambda ch: ch.update({"unit": text.strip()}))

    def _on_derived_unit_changed(self, text: str) -> None:
        value = text.strip()

        def _apply(ch: dict[str, Any]) -> None:
            if value:
                ch["derived_unit"] = value
            else:
                ch.pop("derived_unit", None)

        self._mutate_current(_apply)

    def _on_sample_rate_changed(self, text: str) -> None:
        value = text.strip()

        def _apply(ch: dict[str, Any]) -> None:
            if not value:
                ch.pop("sample_rate_hz", None)
                return
            with contextlib.suppress(ValueError):
                ch["sample_rate_hz"] = float(value)

        self._mutate_current(_apply)

    def _on_calibration_changed(self) -> None:
        if self._calibration_field is None:
            return
        cal = self._calibration_field.value()
        self._mutate_current(lambda ch: ch.update({"calibration": cal}))

    def _on_plot_calibration(self) -> None:
        if self._current_row < 0:
            return
        channel = self._model.channel_at(self._current_row)
        if channel is None:
            return
        name = channel.get("name", "")
        if isinstance(name, str) and name:
            self.plotCalibrationRequested.emit(name)

    def _on_apply_to_others(self) -> None:
        if self._current_row < 0:
            return
        channel = self._model.channel_at(self._current_row)
        if channel is None:
            return
        name = channel.get("name", "")
        if isinstance(name, str) and name:
            self.applyCalibrationRequested.emit(name)

    # -- internals: detail wiring -------------------------------------------

    def _current_devices(self) -> list[dict[str, Any]]:
        if self._draft is None:
            return []
        hw = self._draft.document.hardware_payload
        devs = hw.get("devices") if isinstance(hw, dict) else None
        if isinstance(devs, list):
            return [dict(d) for d in devs if isinstance(d, dict)]
        return []

    def _mutate_current(self, mutator: Callable[[dict[str, Any]], None]) -> None:
        if self._suppress or self._current_row < 0:
            return
        channel = self._model.channel_at(self._current_row)
        if channel is None:
            return
        mutator(channel)
        self._model.update_channel(self._current_row, channel)

    def _reset_detail(self) -> None:
        self._current_row = -1
        self._identity_widget.hide()
        self._source_box.hide()
        self._units_widget.hide()
        self._calibration_box.hide()
        self._detail_placeholder.show()
        if self._calibration_field is not None:
            self._calibration_field.deleteLater()
            self._calibration_field = None

    def _build_detail(self, channel: dict[str, Any]) -> None:
        self._detail_placeholder.hide()
        self._identity_widget.show()
        self._source_box.show()
        self._units_widget.show()
        self._calibration_box.show()

        self._suppress = True
        try:
            self._name_edit.setText(str(channel.get("name", "")))
            kind_value = channel.get("kind", ChannelKind.PROCESS_VAR.value)
            if hasattr(kind_value, "value"):
                kind_value = kind_value.value
            idx = self._kind_combo.findData(kind_value)
            if idx >= 0:
                self._kind_combo.setCurrentIndex(idx)
            self._plot_group_edit.setText(str(channel.get("plot_group") or ""))
            metadata = channel.get("metadata") or {}
            self._capa_group_edit.setText(str(metadata.get("capa_group") or ""))
            self._unit_edit.setText(str(channel.get("unit") or ""))
            self._derived_unit_edit.setText(str(channel.get("derived_unit") or ""))
            sample_rate = channel.get("sample_rate_hz")
            self._sample_rate_edit.setText("" if sample_rate is None else str(sample_rate))

            # Source binding editor.
            self._source_editor.set_context(
                devices=self._current_devices(),
                kind=kind_value if isinstance(kind_value, str) else None,
            )
            self._source_editor.set_value(channel.get("source") or {})

            # Calibration sub-form — uses _DiscriminatedUnionField directly.
            self._rebuild_calibration(channel.get("calibration") or {})
        finally:
            self._suppress = False

    def _rebuild_calibration(self, calibration: dict[str, Any]) -> None:
        # Drop the previous widget.
        if self._calibration_field is not None:
            self._calibration_field.deleteLater()
            self._calibration_field = None

        # FieldInfo built from the SourceBinding annotation already
        # carries the discriminator metadata Pydantic produced; reuse
        # ExperimentConfig's ChannelSpec.calibration field for the same
        # effect — its FieldInfo has the discriminator set on its
        # default_factory marker. _DiscriminatedUnionField only needs
        # the annotation and a FieldInfo to read the discriminator name.
        from capa.channels.spec import ChannelSpec  # noqa: PLC0415

        field_info = ChannelSpec.model_fields["calibration"]
        field = _DiscriminatedUnionField(union=Calibration, field=field_info, parent=self._cal_host)
        with contextlib.suppress(Exception):
            if isinstance(calibration, dict) and calibration:
                field.set_value(calibration)
        field.valueChanged.connect(self._on_calibration_changed)
        self._cal_host_layout.addWidget(field)
        self._calibration_field = field

    # -- internals: Add menu ------------------------------------------------

    def _rebuild_add_menu(self) -> None:
        self._add_menu.clear()
        # Collect all templates from currently-loaded descriptors, grouped
        # by adapter family so the menu is readable.
        templates_by_family: dict[str, list[ChannelTemplate]] = {}
        for descriptor in ADAPTERS.values():
            for template in descriptor.channel_templates:
                templates_by_family.setdefault(descriptor.family, []).append(template)

        # Real-hardware templates first; sim templates pushed to the bottom
        # under their own labelled group — this is production tooling and
        # sims are a testing affordance, not the primary path.
        family_order = sorted(templates_by_family.keys(), key=lambda f: (f == "sim", f))
        for family in family_order:
            header_text = "SIMULATED (TESTING ONLY)" if family == "sim" else family.upper()
            header = self._add_menu.addAction(header_text)
            header.setEnabled(False)
            for template in sorted(templates_by_family[family], key=lambda t: t.label):
                action = self._add_menu.addAction(template.label)
                action.triggered.connect(
                    lambda _checked=False, t=template: self._on_add_from_template(t)
                )
            self._add_menu.addSeparator()
        # Blank channel escape hatch.
        blank = self._add_menu.addAction("Blank channel")
        blank.triggered.connect(self._on_add_blank)

    def _select_default_device(self, binding_source: str | None) -> str | None:
        """Pick a device whose descriptor advertises ``binding_source``.

        Matches by :attr:`AdapterDescriptor.supported_binding_sources`
        rather than family, since "sim" is a single family covering
        every simulator (a Watlow sim and a Sartorius sim are both
        family="sim" but ship different binding sources)."""
        if binding_source is None:
            return None
        for dev in self._current_devices():
            adapter_id = dev.get("adapter")
            if not isinstance(adapter_id, str):
                continue
            descriptor = get_descriptor(adapter_id)
            if descriptor is None:
                continue
            if binding_source in descriptor.supported_binding_sources:
                return str(dev.get("name", ""))
        return None

    def _on_add_from_template(self, template: ChannelTemplate) -> None:
        # Probe the template's source factory to figure out which
        # binding-source it produces, then pick a matching device.
        probe = template.source_factory("__probe__")
        binding_source = probe.get("source") if isinstance(probe, dict) else None
        device_name = self._select_default_device(
            binding_source if isinstance(binding_source, str) else None
        )
        existing = [c.get("name", "") for c in self._model.channels()]
        channel = _channel_from_template(template, existing, device_name)
        new_row = self._model.add_channel(channel)
        self._table.selectRow(new_row)

    def _on_add_blank(self) -> None:
        existing = [c.get("name", "") for c in self._model.channels()]
        new_row = self._model.add_channel(_blank_channel(existing))
        self._table.selectRow(new_row)


def _deep_dict_copy(src: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in src.items():
        if isinstance(v, dict):
            out[k] = _deep_dict_copy(v)
        elif isinstance(v, list):
            out[k] = [_deep_dict_copy(e) if isinstance(e, dict) else e for e in v]
        else:
            out[k] = v
    return out


__all__ = ["ChannelTableModel", "ChannelsSection"]
