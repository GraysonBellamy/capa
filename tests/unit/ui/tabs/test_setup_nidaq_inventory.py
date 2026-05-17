"""Tests for SetupTab's NI-DAQ inventory cache.

The Devices and Channels editors (Steps 4 and 5 of the NI-DAQ UX work)
read NI hardware inventory through :meth:`SetupTab.nidaq_inventory`
instead of calling ``capa.devices.nidaq.discover()`` themselves —
form widgets should be pure UI, discovery is I/O. These tests cover the
cache surface, signal emission, and the DiscoveryDialog → SetupTab join.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

from capa.ui.tabs.setup import SetupTab


def test_initial_inventory_is_empty(qtbot: Any) -> None:
    tab = SetupTab()
    qtbot.addWidget(tab)
    assert tab.nidaq_inventory() == {}


def test_update_nidaq_inventory_caches_rows_by_device_name(qtbot: Any) -> None:
    tab = SetupTab()
    qtbot.addWidget(tab)
    rows = [
        {
            "adapter": "nidaq",
            "device": "cDAQ1",
            "product_type": "cDAQ-9171",
            "serial": "0xABC",
            "ai_channels": ["cDAQ1Mod1/ai0", "cDAQ1Mod1/ai1"],
            "ao_channels": [],
            "di_lines": [],
            "do_lines": [],
            "ci_channels": [],
            "co_channels": [],
        }
    ]
    tab.update_nidaq_inventory(rows)
    inventory = tab.nidaq_inventory()
    assert "cDAQ1" in inventory
    assert inventory["cDAQ1"]["ai_channels"] == ["cDAQ1Mod1/ai0", "cDAQ1Mod1/ai1"]


def test_update_nidaq_inventory_emits_changed_signal(qtbot: Any) -> None:
    tab = SetupTab()
    qtbot.addWidget(tab)
    with qtbot.waitSignal(tab.nidaqInventoryChanged, timeout=1000):
        tab.update_nidaq_inventory([{"adapter": "nidaq", "device": "cDAQ1", "ai_channels": []}])


def test_update_nidaq_inventory_drops_malformed_rows(qtbot: Any) -> None:
    tab = SetupTab()
    qtbot.addWidget(tab)
    tab.update_nidaq_inventory(
        [
            None,  # type: ignore[list-item]
            "not a dict",  # type: ignore[list-item]
            {"adapter": "nidaq"},  # missing device
            {"device": ""},  # empty name
            {"device": "good", "adapter": "nidaq", "ai_channels": ["x/ai0"]},
        ]
    )
    inventory = tab.nidaq_inventory()
    assert list(inventory.keys()) == ["good"]


def test_inventory_accessor_returns_copy_not_reference(qtbot: Any) -> None:
    """Callers shouldn't be able to mutate the cache through the accessor."""
    tab = SetupTab()
    qtbot.addWidget(tab)
    tab.update_nidaq_inventory([{"adapter": "nidaq", "device": "cDAQ1", "ai_channels": ["x/ai0"]}])
    inventory = tab.nidaq_inventory()
    inventory["cDAQ1"]["ai_channels"].append("bogus")
    # Internal cache is unchanged.
    assert tab.nidaq_inventory()["cDAQ1"]["ai_channels"] == ["x/ai0"]


def test_rescan_without_event_loop_is_safe(qtbot: Any) -> None:
    """``rescan_nidaq_inventory`` is callable from unit tests without a
    qasync loop — it short-circuits silently when no loop is running.
    """
    tab = SetupTab()
    qtbot.addWidget(tab)
    tab.rescan_nidaq_inventory()
    # No crash, cache unchanged.
    assert tab.nidaq_inventory() == {}


class _FakeDialog(QWidget):
    """Stand-in for the real DiscoveryDialog with just the signal we test."""

    nidaqScanCompleted = Signal(list)  # noqa: N815


def test_discovery_dialog_signal_routes_into_cache(qtbot: Any) -> None:
    """When the Discovery dialog reports an NI scan result, SetupTab's
    cache picks it up automatically. This is the production path —
    operators open Discover, hit Scan, results land in the cache.
    """
    tab = SetupTab()
    qtbot.addWidget(tab)
    dialog = _FakeDialog()
    qtbot.addWidget(dialog)
    dialog.nidaqScanCompleted.connect(tab._on_nidaq_scan_completed)
    rows = [{"adapter": "nidaq", "device": "cDAQ1", "ai_channels": ["x/ai0"]}]
    with qtbot.waitSignal(tab.nidaqInventoryChanged, timeout=1000):
        dialog.nidaqScanCompleted.emit(rows)
    assert "cDAQ1" in tab.nidaq_inventory()
