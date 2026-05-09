"""Numerics dock — large readouts of starred channels.

Plan §10.2. Reads :meth:`ChannelRingBuffer.latest` for each registered
channel at 1 Hz (the 10 Hz plot cadence is overkill for digit displays;
operators prefer stable readouts to flickering decimals).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QDockWidget,
    QGridLayout,
    QLabel,
    QWidget,
)

from capa.channels.spec import ChannelSpec
from capa.core.ringbuffer import RingBufferRegistry
from capa.ui.theme import COLOR_IDLE, monospace_font, numeric_display_font

REFRESH_INTERVAL_MS: Final[int] = 1000
"""1 Hz refresh — the eye does not benefit from faster digit changes."""


class _NumericTile(QWidget):
    """One row in the numerics dock: channel name, value, unit."""

    def __init__(self, channel: ChannelSpec, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._channel: ChannelSpec = channel
        self._unit: str = channel.derived_unit or channel.unit

        layout = QGridLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setHorizontalSpacing(12)

        self._name_label = QLabel(channel.name, self)
        self._name_label.setFont(monospace_font(point_size=10))

        self._value_label = QLabel("—", self)
        self._value_label.setFont(numeric_display_font())
        self._value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._value_label.setMinimumWidth(140)

        self._unit_label = QLabel(self._unit, self)
        self._unit_label.setFont(monospace_font(point_size=10))
        self._unit_label.setStyleSheet(f"color: {COLOR_IDLE.name()}")

        layout.addWidget(self._name_label, 0, 0)
        layout.addWidget(self._value_label, 0, 1)
        layout.addWidget(self._unit_label, 0, 2)
        layout.setColumnStretch(0, 1)

    def update_value(self, value: float | None) -> None:
        if value is None:
            self._value_label.setText("—")
        else:
            self._value_label.setText(_format(value))


def _format(value: float) -> str:
    """Pick a width-stable display for a wide value range."""
    av = abs(value)
    if av == 0:
        return "0.000"
    if av >= 1e4 or av < 1e-2:
        return f"{value: .3e}"
    if av >= 1e2:
        return f"{value: .2f}"
    return f"{value: .3f}"


class NumericsDock(QDockWidget):
    """Dockable grid of large channel readouts."""

    def __init__(
        self,
        *,
        registry: RingBufferRegistry,
        channels: Iterable[ChannelSpec],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Numerics", parent)
        self.setObjectName("dock_numerics")
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )

        self._registry: RingBufferRegistry = registry
        self._tiles: dict[str, _NumericTile] = {}

        body = QWidget(self)
        layout = QGridLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setVerticalSpacing(2)
        layout.setColumnStretch(0, 1)

        for row, ch in enumerate(channels):
            tile = _NumericTile(ch, body)
            self._tiles[ch.name] = tile
            layout.addWidget(tile, row, 0)

        layout.setRowStretch(layout.rowCount(), 1)
        self.setWidget(body)

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self._refresh)

    # ------------------------------------------------------------------ control

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def set_registry(self, registry: RingBufferRegistry) -> None:
        self._registry = registry
        for tile in self._tiles.values():
            tile.update_value(None)

    # ------------------------------------------------------------------ internal

    def _refresh(self) -> None:
        for channel, tile in self._tiles.items():
            buf = self._registry.get(channel)
            if buf is None:
                tile.update_value(None)
                continue
            latest = buf.latest()
            tile.update_value(latest[1] if latest is not None else None)


__all__ = ["REFRESH_INTERVAL_MS", "NumericsDock"]
