""":class:`ApplyCalibrationDialog` — clone one calibration across channels.

A common operator task is "I just
characterised one thermocouple; copy that curve to the other five".
The dialog lists every channel in the draft other than the source,
with the same-kind siblings pre-selected. Channels whose
``raw_unit`` is dimensionally incompatible with the source
calibration's ``input_unit`` are shown disabled with a tooltip.

The dialog never mutates the draft on its own — it returns the
selected channel-name set to the caller (the Setup tab), which
applies the clone and marks the channels section dirty.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)


class ApplyCalibrationDialog(QDialog):
    """Multi-select dialog with same-kind siblings pre-checked.

    Constructed via :meth:`choose`, which is exec-modal and returns the
    chosen channel-name set (or an empty set on cancel). The exec
    pattern keeps the caller's code straight-line:

    .. code-block:: python

        targets = ApplyCalibrationDialog.choose(
            source_name="TC_top_1",
            source_calibration=cal_dict,
            channels=draft_channels,
            parent=self,
        )
        if targets:
            apply_clone(targets)
    """

    def __init__(
        self,
        *,
        source_name: str,
        source_calibration: dict[str, Any],
        channels: list[dict[str, Any]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Apply calibration to other channels")
        self.resize(480, 360)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        header_lines = [
            f"Source: <b>{source_name}</b>",
            f"Kind: {source_calibration.get('kind', '?')}",
            (
                f"Input unit: {source_calibration.get('input_unit', '?')}"
                f"  →  Output: {source_calibration.get('output_unit', '?')}"
            ),
        ]
        header = QLabel("<br>".join(header_lines), self)
        outer.addWidget(header)

        instructions = QLabel(
            "Tick the channels that should receive this calibration."
            " Same-kind siblings are pre-selected; disabled rows have"
            " an incompatible raw unit.",
            self,
        )
        instructions.setWordWrap(True)
        instructions.setStyleSheet("color: #555;")
        outer.addWidget(instructions)

        self._list = QListWidget(self)
        outer.addWidget(self._list, stretch=1)

        source_kind = self._kind_for_name(channels, source_name)
        source_input_unit = source_calibration.get("input_unit")
        for ch in channels:
            name = ch.get("name")
            if not isinstance(name, str) or name == source_name:
                continue
            item = QListWidgetItem(self._row_label(ch), self._list)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setData(Qt.ItemDataRole.UserRole, name)
            compatible = self._units_compatible(ch, source_input_unit)
            same_kind = ch.get("kind") == source_kind
            if not compatible:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                item.setForeground(Qt.GlobalColor.gray)
                item.setToolTip(
                    f"Channel raw_unit {ch.get('unit')!r} is not"
                    f" compatible with source input_unit"
                    f" {source_input_unit!r}."
                )
                item.setCheckState(Qt.CheckState.Unchecked)
            elif same_kind:
                item.setCheckState(Qt.CheckState.Checked)
            else:
                item.setCheckState(Qt.CheckState.Unchecked)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # ------------------------------------------------------------------
    # Public construction.
    # ------------------------------------------------------------------

    @classmethod
    def choose(
        cls,
        *,
        source_name: str,
        source_calibration: dict[str, Any],
        channels: list[dict[str, Any]],
        parent: QWidget | None,
    ) -> set[str]:
        """Run the dialog modally; return the selected channel names."""
        dialog = cls(
            source_name=source_name,
            source_calibration=source_calibration,
            channels=channels,
            parent=parent,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return set()
        return dialog.selected_targets()

    def selected_targets(self) -> set[str]:
        """Tuple of selected target identifiers."""
        out: set[str] = set()
        for idx in range(self._list.count()):
            item = self._list.item(idx)
            if item.checkState() != Qt.CheckState.Checked:
                continue
            name = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(name, str):
                out.add(name)
        return out

    # ------------------------------------------------------------------
    # Helpers.
    # ------------------------------------------------------------------

    @staticmethod
    def _kind_for_name(channels: list[dict[str, Any]], name: str) -> str | None:
        for ch in channels:
            if ch.get("name") == name:
                kind = ch.get("kind")
                return kind if isinstance(kind, str) else None
        return None

    @staticmethod
    def _row_label(channel: dict[str, Any]) -> str:
        bits = [str(channel.get("name", "?"))]
        kind = channel.get("kind")
        if isinstance(kind, str):
            bits.append(f"({kind})")
        unit = channel.get("unit")
        if isinstance(unit, str):
            bits.append(f"[{unit}]")
        return " ".join(bits)

    @staticmethod
    def _units_compatible(channel: dict[str, Any], source_input_unit: Any) -> bool:
        """Dimensional compatibility check; falls back to string equality
        when the units library isn't available or the source unit isn't a
        string."""
        if not isinstance(source_input_unit, str):
            return True
        ch_unit = channel.get("unit")
        if not isinstance(ch_unit, str):
            return True
        try:
            from capa.core.units import units_compatible  # noqa: PLC0415

            return bool(units_compatible(ch_unit, source_input_unit))
        except Exception:
            return ch_unit == source_input_unit


__all__ = ["ApplyCalibrationDialog"]
