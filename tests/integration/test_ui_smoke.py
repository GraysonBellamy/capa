"""GUI smoke tests for the P1 UI.

These exercise the :class:`RunController` and :class:`MainWindow` against
the same simulated adapters the headless engine tests use. We do *not*
spin up qasync here — the controller's async path runs under anyio's
asyncio backend just fine, as long as a ``QApplication`` exists for Qt
signals to bind to. The pytest-qt ``qtbot`` fixture provides that.

What's covered:

* Ring buffers populate during a sim run (controller-level integration).
* :attr:`RunController.run_finished` fires with a sealed result.
* MainWindow renders Setup + Run tabs and Numerics + Events docks when a
  config loads.

What's *not* covered (deferred):

* End-to-end Start-button → Sealed via the qasync event loop. The plot
  redraw timer and the elapsed-time pill require a Qt loop to actually
  fire ``QTimer`` ticks, which clashes with pytest-anyio's asyncio loop.
  The headless engine tests already prove the engine path; the controller
  tests below prove the UI seam attaches correctly.
"""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

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
from capa.experiment.engine import EngineResult, EngineState
from capa.ui.state import RunController


def _make_config(
    *,
    sample_id: str = "UI-SMOKE",
    duration_s: float = 0.15,
    channels: int = 2,
) -> ExperimentConfig:
    chans: list[ChannelSpec] = []
    signals: dict[tuple[str, int], Sine] = {}
    for i in range(channels):
        chans.append(
            ChannelSpec(
                name=f"heater.pv{i}",
                kind="process_var",
                unit="degC",
                derived_unit="degC",
                source=WatlowParameter(device="heater", parameter="process_value", instance=i + 1),
                calibration=Identity(input_unit="degC", output_unit="degC"),
                plot_group="temperature",
                decimate_to_hz=20.0,
            )
        )
        signals[("process_value", i + 1)] = Sine(
            amplitude=2.0, frequency_hz=2.0, offset=400.0 + i * 5
        )

    return ExperimentConfig(
        hardware=HardwareProfile(
            name="sim",
            devices=(
                DeviceConfig(
                    name="heater",
                    adapter="capa.devices.sim.watlow_sim",
                    params={"tick_period_s": 0.02, "signals": signals},
                ),
            ),
            channels=tuple(chans),
        ),
        procedure=ProcedureRef(id="capa.builtin.free_run", config={"duration_s": duration_s}),
        calibration_set=CalibrationSetRef(name="default"),
        operator=OperatorRef(id="abr", display_name="A. Researcher"),
        sample=SampleInfo(id=sample_id),
    )


# ---------------------------------------------------------------------------
# RunController integration
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_run_controller_run_completes_and_populates_buffers(
    qapp,
    tmp_path: Path,
) -> None:
    config = _make_config(sample_id="CTRL-1", duration_s=0.15, channels=2)
    controller = RunController(runs_root=tmp_path, configure_logging_for_bundle=False)

    states: list[EngineState] = []
    finished: list[EngineResult] = []
    events: list[object] = []
    controller.state_changed.connect(states.append)
    controller.run_finished.connect(finished.append)
    controller.event_received.connect(events.append)

    # Drive the controller's async path directly. start() is the
    # event-loop-task-spawning variant the GUI uses; under pytest-anyio we
    # simply await the work coroutine.
    await controller._run(config)

    assert finished, "expected run_finished signal to fire"
    result = finished[0]
    assert result.run_status == "completed"
    assert result.bundle_status == "sealed"
    assert result.bundle_path is not None and result.bundle_path.is_dir()

    # Both expected channels saw at least one sample. With duration_s=0.15
    # and a 50 Hz sim feed, decimated to 20 Hz, we should have ~3 samples
    # per channel — a non-zero floor is what we assert.
    for ch_name in ("heater.pv0", "heater.pv1"):
        buf = controller.buffers.get(ch_name)
        assert buf is not None, f"buffer for {ch_name} not registered"
        assert buf.size > 0, f"buffer for {ch_name} got no samples"

    # Plan §10.1 transitions all visible to the UI.
    assert EngineState.PREPARING in states
    assert EngineState.RUNNING in states
    assert EngineState.FINALIZING in states
    assert EngineState.SEALED in states


@pytest.mark.anyio
async def test_run_controller_abort_sets_aborted_status(
    qapp,
    tmp_path: Path,
) -> None:
    """request_abort() flows through to the engine and produces an aborted
    bundle. The mode is recorded for downstream phases."""
    config = _make_config(sample_id="CTRL-ABORT", duration_s=10.0, channels=1)
    controller = RunController(runs_root=tmp_path, configure_logging_for_bundle=False)

    finished: list[EngineResult] = []
    controller.run_finished.connect(finished.append)

    async def fire_abort() -> None:
        # Wait for engine to be RUNNING before aborting.
        for _ in range(200):
            engine = controller.engine
            if engine is not None and engine.state is EngineState.RUNNING:
                break
            await anyio.sleep(0.005)
        controller.request_abort(mode="immediate")

    async with anyio.create_task_group() as tg:
        tg.start_soon(fire_abort)
        await controller._run(config)

    result = finished[0]
    assert result.run_status == "aborted"
    assert result.bundle_status == "sealed"
    engine = controller.engine
    assert engine is not None
    assert engine.abort_mode == "immediate"


def test_run_controller_request_abort_with_no_active_run_is_noop(
    qapp,
    tmp_path: Path,
) -> None:
    controller = RunController(runs_root=tmp_path)
    # Should not raise.
    controller.request_abort(mode="safe_shutdown")
    assert controller.engine is None


# ---------------------------------------------------------------------------
# MainWindow smoke
# ---------------------------------------------------------------------------


def test_main_window_renders_with_initial_config(qtbot, tmp_path: Path) -> None:
    from capa.ui.main_window import MainWindow

    config = _make_config(sample_id="WIN-1", duration_s=0.1, channels=2)

    window = MainWindow(
        runs_root=tmp_path,
        configure_logging_for_bundle=False,
        initial_config=config,
    )
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)

    # Setup tab populated.
    setup = window.setup_tab
    assert setup is not None
    # Tree has the experiment root + device + 2 channels under it.
    tree = setup.findChild(type(window).__bases__[0].__bases__[0], "")

    # Numerics dock attached.
    nd = window.numerics_dock
    assert nd is not None

    # Events dock present.
    events = window.events_dock
    assert events is not None

    # Run tab knows the config and Start is enabled.
    run_tab = window.run_tab
    assert run_tab.can_start() is True

    # Operator id reached the status-bar provider.
    assert window._operator_provider.current_operator_id() == "abr"

    window.close()


def test_main_window_open_dialog_path_handling(qtbot, tmp_path: Path) -> None:
    """Constructing MainWindow with no initial config leaves Start disabled
    until a config loads."""
    from capa.ui.main_window import MainWindow

    window = MainWindow(runs_root=tmp_path, configure_logging_for_bundle=False)
    qtbot.addWidget(window)

    # No config loaded → can_start() must be False.
    assert window.run_tab.can_start() is False
    assert window.numerics_dock is None  # built lazily on config load
    window.close()
