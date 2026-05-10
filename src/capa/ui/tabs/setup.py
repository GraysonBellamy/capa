"""Setup tab — read-only inspect of the loaded :class:`ExperimentConfig`.

Plan §10.1. Devices on the left as a tree; selection populates a detail
panel on the right with the full Pydantic dump for the selected node.

Editing lands in P3 alongside the auto-form generator. For P1 the operator
opens a config file (YAML/TOML) and inspects what it resolved to.
"""

from __future__ import annotations

import json

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMenu,
    QPlainTextEdit,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QWidget,
)

from capa.experiment.config import ExperimentConfig
from capa.ui.theme import monospace_font


class SetupTab(QWidget):
    """Two-pane layout: device/channel tree on the left, JSON detail on the
    right. Channels are nested under their owning device when the binding
    declares a device; otherwise they hang off a synthetic "(unbound)"
    parent.

    Right-clicking a device row emits :attr:`device_action_requested` with
    the device name. :class:`MainWindow` listens for that signal and reveals
    the device's manual control card.
    """

    device_action_requested = Signal(str)
    """Emitted when the operator chooses "Open Manual Control" from the
    right-click menu on a device row. Argument is the device name."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config: ExperimentConfig | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        layout.addWidget(splitter)

        self._tree = QTreeWidget(self)
        self._tree.setHeaderLabels(["name", "kind"])
        self._tree.setColumnWidth(0, 240)
        self._tree.setRootIsDecorated(True)
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        splitter.addWidget(self._tree)

        self._detail = QPlainTextEdit(self)
        self._detail.setReadOnly(True)
        self._detail.setFont(monospace_font(point_size=10))
        self._detail.setPlaceholderText("Select a device or channel.")
        splitter.addWidget(self._detail)
        splitter.setSizes([320, 600])

    # ------------------------------------------------------------------ slots

    def load_config(self, config: ExperimentConfig) -> None:
        """Populate the tree from ``config``. Replaces previous contents."""
        self._config = config
        self._tree.clear()

        # Top-level summary node so the detail pane has something to show on
        # initial load.
        root = QTreeWidgetItem(["experiment", config.procedure.id])
        root.setData(0, Qt.ItemDataRole.UserRole, ("experiment", None))
        self._tree.addTopLevelItem(root)

        device_nodes: dict[str, QTreeWidgetItem] = {}
        for dev in config.hardware.devices:
            node = QTreeWidgetItem([dev.name, dev.adapter])
            node.setData(0, Qt.ItemDataRole.UserRole, ("device", dev.name))
            root.addChild(node)
            device_nodes[dev.name] = node

        unbound: QTreeWidgetItem | None = None
        for ch in config.hardware.channels:
            owner = _channel_device(ch)
            parent = device_nodes.get(owner) if owner else None
            if parent is None:
                if unbound is None:
                    unbound = QTreeWidgetItem(["(unbound)", ""])
                    root.addChild(unbound)
                parent = unbound
            child = QTreeWidgetItem([ch.name, str(ch.kind)])
            child.setData(0, Qt.ItemDataRole.UserRole, ("channel", ch.name))
            parent.addChild(child)

        self._tree.expandAll()

    def clear(self) -> None:
        self._tree.clear()
        self._detail.clear()
        self._config = None

    # ------------------------------------------------------------------ internal

    def _on_context_menu(self, pos: QPoint) -> None:
        item = self._tree.itemAt(pos)
        if item is None:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not (isinstance(data, tuple) and len(data) == 2):
            return
        kind, name = data
        if kind != "device" or not name:
            return
        menu = QMenu(self._tree)
        action = menu.addAction("Open Manual Control")
        # ``viewport()`` is the actual interactive surface; the
        # customContextMenuRequested coordinate is relative to it.
        viewport = self._tree.viewport()
        global_pos = viewport.mapToGlobal(pos) if viewport is not None else pos
        chosen = menu.exec(global_pos)
        if chosen is action:
            self.device_action_requested.emit(name)

    def _on_selection_changed(self) -> None:
        items = self._tree.selectedItems()
        if not items or self._config is None:
            self._detail.clear()
            return
        kind, name = items[0].data(0, Qt.ItemDataRole.UserRole)
        if kind == "experiment":
            self._detail.setPlainText(_dump(self._config.model_dump(mode="json")))
        elif kind == "device":
            for dev in self._config.hardware.devices:
                if dev.name == name:
                    self._detail.setPlainText(_dump(dev.model_dump(mode="json")))
                    return
        elif kind == "channel":
            for ch in self._config.hardware.channels:
                if ch.name == name:
                    self._detail.setPlainText(_dump(ch.model_dump(mode="json")))
                    return


def _dump(payload: dict[str, object]) -> str:
    return json.dumps(payload, indent=2, default=str, sort_keys=False)


def _channel_device(channel: object) -> str | None:
    """Best-effort: extract the binding's device, accepting that not every
    SourceBinding variant declares one."""
    src = getattr(channel, "source", None)
    if src is None:
        return None
    return getattr(src, "device", None) or getattr(src, "task", None)


__all__ = ["SetupTab"]
