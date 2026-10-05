"""Tests for the manual control panel cards and dock.

Drives the cards against a recording stub adapter (no hardware) and
asserts that:

* every action button issues the right ``DeviceCommand.kind``,
* the run-state gate disables widgets while the engine is non-idle,
* the destructive-confirm dialog suppresses the dispatch on decline,
* an empty operator id blocks dispatch without raising,
* the dock builds and tears down cards on each ``load_config`` call,
* :class:`SetupTab` emits ``deviceActionRequested`` on right-click.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMainWindow, QMessageBox, QPushButton, QWidget

from capa.channels.calibration import Identity
from capa.channels.spec import ChannelSpec, WatlowParameter
from capa.devices.sim._signals import Sine
from capa.experiment.config import (
    CalibrationSetRef,
    DeviceConfig,
    ExperimentConfig,
    HardwareProfile,
    OperatorRef,
    ProcedureRef,
    SampleInfo,
)
from capa.ui.docks.manual_control import ManualControlDock
from capa.ui.manual.cards.alicat import AlicatCard
from capa.ui.manual.cards.balance import BalanceCard
from capa.ui.state import RunController, RunUiState
from capa.ui.statusbar import OperatorIdProvider

# Test stubs. Aliases in tests/fixtures/ shaped so the card fingerprinters
# (is_balance_device / is_alicat_device) match them via the "sartorius" /
# "alicat" substring rule.
STUB_BALANCE = "tests.fixtures.stub_sartorius"
STUB_ALICAT = "tests.fixtures.stub_alicat"


def _stub_device_config(
    name: str,
    *,
    capabilities: list[str] | None = None,
    family: str = "balance",  # "balance" | "alicat"
) -> DeviceConfig:
    params: dict[str, Any] = {}
    if capabilities is not None:
        params["capabilities"] = capabilities
    adapter = STUB_BALANCE if family == "balance" else STUB_ALICAT
    return DeviceConfig(name=name, adapter=adapter, params=params)


def _make_config(devices: tuple[DeviceConfig, ...]) -> ExperimentConfig:
    return ExperimentConfig(
        hardware=HardwareProfile(
            name="manual",
            devices=devices,
            channels=(),
        ),
        procedure=ProcedureRef(id="capa.builtin.free_run", config={"duration_s": 0.1}),
        calibration_set=CalibrationSetRef(name="default"),
        operator=OperatorRef(id="opA", display_name="Op A"),
        sample=SampleInfo(id="S"),
    )


@pytest.fixture
def controller(tmp_path: Path) -> RunController:
    ctrl = RunController(runs_root=tmp_path)
    return ctrl


@pytest.fixture
def op_provider() -> OperatorIdProvider:
    return OperatorIdProvider(initial="opA")


@pytest.fixture(autouse=True)
def _pool_closed_after(controller: RunController) -> Iterator[None]:
    """Close the worker pool even when a test fails: an open pool's worker
    thread would keep the test process alive."""
    yield
    _close_pool_sync(controller)


def _run_async(coro: Any) -> Any:
    """Run an awaitable on a fresh loop. Cards use ``asyncio.get_event_loop``
    inside ``schedule_dispatch`` — we use ``asyncio.new_event_loop`` and set
    it as the current loop so that path resolves correctly under pytest."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _open_pool_sync(controller: RunController, cfg: ExperimentConfig) -> None:
    """Apply ``cfg`` to ``controller`` and drive the async pool open to
    completion synchronously.

    :meth:`RunController.set_active_config` is split into a sync
    "build a fresh :class:`WorkerPool`" step plus a scheduled async
    :meth:`WorkerPool.open`. Tests construct adapters in-process (no
    real hardware), so we run the open on a fresh loop and then drop
    it — production runs the open on the qasync loop and the cards
    then dispatch through the live ``ManualClient``.
    """
    from capa.runtime.pool import WorkerPool

    new_pool = WorkerPool.from_config(cfg)
    controller._active_config = cfg
    controller._worker_pool = new_pool

    from capa.runtime.dispatch import ManualClient

    controller._manual_client = ManualClient(
        pool=new_pool,
        conductor_provider=lambda: controller._conductor,
    )
    _run_async(new_pool.open())


def _close_pool_sync(controller: RunController) -> None:
    """Tear down the pool — mirror of :func:`_open_pool_sync`."""
    pool = controller._worker_pool
    if pool is not None:
        _run_async(pool.close())
    controller._worker_pool = None
    controller._manual_client = None
    controller._active_config = None


def _adapter_for(controller: RunController, name: str) -> Any:
    """Return the worker-hosted adapter for ``name`` (for assertions)."""
    pool = controller._worker_pool
    assert pool is not None
    worker = pool.worker_for(name)
    return worker.adapters[name]


def _load_like_main_window(
    controller: RunController, dock: ManualControlDock, cfg: ExperimentConfig
) -> None:
    """Apply ``cfg`` in :meth:`MainWindow._apply_loaded_config`'s order.

    ``set_active_config`` only *schedules* the pool open, and the dock
    builds its cards (and fires their first readback) before it finishes.
    Runs until the pool is open and every task it spawned (the dock's
    pool-open readback included) is done.
    """

    async def _apply() -> None:
        controller.set_active_config(cfg)
        dock.load_config(cfg)
        await _wait_hardware_ready(controller)
        await _drain_tasks()

    _run_async(_apply())


async def _wait_hardware_ready(controller: RunController) -> None:
    for _ in range(500):
        if controller.hardware_ready:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("pool never opened")


async def _drain_tasks() -> None:
    """Wait for every other task on the loop — the dispatch and read-back
    a card schedules from a button slot included."""
    pending = asyncio.all_tasks() - {asyncio.current_task()}
    if pending:
        await asyncio.wait(pending, timeout=5.0)


def _click(card: Any, text: str) -> None:
    """Click ``card``'s button labelled ``text`` on a running loop and wait
    for what it schedules."""
    button = next(b for b in card.findChildren(QPushButton) if b.text() == text)

    async def _go() -> None:
        button.click()
        await _drain_tasks()

    _run_async(_go())


def _section_titles(card: Any) -> list[str]:
    layout = card._sections_layout
    titles = []
    for i in range(layout.count()):
        widget = layout.itemAt(i).widget()
        if isinstance(widget, QLabel):
            titles.append(widget.text().strip("─ "))
    return titles


_ALICAT_METER_FLAGS = [
    "HAS_TARE",
    "HAS_GAS_SELECT",
    "HAS_PARAMETER_CONFIG",
    "HAS_DISPLAY_CONTROL",
    "HAS_TOTALIZER",
]
"""What the real :class:`AlicatAdapter` advertises before ``open()``; it
adds ``HAS_SETPOINT`` / ``HAS_VALVE_HOLD`` once it identifies a controller."""
_ALICAT_CONTROLLER_FLAGS = [*_ALICAT_METER_FLAGS, "HAS_SETPOINT", "HAS_VALVE_HOLD"]


def _stub_alicat_config(name: str = "purge_mfc", **params: Any) -> DeviceConfig:
    return DeviceConfig(name=name, adapter=STUB_ALICAT, params=params)


# ============================================================================
# BalanceCard
# ============================================================================


class TestBalanceCard:
    def test_renders_only_advertised_capability_sections(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        cfg = _make_config(
            (_stub_device_config("balance.main", capabilities=["HAS_TARE", "HAS_ZERO"]),)
        )
        _open_pool_sync(controller, cfg)
        card = BalanceCard(
            spec=cfg.hardware.devices[0],
            controller=controller,
            operator_provider=op_provider,
        )
        qtbot.addWidget(card)

        # With the pool open, the card reads the live adapter's
        # capability set and only renders sections for advertised flags.
        button_texts = [b.text() for b in card.findChildren(QPushButton)]
        assert "Tare" in button_texts
        assert "Zero" in button_texts
        _close_pool_sync(controller)

    def test_tare_button_dispatches_tare_command(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        cfg = _make_config((_stub_device_config("balance.main"),))
        _open_pool_sync(controller, cfg)
        card = BalanceCard(
            spec=cfg.hardware.devices[0],
            controller=controller,
            operator_provider=op_provider,
        )
        qtbot.addWidget(card)

        result = _run_async(card.dispatch(kind="tare"))
        assert result is not None
        assert result.accepted is True

        adapter = _adapter_for(controller, "balance.main")
        assert len(adapter.commands_received) == 1
        cmd = adapter.commands_received[0]
        assert cmd.kind == "tare"
        assert cmd.issued_by == "opA"
        assert cmd.confirmed_by == "opA"
        assert cmd.authorization_id is None  # manual override

        _close_pool_sync(controller)

    def test_empty_operator_id_blocks_dispatch(
        self,
        qtbot: Any,
        controller: RunController,
    ) -> None:
        cfg = _make_config((_stub_device_config("balance.main"),))
        _open_pool_sync(controller, cfg)
        provider = OperatorIdProvider(initial="")
        card = BalanceCard(
            spec=cfg.hardware.devices[0],
            controller=controller,
            operator_provider=provider,
        )
        qtbot.addWidget(card)

        result = _run_async(card.dispatch(kind="tare"))
        assert result is None
        assert "operator id required" in card._status_label.text()
        # No command reached the adapter — the operator-id gate fires
        # before ManualClient.dispatch is invoked.
        adapter = _adapter_for(controller, "balance.main")
        assert adapter.commands_received == []

        _close_pool_sync(controller)

    def test_engine_running_disables_action_widgets(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        cfg = _make_config((_stub_device_config("balance.main"),))
        _open_pool_sync(controller, cfg)
        card = BalanceCard(
            spec=cfg.hardware.devices[0],
            controller=controller,
            operator_provider=op_provider,
        )
        qtbot.addWidget(card)

        # Pre-condition: enabled.
        any_button = next(iter(card.findChildren(QPushButton)))
        assert any_button.isEnabled()

        # Simulate the controller transitioning to RUNNING.
        controller.state_changed.emit(RunUiState.RUNNING)
        assert not any_button.isEnabled()

        # Back to IDLE — re-enabled.
        controller.state_changed.emit(RunUiState.IDLE)
        assert any_button.isEnabled()
        _close_pool_sync(controller)

    def test_destructive_dispatch_blocked_by_no_confirmation(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = _make_config((_stub_device_config("balance.main"),))
        _open_pool_sync(controller, cfg)
        card = BalanceCard(
            spec=cfg.hardware.devices[0],
            controller=controller,
            operator_provider=op_provider,
        )
        qtbot.addWidget(card)

        # Patch QMessageBox.question to always reject.
        monkeypatch.setattr(
            QMessageBox,
            "question",
            lambda *a, **kw: QMessageBox.StandardButton.No,
        )
        result = _run_async(
            card.dispatch(
                kind="save_menu",
                destructive=True,
                destructive_summary="test save",
            )
        )
        assert result is None
        # Adapter recorded no save_menu — destructive-confirm refusal
        # fires before ManualClient.dispatch is invoked.
        adapter = _adapter_for(controller, "balance.main")
        assert all(c.kind != "save_menu" for c in adapter.commands_received)
        _close_pool_sync(controller)


# ============================================================================
# AlicatCard
# ============================================================================


class TestAlicatCard:
    def test_set_sends_value_in_the_read_back_unit(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        # alicatlib applies a setpoint in the device's units and drops any
        # unit it's given, so the card shows the device's unit and sends it
        # for the adapter to check, rather than offering a unit to pick.
        cfg = _make_config((_stub_alicat_config(setpoint=5.0, setpoint_unit="SLPM"),))
        _open_pool_sync(controller, cfg)
        card = AlicatCard(
            spec=cfg.hardware.devices[0],
            controller=controller,
            operator_provider=op_provider,
        )
        qtbot.addWidget(card)
        adapter = _adapter_for(controller, "purge_mfc")
        assert card._setpoint_unit_label is not None
        assert card._setpoint_unit_label.text() == "device units"

        card.apply_snapshot(adapter.state)
        assert card._setpoint_unit_label.text() == "SLPM"
        assert card._setpoint_spin is not None
        card._setpoint_spin.setValue(50.0)
        _click(card, "Set")

        cmd = adapter.commands_received[0]
        assert cmd.kind == "set_setpoint"
        assert cmd.payload == {"value": 50.0, "unit": "SLPM"}
        # The card re-read the device after the command.
        assert adapter.readback_count == 1

    def test_set_before_any_read_back_sends_no_unit(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        cfg = _make_config((_stub_alicat_config(),))
        _open_pool_sync(controller, cfg)
        card = AlicatCard(
            spec=cfg.hardware.devices[0],
            controller=controller,
            operator_provider=op_provider,
        )
        qtbot.addWidget(card)
        assert card._setpoint_spin is not None
        card._setpoint_spin.setValue(3.0)
        _click(card, "Set")
        assert _adapter_for(controller, "purge_mfc").commands_received[0].payload == {"value": 3.0}

    def test_refused_set_re_reads_the_device(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A refusal usually means the card was out of date — here the unit
        # changed on the front panel — so the card re-reads either way.
        from capa.core.errors import AdapterError
        from capa.devices.alicat import AlicatStateSnapshot

        cfg = _make_config((_stub_alicat_config(setpoint=5.0, setpoint_unit="SLPM"),))
        _open_pool_sync(controller, cfg)
        card = AlicatCard(
            spec=cfg.hardware.devices[0],
            controller=controller,
            operator_provider=op_provider,
        )
        qtbot.addWidget(card)
        adapter = _adapter_for(controller, "purge_mfc")
        card.apply_snapshot(adapter.state)
        adapter.state = AlicatStateSnapshot(gas="Air", setpoint=5.0, setpoint_unit="SCCM")

        async def _refuse(cmd: Any) -> Any:
            raise AdapterError("setpoint unit doesn't match", device="purge_mfc")

        monkeypatch.setattr(adapter, "command", _refuse)
        _click(card, "Set")
        assert "failed" in card._status_label.text()
        assert card._setpoint_unit_label is not None
        assert card._setpoint_unit_label.text() == "SCCM"

    def test_destructive_dispatch_with_confirm_yes_proceeds(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = _make_config((_stub_device_config("mfc.purge", family="alicat"),))
        _open_pool_sync(controller, cfg)
        card = AlicatCard(
            spec=cfg.hardware.devices[0],
            controller=controller,
            operator_provider=op_provider,
        )
        qtbot.addWidget(card)

        monkeypatch.setattr(
            QMessageBox,
            "question",
            lambda *a, **kw: QMessageBox.StandardButton.Yes,
        )
        result = _run_async(
            card.dispatch(
                kind="hold_valves_closed",
                destructive=True,
                destructive_summary="seal off line",
            )
        )
        assert result is not None and result.accepted

        adapter = _adapter_for(controller, "mfc.purge")
        cmd = adapter.commands_received[0]
        assert cmd.kind == "hold_valves_closed"

        _close_pool_sync(controller)

    def test_gas_combo_shows_no_gas_until_read(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        # A preset first entry used to read as the device's active gas.
        card = AlicatCard(
            spec=_stub_alicat_config(),
            controller=controller,
            operator_provider=op_provider,
        )
        qtbot.addWidget(card)
        assert card._gas_combo is not None
        assert card._gas_combo.count() == 0
        assert card._gas_combo.currentText() == ""

    def test_set_gas_without_a_gas_does_not_dispatch(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        cfg = _make_config((_stub_alicat_config(),))
        _open_pool_sync(controller, cfg)
        card = AlicatCard(
            spec=cfg.hardware.devices[0],
            controller=controller,
            operator_provider=op_provider,
        )
        qtbot.addWidget(card)
        set_btn = next(b for b in card.findChildren(QPushButton) if b.text() == "Set (session)")
        set_btn.click()
        assert "pick or type a gas" in card._status_label.text()
        assert _adapter_for(controller, "purge_mfc").commands_received == []

    def test_apply_snapshot_selects_active_gas_not_first_listed(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        from capa.devices.alicat import AlicatStateSnapshot

        card = AlicatCard(
            spec=_stub_alicat_config(),
            controller=controller,
            operator_provider=op_provider,
        )
        qtbot.addWidget(card)
        card.apply_snapshot(
            AlicatStateSnapshot(
                gas="Air",
                gas_list=("N2", "Air", "Ar"),
                setpoint=12.5,
                setpoint_unit="SLPM",
            )
        )
        assert card._gas_combo is not None
        assert card._gas_combo.currentText() == "Air"
        assert [card._gas_combo.itemText(i) for i in range(3)] == ["N2", "Air", "Ar"]
        assert card._setpoint_spin is not None
        assert card._setpoint_spin.value() == 12.5
        assert card._setpoint_unit_label is not None
        assert card._setpoint_unit_label.text() == "SLPM"
        assert "Gas: Air" in card._subtitle_label.text()
        assert "Setpoint: 12.5 SLPM" in card._subtitle_label.text()

        # A device that doesn't report its gas leaves none selected.
        card.apply_snapshot(AlicatStateSnapshot(gas=None, gas_list=("N2", "Air")))
        assert card._gas_combo.currentText() == ""


class TestAlicatCardLoadOrder:
    """The card as :class:`MainWindow` builds it: before the pool has
    finished opening the adapter, then refreshed once it has."""

    def test_reads_gas_and_setpoint_from_device(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        cfg = _make_config(
            (
                _stub_alicat_config(
                    gas="Air",
                    gas_list=["N2", "Air", "Ar"],
                    setpoint=5.0,
                    setpoint_unit="SLPM",
                ),
            )
        )
        dock = ManualControlDock(controller=controller, operator_provider=op_provider)
        qtbot.addWidget(dock)
        _load_like_main_window(controller, dock, cfg)

        card = dock.card_for("purge_mfc")
        assert isinstance(card, AlicatCard)
        assert card._gas_combo is not None
        assert card._gas_combo.currentText() == "Air"
        assert card._setpoint_spin is not None
        assert card._setpoint_spin.value() == 5.0
        assert "Gas: Air" in card._subtitle_label.text()
        # The readback that ran while the pool was opening stayed quiet.
        assert card._status_label.text() == "idle"

    def test_controller_keeps_setpoint_and_valve_sections(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        # The adapter only reports HAS_SETPOINT / HAS_VALVE_HOLD after
        # open(); a card that trusted its pre-open flags dropped both.
        cfg = _make_config(
            (
                _stub_alicat_config(
                    capabilities=_ALICAT_METER_FLAGS,
                    capabilities_after_open=_ALICAT_CONTROLLER_FLAGS,
                ),
            )
        )
        dock = ManualControlDock(controller=controller, operator_provider=op_provider)
        qtbot.addWidget(dock)
        _load_like_main_window(controller, dock, cfg)

        card = dock.card_for("purge_mfc")
        assert card is not None
        titles = _section_titles(card)
        assert "Setpoint" in titles
        assert "Valves" in titles

    def test_meter_loses_setpoint_and_valve_sections_once_open(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        cfg = _make_config((_stub_alicat_config(capabilities=_ALICAT_METER_FLAGS),))
        dock = ManualControlDock(controller=controller, operator_provider=op_provider)
        qtbot.addWidget(dock)
        _load_like_main_window(controller, dock, cfg)

        card = dock.card_for("purge_mfc")
        assert isinstance(card, AlicatCard)
        titles = _section_titles(card)
        assert "Setpoint" not in titles
        assert "Valves" not in titles
        assert "Gas / fluid" in titles
        # The rebuilt gas section still got the read-back.
        assert card._gas_combo is not None
        assert card._gas_combo.currentText() == "Air"

    def test_balance_card_reads_settings_and_last_cal(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        # The balance card's read-back used to ask the dispatch client for
        # ``read_last_cal_record``, which it doesn't have: nothing was read.
        cfg = _make_config((_stub_device_config("balance.main", family="balance"),))
        dock = ManualControlDock(controller=controller, operator_provider=op_provider)
        qtbot.addWidget(dock)
        _load_like_main_window(controller, dock, cfg)

        card = dock.card_for("balance.main")
        assert isinstance(card, BalanceCard)
        assert card._param_combos["filter_mode"].currentText() == "stable"
        assert card._param_combos["tare_behavior"].currentText() == "with stability"
        assert "Last cal: 22.4 °C" in card._subtitle_label.text()
        assert card._status_label.text() == "idle"


class TestDispatchTargetWhileOpening:
    def test_status_says_initializing_not_no_config(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        cfg = _make_config((_stub_device_config("balance.main", family="balance"),))
        card = BalanceCard(
            spec=cfg.hardware.devices[0],
            controller=controller,
            operator_provider=op_provider,
        )
        qtbot.addWidget(card)
        assert _run_async(card._ensure_adapter()) is None
        assert card._status_label.text() == "no config loaded — open a config first"

        async def _go() -> None:
            controller.set_active_config(cfg)
            assert await card._ensure_adapter() is None
            assert card._status_label.text() == "hardware initializing — manual writes disabled"
            await _wait_hardware_ready(controller)
            await _drain_tasks()

        _run_async(_go())
        # The hardware-ready transition clears the initializing message.
        assert card._status_label.text() == "ready"


class TestBalanceCardReadback:
    def test_parameters_show_nothing_until_read(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        card = BalanceCard(
            spec=_stub_device_config("balance.main", family="balance"),
            controller=controller,
            operator_provider=op_provider,
        )
        qtbot.addWidget(card)
        assert set(card._param_combos) == {
            "filter_mode",
            "app_filter",
            "stability_range",
            "stability_delay",
            "auto_zero",
            "display_unit",
            "tare_behavior",
        }
        for combo in card._param_combos.values():
            assert combo.currentIndex() == -1
        assert "Use any control to connect" not in card._subtitle_label.text()

    def test_apply_with_nothing_selected_does_not_dispatch(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        cfg = _make_config((_stub_device_config("balance.main", family="balance"),))
        _open_pool_sync(controller, cfg)
        card = BalanceCard(
            spec=cfg.hardware.devices[0],
            controller=controller,
            operator_provider=op_provider,
        )
        qtbot.addWidget(card)
        _click(card, "Apply")
        assert "first" in card._status_label.text()
        assert _adapter_for(controller, "balance.main").commands_received == []

    def test_apply_sends_selection_then_reads_back(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        cfg = _make_config((_stub_device_config("balance.main", family="balance"),))
        _open_pool_sync(controller, cfg)
        card = BalanceCard(
            spec=cfg.hardware.devices[0],
            controller=controller,
            operator_provider=op_provider,
        )
        qtbot.addWidget(card)
        combo = card._param_combos["tare_behavior"]
        combo.setCurrentIndex(combo.findText("at stability"))
        # Rows are built in order; the tare-behavior row's Apply is last.
        buttons = [b for b in card.findChildren(QPushButton) if b.text() == "Apply"]

        async def _go() -> None:
            buttons[-1].click()
            await _drain_tasks()

        _run_async(_go())
        adapter = _adapter_for(controller, "balance.main")
        cmd = adapter.commands_received[0]
        assert (cmd.kind, cmd.payload) == ("set_tare_behavior", {"mode": "at stability"})
        # Re-read after the write: the stub still reports "with stability".
        assert adapter.readback_count == 1
        assert combo.currentText() == "with stability"

    def test_apply_snapshot_selects_values_and_shows_last_cal(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        from capa.devices.sartorius import SartoriusStateSnapshot

        card = BalanceCard(
            spec=_stub_device_config("balance.main", family="balance"),
            controller=controller,
            operator_provider=op_provider,
        )
        qtbot.addWidget(card)
        card.apply_snapshot(
            SartoriusStateSnapshot(
                filter_mode="very unstable",
                app_filter="filling",
                stability_range="max fast",
                stability_delay="long",
                auto_zero="off",
                display_unit="lb",
                tare_behavior="at stability",
                cal_temperature_c=21.7,
                cal_on_record=True,
            )
        )
        assert {k: c.currentText() for k, c in card._param_combos.items()} == {
            "filter_mode": "very unstable",
            "app_filter": "filling",
            "stability_range": "max fast",
            "stability_delay": "long",
            "auto_zero": "off",
            # Not a preset choice: added so the read-back still shows.
            "display_unit": "lb",
            "tare_behavior": "at stability",
        }
        assert "Last cal: 21.7 °C" in card._subtitle_label.text()

        card.apply_snapshot(SartoriusStateSnapshot(cal_temperature_c=21.0, cal_on_record=False))
        for combo in card._param_combos.values():
            assert combo.currentIndex() == -1
        assert "Last cal: none since power-up" in card._subtitle_label.text()


# ============================================================================
# HeaterCard — Cool to safe
# ============================================================================


def _heater_cfg(*, procedure: ProcedureRef | None = None) -> ExperimentConfig:
    """Build a watlow-sim-backed ExperimentConfig.

    Procedure defaults to free_run; pass a heat_flux_tune ProcedureRef
    to exercise the t_safe_c readout path.
    """
    if procedure is None:
        procedure = ProcedureRef(id="capa.builtin.free_run", config={"duration_s": 0.1})
    return ExperimentConfig(
        hardware=HardwareProfile(
            name="x",
            devices=(
                DeviceConfig(
                    name="heater",
                    adapter="capa.devices.sim.watlow_sim",
                    params={
                        "tick_period_s": 0.05,
                        "signals": {
                            ("process_value", 1): Sine(
                                amplitude=1.0, frequency_hz=1.0, offset=300.0
                            ),
                        },
                    },
                ),
            ),
            channels=(
                ChannelSpec(
                    name="heater.pv",
                    kind="process_var",
                    unit="degC",
                    derived_unit="degC",
                    source=WatlowParameter(device="heater", parameter="process_value", instance=1),
                    calibration=Identity(input_unit="degC", output_unit="degC"),
                ),
            ),
        ),
        procedure=procedure,
        calibration_set=CalibrationSetRef(name="default"),
        operator=OperatorRef(id="opA", display_name="Op A"),
        sample=SampleInfo(id="S"),
    )


class TestHeaterCardSafeCool:
    """Cool-to-safe quick-action on the heater card.

    Ships standalone of hold mode — the affordance is generally useful.
    Reads the safe temperature from the active method's Heat-Flux Tune
    config when present, otherwise falls back to 25 °C.
    """

    def test_button_renders_with_fallback_temp_when_no_tune_config(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        from capa.ui.manual.cards.watlow import HeaterCard

        cfg = _heater_cfg()  # free_run procedure — no t_safe_c
        _open_pool_sync(controller, cfg)
        card = HeaterCard(
            spec=cfg.hardware.devices[0],
            controller=controller,
            operator_provider=op_provider,
        )
        qtbot.addWidget(card)

        button_texts = [b.text() for b in card.findChildren(QPushButton)]
        assert "Cool to safe" in button_texts
        # The fallback constant is 25 °C — surfaced in the info label.
        from PySide6.QtWidgets import QLabel

        info_texts = [lbl.text() for lbl in card.findChildren(QLabel)]
        assert any("25" in t and "°C" in t for t in info_texts), info_texts
        _close_pool_sync(controller)

    def test_resolve_safe_temp_reads_heat_flux_tune_config(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        """When the active procedure is heat_flux_tune, ``t_safe_c`` from
        the procedure config wins over the 25 °C fallback."""
        from capa.ui.manual.cards.watlow import HeaterCard

        cfg = _heater_cfg(
            procedure=ProcedureRef(
                id="capa.builtin.heat_flux_tune",
                config={"targets_kw_m2": [50.0], "t_safe_c": 20.0},
            )
        )
        _open_pool_sync(controller, cfg)
        card = HeaterCard(
            spec=cfg.hardware.devices[0],
            controller=controller,
            operator_provider=op_provider,
        )
        qtbot.addWidget(card)
        assert card._resolve_safe_temp_c() == 20.0
        _close_pool_sync(controller)

    def test_resolve_safe_temp_falls_back_on_missing_key(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        """A heat_flux_tune procedure config that omits ``t_safe_c``
        (defaulted at validation time, absent from a partial dict) still
        resolves to the 25 °C fallback rather than raising."""
        from capa.ui.manual.cards.watlow import HeaterCard

        cfg = _heater_cfg(
            procedure=ProcedureRef(
                id="capa.builtin.heat_flux_tune",
                config={"targets_kw_m2": [50.0]},  # no t_safe_c
            )
        )
        _open_pool_sync(controller, cfg)
        card = HeaterCard(
            spec=cfg.hardware.devices[0],
            controller=controller,
            operator_provider=op_provider,
        )
        qtbot.addWidget(card)
        assert card._resolve_safe_temp_c() == 25.0
        _close_pool_sync(controller)

    def test_confirmation_rejection_suppresses_dispatch(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A 'Cancel' on the safe-cool confirm dialog must not dispatch.

        Watlow sim doesn't expose a ``commands_received`` log, so we
        observe via the card's own ``schedule_dispatch_and_read_back`` — a clean
        seam since the test cares about whether the dispatch was
        scheduled, not what the sim adapter did afterwards.
        """
        from capa.ui.manual.cards.watlow import HeaterCard

        cfg = _heater_cfg()
        _open_pool_sync(controller, cfg)
        card = HeaterCard(
            spec=cfg.hardware.devices[0],
            controller=controller,
            operator_provider=op_provider,
        )
        qtbot.addWidget(card)

        dispatched: list[dict[str, Any]] = []
        monkeypatch.setattr(
            card,
            "schedule_dispatch_and_read_back",
            lambda **kw: dispatched.append(kw),
        )
        monkeypatch.setattr(
            QMessageBox,
            "question",
            lambda *a, **kw: QMessageBox.StandardButton.Cancel,
        )
        card._on_safe_cool_clicked()
        assert dispatched == []
        _close_pool_sync(controller)

    def test_confirmation_accept_dispatches_safe_setpoint(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Accepting the confirm dialog dispatches a ``set_setpoint`` write
        at the procedure's ``t_safe_c`` (30 °C in this fixture)."""
        from capa.ui.manual.cards.watlow import HeaterCard

        cfg = _heater_cfg(
            procedure=ProcedureRef(
                id="capa.builtin.heat_flux_tune",
                config={"targets_kw_m2": [50.0], "t_safe_c": 30.0},
            )
        )
        _open_pool_sync(controller, cfg)
        card = HeaterCard(
            spec=cfg.hardware.devices[0],
            controller=controller,
            operator_provider=op_provider,
        )
        qtbot.addWidget(card)

        dispatched: list[dict[str, Any]] = []
        monkeypatch.setattr(
            card,
            "schedule_dispatch_and_read_back",
            lambda **kw: dispatched.append(kw),
        )
        monkeypatch.setattr(
            QMessageBox,
            "question",
            lambda *a, **kw: QMessageBox.StandardButton.Ok,
        )
        card._on_safe_cool_clicked()
        assert len(dispatched) == 1
        call = dispatched[0]
        assert call["kind"] == "set_setpoint"
        assert call["payload"]["value"] == 30.0
        assert call["payload"]["instance"] == 1
        assert call["destructive"] is True
        _close_pool_sync(controller)


# ============================================================================
# ManualControlDock
# ============================================================================


class TestManualControlDock:
    def test_watlow_sim_renders_heater_card(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        # Watlow adapters (sim or real) render a HeaterCard. The card
        # exposes setpoint, display-unit toggle (param 17050), and a raw
        # write_parameter row.
        cfg = ExperimentConfig(
            hardware=HardwareProfile(
                name="x",
                devices=(
                    DeviceConfig(
                        name="heater",
                        adapter="capa.devices.sim.watlow_sim",
                        params={
                            "tick_period_s": 0.05,
                            "signals": {
                                ("process_value", 1): Sine(
                                    amplitude=1.0, frequency_hz=1.0, offset=300.0
                                ),
                            },
                        },
                    ),
                ),
                channels=(
                    ChannelSpec(
                        name="heater.pv",
                        kind="process_var",
                        unit="degC",
                        derived_unit="degC",
                        source=WatlowParameter(
                            device="heater", parameter="process_value", instance=1
                        ),
                        calibration=Identity(input_unit="degC", output_unit="degC"),
                    ),
                ),
            ),
            procedure=ProcedureRef(id="capa.builtin.free_run", config={"duration_s": 0.1}),
            calibration_set=CalibrationSetRef(name="default"),
            operator=OperatorRef(id="op", display_name="Op"),
            sample=SampleInfo(id="S"),
        )
        _open_pool_sync(controller, cfg)
        dock = ManualControlDock(controller=controller, operator_provider=op_provider)
        qtbot.addWidget(dock)
        dock.load_config(cfg)
        from capa.ui.manual.cards.watlow import HeaterCard

        assert set(dock._cards_by_name.keys()) == {"heater"}
        assert isinstance(dock._cards_by_name["heater"], HeaterCard)
        _close_pool_sync(controller)

    def test_balance_and_alicat_specs_render_one_card_each(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        cfg = _make_config(
            (
                _stub_device_config("balance.main", family="balance", capabilities=["HAS_TARE"]),
                _stub_device_config("mfc.purge", family="alicat", capabilities=["HAS_SETPOINT"]),
            )
        )
        _open_pool_sync(controller, cfg)
        dock = ManualControlDock(controller=controller, operator_provider=op_provider)
        qtbot.addWidget(dock)
        dock.load_config(cfg)
        assert set(dock._cards_by_name.keys()) == {"balance.main", "mfc.purge"}
        assert dock._empty_label.isHidden()
        _close_pool_sync(controller)

    def test_dock_widens_to_fit_cards_and_scrolls_when_narrowed(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        # A dock narrower than its cards used to clip the right-hand
        # controls with no way to scroll to them.
        cfg = _make_config(
            (
                DeviceConfig(name="heater", adapter="capa.devices.sim.watlow_sim"),
                DeviceConfig(name="air_mfc", adapter="capa.devices.sim.alicat_sim"),
            )
        )
        window = QMainWindow()
        window.setCentralWidget(QWidget())
        qtbot.addWidget(window)
        dock = ManualControlDock(controller=controller, operator_provider=op_provider)
        window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        window.resize(1500, 950)
        window.show()
        qtbot.waitExposed(window)

        dock.load_config(cfg)
        content = dock._scroll.widget()
        assert content is not None
        content_min = content.minimumSizeHint().width()
        qtbot.waitUntil(lambda: dock._scroll.viewport().width() >= content_min)

        window.resizeDocks([dock], [content_min // 2], Qt.Orientation.Horizontal)
        qtbot.waitUntil(lambda: dock._scroll.horizontalScrollBar().isVisible())

    def test_reload_config_rebuilds_cards(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        dock = ManualControlDock(controller=controller, operator_provider=op_provider)
        qtbot.addWidget(dock)

        cfg1 = _make_config((_stub_device_config("balance.main", family="balance"),))
        _open_pool_sync(controller, cfg1)
        dock.load_config(cfg1)
        assert "balance.main" in dock._cards_by_name
        _close_pool_sync(controller)

        cfg2 = _make_config((_stub_device_config("mfc.purge", family="alicat"),))
        _open_pool_sync(controller, cfg2)
        dock.load_config(cfg2)
        # Old card gone, new card present.
        assert "balance.main" not in dock._cards_by_name
        assert "mfc.purge" in dock._cards_by_name
        _close_pool_sync(controller)


# ============================================================================
# SetupTab right-click
# ============================================================================


class TestSetupTabContextMenu:
    def test_device_action_signal_routes_to_listener(self, qtbot: Any) -> None:
        """``deviceActionRequested`` carries the device name to MainWindow."""
        from capa.ui.tabs.setup import SetupTab

        tab = SetupTab()
        qtbot.addWidget(tab)

        captured: list[str] = []
        tab.deviceActionRequested.connect(captured.append)
        tab.deviceActionRequested.emit("balance.main")
        assert captured == ["balance.main"]


# ============================================================================
# WebcamCard — visible camera (UVC) manual-control card
# ============================================================================


def _webcam_spec(name: str = "visible_cam0") -> Any:
    from capa.devices.camera.base import CameraSpec

    return CameraSpec.model_validate(
        {
            "name": name,
            "adapter": "capa.devices.camera.webcam",
            "kind": "visible",
        }
    )


class TestWebcamCard:
    def test_renders_all_optimistic_sections(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        """Without a live UVC probe, the card falls back to the optimistic
        default capability set and renders every section — verbs against
        unsupported properties reject at dispatch time."""
        from capa.ui.manual.cards.webcam import WebcamCard

        card = WebcamCard(
            spec=_webcam_spec(),
            controller=controller,
            operator_provider=op_provider,
        )
        qtbot.addWidget(card)
        button_texts = [b.text() for b in card.findChildren(QPushButton)]
        # Every section that builds adds at least one "Apply" button.
        # 14 expected: stream-format ×2 (res, fps), exposure ×2 (auto, manual),
        # focus ×2, zoom ×2 (optical, digital), WB ×2, pan/tilt ×2,
        # image-adjust ×8 → 20 Apply buttons in total. Don't pin the exact
        # count (the spec may shift); just require the card is non-empty
        # and at least the stream-format section is present.
        assert "Apply" in button_texts
        assert len(button_texts) >= 5

    def test_renders_under_manual_control_dock(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
        tmp_path: Path,
    ) -> None:
        """When the config carries a visible camera spec, the dock builds
        a WebcamCard for it."""
        from capa.devices.camera.base import CameraSpec
        from capa.ui.manual.cards.webcam import WebcamCard

        cam = CameraSpec.model_validate(
            {
                "name": "vis0",
                "adapter": "capa.devices.camera.webcam",
                "kind": "visible",
            }
        )
        cfg = ExperimentConfig(
            hardware=HardwareProfile(
                name="manual",
                devices=(),
                channels=(),
                cameras=(cam,),
            ),
            procedure=ProcedureRef(id="capa.builtin.free_run", config={"duration_s": 0.1}),
            calibration_set=CalibrationSetRef(name="default"),
            operator=OperatorRef(id="opA", display_name="Op A"),
            sample=SampleInfo(id="S"),
        )
        controller.set_active_config(cfg)
        dock = ManualControlDock(controller=controller, operator_provider=op_provider)
        qtbot.addWidget(dock)
        dock.load_config(cfg)
        assert dock.card_for("vis0") is not None
        assert isinstance(dock.card_for("vis0"), WebcamCard)

    def test_apply_metadata_rewrites_combo_and_fps_cap(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        """After ``_apply_metadata`` runs (driven on the UI loop by the
        :meth:`ManualClient.camera_metadata` round-trip), the resolution
        combo reflects the camera-reported list, the matching entry is
        selected from ``resolution_hint``, and the fps spinbox is capped for
        the selected resolution. UVC controls come from the read-back
        (``tests/unit/test_webcam_card.py``).
        """
        from capa.devices.camera.metadata import WebcamMetadata
        from capa.ui.manual.cards.webcam import WebcamCard

        card = WebcamCard(
            spec=_webcam_spec(),
            controller=controller,
            operator_provider=op_provider,
        )
        qtbot.addWidget(card)

        metadata = WebcamMetadata(
            supported_resolutions=((640, 480), (1280, 720), (1920, 1080)),
            resolution_hint=(1280, 720),
            resolution_fps_caps={
                (640, 480): 60.0,
                (1280, 720): 30.0,
                (1920, 1080): 15.0,
            },
        )

        card._apply_metadata(metadata)

        combo = card._resolution_combo
        assert combo is not None
        assert combo.count() == 3
        assert combo.itemData(combo.currentIndex()) == (1280, 720)

        # FPS spinbox is capped to the per-resolution cap for the
        # currently-selected resolution (1280×720 → 30 fps in the stub).
        fps_spin = card._fps_spin
        assert fps_spin is not None
        assert fps_spin.maximum() == 30.0

        # Switching the resolution combo to 640×480 (60 fps cap) raises
        # the cap; switching to 1920×1080 (15 fps cap) drops it and
        # clamps the current value down.
        idx_640 = next(i for i in range(combo.count()) if combo.itemData(i) == (640, 480))
        combo.setCurrentIndex(idx_640)
        assert fps_spin.maximum() == 60.0

        fps_spin.setValue(30.0)
        idx_1080 = next(i for i in range(combo.count()) if combo.itemData(i) == (1920, 1080))
        combo.setCurrentIndex(idx_1080)
        assert fps_spin.maximum() == 15.0
        assert fps_spin.value() == 15.0  # clamped down from 30

        assert card._controls_initialized is True
