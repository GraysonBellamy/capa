"""Device settings section — the experiment's ``device_settings`` editor.

One collapsible form per device or camera whose adapter declares
settings (:attr:`AdapterDescriptor.settings`), built from that adapter's
settings model. Every field is optional: its **Set** box decides whether
the experiment declares it; an unticked field is left as the device has
it. Devices are read from the raw hardware payload, so the list keeps up
with unsaved edits in Devices / Cameras.

**Capture from devices** fills the forms from what the connected devices
report — set the rig up on the manual cards once, then save it into the
experiment.

The section writes ``experiment_payload["device_settings"]``; entries
whose device left the hardware are listed with a Remove button rather
than dropped silently.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from capa.devices.registry import ensure_adapters_loaded, get_descriptor
from capa.devices.settings import DeviceSettingsSpec, capture_settings
from capa.ui.async_util import schedule_bg
from capa.ui.forms import ModelForm, build_form
from capa.ui.forms.widgets import CollapsibleGroup
from capa.ui.tabs.setup_sections._base import SectionWidget
from capa.ui.theme import COLOR_FAIL, COLOR_IDLE, COLOR_OK, COLOR_WARN

if TYPE_CHECKING:
    from capa.runtime.dispatch import ManualClient
    from capa.ui.state import RunController
    from capa.ui.tabs.setup_state import SetupDraft


class DeviceSettingsSection(SectionWidget):
    """Editor for the experiment's ``device_settings``."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        ensure_adapters_loaded()
        self._draft: SetupDraft | None = None
        self._controller: RunController | None = None
        self._suppress_signals = False
        self._forms: dict[str, tuple[DeviceSettingsSpec, ModelForm]] = {}
        self._orphans: dict[str, Mapping[str, Any]] = {}
        self._layout_key: tuple[tuple[str, str], ...] | None = None
        self._hardware_names: frozenset[str] = frozenset()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        title = QLabel("Device settings", self)
        title.setStyleSheet("font-size: 14pt; font-weight: 600;")
        outer.addWidget(title)

        intro = QLabel(
            "Applied to the devices when this config loads, after a dialog that lists "
            "what differs. Tick <b>Set</b> on a field to declare it; unticked fields "
            "are left as the device has them. Saved in the experiment file.",
            self,
        )
        intro.setWordWrap(True)
        outer.addWidget(intro)

        capture_row = QHBoxLayout()
        self._capture_btn = QPushButton("Capture from devices", self)
        self._capture_btn.setToolTip(
            "Fill every form below from what the connected devices currently report"
        )
        self._capture_btn.clicked.connect(self._on_capture)
        capture_row.addWidget(self._capture_btn)
        self._status = QLabel("", self)
        self._status.setWordWrap(True)
        capture_row.addWidget(self._status, stretch=1)
        outer.addLayout(capture_row)

        self._body = QWidget(self)
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(4)
        outer.addWidget(self._body)
        outer.addStretch(1)
        self._sync_capture_enabled()

    # -- wiring --------------------------------------------------------------

    def set_run_controller(self, controller: RunController) -> None:
        """Attach the controller whose open devices Capture reads."""
        if self._controller is controller:
            return
        self._controller = controller
        ready_signal = getattr(controller, "hardware_ready_changed", None)
        if ready_signal is not None:
            ready_signal.connect(self._sync_capture_enabled)
        self._sync_capture_enabled()

    # -- SectionWidget API ---------------------------------------------------

    def set_draft(self, draft: SetupDraft) -> None:
        """Replace the in-progress draft."""
        self._draft = draft
        self._layout_key = None
        self.refresh()

    def refresh(self) -> None:
        """Rebuild the forms from the draft's hardware and settings."""
        if self._draft is None:
            return
        document = self._draft.document
        declared = document.experiment_payload.get("device_settings")
        declared = declared if isinstance(declared, Mapping) else {}
        devices = _settings_devices(document.hardware_payload)
        self._hardware_names = _hardware_names(document.hardware_payload)
        orphans = {name: raw for name, raw in declared.items() if name not in devices}
        layout_key = tuple((name, adapter) for name, (adapter, _spec) in devices.items())
        self._suppress_signals = True
        try:
            if layout_key != self._layout_key or set(orphans) != set(self._orphans):
                self._rebuild(devices, declared, orphans)
                self._layout_key = layout_key
            for name, (_spec, form) in self._forms.items():
                raw = declared.get(name)
                form.set_values(dict(raw) if isinstance(raw, Mapping) else {}, replace=True)
            self._orphans = dict(orphans)
        finally:
            self._suppress_signals = False

    def payload(self) -> dict[str, object]:
        """Return ``{"device_settings": {...}}`` — set fields only."""
        out: dict[str, object] = {}
        for name, (_spec, form) in self._forms.items():
            values = {key: value for key, value in form.values().items() if value is not None}
            if values:
                out[name] = values
        for name, raw in self._orphans.items():
            out[name] = dict(raw)
        return {"device_settings": out}

    # -- build ---------------------------------------------------------------

    def _rebuild(
        self,
        devices: Mapping[str, tuple[str, DeviceSettingsSpec]],
        declared: Mapping[str, Any],
        orphans: Mapping[str, Any],
    ) -> None:
        while (item := self._body_layout.takeAt(0)) is not None:
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._forms = {}
        if not devices and not orphans:
            empty = QLabel(
                "No device in this hardware has settings an experiment can declare "
                "(Alicat MFCs, Sartorius balances, IR cameras and USB webcams do).",
                self._body,
            )
            empty.setWordWrap(True)
            empty.setStyleSheet(f"color: {COLOR_IDLE.name()};")
            self._body_layout.addWidget(empty)
        for name, (adapter_id, spec) in devices.items():
            descriptor = get_descriptor(adapter_id)
            label = descriptor.label if descriptor is not None else adapter_id
            group = CollapsibleGroup(
                name, subtitle=label, default_open=name in declared, parent=self._body
            )
            form = build_form(spec.model, parent=group)
            form.valuesChanged.connect(self._on_form_changed)
            group.add_widget(form)
            self._body_layout.addWidget(group)
            self._forms[name] = (spec, form)
        for name in orphans:
            self._body_layout.addWidget(self._orphan_row(name))

    def _orphan_row(self, name: str) -> QWidget:
        row = QWidget(self._body)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 4, 0, 4)
        reason = (
            "this device has no settings an experiment can declare"
            if name in self._hardware_names
            else "not a device or camera in the hardware"
        )
        note = QLabel(f"<b>{name}</b> — {reason}; these settings can't be applied.", row)
        note.setStyleSheet(f"color: {COLOR_WARN.name()};")
        layout.addWidget(note, stretch=1)
        remove = QPushButton("Remove", row)
        remove.clicked.connect(lambda: self._remove_orphan(name))
        layout.addWidget(remove)
        return row

    # -- slots ---------------------------------------------------------------

    def _on_form_changed(self) -> None:
        if self._suppress_signals:
            return
        self.valuesChanged.emit()

    def _remove_orphan(self, name: str) -> None:
        self._orphans.pop(name, None)
        self.valuesChanged.emit()
        self._layout_key = None
        self.refresh()

    def _manual_client(self) -> ManualClient | None:
        return getattr(self._controller, "manual_client", None)

    def _sync_capture_enabled(self, *_args: object) -> None:
        self._capture_btn.setEnabled(self._manual_client() is not None)

    def _on_capture(self) -> None:
        client = self._manual_client()
        if client is None:
            self._set_status("Connect the hardware first (Apply & Connect).", "warn")
            return
        self._capture_btn.setEnabled(False)
        self._set_status("Reading devices…", "idle")
        if schedule_bg(self._capture(client)) is None:
            self._sync_capture_enabled()

    async def _capture(self, client: ManualClient) -> None:
        captured: list[str] = []
        failed: list[str] = []
        self._suppress_signals = True
        try:
            for name, (spec, form) in self._forms.items():
                try:
                    snapshot = await client.device_readback(name)
                except Exception as exc:
                    failed.append(f"{name} ({exc})")
                    continue
                if not isinstance(snapshot, spec.snapshot_type):
                    failed.append(f"{name} (not connected)")
                    continue
                values = capture_settings(spec, snapshot)
                if not values:
                    # A webcam whose controls are out of reach reads back
                    # nothing; replacing would wipe what's declared.
                    failed.append(f"{name} (reported no settings)")
                    continue
                form.set_values(values, replace=True)
                captured.append(name)
        finally:
            self._suppress_signals = False
            self._sync_capture_enabled()
        if captured:
            self.valuesChanged.emit()
        if failed:
            self._set_status(
                f"Couldn't read: {', '.join(failed)}.", "error" if not captured else "warn"
            )
        else:
            self._set_status(f"Captured {', '.join(captured)}.", "ok")

    def _set_status(self, text: str, level: str) -> None:
        color = {"ok": COLOR_OK, "warn": COLOR_WARN, "error": COLOR_FAIL}.get(level, COLOR_IDLE)
        self._status.setText(text)
        self._status.setStyleSheet(f"color: {color.name()};")


def _hardware_names(hardware: Mapping[str, Any]) -> frozenset[str]:
    """Every device and camera name in the raw hardware payload."""
    names: set[str] = set()
    for key in ("devices", "cameras"):
        rows = hardware.get(key)
        if isinstance(rows, Sequence) and not isinstance(rows, str):
            names.update(
                row["name"]
                for row in rows
                if isinstance(row, Mapping) and isinstance(row.get("name"), str)
            )
    return frozenset(names)


def _settings_devices(
    hardware: Mapping[str, Any],
) -> dict[str, tuple[str, DeviceSettingsSpec]]:
    """``{name: (adapter id, spec)}`` for every device and camera in the
    raw hardware payload whose adapter declares settings, devices first."""
    out: dict[str, tuple[str, DeviceSettingsSpec]] = {}
    for key in ("devices", "cameras"):
        rows = hardware.get(key)
        if not isinstance(rows, Sequence) or isinstance(rows, str):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            name, adapter_id = row.get("name"), row.get("adapter")
            if not isinstance(name, str) or not name or not isinstance(adapter_id, str):
                continue
            descriptor = get_descriptor(adapter_id)
            if descriptor is not None and descriptor.settings is not None:
                out[name] = (adapter_id, descriptor.settings)
    return out


__all__ = ["DeviceSettingsSection"]
