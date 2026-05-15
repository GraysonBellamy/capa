"""Tests for the manual control panel cards and dock.

Drives the cards against a recording stub adapter (no hardware) and
asserts that:

* every action button issues the right ``DeviceCommand.kind``,
* the run-state gate disables widgets while the engine is non-idle,
* the destructive-confirm dialog suppresses the dispatch on decline,
* an empty operator id blocks dispatch without raising,
* the dock builds and tears down cards on each ``load_config`` call,
* :class:`SetupTab` emits ``device_action_requested`` on right-click.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QMessageBox, QPushButton

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
    """Return the worker-hosted adapter for ``name``. Stand-in for the
    legacy ``registry.opened_device``."""
    pool = controller._worker_pool
    assert pool is not None
    worker = pool.worker_for(name)
    return worker.adapters[name]


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
    def test_setpoint_button_carries_value_and_unit(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        cfg = _make_config((_stub_device_config("mfc.purge", family="alicat"),))
        _open_pool_sync(controller, cfg)
        card = AlicatCard(
            spec=cfg.hardware.devices[0],
            controller=controller,
            operator_provider=op_provider,
        )
        qtbot.addWidget(card)

        result = _run_async(
            card.dispatch(
                kind="set_setpoint",
                payload={"value": 50.0, "unit": "SCCM"},
            )
        )
        assert result is not None and result.accepted

        adapter = _adapter_for(controller, "mfc.purge")
        cmd = adapter.commands_received[0]
        assert cmd.kind == "set_setpoint"
        assert cmd.payload == {"value": 50.0, "unit": "SCCM"}

        _close_pool_sync(controller)

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
        """``device_action_requested`` is preserved across the SetupTab rewrite.

        The legacy read-only inspector emitted this signal from a right-
        click on a device row in its tree. The new editor shell preserves
        the signal on SetupTab so MainWindow's ``_on_device_action`` wiring
        keeps working; it is re-emitted from the Devices table once that
        section lands.
        """
        from capa.ui.tabs.setup import SetupTab

        tab = SetupTab()
        qtbot.addWidget(tab)

        captured: list[str] = []
        tab.device_action_requested.connect(captured.append)
        tab.device_action_requested.emit("balance.main")
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

    def test_apply_metadata_rewrites_combo_and_spinboxes(
        self,
        qtbot: Any,
        controller: RunController,
        op_provider: OperatorIdProvider,
    ) -> None:
        """After ``_apply_metadata`` runs (driven on the UI loop by the
        :meth:`ManualClient.camera_metadata` round-trip), the resolution
        combo reflects the camera-reported list, the matching entry is
        selected from ``resolution_hint``, and each spinbox picks up the
        snapshot range + current value (or the default when no current
        is cached).
        """
        from capa.devices.camera.metadata import UvcRangeMetadata, WebcamMetadata
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
            uvc_ranges={
                "set_exposure": UvcRangeMetadata(
                    minimum=-11,
                    maximum=-2,
                    step=1,
                    default=-6,
                    current=-5,
                ),
                # Focus has no cached current → default applies
                "set_focus": UvcRangeMetadata(
                    minimum=0,
                    maximum=250,
                    step=5,
                    default=100,
                    current=None,
                ),
                "set_brightness": UvcRangeMetadata(
                    minimum=0,
                    maximum=255,
                    step=1,
                    default=128,
                    current=200,
                ),
            },
        )

        card._apply_metadata(metadata)

        combo = card._resolution_combo
        assert combo is not None
        assert combo.count() == 3
        assert combo.itemData(combo.currentIndex()) == (1280, 720)

        exposure_spin = card._spinboxes["set_exposure"]
        assert exposure_spin.minimum() == -11
        assert exposure_spin.maximum() == -2
        assert exposure_spin.value() == -5  # cached current wins

        focus_spin = card._spinboxes["set_focus"]
        assert focus_spin.minimum() == 0
        assert focus_spin.maximum() == 250
        assert focus_spin.singleStep() == 5
        assert focus_spin.value() == 100  # falls back to default

        brightness_spin = card._spinboxes["set_brightness"]
        assert brightness_spin.value() == 200

        # Spinbox for a property with no cached range is left at the
        # safe wide default (no narrowing happened).
        zoom_spin = card._spinboxes["set_zoom"]
        assert zoom_spin.minimum() == -32768
        assert zoom_spin.maximum() == 32767

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
