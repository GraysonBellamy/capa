"""Slice F5 — :class:`DiscoveryDialog` and Setup wiring (plan §4.2, §4.9)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from PySide6.QtCore import QObject, Signal

from capa.devices.registry import _import_builtins, get_descriptor
from capa.ui.state import RunUiState
from capa.ui.tabs.setup import SetupTab
from capa.ui.tabs.setup_discovery import (
    DiscoveryDialog,
    build_device_payload_from_row,
    build_hw_entry_from_row,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
SIM_CAPA_EXP = REPO_ROOT / "configs" / "experiments" / "sim_capa_pyrolysis.yaml"


# Ensure the descriptor registry is populated for the helpers below.
_import_builtins()


class _ControllerStub(QObject):
    state_changed = Signal(object)
    config_load_finished = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.state = RunUiState.IDLE
        self.is_active = False


# ---------------------------------------------------------------------------
# Payload-extraction (pure helper)
# ---------------------------------------------------------------------------


def test_build_payload_for_alicat() -> None:
    desc = get_descriptor("capa.devices.alicat")
    assert desc is not None
    row = {
        "adapter": "capa.devices.alicat",
        "port": "COM7",
        "unit_id": "A",
        "baudrate": 19200,
        "model": "MC-100SCCM-D",
        "serial": "237412",
    }
    payload = build_device_payload_from_row(desc, row, existing_names=set())
    assert payload["adapter"] == "capa.devices.alicat"
    assert payload["name"] == "alicat1"
    assert payload["params"]["port"] == "COM7"
    assert payload["params"]["unit_id"] == "A"
    assert payload["params"]["baudrate"] == 19200


def test_build_payload_for_watlow() -> None:
    desc = get_descriptor("capa.devices.watlow")
    assert desc is not None
    row = {"port": "COM6", "address": 1, "model": "PM3R1CA"}
    payload = build_device_payload_from_row(desc, row, existing_names=set())
    assert payload["adapter"] == "capa.devices.watlow"
    assert payload["params"]["port"] == "COM6"
    assert payload["params"]["address"] == 1


def test_build_payload_for_nidaq() -> None:
    desc = get_descriptor("capa.devices.nidaq")
    assert desc is not None
    row = {
        "adapter": "nidaq",
        "device": "Dev1",
        "product_type": "USB-6212",
        "ai_channels": ["Dev1/ai0", "Dev1/ai1"],
        "ao_channels": [],
    }
    payload = build_device_payload_from_row(desc, row, existing_names=set())
    assert payload["adapter"] == "capa.devices.nidaq"
    assert payload["params"]["task_name"] == "Dev1_ai"


def test_build_hw_entry_routes_camera_visible() -> None:
    desc = get_descriptor("capa.devices.camera.webcam")
    assert desc is not None
    row = {
        "adapter": "capa.devices.camera.webcam",
        "selector": "/dev/video0",
        "model": "Logitech C920",
        "serial": "ABC123",
        "transport": "usb",
    }
    section, payload = build_hw_entry_from_row(desc, row, existing_names=set())
    assert section == "cameras"
    assert payload["adapter"] == "capa.devices.camera.webcam"
    assert payload["kind"] == "visible"
    assert payload["model_hint"] == "Logitech C920"
    assert payload["serial"] == "ABC123"
    assert payload["params"]["selector"] == "/dev/video0"


def test_build_hw_entry_routes_camera_ir() -> None:
    desc = get_descriptor("capa.devices.sim.flir_ir_sim")
    assert desc is not None
    row = {
        "adapter": "capa.devices.sim.flir_ir_sim",
        "selector": "SIM-IR-0001",
        "model": "FLIR IR sim",
        "serial": "SIM-IR-0001",
        "transport": "sim",
    }
    section, payload = build_hw_entry_from_row(desc, row, existing_names=set())
    assert section == "cameras"
    assert payload["kind"] == "ir"
    assert payload["serial"] == "SIM-IR-0001"


def test_build_hw_entry_routes_device_to_devices() -> None:
    """Non-camera adapters keep landing in the devices section."""
    desc = get_descriptor("capa.devices.alicat")
    assert desc is not None
    row = {"port": "COM7", "unit_id": "A"}
    section, payload = build_hw_entry_from_row(desc, row, existing_names=set())
    assert section == "devices"
    assert payload["adapter"] == "capa.devices.alicat"


def test_build_payload_assigns_unique_names() -> None:
    desc = get_descriptor("capa.devices.alicat")
    assert desc is not None
    row = {"port": "COM7", "unit_id": "A"}
    existing = {"alicat1"}
    payload = build_device_payload_from_row(desc, row, existing_names=existing)
    assert payload["name"] == "alicat2"
    existing.add("alicat2")
    payload2 = build_device_payload_from_row(desc, row, existing_names=existing)
    assert payload2["name"] == "alicat3"


# ---------------------------------------------------------------------------
# DiscoveryDialog widget
# ---------------------------------------------------------------------------


def test_dialog_lists_discoverable_adapters(qtbot: Any) -> None:
    dialog = DiscoveryDialog()
    qtbot.addWidget(dialog)
    families = {d.family for d in dialog._discoverable}
    # At minimum alicat + sartorius + nidaq are discoverable.
    assert {"alicat", "sartorius", "nidaq"}.issubset(families)


def test_dialog_mark_scan_complete_appends_rows(qtbot: Any) -> None:
    dialog = DiscoveryDialog()
    qtbot.addWidget(dialog)
    actionable_before = len(dialog._rows)
    dialog.mark_scan_complete(
        "capa.devices.alicat",
        rows=[
            {"port": "COM7", "unit_id": "A", "model": "MC-100SCCM-D"},
            {"port": "COM8", "unit_id": "A", "model": "MC-50SCCM-D"},
        ],
    )
    # ``_rows`` tracks actionable rows only — placeholder rows for
    # non-scannable adapters don't appear here.
    assert len(dialog._rows) == actionable_before + 2
    # Status badge flips to ✓.
    assert dialog._scan_status["capa.devices.alicat"] == "✓"


def test_dialog_failed_scan_records_status_and_placeholder(qtbot: Any) -> None:
    dialog = DiscoveryDialog()
    qtbot.addWidget(dialog)
    actionable_before = len(dialog._rows)
    dialog.mark_scan_complete("capa.devices.alicat", error="port not present")
    # No actionable row was added.
    assert len(dialog._rows) == actionable_before
    # Status badge flips to ✗ and the dialog adds a placeholder row so
    # the failure is visible alongside other scans.
    assert dialog._scan_status["capa.devices.alicat"] == "✗"


def test_dialog_empty_scan_shows_placeholder_row(qtbot: Any) -> None:
    """Plan §7.2 item 3: empty success is visibly distinct from
    "still scanning"."""
    dialog = DiscoveryDialog()
    qtbot.addWidget(dialog)
    rows_before = dialog._table.rowCount()
    dialog.mark_scan_complete("capa.devices.alicat", rows=[])
    # An informational row was added; ``_rows`` (actionable) is unchanged.
    assert dialog._table.rowCount() == rows_before + 1
    # The placeholder is not actionable.
    last_idx = dialog._table.rowCount() - 1
    add_widget = dialog._table.cellWidget(last_idx, 3)
    assert add_widget is not None
    assert not add_widget.isEnabled()
    # And the summary cell explains the state.
    summary_item = dialog._table.item(last_idx, 1)
    assert summary_item is not None
    assert "no devices found" in summary_item.text()


def test_dialog_lists_non_scannable_adapters(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plan §7.2 item 3: adapters with ``discoverable=False`` but a
    non-None ``discoverable_reason`` show as disabled rows so operators
    can tell scannable-empty from not-scannable.

    After PR-G3 every built-in adapter is scannable, so the test
    inserts a synthetic non-scannable descriptor to exercise the
    rendering mechanism.
    """
    from capa.devices.registry import ADAPTERS, AdapterDescriptor

    stub = AdapterDescriptor(
        id="capa.tests.plugin_stub",
        label="Stub plugin (not scannable)",
        family="plugin",
        adapter_factory=lambda **_: None,
        discoverable=False,
        discoverable_reason="stub adapter — used by tests only",
        handshake_available=False,
    )
    monkeypatch.setitem(ADAPTERS, stub.id, stub)

    dialog = DiscoveryDialog()
    qtbot.addWidget(dialog)
    families = {d.family for d in dialog._non_discoverable}
    assert "plugin" in families
    placeholder_summaries: list[str] = []
    for idx in range(dialog._table.rowCount()):
        item = dialog._table.item(idx, 1)
        if item is not None:
            placeholder_summaries.append(item.text())
    assert any("not scannable" in s for s in placeholder_summaries)


def test_non_scannable_row_tooltip_carries_reason(
    qtbot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from capa.devices.registry import ADAPTERS, AdapterDescriptor

    stub = AdapterDescriptor(
        id="capa.tests.plugin_stub_tip",
        label="Stub plugin (with tooltip)",
        family="plugin",
        adapter_factory=lambda **_: None,
        discoverable=False,
        discoverable_reason="awaiting upstream find_devices() helper",
        handshake_available=False,
    )
    monkeypatch.setitem(ADAPTERS, stub.id, stub)

    dialog = DiscoveryDialog()
    qtbot.addWidget(dialog)
    # Find the placeholder row carrying our synthetic id by reading
    # the [Add] button's tooltip — the only place the reason string lands.
    found_tooltip: str | None = None
    for idx in range(dialog._table.rowCount()):
        btn = dialog._table.cellWidget(idx, 3)
        if btn is None or btn.isEnabled():
            continue
        tip = btn.toolTip()
        if "find_devices" in tip:
            found_tooltip = tip
            break
    assert found_tooltip is not None
    assert "find_devices" in found_tooltip


def test_dialog_rescan_cancels_in_flight_scans(qtbot: Any) -> None:
    """Regression: Rescan must cancel pending scan tasks so the late
    arrivals from the previous round can't add duplicate rows or
    "no devices found" placeholders alongside the new round's hits.

    Drives the dialog without a running asyncio loop — the cancellation
    code path uses the dialog's own ``_scan_tasks`` list, which is the
    handle we want to assert was emptied.
    """
    import asyncio

    dialog = DiscoveryDialog()
    qtbot.addWidget(dialog)

    # Simulate two in-flight scans by stuffing fake tasks into the
    # tracker. Cancellation flips ``cancelled()`` on a future-like
    # object; we use ``asyncio.Future`` since it satisfies the
    # ``Task``-shaped attributes we touch (``done`` + ``cancel``).
    loop = asyncio.new_event_loop()
    try:
        f1: asyncio.Future[None] = loop.create_future()
        f2: asyncio.Future[None] = loop.create_future()
        # Mypy: the tracker is typed for ``Task`` but ``Future`` is the
        # superset shape — close enough for the cancellation contract.
        dialog._scan_tasks.extend([f1, f2])  # type: ignore[list-item]

        dialog.rescan()

        assert f1.cancelled()
        assert f2.cancelled()
        # After cancellation the dialog drops the references so a
        # second rescan doesn't double-cancel.
        assert dialog._scan_tasks == []
    finally:
        loop.close()


def test_dialog_close_cancels_in_flight_scans(qtbot: Any) -> None:
    """Closing the dialog must cancel in-flight scan tasks — otherwise
    pyserial threads holding open serial-port handles prevent the
    parent process from exiting (terminal-hang symptom on Windows)."""
    import asyncio

    dialog = DiscoveryDialog()
    qtbot.addWidget(dialog)

    loop = asyncio.new_event_loop()
    try:
        f1: asyncio.Future[None] = loop.create_future()
        dialog._scan_tasks.append(f1)  # type: ignore[arg-type]

        dialog.reject()  # equivalent to dialog.close() on a QDialog

        assert f1.cancelled()
        assert dialog._closed is True
    finally:
        loop.close()


def test_dialog_serializes_serial_port_scans(qtbot: Any) -> None:
    """Regression: serial-port-using scans must not run concurrently.

    Upstream ``find_devices`` puts a port in its ``dead_ports`` set on
    the first WatlowConnectionError and skips every subsequent baud /
    protocol probe on that port — so when alicat + watlow + sartorius
    scan concurrently they race for COM-port handles and the loser
    often reports its device as "not found." The dialog groups those
    families into one sequential task; nidaq / cameras keep their
    parallel fan-out.
    """
    import asyncio

    dialog = DiscoveryDialog()
    qtbot.addWidget(dialog)

    started: list[str] = []
    finished: list[str] = []

    async def fake_run_one_scan(descriptor: Any) -> None:
        started.append(descriptor.family)
        await asyncio.sleep(0)  # yield once so the scheduler can interleave
        finished.append(descriptor.family)

    dialog._run_one_scan = fake_run_one_scan  # type: ignore[method-assign]

    async def drive() -> None:
        dialog._start_all_scans()
        # Wait for all created tasks to finish.
        await asyncio.gather(*dialog._scan_tasks, return_exceptions=True)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(drive())
    finally:
        loop.close()

    # Each serial-port family started exactly once and finished in
    # order — i.e. no overlapping starts before the previous finish.
    serial = ["alicat", "watlow", "sartorius"]
    serial_started_order = [f for f in started if f in serial]
    serial_finished_order = [f for f in finished if f in serial]
    assert serial_started_order == serial_finished_order, (
        f"serial scans overlapped — started={serial_started_order} finished={serial_finished_order}"
    )


def test_dialog_registers_scan_tasks_with_lifecycle(qtbot: Any) -> None:
    """Regression: every spawned scan task lands in the controller's
    lifecycle registry as a non-critical DISCOVERY entry. Without this,
    the ShutdownCoordinator can't see them at app-close, scans leak
    past the dialog, and the terminal hangs on Windows (open IOCP
    handles wedge ``loop.close()``)."""
    import asyncio

    from capa.ui.lifecycle import LifecycleKind, LifecycleRegistry

    registry = LifecycleRegistry()
    dialog = DiscoveryDialog(lifecycle=registry)
    qtbot.addWidget(dialog)

    async def fake_run_one_scan(descriptor: Any) -> None:
        # Long enough that snapshot() sees the task alive.
        await asyncio.sleep(0.05)

    dialog._run_one_scan = fake_run_one_scan  # type: ignore[method-assign]

    async def drive() -> None:
        dialog._start_all_scans()
        live = registry.snapshot()
        # Every task created by the dialog is registered as DISCOVERY,
        # non-critical (the coordinator cancels + moves on).
        assert live, "no tasks registered with the lifecycle registry"
        assert all(e.kind is LifecycleKind.DISCOVERY for e in live)
        assert all(not e.critical for e in live)
        # And the names are descriptive (per-family for parallel scans,
        # one batched ``discover.serial`` for the sequential group).
        names = {e.name for e in live}
        assert any(n.startswith("discover.") for n in names)
        await asyncio.gather(*dialog._scan_tasks, return_exceptions=True)
        # Done tasks self-unregister via the registry's done-callback.
        assert registry.snapshot() == ()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(drive())
    finally:
        loop.close()


def test_dialog_destroyed_signal_cancels_scans(qtbot: Any) -> None:
    """Regression: when Qt destroys the dialog without a closeEvent
    (parent-window-closed-first path), ``destroyed`` must still cancel
    scan tasks. ``_on_destroyed`` is wired in ``__init__`` and only
    touches Python state, so it's safe to invoke even when the C++
    widget is mid-teardown."""
    import asyncio

    dialog = DiscoveryDialog()
    qtbot.addWidget(dialog)

    loop = asyncio.new_event_loop()
    try:
        f1: asyncio.Future[None] = loop.create_future()
        dialog._scan_tasks.append(f1)  # type: ignore[arg-type]

        # Don't actually destroy the widget — invoke the slot directly
        # so the test doesn't depend on Qt's destruction timing.
        dialog._on_destroyed()

        assert f1.cancelled()
        assert dialog._closed is True
        assert dialog._scan_tasks == []
    finally:
        loop.close()


def test_dialog_add_emits_device_payload(qtbot: Any) -> None:
    dialog = DiscoveryDialog(existing_names={"alicat1"})
    qtbot.addWidget(dialog)
    dialog.mark_scan_complete(
        "capa.devices.alicat",
        rows=[{"port": "COM7", "unit_id": "A"}],
    )
    captured: list[tuple[str, dict[str, Any]]] = []
    dialog.entryAdded.connect(lambda section, payload: captured.append((section, payload)))
    desc = get_descriptor("capa.devices.alicat")
    assert desc is not None
    dialog._on_add(desc, {"port": "COM7", "unit_id": "A"})
    assert len(captured) == 1
    section, payload = captured[0]
    assert section == "devices"
    # ``alicat1`` is already taken; dialog picks ``alicat2``.
    assert payload["name"] == "alicat2"


# ---------------------------------------------------------------------------
# Setup wiring
# ---------------------------------------------------------------------------


def test_setup_appends_discovered_device(qtbot: Any) -> None:
    controller = _ControllerStub()
    tab = SetupTab(controller=controller)  # type: ignore[arg-type]
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    before = len(tab._draft.document.hardware_payload.get("devices", []))
    tab._on_discovered_device_added(
        {
            "name": "alicat2",
            "adapter": "capa.devices.alicat",
            "params": {"port": "COM8", "unit_id": "A"},
        }
    )
    after = len(tab._draft.document.hardware_payload["devices"])
    assert after == before + 1
    assert tab._draft.is_dirty
    assert tab._draft.unapplied is True


def test_setup_existing_device_names_helper(qtbot: Any) -> None:
    controller = _ControllerStub()
    tab = SetupTab(controller=controller)  # type: ignore[arg-type]
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    names = tab._existing_device_names()
    # The sim fixture declares heater / purge_mfc / balance / nidaq.
    assert "heater" in names
    assert "purge_mfc" in names


def test_discover_refused_during_active_run(qtbot: Any) -> None:
    controller = _ControllerStub()
    tab = SetupTab(controller=controller)  # type: ignore[arg-type]
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    controller.is_active = True
    controller.state_changed.emit(RunUiState.RUNNING)
    with patch("capa.ui.tabs.setup.QMessageBox.information") as info:
        tab._on_discover()
    assert info.call_count == 1
    # No dialog was constructed.
    assert not tab.findChild(DiscoveryDialog)


def test_discover_button_disabled_during_active_run(qtbot: Any) -> None:
    controller = _ControllerStub()
    tab = SetupTab(controller=controller)  # type: ignore[arg-type]
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    assert tab._action_discover.isEnabled()
    controller.is_active = True
    controller.state_changed.emit(RunUiState.RUNNING)
    assert not tab._action_discover.isEnabled()
