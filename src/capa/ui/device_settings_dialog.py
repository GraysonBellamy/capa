""":class:`DeviceSettingsDialog` — confirm and apply an experiment's
``device_settings``.

Lists every declared setting a device currently differs on (current and
declared value, plus any note such as an IR camera's recalibration), and
every one that can't be applied as written. The operator unticks what
they don't want and clicks Apply; the dialog then becomes a status view
of what took.

Two modes:

* ``"load"`` — after a config loads. Buttons: **Apply selected** /
  **Skip**, then **Close**.
* ``"start"`` — before a run starts, when settings have drifted.
  Buttons: **Apply selected** / **Start anyway** / **Cancel**, then
  **Start run** / **Cancel**. :attr:`startRequested` fires when the
  operator chooses to start.

Always opened with :meth:`QDialog.open` — never ``exec()`` — so no nested
event loop runs while the apply task is in flight.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Literal

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent, QColor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from capa.runtime.device_settings import ChangeResult, Outcome, SettingsPlan
from capa.ui.theme import COLOR_FAIL, COLOR_IDLE, COLOR_OK, COLOR_WARN

if TYPE_CHECKING:
    import asyncio

    from capa.runtime.device_settings import SettingsReport
    from capa.ui.state import RunController
    from capa.ui.statusbar import OperatorIdProvider

DialogMode = Literal["load", "start"]

_COLUMNS: Final[tuple[str, ...]] = ("Apply", "Device", "Setting", "Current", "Declared", "Note")
_COL_APPLY, _COL_DEVICE, _COL_SETTING, _COL_CURRENT, _COL_DECLARED, _COL_NOTE = range(6)

_OUTCOME_TEXT: Final[dict[Outcome, str]] = {
    Outcome.VERIFIED: "✓ applied",
    Outcome.UNVERIFIED: "⚠ sent; the device doesn't report it back",
    Outcome.STILL_DIFFERS: "⚠ didn't take",
    Outcome.REFUSED: "✗ refused",
    Outcome.FAILED: "✗ failed",
}
_OUTCOME_COLOR: Final[dict[Outcome, QColor]] = {
    Outcome.VERIFIED: COLOR_OK,
    Outcome.UNVERIFIED: COLOR_WARN,
    Outcome.STILL_DIFFERS: COLOR_WARN,
    Outcome.REFUSED: COLOR_FAIL,
    Outcome.FAILED: COLOR_FAIL,
}


class DeviceSettingsDialog(QDialog):
    """Confirm and apply the settings a device differs on."""

    startRequested = Signal()  # noqa: N815 - Qt signal naming convention
    """``"start"`` mode only: the operator chose Start anyway / Start run."""

    def __init__(
        self,
        *,
        plan: SettingsPlan,
        controller: RunController,
        operator_provider: OperatorIdProvider,
        mode: DialogMode = "load",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Device settings")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.resize(820, 420)
        self._plan = plan
        self._controller = controller
        self._operator_provider = operator_provider
        self._mode: DialogMode = mode
        self._task: asyncio.Task[SettingsReport] | None = None
        self._report: SettingsReport | None = None
        # Table row → (device, setting) for the rows that can be applied.
        self._row_keys: dict[int, tuple[str, str]] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self._header = QLabel(self._header_text(), self)
        self._header.setTextFormat(Qt.TextFormat.RichText)
        self._header.setWordWrap(True)
        layout.addWidget(self._header)

        self._table = QTableWidget(0, len(_COLUMNS), self)
        self._table.setHorizontalHeaderLabels(list(_COLUMNS))
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        for col in range(_COL_NOTE):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_NOTE, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._table, stretch=1)
        self._populate()
        self._table.itemChanged.connect(self._sync_apply_enabled)

        self._status = QLabel("", self)
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self._buttons = QDialogButtonBox(self)
        self._apply_btn = self._add_button("Apply selected", QDialogButtonBox.ButtonRole.AcceptRole)
        self._apply_btn.setDefault(True)
        self._apply_btn.clicked.connect(self._on_apply)
        if mode == "start":
            self._start_btn = self._add_button(
                "Start anyway", QDialogButtonBox.ButtonRole.DestructiveRole
            )
            self._start_btn.clicked.connect(self._on_start)
            self._dismiss_btn = self._add_button("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        else:
            self._dismiss_btn = self._add_button("Skip", QDialogButtonBox.ButtonRole.RejectRole)
        self._dismiss_btn.clicked.connect(self._on_dismiss)
        layout.addWidget(self._buttons)
        self._sync_apply_enabled()

    # ------------------------------------------------------------------ public

    def observed(self) -> dict[str, dict[str, Any]]:
        """Each device's settings as last read — re-read after an apply,
        otherwise as listed. What a run started from here records."""
        source = self._report.plan_after if self._report is not None else self._plan
        return source.observed()

    def selected(self) -> set[tuple[str, str]]:
        """``(device, setting)`` for every ticked row."""
        out: set[tuple[str, str]] = set()
        for row, key in self._row_keys.items():
            item = self._table.item(row, _COL_APPLY)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                out.add(key)
        return out

    @property
    def applying(self) -> bool:
        """``True`` while the apply task is in flight."""
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------ build

    def _header_text(self) -> str:
        count = self._plan.change_count
        if count == 0:
            return "<b>Some declared device settings can't be applied as written.</b>"
        noun = "setting differs" if count == 1 else "settings differ"
        lead = f"<b>{count} device {noun} from the experiment.</b>"
        if self._mode == "start":
            lead = f"{lead} Apply before the run starts?"
        return (
            f"{lead}<br>Ticked settings are applied for this session only; "
            "nothing is saved to the devices."
        )

    def _add_button(self, text: str, role: QDialogButtonBox.ButtonRole) -> QPushButton:
        button = QPushButton(text, self)
        self._buttons.addButton(button, role)
        return button

    def _populate(self) -> None:
        for device in self._plan.devices:
            if device.error is not None:
                self._add_row(device.name, "—", "", "", device.error, key=None)
            for change in device.changes:
                current = change.current if change.current is not None else "unknown"
                self._add_row(
                    device.name,
                    change.label,
                    current,
                    change.desired,
                    change.note or "",
                    key=(device.name, change.field),
                )
            for issue in device.issues:
                self._add_row(device.name, issue.label, "", "", issue.message, key=None)

    def _add_row(
        self,
        device: str,
        setting: str,
        current: str,
        declared: str,
        note: str,
        *,
        key: tuple[str, str] | None,
    ) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        apply_item = QTableWidgetItem("")
        if key is not None:
            apply_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            apply_item.setCheckState(Qt.CheckState.Checked)
            self._row_keys[row] = key
        else:
            apply_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self._table.setItem(row, _COL_APPLY, apply_item)
        for col, text in (
            (_COL_DEVICE, device),
            (_COL_SETTING, setting),
            (_COL_CURRENT, current),
            (_COL_DECLARED, declared),
            (_COL_NOTE, note),
        ):
            item = QTableWidgetItem(text)
            if key is None:
                item.setForeground(COLOR_WARN if col == _COL_NOTE else COLOR_IDLE)
            self._table.setItem(row, col, item)

    # ------------------------------------------------------------------ slots

    def _sync_apply_enabled(self, *_args: object) -> None:
        self._apply_btn.setEnabled(not self.applying and bool(self.selected()))

    def _on_apply(self) -> None:
        selected = self.selected()
        if not selected:
            return
        operator = self._operator_provider.current_operator_id()
        if not operator:
            self._set_status("Set an operator id first (status bar).", COLOR_WARN)
            return
        task = self._controller.apply_device_settings(
            self._plan, selected, operator_id=operator, on_progress=self._on_result
        )
        if task is None:
            self._set_status(
                "Can't apply right now — a run is active or the hardware isn't ready.",
                COLOR_WARN,
            )
            return
        self._task = task
        for row, key in self._row_keys.items():
            item = self._table.item(row, _COL_APPLY)
            if item is not None:
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            if key in selected:
                self._set_note(row, "applying…", COLOR_IDLE)
        self._set_status("Applying…", COLOR_IDLE)
        for button in self._buttons.buttons():
            button.setEnabled(False)
        task.add_done_callback(self._on_applied)

    def _on_result(self, result: ChangeResult) -> None:
        row = self._row_for(result.device, result.change.field)
        if row is None:
            return
        text = _OUTCOME_TEXT[result.outcome]
        if result.detail and result.outcome is not Outcome.VERIFIED:
            text = f"{text}: {result.detail}"
        self._set_note(row, text, _OUTCOME_COLOR[result.outcome])

    def _on_applied(self, task: asyncio.Task[SettingsReport]) -> None:
        if task.cancelled():
            self._set_status("Cancelled — the config was closed or reloaded.", COLOR_WARN)
        elif task.exception() is not None:
            self._set_status(f"Apply failed: {task.exception()}", COLOR_FAIL)
        else:
            self._report = task.result()
            if self._report.ok:
                self._set_status("Every applied setting took.", COLOR_OK)
            else:
                self._set_status("Some settings didn't take — see the Note column.", COLOR_WARN)
        self._buttons.clear()
        if self._mode == "start":
            self._start_btn = self._add_button("Start run", QDialogButtonBox.ButtonRole.AcceptRole)
            self._start_btn.setDefault(True)
            self._start_btn.clicked.connect(self._on_start)
            self._dismiss_btn = self._add_button("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        else:
            self._dismiss_btn = self._add_button("Close", QDialogButtonBox.ButtonRole.AcceptRole)
            self._dismiss_btn.setDefault(True)
        self._dismiss_btn.clicked.connect(self._on_close_after_apply)

    def _on_start(self) -> None:
        self.startRequested.emit()
        self.accept()

    def _on_dismiss(self) -> None:
        self.reject()

    def _on_close_after_apply(self) -> None:
        if self._mode == "load":
            self.accept()
        else:
            self.reject()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        """Qt event handler — see :class:`PySide6.QtWidgets.QWidget`."""
        if self.applying:
            event.ignore()
            return
        super().closeEvent(event)

    def reject(self) -> None:
        """Qt dialog reject slot. Esc does nothing while applying; before
        anything is applied, dismissing a load-time dialog is a Skip."""
        if self.applying:
            return
        if self._mode == "load" and self._task is None:
            self._controller.record_device_settings_skipped(self._plan)
        super().reject()

    # ------------------------------------------------------------------ helpers

    def _row_for(self, device: str, field: str) -> int | None:
        for row, key in self._row_keys.items():
            if key == (device, field):
                return row
        return None

    def _set_note(self, row: int, text: str, color: QColor) -> None:
        item = QTableWidgetItem(text)
        item.setForeground(color)
        self._table.setItem(row, _COL_NOTE, item)

    def _set_status(self, text: str, color: QColor) -> None:
        self._status.setText(text)
        self._status.setStyleSheet(f"color: {color.name()};")


__all__ = ["DeviceSettingsDialog", "DialogMode"]
