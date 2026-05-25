"""``NIDAQChannelsField`` — Devices-pane editor for ``NIDAQAdapterParams.channels``.

The auto-form factory's generic ``tuple[NIDAQChannelConfig, ...]`` path
falls through to :class:`_JsonFallbackField` (a single ``QLineEdit``
where the operator pastes JSON) because the element type is an
``Annotated[Union, Discriminator]`` — :func:`isinstance(elem_type, type)`
fails, so the factory can't build a typed sub-form for each row. This
widget replaces that fallback with a hardware-aware editor:

* **Table** of declared NI channels (one row per ``[[devices.params.channels]]``
  entry): name, physical channel, kind, kind-specific summary.
* **Detail pane** below the table, kind-aware — picking ``thermocouple``
  reveals TC type / min / max / CJC / units / ADC timing; ``ai_voltage``
  reveals range / terminal config; ``raw`` is a pass-through JSON
  fallback for unfamiliar kinds. Kind switching preserves overlapping
  field values (the same buffer pattern as :class:`_DiscriminatedUnionField`).
* **Add from inventory** menu populated from
  :func:`get_nidaq_inventory_provider`'s callable, which the SetupTab
  sets at construction. Physical channels already used in this task are
  greyed out. Falls back to "Add blank" when no inventory is available
  (offline editing / machine with no NI driver).
* **Inline validation**: duplicate ``name`` or ``physical_channel`` paints
  a red border with the offending value in the tooltip.

The widget never writes to the top-level ``[[channels]]`` table or any
other section: cross-section mutations are SetupTab's job. Operator
actions that need a dual write (delete-a-channel-that's-bound, "create
capa channels for unbound inputs") emit Qt signals that SetupTab routes
through ``_apply_payload``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from capa.devices.nidaq_channels import (
    NIDAQChannelConfig,
    NIDAQRawChannelConfig,
    NIDAQThermocoupleConfig,
    NIDAQVoltageConfig,
)
from capa.ui.forms.widgets._base import FieldWidget
from capa.ui.forms.widgets._helpers import _ERROR_STYLE

# ---------------------------------------------------------------------------
# Inventory provider hook
#
# The widget needs the current NI hardware inventory (from
# :func:`capa.devices.nidaq.discover`) to populate "Add from inventory".
# That state lives on :class:`SetupTab` (it's the only thing that owns
# the long-lived cache + Rescan button). To avoid coupling the widget
# to SetupTab — or threading an accessor through every level of the
# form-building chain — we expose a module-level provider hook.
# ``SetupTab`` sets it once at construction; the widget reads from it
# lazily when the operator opens the menu.
# ---------------------------------------------------------------------------


InventoryProvider = Callable[[], Mapping[str, Mapping[str, Any]]]
"""Returns a mapping ``device_name → device_info`` matching the dict
shape from :func:`capa.devices.nidaq.discover` (``ai_channels``,
``ao_channels``, etc.). Returning ``{}`` is fine — the widget falls back
to "Add blank" when no inventory is available."""


BoundFieldsProvider = Callable[[], set[tuple[str, str, str]]]
"""Returns the set of ``(device, task, field)`` triples currently
referenced by a capa-side ``[[channels]]`` binding. Used by the widget's
delete handler to detect when removing an NI input row would break a
capa channel — the widget emits ``deleteWithBindingsRequested`` and the
SetupTab orchestrator handles the dual-section prompt."""


BoundNamesProvider = Callable[[str, str, str], set[str]]
"""Returns the names of capa-side ``[[channels]]`` rows referencing one
NI ``(device, task, field)`` triple. Used for delete-propagation prompts."""


CrossSectionHandler = Callable[..., None]
"""SetupTab-supplied receiver for cross-section action requests. The
widget itself does not perform dual-section writes; it routes them
through this handler so :class:`SetupTab._apply_payload` stays the
single source of truth for payload mutations."""


@dataclass
class _ProviderState:
    inventory: InventoryProvider | None = None
    bound_fields: BoundFieldsProvider | None = None
    bound_names: BoundNamesProvider | None = None
    create_capa_channels: CrossSectionHandler | None = None
    delete_with_bindings: CrossSectionHandler | None = None
    rescan_inventory: Callable[[], None] | None = None


_providers = _ProviderState()


def set_nidaq_inventory_provider(provider: InventoryProvider | None) -> None:
    """Install a callable that returns the current NI-DAQ inventory.

    SetupTab calls this in its ``__init__`` so every NIDAQChannelsField
    instance — present and future — sees the same cache. Pass ``None``
    to reset (used by tests that don't want the previous provider to
    leak across cases)."""
    _providers.inventory = provider


def get_nidaq_inventory_provider() -> InventoryProvider | None:
    """Return the installed provider, or ``None`` if no SetupTab is wired."""
    return _providers.inventory


def set_nidaq_bound_provider(provider: BoundFieldsProvider | None) -> None:
    """Install a callable that returns the bound NI ``(device, task, field)``
    triples. Used by the widget to detect unbound-channel banner state
    and delete-propagation prompts."""
    _providers.bound_fields = provider


def set_nidaq_bound_names_provider(provider: BoundNamesProvider | None) -> None:
    """Install a callable that returns capa channel names for one NI triple."""
    _providers.bound_names = provider


def set_nidaq_cross_section_handlers(
    *,
    create_capa_channels: CrossSectionHandler | None = None,
    delete_with_bindings: CrossSectionHandler | None = None,
) -> None:
    """Install SetupTab-side receivers for cross-section actions.

    Either handler may be ``None`` to disable that path. The widget
    short-circuits its cross-section emit if no handler is installed —
    that keeps the widget functional in tests / standalone use without
    routing dual-section writes through a non-existent orchestrator.
    """
    _providers.create_capa_channels = create_capa_channels
    _providers.delete_with_bindings = delete_with_bindings


def set_nidaq_rescan_handler(handler: Callable[[], None] | None) -> None:
    """Install the SetupTab rescan callback used by the empty-inventory menu."""
    _providers.rescan_inventory = handler


# ---------------------------------------------------------------------------
# Kind metadata
#
# One descriptor per typed kind: how to label it in the menu, which
# Pydantic model parses it, the kind-specific detail fields the operator
# can edit, and the default-row factory used when the operator picks
# "Add from inventory" on a physical channel.
# ---------------------------------------------------------------------------


_KIND_LABELS: dict[str, str] = {
    "thermocouple": "Thermocouple",
    "ai_voltage": "Analog input — voltage",
    "raw": "Other (raw passthrough)",
}


# Per-kind detail-field spec: (model field name, label, dtype, choices or None).
# Renderer maps dtype to widget — ``str`` → QLineEdit, ``float`` → QLineEdit
# (we don't run QDoubleSpinBox here because NI ranges span orders of
# magnitude and the spinbox arrows aren't useful), ``Literal`` choices →
# editable QComboBox. ``raw`` kind has no detail fields; the operator
# edits its dict via the table's name + physical_channel cells plus the
# free-form metadata column.
_KIND_DETAIL_FIELDS: dict[str, tuple[tuple[str, str, type, tuple[str, ...] | None], ...]] = {
    "thermocouple": (
        ("thermocouple_type", "TC type", str, ("J", "K", "N", "R", "S", "T", "B", "E", "A", "C")),
        ("min_val", "Min (units)", float, None),
        ("max_val", "Max (units)", float, None),
        (
            "cjc_source",
            "CJC source",
            str,
            ("", "BUILT_IN", "CONSTANT_USER_VALUE", "SCANNABLE_CHANNEL"),
        ),
        ("cjc_val", "CJC value", float, None),
        ("units", "Units", str, ("DEG_C", "DEG_F", "K", "DEG_R", "FROM_CUSTOM_SCALE")),
        (
            "adc_timing_mode",
            "ADC timing",
            str,
            (
                "",
                "AUTOMATIC",
                "HIGH_RESOLUTION",
                "HIGH_SPEED",
                "BEST_50_HZ_REJECTION",
                "BEST_60_HZ_REJECTION",
                "CUSTOM",
            ),
        ),
        (
            "auto_zero_mode",
            "Auto-zero",
            str,
            ("", "NONE", "ONCE", "EVERY_SAMPLE"),
        ),
    ),
    "ai_voltage": (
        ("min_val", "Min (V)", float, None),
        ("max_val", "Max (V)", float, None),
        (
            "terminal_config",
            "Terminal config",
            str,
            ("", "RSE", "NRSE", "DIFF", "PSEUDO_DIFF", "DEFAULT"),
        ),
        ("custom_scale_name", "Custom scale", str, None),
        (
            "adc_timing_mode",
            "ADC timing",
            str,
            (
                "",
                "AUTOMATIC",
                "HIGH_RESOLUTION",
                "HIGH_SPEED",
                "BEST_50_HZ_REJECTION",
                "BEST_60_HZ_REJECTION",
                "CUSTOM",
            ),
        ),
        (
            "auto_zero_mode",
            "Auto-zero",
            str,
            ("", "NONE", "ONCE", "EVERY_SAMPLE"),
        ),
    ),
    "raw": (),
}


def _default_row_for_kind(kind: str, physical_channel: str, name: str) -> dict[str, Any]:
    """Synthesise a fresh row dict for the given kind.

    The defaults match the canonical NI 9214 / 9215 setup most CAPA
    rigs use — K-type TC with built-in CJC and DEG_C output for
    thermocouples; ±10 V differential for voltage. ``raw`` kind ships
    the minimum dict the pass-through model accepts; operators paste
    additional fields as needed.
    """
    if kind == "thermocouple":
        return {
            "kind": "thermocouple",
            "physical_channel": physical_channel,
            "name": name,
            "thermocouple_type": "K",
            "min_val": 0.0,
            "max_val": 1000.0,
            "cjc_source": "BUILT_IN",
            "units": "DEG_C",
            "adc_timing_mode": "HIGH_RESOLUTION",
            "auto_zero_mode": "ONCE",
        }
    if kind == "ai_voltage":
        return {
            "kind": "ai_voltage",
            "physical_channel": physical_channel,
            "name": name,
            "min_val": -10.0,
            "max_val": 10.0,
            "terminal_config": "DIFF",
        }
    return {
        "kind": kind or "raw",
        "physical_channel": physical_channel,
        "name": name,
    }


def _row_summary(row: Mapping[str, Any]) -> str:
    """One-line kind-specific detail for the table row.

    Mirrors what the operator wants at a glance: TC type + range for
    thermocouples, range for voltage, raw kind string otherwise. Empty
    string when nothing distinguishing is set yet.
    """
    kind = row.get("kind")
    if kind == "thermocouple":
        tc = row.get("thermocouple_type") or "?"
        lo = row.get("min_val")
        hi = row.get("max_val")
        units = row.get("units") or "DEG_C"
        return f"{tc} TC, {lo}…{hi} {units}"
    if kind == "ai_voltage":
        lo = row.get("min_val")
        hi = row.get("max_val")
        return f"{lo}…{hi} V"
    if isinstance(kind, str):
        return kind
    return ""


def _normalise_model_or_dict(entry: Any) -> dict[str, Any] | None:
    """Coerce a NIDAQ channel — either a model instance or a dict — to a dict.

    The form's ``set_value`` receives validated Pydantic models in the
    runtime path and raw dicts during incremental edits. Both flow
    through the same table-row representation.
    """
    if isinstance(entry, BaseModel):
        return entry.model_dump(exclude_none=True)
    if isinstance(entry, Mapping):
        return {k: v for k, v in entry.items() if v is not None}
    return None


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------


class NIDAQChannelsField(FieldWidget):
    """Devices-pane editor for the NI-DAQ ``channels`` array.

    Opt-in via ``Field(json_schema_extra={"capa_widget": "nidaq_channels"})``
    on ``NIDAQAdapterParams.channels`` so plugin adapters with their own
    discriminated-union tuples aren't affected by the dispatcher change.

    Emits two cross-section requests for actions SetupTab orchestrates
    (never performed by this widget directly):

    * ``unboundChannelsActionRequested`` (list of declared NI channels)
      — operator clicked "Create capa channels…" in the unbound banner.
    * ``deleteWithBindingsRequested`` (device, task, field, bound_names)
      — operator removed an NI input row referenced by one or more capa
      channels. SetupTab opens the confirm-and-propagate prompt.
    """

    unboundChannelsActionRequested = Signal(list)  # noqa: N815
    deleteWithBindingsRequested = Signal(str, str, str, list)  # noqa: N815

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._suppress = False
        self._rows: list[dict[str, Any]] = []
        # Detail-field widgets for the currently-selected row, keyed by
        # field name. Rebuilt every time the row selection or kind
        # changes. Cross-kind buffer (last seen value per field name)
        # preserves overlapping values across kind switches, matching
        # :class:`_DiscriminatedUnionField`'s buffer pattern.
        self._detail_widgets: dict[str, QWidget] = {}
        self._detail_buffer: dict[str, Any] = {}
        self._current_row: int = -1
        self._device_name = ""
        self._task_name = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # Toolbar: Add ▾ menu + Add blank + Remove. Visible buttons match
        # the convention the Channels section uses.
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        self._add_btn = QToolButton(self)
        self._add_btn.setText("Add from inventory ▾")
        self._add_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._add_menu = QMenu(self._add_btn)
        self._add_btn.setMenu(self._add_menu)
        self._add_menu.aboutToShow.connect(self._rebuild_add_menu)
        bar.addWidget(self._add_btn)
        self._add_blank_btn = QPushButton("Add blank", self)
        self._add_blank_btn.clicked.connect(self._on_add_blank)
        bar.addWidget(self._add_blank_btn)
        self._remove_btn = QPushButton("Remove", self)
        self._remove_btn.clicked.connect(self._on_remove)
        bar.addWidget(self._remove_btn)
        bar.addStretch(1)
        outer.addLayout(bar)

        # Table of channels.
        self._table = QTableWidget(0, 4, self)
        self._table.setHorizontalHeaderLabels(["Name", "Physical channel", "Kind", "Detail"])
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked)
        header = self._table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._table.itemSelectionChanged.connect(self._on_row_selected)
        self._table.itemChanged.connect(self._on_table_item_changed)
        # CAPA tasks have at most a handful of channels per NI device, so
        # the table expands to fit its rows rather than scrolling. Hide
        # the vertical scrollbar and tell the layout the table's height
        # is fixed (recomputed in ``_update_table_height``).
        self._table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        outer.addWidget(self._table)

        # Detail pane (kind-switcher + per-kind fields).
        detail_holder = QWidget(self)
        detail_outer = QVBoxLayout(detail_holder)
        detail_outer.setContentsMargins(0, 6, 0, 0)
        kind_row = QHBoxLayout()
        kind_row.addWidget(QLabel("Kind:", detail_holder))
        self._kind_combo = QComboBox(detail_holder)
        for value, label in _KIND_LABELS.items():
            self._kind_combo.addItem(label, value)
        self._kind_combo.currentIndexChanged.connect(self._on_kind_changed)
        kind_row.addWidget(self._kind_combo)
        kind_row.addStretch(1)
        detail_outer.addLayout(kind_row)
        self._detail_host = QWidget(detail_holder)
        self._detail_layout = QFormLayout(self._detail_host)
        self._detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_outer.addWidget(self._detail_host)
        outer.addWidget(detail_holder)

        # Validation banner — collision summary at the bottom. Hidden
        # when the table is clean.
        self._banner = QLabel("", self)
        self._banner.setStyleSheet("color: #d33; padding: 2px;")
        self._banner.setVisible(False)
        outer.addWidget(self._banner)

        # Unbound NI inputs banner — distinct from the validation banner
        # because it's actionable (offers the "Create capa channels" button)
        # and informational (not a schema error). Hidden when every NI
        # channel is referenced by at least one capa channel.
        unbound_bar = QHBoxLayout()
        unbound_bar.setContentsMargins(0, 4, 0, 0)
        self._unbound_label = QLabel("", self)
        self._unbound_label.setStyleSheet("color: #a86b00;")
        unbound_bar.addWidget(self._unbound_label)
        self._unbound_btn = QPushButton("Create capa channels…", self)
        self._unbound_btn.clicked.connect(self._on_create_capa_channels_clicked)
        unbound_bar.addWidget(self._unbound_btn)
        unbound_bar.addStretch(1)
        self._unbound_widget = QWidget(self)
        self._unbound_widget.setLayout(unbound_bar)
        self._unbound_widget.setVisible(False)
        outer.addWidget(self._unbound_widget)

        self._rebuild_table()
        self._reset_detail()

    # -- FieldWidget API ----------------------------------------------------

    def value(self) -> list[dict[str, Any]]:
        """Return the channels list as plain dicts.

        Mirrors :func:`NIDAQChannelConfig`'s expected dict shape so
        :func:`pydantic.TypeAdapter(NIDAQChannelConfig).validate_python`
        round-trips cleanly. Empty values (``None`` / ``""``) are
        stripped so optional fields stay unset by default.
        """
        # Make sure the detail-pane edits are reflected in the underlying
        # row before exporting.
        self._commit_detail_to_current_row()
        return [{k: v for k, v in row.items() if v not in (None, "")} for row in self._rows]

    def set_value(self, v: Any) -> None:
        """Set this widget's value from a model-side value."""
        rows: list[dict[str, Any]] = []
        if isinstance(v, Iterable):
            for entry in v:
                row = _normalise_model_or_dict(entry)
                if row is not None:
                    rows.append(row)
        self._suppress = True
        try:
            self._rows = rows
            self._rebuild_table()
            self._reset_detail()
        finally:
            self._suppress = False
        self._refresh_validation()

    def set_join_context(self, *, device_name: str, task_name: str) -> None:
        """Set the owning NI device/task for this params-field instance.

        The form widget edits only ``params.channels``. Its parent
        Devices row owns the join context, so the section calls this after
        building the params form and whenever device name or task changes.
        """
        self._device_name = device_name
        self._task_name = task_name
        self._refresh_validation()

    # -- internals: table ---------------------------------------------------

    def _rebuild_table(self) -> None:
        with QSignalBlocker(self._table):
            self._table.setRowCount(len(self._rows))
            for i, row in enumerate(self._rows):
                self._set_table_row(i, row)
        self._update_table_height()

    def _update_table_height(self) -> None:
        """Lock the table height to header + every row, so all channels show at once."""
        table = self._table
        header = table.horizontalHeader()
        header_h = header.sizeHint().height() if header is not None else 0
        if header_h <= 0:
            header_h = table.fontMetrics().height() + 8
        rows_h = sum(table.rowHeight(r) for r in range(table.rowCount()))
        if table.rowCount() == 0:
            vheader = table.verticalHeader()
            rows_h = vheader.defaultSectionSize() if vheader is not None else 24
        frame = 2 * table.frameWidth()
        table.setFixedHeight(header_h + rows_h + frame)

    def _set_table_row(self, i: int, row: Mapping[str, Any]) -> None:
        name = str(row.get("name") or row.get("physical_channel") or "")
        physical = str(row.get("physical_channel") or "")
        kind = str(row.get("kind") or "raw")
        items = [
            QTableWidgetItem(name),
            QTableWidgetItem(physical),
            QTableWidgetItem(kind),
            QTableWidgetItem(_row_summary(row)),
        ]
        # The detail summary cell is computed, not edited inline.
        items[3].setFlags(items[3].flags() & ~Qt.ItemFlag.ItemIsEditable)
        # Kind is edited via the combobox below, not in the table cell.
        items[2].setFlags(items[2].flags() & ~Qt.ItemFlag.ItemIsEditable)
        for col, item in enumerate(items):
            self._table.setItem(i, col, item)

    def _on_table_item_changed(self, item: QTableWidgetItem) -> None:
        if self._suppress:
            return
        row = item.row()
        col = item.column()
        if not (0 <= row < len(self._rows)):
            return
        text = item.text().strip()
        if col == 0:
            self._rows[row]["name"] = text or None
        elif col == 1:
            self._rows[row]["physical_channel"] = text
        # Re-render the summary in case anything changed by side effect,
        # and re-run validation.
        with QSignalBlocker(self._table):
            self._set_table_row(row, self._rows[row])
        self._refresh_validation()
        self.valueChanged.emit()

    # -- internals: detail pane ---------------------------------------------

    def _on_row_selected(self) -> None:
        idx = self._table.currentRow()
        if idx < 0 or idx >= len(self._rows):
            self._reset_detail()
            return
        self._current_row = idx
        row = self._rows[idx]
        kind = str(row.get("kind") or "raw")
        with QSignalBlocker(self._kind_combo):
            target = kind if kind in _KIND_LABELS else "raw"
            for i in range(self._kind_combo.count()):
                if self._kind_combo.itemData(i) == target:
                    self._kind_combo.setCurrentIndex(i)
                    break
        self._rebuild_detail_fields(row)

    def _reset_detail(self) -> None:
        self._current_row = -1
        self._clear_detail_layout()
        self._detail_widgets = {}

    def _clear_detail_layout(self) -> None:
        while self._detail_layout.rowCount() > 0:
            self._detail_layout.removeRow(0)

    def _on_kind_changed(self) -> None:
        if self._suppress or self._current_row < 0:
            return
        # Buffer current edits before swapping the form.
        self._commit_detail_to_current_row()
        kind = self._kind_combo.currentData()
        if not isinstance(kind, str):
            return
        row = self._rows[self._current_row]
        row["kind"] = kind
        # Seed the new kind's defaults for any required field that's
        # currently unset — keeps overlapping values (physical_channel,
        # name, min/max, units) but fills in TC type / terminal_config
        # so a flip from raw → thermocouple isn't immediately broken.
        defaults = _default_row_for_kind(
            kind, str(row.get("physical_channel") or ""), str(row.get("name") or "")
        )
        for k, default_value in defaults.items():
            row.setdefault(k, default_value)
        # Also replay the cross-kind buffer for any overlapping fields.
        for k, v in self._detail_buffer.items():
            if k in defaults and row.get(k) in (None, ""):
                row[k] = v
        self._rebuild_detail_fields(row)
        # Refresh the table summary cell.
        with QSignalBlocker(self._table):
            self._set_table_row(self._current_row, row)
        self.valueChanged.emit()

    def _rebuild_detail_fields(self, row: Mapping[str, Any]) -> None:
        self._clear_detail_layout()
        self._detail_widgets = {}
        kind = str(row.get("kind") or "raw")
        spec = _KIND_DETAIL_FIELDS.get(kind, ())
        if not spec:
            # Raw kind / unknown — render the row as JSON so the
            # operator can edit unfamiliar fields without losing access.
            blob = QLineEdit(self._detail_host)
            blob.setText(
                json.dumps(
                    {k: v for k, v in row.items() if k not in ("kind", "physical_channel", "name")}
                )
            )
            blob.editingFinished.connect(self._on_raw_blob_changed)
            self._detail_layout.addRow("Extra (JSON):", blob)
            self._detail_widgets["__raw__"] = blob
            return
        for field_name, label, dtype, choices in spec:
            widget = self._build_detail_widget(field_name, dtype, choices, row.get(field_name))
            self._detail_layout.addRow(label + ":", widget)
            self._detail_widgets[field_name] = widget

    def _build_detail_widget(
        self,
        field_name: str,
        dtype: type,
        choices: tuple[str, ...] | None,
        current: Any,
    ) -> QWidget:
        if choices is not None:
            combo = QComboBox(self._detail_host)
            combo.setEditable(True)
            for c in choices:
                combo.addItem(c)
            if current is None or current == "":
                combo.setCurrentText("")
            else:
                text = str(current)
                if combo.findText(text) < 0:
                    combo.addItem(text)
                combo.setCurrentText(text)
            combo.currentTextChanged.connect(lambda _t, fn=field_name: self._on_detail_changed(fn))
            return combo
        line = QLineEdit(self._detail_host)
        if current is not None:
            line.setText(str(current))
        line.editingFinished.connect(lambda fn=field_name: self._on_detail_changed(fn))
        return line

    def _on_detail_changed(self, field_name: str) -> None:
        if self._suppress or self._current_row < 0:
            return
        self._commit_detail_to_current_row()
        self.valueChanged.emit()

    def _on_raw_blob_changed(self) -> None:
        if self._suppress or self._current_row < 0:
            return
        widget = self._detail_widgets.get("__raw__")
        if not isinstance(widget, QLineEdit):
            return
        try:
            parsed = json.loads(widget.text() or "{}")
        except json.JSONDecodeError:
            widget.setStyleSheet(_ERROR_STYLE)
            widget.setToolTip("Not valid JSON — fix or revert.")
            return
        widget.setStyleSheet("")
        widget.setToolTip("")
        if not isinstance(parsed, dict):
            return
        row = self._rows[self._current_row]
        # Preserve the protected fields the table edits.
        protected = {
            "kind": row.get("kind"),
            "name": row.get("name"),
            "physical_channel": row.get("physical_channel"),
        }
        self._rows[self._current_row] = {
            **parsed,
            **{k: v for k, v in protected.items() if v is not None},
        }
        with QSignalBlocker(self._table):
            self._set_table_row(self._current_row, self._rows[self._current_row])
        self._refresh_validation()
        self.valueChanged.emit()

    def _commit_detail_to_current_row(self) -> None:
        if self._current_row < 0 or self._current_row >= len(self._rows):
            return
        row = self._rows[self._current_row]
        for field_name, widget in self._detail_widgets.items():
            if field_name == "__raw__":
                continue
            value = self._read_detail_widget(widget)
            if value is None or value == "":
                row.pop(field_name, None)
            else:
                row[field_name] = value
            self._detail_buffer[field_name] = value
        with QSignalBlocker(self._table):
            self._set_table_row(self._current_row, row)
        self._refresh_validation()

    @staticmethod
    def _read_detail_widget(widget: QWidget) -> Any:
        if isinstance(widget, QComboBox):
            return widget.currentText().strip()
        if isinstance(widget, QLineEdit):
            text = widget.text().strip()
            # Numeric autodetect — Pydantic accepts either str or float
            # for the float fields, but a typed-as-str-but-numeric value
            # round-trips cleaner if we coerce here.
            if text and (text.replace(".", "", 1).lstrip("-").isdigit() or _is_floatlike(text)):
                try:
                    return float(text)
                except ValueError:
                    return text
            return text
        return None

    # -- internals: Add menu / Remove ---------------------------------------

    def _rebuild_add_menu(self) -> None:
        self._add_menu.clear()
        provider = get_nidaq_inventory_provider()
        inventory: Mapping[str, Mapping[str, Any]] = {}
        if provider is not None:
            try:
                inventory = provider()
            except Exception:  # pragma: no cover — defensive
                inventory = {}
        if not inventory:
            no_inv = self._add_menu.addAction("No NI inventory cached — Scan from the toolbar")
            no_inv.setEnabled(False)
            if _providers.rescan_inventory is not None:
                rescan = self._add_menu.addAction("Rescan NI inventory…")
                rescan.triggered.connect(_providers.rescan_inventory)
            return
        used_physical = {
            str(row.get("physical_channel"))
            for row in self._rows
            if isinstance(row.get("physical_channel"), str)
        }
        for device_name, info in sorted(inventory.items()):
            header = self._add_menu.addAction(f"— {device_name} —")
            header.setEnabled(False)
            ai_channels = info.get("ai_channels") if isinstance(info, Mapping) else None
            if not isinstance(ai_channels, Sequence):
                ai_channels = ()
            for physical in ai_channels:
                if not isinstance(physical, str):
                    continue
                action = self._add_menu.addAction(physical)
                if physical in used_physical:
                    action.setEnabled(False)
                    action.setText(f"{physical}  (already used)")
                else:
                    action.triggered.connect(
                        lambda _checked=False, pc=physical: self._on_add_from_inventory(pc)
                    )
            self._add_menu.addSeparator()

    def _on_add_from_inventory(self, physical_channel: str) -> None:
        existing_names = {str(r.get("name") or "") for r in self._rows}
        base = physical_channel.replace("/", "_")
        name = base
        suffix = 1
        while name in existing_names:
            suffix += 1
            name = f"{base}_{suffix}"
        new_row = _default_row_for_kind("thermocouple", physical_channel, name)
        self._rows.append(new_row)
        self._rebuild_table()
        self._table.selectRow(len(self._rows) - 1)
        self._refresh_validation()
        self.valueChanged.emit()

    def _on_add_blank(self) -> None:
        existing_names = {str(r.get("name") or "") for r in self._rows}
        base = "channel"
        name = base
        suffix = 1
        while name in existing_names:
            suffix += 1
            name = f"{base}_{suffix}"
        new_row = _default_row_for_kind("thermocouple", "", name)
        self._rows.append(new_row)
        self._rebuild_table()
        self._table.selectRow(len(self._rows) - 1)
        self._refresh_validation()
        self.valueChanged.emit()

    def _on_remove(self) -> None:
        idx = self._table.currentRow()
        if idx < 0 or idx >= len(self._rows):
            return
        # Delete-propagation: if this NI row's field name is referenced
        # by any capa channel binding, route through the SetupTab
        # orchestrator so the operator gets a Yes / Just-input / Cancel
        # prompt and the dual-section write goes through
        # ``_apply_payload``. We don't reach into the top-level channels
        # ourselves — that's the rule the widget honours.
        row = self._rows[idx]
        physical = str(row.get("physical_channel") or "")
        name = str(row.get("name") or physical)
        bound_triples = self._lookup_bound_for_field(name)
        if bound_triples and _providers.delete_with_bindings is not None:
            device, task, field = next(iter(bound_triples))
            bound_capa_names = sorted(self._bound_capa_names_for(device, task, field))
            self.deleteWithBindingsRequested.emit(device, task, field, bound_capa_names)
            _providers.delete_with_bindings(self, device, task, field, bound_capa_names)
            # The handler decides whether to remove the row + propagate.
            # Don't pop locally — the orchestrator will refresh us via
            # set_value if the operator confirms.
            return
        self._rows.pop(idx)
        self._rebuild_table()
        self._reset_detail()
        self._refresh_validation()
        self.valueChanged.emit()

    def _lookup_bound_for_field(self, field_name: str) -> set[tuple[str, str, str]]:
        """Return ``(device, task, field)`` triples matching ``field_name``.

        Uses the bound-fields provider installed by SetupTab; when no
        provider is installed the widget treats every removal as
        non-propagating (typical in standalone tests).
        """
        if _providers.bound_fields is None or not field_name:
            return set()
        try:
            bound = _providers.bound_fields()
        except Exception:  # pragma: no cover — defensive
            return set()
        if self._device_name and self._task_name:
            return {
                triple
                for triple in bound
                if triple == (self._device_name, self._task_name, field_name)
            }
        return {triple for triple in bound if triple[2] == field_name}

    def _bound_capa_names_for(self, device: str, task: str, field: str) -> set[str]:
        """Return the capa channel names referencing the given NI triple.

        Falls back to an empty set when no provider supplies the join; the
        SetupTab handler recomputes names defensively before mutating.
        """
        if _providers.bound_names is None:
            return set()
        try:
            return _providers.bound_names(device, task, field)
        except Exception:  # pragma: no cover — defensive
            return set()

    def _on_create_capa_channels_clicked(self) -> None:
        """Emit + route the "create capa channels for unbound inputs" request."""
        unbound = self._unbound_declared()
        if not unbound:
            return
        self.unboundChannelsActionRequested.emit(unbound)
        if _providers.create_capa_channels is not None:
            _providers.create_capa_channels(self, unbound)

    def _unbound_declared(self) -> list[dict[str, Any]]:
        """Build the unbound-channel descriptors emitted to the SetupTab.

        Each entry carries enough to synthesise a capa channel — the
        NI row's display name, kind, units, plus the (device, task)
        join. The actual device/task names live on the surrounding
        Devices section's row, not on the widget itself, so the
        descriptors are dispatched to SetupTab which already has the
        section context to fill them in.
        """
        bound: set[tuple[str, str, str]] = set()
        if _providers.bound_fields is not None:
            try:
                bound = _providers.bound_fields()
            except Exception:  # pragma: no cover — defensive
                bound = set()
        out: list[dict[str, Any]] = []
        # The widget doesn't know its own device/task — SetupTab walks
        # the draft to match. We hand over the NI row's display name and
        # kind / units; SetupTab cross-references against the draft to
        # produce binding payloads.
        for row in self._rows:
            name = row.get("name") or row.get("physical_channel")
            if not isinstance(name, str) or not name:
                continue
            kind = row.get("kind")
            units = row.get("units") if kind == "thermocouple" else row.get("unit")
            # If any bound triple already names this field, count as
            # bound. Prefer exact context when DevicesSection has supplied
            # it; field-only fallback keeps standalone tests functional.
            if self._device_name and self._task_name:
                is_bound = (self._device_name, self._task_name, name) in bound
            else:
                is_bound = any(field == name for (_d, _t, field) in bound)
            if is_bound:
                continue
            entry = {
                "field_name": name,
                "kind": kind,
                "units": units,
                "physical_channel": row.get("physical_channel"),
            }
            if self._device_name:
                entry["device_name"] = self._device_name
            if self._task_name:
                entry["task_name"] = self._task_name
            out.append(entry)
        return out

    # -- internals: validation ---------------------------------------------

    def _refresh_validation(self) -> None:
        """Surface duplicate names / physical channels.

        Detailed Pydantic errors land in the Problems panel on the
        section's debounced re-validate; this is fast-feedback only. A
        non-empty banner means the table will not pass schema validation.
        """
        problems: list[str] = []
        names: dict[str, list[int]] = {}
        physicals: dict[str, list[int]] = {}
        for i, row in enumerate(self._rows):
            name = row.get("name") or row.get("physical_channel")
            if isinstance(name, str) and name:
                names.setdefault(name, []).append(i)
            physical = row.get("physical_channel")
            if isinstance(physical, str) and physical:
                physicals.setdefault(physical, []).append(i)
        for name, rows in names.items():
            if len(rows) > 1:
                problems.append(f"Duplicate channel name {name!r} on rows {[r + 1 for r in rows]}")
        for physical, rows in physicals.items():
            if len(rows) > 1:
                problems.append(
                    f"Duplicate physical channel {physical!r} on rows {[r + 1 for r in rows]}"
                )
        if problems:
            self._banner.setText(" · ".join(problems))
            self._banner.setVisible(True)
        else:
            self._banner.setVisible(False)
        self._refresh_unbound_banner()

    def _refresh_unbound_banner(self) -> None:
        unbound = self._unbound_declared()
        if not unbound:
            self._unbound_widget.setVisible(False)
            return
        self._unbound_label.setText(f"{len(unbound)} NI input(s) aren't read by a capa channel.")
        # Enable the button only when a SetupTab handler is wired; in
        # standalone tests the signal still emits but the button is a
        # no-op affordance otherwise.
        self._unbound_btn.setEnabled(_providers.create_capa_channels is not None)
        self._unbound_widget.setVisible(True)


def _is_floatlike(text: str) -> bool:
    """Cheap float-prefix check that handles ``-1.0e3``, ``.5``, and friends.

    Returns ``True`` iff :func:`float` would accept the string. Done by
    a try/except because Python's float parser is the canonical answer
    and writing the regex correctly is fiddlier than the helper merits.
    """
    try:
        float(text)
    except (ValueError, TypeError):
        return False
    return True


__all__ = [
    "InventoryProvider",
    "NIDAQChannelsField",
    "get_nidaq_inventory_provider",
    "set_nidaq_bound_names_provider",
    "set_nidaq_bound_provider",
    "set_nidaq_cross_section_handlers",
    "set_nidaq_inventory_provider",
    "set_nidaq_rescan_handler",
]


# Re-export validation models for callers that want to round-trip the
# widget's output dicts through Pydantic. Not strictly needed by the
# widget itself; placed here to keep the public surface discoverable.
__model_exports = (
    NIDAQChannelConfig,
    NIDAQRawChannelConfig,
    NIDAQThermocoupleConfig,
    NIDAQVoltageConfig,
)
del __model_exports
