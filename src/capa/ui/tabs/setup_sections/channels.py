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
    QCheckBox,
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
from capa.ui.tabs.setup_sections._models import (
    fit_table_height,
    horizontal_header,
    unique_name,
)

if TYPE_CHECKING:
    from capa.devices.registry import ChannelTemplate
    from capa.ui.tabs.setup_state import SetupDraft


def _compose_reads_from(source: object) -> str:
    """One-line "device.parameter" rendering for the Simple view.

    Falls back to ``"—"`` for missing / malformed sources, and to a
    parenthesised variant name when the operator hasn't filled in the
    device / parameter fields yet (so the Simple view still tells them
    the binding *type* even before it's complete).
    """
    if not isinstance(source, dict):
        return "—"
    variant = source.get("source")
    device = source.get("device")
    if not isinstance(variant, str):
        return "—"
    if variant == "watlow_parameter":
        param = source.get("parameter") or "?"
        instance = source.get("instance", 1)
        if device:
            return f"{device}.{param} (loop {instance})"
        return f"({variant})"
    if variant in ("alicat_frame_field", "sartorius_reading"):
        field = source.get("field") or "?"
        if device:
            return f"{device}.{field}"
        return f"({variant})"
    if variant == "nidaq_reading_field":
        task = source.get("task") or "?"
        field = source.get("field") or "?"
        if device:
            return f"{device}.{task}.{field}"
        return f"({variant})"
    if variant == "nidaq_block_channel":
        task = source.get("task") or "?"
        channel = source.get("channel") or "?"
        if device:
            return f"{device}.{task}.{channel}"
        return f"({variant})"
    if variant == "derived":
        expr = source.get("expression") or "?"
        return f"derived: {expr}"
    if device:
        return f"{device} ({variant})"
    return f"({variant})"


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
#
# ``choices_key`` (when not ``None``) names a provider on
# :class:`_SourceBindingEditor` that returns the list of valid completions
# for the field — the renderer swaps the plain QLineEdit for an editable
# QComboBox populated from that list. For NI-DAQ ``task`` / ``field`` the
# provider returns the declared NI channel taxonomy for the selected
# device (see :func:`capa.devices.nidaq_join.declared_channels_from_payload`),
# so the operator picks from the rig's real inventory instead of typing
# a string that has to match exactly.
_VARIANT_FIELDS: dict[str, tuple[tuple[str, str, type, str | None], ...]] = {
    "watlow_parameter": (
        ("parameter", "Parameter", str, None),
        ("instance", "Instance", int, None),
    ),
    "alicat_frame_field": (("field", "Field", str, None),),
    "sartorius_reading": (("field", "Field", str, None),),
    "nidaq_reading_field": (
        ("task", "Task", str, "nidaq_tasks"),
        ("field", "Field", str, "nidaq_fields"),
    ),
    "nidaq_block_channel": (
        ("task", "Task", str, "nidaq_tasks"),
        ("channel", "Channel", str, "nidaq_fields"),
    ),
    "derived": (
        ("expression", "Expression", str, None),
        ("inputs", "Inputs (comma-separated)", str, None),
    ),
}


# ---------------------------------------------------------------------------
# Table model.
# ---------------------------------------------------------------------------


class ChannelTableModel(QAbstractTableModel):
    """List-of-dicts model over ``hardware_payload["channels"]``."""

    channelsChanged = Signal()  # noqa: N815 — Qt signal naming convention

    COLUMN_KEYS: tuple[str, ...] = ("name", "kind", "device", "binding", "unit")
    HEADERS: tuple[str, ...] = ("name", "kind", "device", "reads from", "unit")

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict[str, Any]] = []
        # Row index → tooltip text for the "reads from" cell. Driven by
        # the section-level live validator: an NI binding whose
        # ``(device, task, field)`` doesn't resolve against any declared
        # NI channel paints the cell red and surfaces the available
        # alternatives in the tooltip. Cleared and re-set whenever the
        # section sees a draft / channel mutation.
        self._row_issues: dict[int, str] = {}

    def channels(self) -> list[dict[str, Any]]:
        """Tuple of channel entries managed by this section."""
        return [dict(row) for row in self._rows]

    def set_channels(self, channels: list[dict[str, Any]]) -> None:
        """Replace the section's channel list."""
        self.beginResetModel()
        self._rows = [dict(row) for row in channels]
        self.endResetModel()

    def channel_at(self, row: int) -> dict[str, Any] | None:
        """Return the channel entry at the given row."""
        if 0 <= row < len(self._rows):
            return dict(self._rows[row])
        return None

    def update_channel(self, row: int, channel: dict[str, Any]) -> None:
        """Apply a partial update to one channel entry."""
        if not (0 <= row < len(self._rows)):
            return
        self._rows[row] = dict(channel)
        top_left = self.index(row, 0)
        bottom_right = self.index(row, len(self.HEADERS) - 1)
        self.dataChanged.emit(top_left, bottom_right)
        self.channelsChanged.emit()

    def add_channel(self, channel: dict[str, Any]) -> int:
        """Append a new channel entry to the section."""
        row = len(self._rows)
        self.beginInsertRows(QModelIndex(), row, row)
        self._rows.append(dict(channel))
        self.endInsertRows()
        self.channelsChanged.emit()
        return row

    def remove_channel(self, row: int) -> None:
        """Remove the channel entry at the given row."""
        if not (0 <= row < len(self._rows)):
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        self._rows.pop(row)
        self.endRemoveRows()
        self.channelsChanged.emit()

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
        if not index.isValid():
            return None
        row = index.row()
        rec = self._rows[row] if 0 <= row < len(self._rows) else None
        if rec is None:
            return None
        key = self.COLUMN_KEYS[index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            if key == "device":
                source = rec.get("source") or {}
                return str(source.get("device", "—")) if isinstance(source, dict) else "—"
            if key == "binding":
                source = rec.get("source") or {}
                return str(source.get("source", "—")) if isinstance(source, dict) else "—"
            value = rec.get(key, "")
            return str(value) if value is not None else ""
        # Row-issue surfacing — paint the "reads from" column for any row
        # whose NI binding doesn't resolve against the declared NI inventory.
        # Background + tooltip combined make the issue noticeable without
        # depending on a theme that respects QPalette::Highlight.
        if key == "binding" and row in self._row_issues:
            from PySide6.QtGui import QBrush, QColor  # noqa: PLC0415

            if role == Qt.ItemDataRole.BackgroundRole:
                return QBrush(QColor("#3a1a1a"))  # readable on dark + light themes
            if role == Qt.ItemDataRole.ToolTipRole:
                return self._row_issues[row]
        return None

    def set_row_issues(self, issues: dict[int, str]) -> None:
        """Update the per-row validation messages used to paint the
        "reads from" cell. Issues map row indices to the tooltip text.
        Does not emit ``channelsChanged`` — issue state isn't part of the
        payload; only ``dataChanged`` fires so the table repaints.
        """
        if self._row_issues == issues:
            return
        self._row_issues = dict(issues)
        if self._rows:
            top_left = self.index(0, 0)
            bottom_right = self.index(len(self._rows) - 1, len(self.HEADERS) - 1)
            self.dataChanged.emit(top_left, bottom_right)


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


# NI temperature-unit names (TemperatureUnits enum members) → capa unit string.
# Used by the "FROM DECLARED NI INPUTS" Add-menu entries so a channel
# created against a real NI thermocouple row carries the right unit
# label automatically — closing the ``K``-vs-``degC`` mismatch
# the generic NIDAQ_THERMOCOUPLE template used to ship with.
_NI_TEMP_UNITS_TO_CAPA: dict[str, str] = {
    "DEG_C": "degC",
    "DEG_F": "degF",
    "K": "K",
    "DEG_R": "degR",
}


def _nidaq_unit_kind_calibration(
    nidaq_kind: str, nidaq_units: str | None
) -> tuple[str, str, dict[str, Any] | None]:
    """Translate an NI channel row's (kind, units) into the capa channel's
    (unit, kind, calibration) triple. Returns identity calibration when the
    unit is known so the channel passes ChannelSpec's dimensional check.

    Unknown kinds fall back to ``analog_in`` with no unit; the operator
    can refine after creation. This keeps the menu entry useful even
    for digital lines / counters where we haven't picked a default kind.
    """
    if nidaq_kind == "thermocouple":
        unit = _NI_TEMP_UNITS_TO_CAPA.get(nidaq_units or "", "degC")
        return unit, "tc", {"kind": "identity", "input_unit": unit, "output_unit": unit}
    if nidaq_kind == "ai_voltage":
        unit = nidaq_units or "V"
        return unit, "analog_in", {"kind": "identity", "input_unit": unit, "output_unit": unit}
    return "", "analog_in", None


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
        # Declared NI channels (from DeclaredNIDAQChannel) covering every NI
        # device in the current draft. Used by the variant-field renderer
        # to populate combo choices for nidaq_reading_field / nidaq_block_channel
        # so the operator picks from real inventory instead of free-typing.
        # Stored as a tuple of (device, task, field) so the section's
        # callers don't have to depend on the dataclass type.
        self._nidaq_declared: tuple[tuple[str, str, str], ...] = ()

        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)

        self._device_combo = QComboBox(self)
        self._device_combo.currentIndexChanged.connect(self._on_device_changed)
        form.addRow("Device:", self._device_combo)

        self._variant_combo = QComboBox(self)
        self._variant_combo.currentIndexChanged.connect(self._on_variant_changed)
        form.addRow("Reads from:", self._variant_combo)

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
        nidaq_declared: tuple[tuple[str, str, str], ...] = (),
    ) -> None:
        """Refresh the device list and channel-kind ordering hint.

        Called whenever the operator switches rows or edits the kind.
        Keeps the current selection if still valid.

        ``nidaq_declared`` is the tuple of ``(device, task, field)`` triples
        produced by :func:`capa.devices.nidaq_join.declared_channels_from_payload`
        — feeds the task / field combos for NI-DAQ binding variants so the
        operator picks from declared inventory instead of free-typing.
        Pass an empty tuple to keep free-text behaviour (e.g. for tests
        that don't construct the helper).
        """
        self._devices = devices
        self._kind = kind
        self._nidaq_declared = nidaq_declared
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
        """Set this widget's value from a model-side value."""
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
        for field_name, label, dtype, choices_key in spec:
            widget: QWidget
            current_val = self._current.get(field_name)
            choices = self._choices_for(choices_key)
            if choices is not None:
                # Editable combo so offline operators can still type a
                # name the discovery cache doesn't know about — and so
                # configs that load on machines without the NI driver
                # don't lose their bindings.
                combo = QComboBox(self)
                combo.setEditable(True)
                combo.addItems(choices)
                if current_val is not None and current_val not in choices:
                    combo.addItem(str(current_val))
                if current_val is not None:
                    combo.setCurrentText(str(current_val))
                else:
                    combo.setCurrentText("")
                combo.currentTextChanged.connect(self._on_inner_changed)
                widget = combo
            elif dtype is int:
                line = QLineEdit(str(current_val) if current_val is not None else "1")
                line.setPlaceholderText("integer")
                line.textChanged.connect(self._on_inner_changed)
                widget = line
            else:
                line = QLineEdit("")
                if field_name == "inputs" and isinstance(current_val, list):
                    line.setText(", ".join(str(p) for p in current_val))
                elif current_val is not None:
                    line.setText(str(current_val))
                line.textChanged.connect(self._on_inner_changed)
                widget = line
            self._fields_layout.addRow(label + ":", widget)
            self._variant_fields[field_name] = widget

    def _choices_for(self, key: str | None) -> list[str] | None:
        """Return completion choices for a variant field, or ``None`` for
        free-text fields. Filters NI-channel-aware providers by the
        currently-selected device so the combo reflects what's reachable
        from this binding.
        """
        if key is None or not self._nidaq_declared:
            return None
        device_name = self._device_combo.currentData()
        if not isinstance(device_name, str) or not device_name:
            # Without a selected device we can't sensibly filter — show
            # every declared task/field across all NI devices. Edge case
            # in practice; the device combo defaults to the first NI device.
            if key == "nidaq_tasks":
                return sorted({task for (_d, task, _f) in self._nidaq_declared})
            if key == "nidaq_fields":
                return sorted({field for (_d, _t, field) in self._nidaq_declared})
            return None
        if key == "nidaq_tasks":
            return sorted({task for (dev, task, _f) in self._nidaq_declared if dev == device_name})
        if key == "nidaq_fields":
            # Field choices depend on the currently-selected task as well —
            # otherwise NI rigs with multiple tasks per chassis would show
            # cross-task fields that don't actually resolve. Falls back to
            # the device-wide list when no task is yet picked.
            current_task = self._current.get("task")
            if isinstance(current_task, str) and current_task:
                return sorted(
                    {
                        field
                        for (dev, task, field) in self._nidaq_declared
                        if dev == device_name and task == current_task
                    }
                )
            return sorted(
                {field for (dev, _t, field) in self._nidaq_declared if dev == device_name}
            )
        return None

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
        sender = self.sender()
        self._current = self.value()
        if sender is self._variant_fields.get("task"):
            self._suppress = True
            try:
                self._rebuild_variant_fields()
            finally:
                self._suppress = False
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

        # Two-layer model explainer: the operator-facing description of
        # what a "channel" is in this section vs. an "NI cDAQ input" in
        # the Devices section. Keeps the join's first-class status visible
        # without forcing the operator to read the glossary.
        hint = QLabel(
            "Capa channels are the named signals recorded into your run. For NI-DAQ, "
            "each one reads from a row in the device's DAQ inputs table.",
            self,
        )
        hint.setStyleSheet("color: #888; font-size: 9pt; padding-bottom: 4px;")
        hint.setWordWrap(True)
        outer.addWidget(hint)

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
        header = self._table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            header.setStretchLastSection(True)
        selection_model = self._table.selectionModel()
        if selection_model is not None:
            selection_model.selectionChanged.connect(self._on_row_changed)
        # Grow the table to fit its rows up to a typical rig's channel
        # count (~15). Past that, the scrollbar comes back so a pathological
        # config doesn't dominate the section.
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
        self._detail_placeholder = QLabel(
            "Select a channel to edit, or use Add channel ▾.",
            self._detail,
        )
        self._detail_placeholder.setStyleSheet("color: #888;")
        detail_layout.addWidget(self._detail_placeholder)

        # Simple / Expert toggle. The Channels detail pane is the most
        # intimidating surface in capa — most users want to rename a
        # channel, change its plot group, or apply a calibration, not
        # edit raw binding parameters. Simple is the default per
        # channel; Expert exposes the source-binding editor, sample
        # rate, and full calibration sub-form.
        toggle_row = QHBoxLayout()
        toggle_row.setContentsMargins(0, 0, 0, 0)
        toggle_row.setSpacing(8)
        self._expert_toggle = QCheckBox("Expert view", self._detail)
        self._expert_toggle.setToolTip(
            "Show the source-binding editor, sample-rate field, and full "
            "calibration sub-form. Per-channel — switching channels reverts "
            "to that channel's last state."
        )
        self._expert_toggle.toggled.connect(self._on_expert_toggled)
        toggle_row.addWidget(self._expert_toggle)
        toggle_row.addStretch(1)
        self._toggle_row_widget = QWidget(self._detail)
        self._toggle_row_widget.setLayout(toggle_row)
        self._toggle_row_widget.hide()
        detail_layout.addWidget(self._toggle_row_widget)

        # Per-channel expert-mode state. Indexed by channel name so it
        # survives row reorderings; defaults to Simple for any unknown
        # name. Stored as a set of channel names currently in Expert.
        self._expert_channels: set[str] = set()

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

        # Simple-view summary row showing where the channel reads from
        # without exposing the discriminated-union details. Hidden in
        # Expert view; the full _source_box takes over.
        self._reads_from_widget = QWidget(self._detail)
        reads_layout = QFormLayout(self._reads_from_widget)
        reads_layout.setContentsMargins(0, 8, 0, 0)
        self._reads_from_label = QLabel("—", self._reads_from_widget)
        self._reads_from_label.setStyleSheet("color: #475467;")
        self._reads_from_label.setToolTip(
            "Where this channel's value comes from. Switch to Expert view "
            "to edit the source binding."
        )
        reads_layout.addRow("Reads from:", self._reads_from_label)
        self._reads_from_widget.hide()
        detail_layout.addWidget(self._reads_from_widget)

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
        # Table region claims its fixed size (driven by ``fit_table_height``);
        # detail collects the remaining space.
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
        # NI binding live validation depends on the declared NI inventory
        # in the draft — recompute now so the table paints unresolved
        # rows red even before the operator touches a cell.
        self._refresh_row_issues()

    def payload(self) -> dict[str, object]:
        """Build the section's serialized payload from current widget state."""
        return {"channels": self._model.channels()}

    def _update_table_height(self) -> None:
        fit_table_height(self._table, max_rows=15)

    # -- slots: table -------------------------------------------------------

    def _on_rows_changed(self) -> None:
        if self._suppress:
            return
        self._refresh_row_issues()
        self.valuesChanged.emit()

    def _refresh_row_issues(self) -> None:
        """Recompute the per-row validation issues that paint NI bindings red.

        Mirrors the save-time check in :func:`_layer2_nidaq_join` but runs
        on every model mutation so the operator sees the problem immediately
        — the Problems panel still re-validates on the 200 ms debounce, but
        the table-cell paint is the fast-feedback channel.
        """
        declared = self._current_nidaq_declared()
        task_keys = self._current_nidaq_task_keys()
        if not declared and not task_keys:
            self._model.set_row_issues({})
            return
        declared_keys = set(declared)
        by_device_task: dict[tuple[str, str], list[str]] = {}
        for declared_dev, declared_task, declared_field in declared:
            by_device_task.setdefault((declared_dev, declared_task), []).append(declared_field)
        issues: dict[int, str] = {}
        for idx, row in enumerate(self._model.channels()):
            source = row.get("source")
            if not isinstance(source, dict):
                continue
            kind = source.get("source")
            if kind not in ("nidaq_reading_field", "nidaq_block_channel"):
                continue
            device = source.get("device")
            task = source.get("task")
            field = source.get("field") if kind == "nidaq_reading_field" else source.get("channel")
            if not (
                isinstance(device, str)
                and isinstance(task, str)
                and isinstance(field, str)
                and device
                and task
                and field
            ):
                # Incomplete edit — let Layer-1 / Layer-2 surface this when
                # the operator commits; don't paint mid-edit.
                continue
            if (device, task, field) in declared_keys:
                continue
            available = sorted(set(by_device_task.get((device, task), [])))
            if available:
                issues[idx] = (
                    f"Field {field!r} not declared on {device}.{task}. Available: {available}"
                )
            elif (device, task) in task_keys:
                issues[idx] = f"No NI fields are declared on {device}.{task}."
            else:
                tasks_on_device = sorted(
                    {t for (d, t) in by_device_task if d == device}
                    | {t for (d, t) in task_keys if d == device}
                )
                if tasks_on_device:
                    issues[idx] = (
                        f"Task {task!r} not declared on device {device!r}. "
                        f"Available tasks: {tasks_on_device}"
                    )
                else:
                    issues[idx] = f"Device {device!r} has no NI channels declared in the draft."
        self._model.set_row_issues(issues)

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
                    nidaq_declared=self._current_nidaq_declared(),
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
        # Keep the Simple-view summary in sync so toggling back to Simple
        # after an Expert edit shows the new binding immediately.
        self._reads_from_label.setText(_compose_reads_from(source))

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

    def _current_nidaq_declared(self) -> tuple[tuple[str, str, str], ...]:
        """Walk the current draft and collect declared NI channels as the
        flat ``(device, task, field)`` tuples the binding-editor combo
        provider expects. Returns an empty tuple when no NI device is
        declared, when params are malformed, or when the draft itself
        isn't loaded yet.
        """
        if self._draft is None:
            return ()
        from capa.devices.nidaq_join import (  # noqa: PLC0415
            declared_channels_from_payload,
        )

        declared = declared_channels_from_payload(self._draft.document.hardware_payload)
        return tuple((d.device_name, d.task_name, d.field_name) for d in declared)

    def _current_nidaq_task_keys(self) -> set[tuple[str, str]]:
        """Return declared NI ``(device, task)`` keys in the current draft."""
        if self._draft is None:
            return set()
        from capa.devices.nidaq_join import (  # noqa: PLC0415
            nidaq_task_keys_from_payload,
        )

        return nidaq_task_keys_from_payload(self._draft.document.hardware_payload)

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
        self._toggle_row_widget.hide()
        self._identity_widget.hide()
        self._reads_from_widget.hide()
        self._source_box.hide()
        self._units_widget.hide()
        self._calibration_box.hide()
        self._detail_placeholder.show()
        if self._calibration_field is not None:
            self._calibration_field.deleteLater()
            self._calibration_field = None

    def _build_detail(self, channel: dict[str, Any]) -> None:
        self._detail_placeholder.hide()
        self._toggle_row_widget.show()
        self._identity_widget.show()
        self._units_widget.show()
        self._calibration_box.show()

        # Sync the toggle to the per-channel expert state. ``_on_expert_toggled``
        # would re-fire under normal Qt signals; ``_suppress`` blocks that.
        channel_name = str(channel.get("name", ""))
        is_expert = channel_name in self._expert_channels
        self._suppress = True
        try:
            self._expert_toggle.setChecked(is_expert)
        finally:
            self._suppress = False
        self._apply_expert_visibility(is_expert)

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
                nidaq_declared=self._current_nidaq_declared(),
            )
            self._source_editor.set_value(channel.get("source") or {})

            # Simple-view summary.
            self._reads_from_label.setText(_compose_reads_from(channel.get("source")))

            # Calibration sub-form — uses _DiscriminatedUnionField directly.
            self._rebuild_calibration(channel.get("calibration") or {})
        finally:
            self._suppress = False

    def _on_expert_toggled(self, checked: bool) -> None:
        if self._suppress or self._current_row < 0:
            return
        channel = self._model.channel_at(self._current_row)
        if channel is None:
            return
        name = str(channel.get("name", ""))
        if checked:
            self._expert_channels.add(name)
        else:
            self._expert_channels.discard(name)
        self._apply_expert_visibility(checked)

    def _apply_expert_visibility(self, expert: bool) -> None:
        """Swap which sub-widgets render the source / sample-rate fields.

        Simple view shows the auto-derived "Reads from: device.field"
        label; Expert view shows the full discriminated-union editor and
        the sample-rate row. The two are mutually exclusive; the
        calibration block stays visible in both views.
        """
        self._reads_from_widget.setVisible(not expert)
        self._source_box.setVisible(expert)
        # Sample-rate row is part of the units form; hide it individually
        # so the unit / derived-unit rows remain in Simple view.
        self._sample_rate_edit.setVisible(expert)
        units_layout = self._units_widget.layout()
        if isinstance(units_layout, QFormLayout):
            label = units_layout.labelForField(self._sample_rate_edit)
            if label is not None:
                label.setVisible(expert)

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

        # NI-aware entries first — one menu item per declared NI channel
        # in the current draft. These are the highest-leverage Add path
        # because they wire the binding directly to a known NI input
        # row, so the operator never has to type a field name. Empty
        # when no NI device has channels declared yet.
        self._append_declared_nidaq_entries()

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

    def _append_declared_nidaq_entries(self) -> None:
        """Add one menu entry per declared NI channel in the draft.

        Units, kind, and calibration come from the NI row's declared
        ``units`` — closing the latent ``K``/``degC`` mismatch in the
        generic ``NIDAQ_THERMOCOUPLE`` template for any new channel
        added through this menu. The generic-template add closes the
        same mismatch; this is the hardware-aware version.
        """
        if self._draft is None:
            return
        from capa.devices.nidaq_join import (  # noqa: PLC0415
            declared_channels_from_payload,
        )

        declared = declared_channels_from_payload(self._draft.document.hardware_payload)
        if not declared:
            return
        header = self._add_menu.addAction("FROM DECLARED NI INPUTS")
        header.setEnabled(False)
        for d in declared:
            label = f"capa channel from {d.device_name}.{d.field_name} ({d.physical_channel})"
            action = self._add_menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, decl=d: self._on_add_from_declared_nidaq(decl)
            )
        self._add_menu.addSeparator()

    def _on_add_from_declared_nidaq(self, decl: Any) -> None:
        """Insert a capa channel pre-bound to a declared NI input.

        Units / kind / calibration are derived from the NI row's
        ``units``: thermocouple kinds with ``DEG_C`` produce a
        ``unit="degC"`` ``kind="tc"`` channel with identity-degC
        calibration. Falls back to a blank-unit ``analog_in`` row for
        kinds we don't have a strong default for so the operator at
        least gets a working binding.
        """
        existing = [c.get("name", "") for c in self._model.channels()]
        unit, kind, calibration = _nidaq_unit_kind_calibration(decl.kind, decl.units)
        record: dict[str, Any] = {
            "name": unique_name(existing, decl.field_name),
            "kind": kind,
            "unit": unit,
            "source": {
                "source": "nidaq_reading_field",
                "device": decl.device_name,
                "task": decl.task_name,
                "field": decl.field_name,
            },
        }
        if unit:
            record["derived_unit"] = unit
        if calibration is not None:
            record["calibration"] = calibration
        if kind == "tc":
            record["plot_group"] = "temperatures"
        new_row = self._model.add_channel(record)
        self._table.selectRow(new_row)

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
