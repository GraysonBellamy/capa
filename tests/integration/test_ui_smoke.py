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
from typing import Any

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
from capa.runtime.progress import DeviceInitStatus
from capa.ui.config_progress import ConfigLoadPhase, ConfigLoadProgress
from capa.ui.state import RunController, RunUiResult, RunUiState


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
    qapp: Any,
    tmp_path: Path,
) -> None:
    config = _make_config(sample_id="CTRL-1", duration_s=0.15, channels=2)
    controller = RunController(runs_root=tmp_path, configure_logging_for_bundle=False)

    states: list[RunUiState] = []
    finished: list[RunUiResult] = []
    events: list[object] = []
    controller.state_changed.connect(states.append)
    controller.run_finished.connect(finished.append)
    controller.event_received.connect(events.append)

    # Phase 4 split set_active_config from start: open the pool first so
    # the conductor has workers to arm. Tests run on a single asyncio
    # loop (pytest-anyio's), so we await the open inline.
    from capa.runtime.dispatch import ManualClient
    from capa.runtime.pool import WorkerPool

    pool = WorkerPool.from_config(config)
    controller._active_config = config
    controller._worker_pool = pool
    controller._manual_client = ManualClient(
        pool=pool,
        conductor_provider=lambda: controller._conductor,
    )
    await pool.open()
    try:
        # Drive the controller's async path directly. start() is the
        # event-loop-task-spawning variant the GUI uses; under
        # pytest-anyio we simply await the work coroutine.
        await controller._run(config)
    finally:
        await pool.close()

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
    assert RunUiState.PREPARING in states
    assert RunUiState.RUNNING in states
    assert RunUiState.FINALIZING in states
    assert RunUiState.SEALED in states


@pytest.mark.anyio
async def test_run_controller_abort_sets_aborted_status(
    qapp: Any,
    tmp_path: Path,
) -> None:
    """request_abort() flows through to the conductor and produces an
    aborted bundle. The conductor's stop reason is recorded for
    downstream phases (operator audit, P3 cooldown)."""
    config = _make_config(sample_id="CTRL-ABORT", duration_s=10.0, channels=1)
    controller = RunController(runs_root=tmp_path, configure_logging_for_bundle=False)

    finished: list[RunUiResult] = []
    controller.run_finished.connect(finished.append)

    from capa.runtime.dispatch import ManualClient
    from capa.runtime.pool import WorkerPool

    pool = WorkerPool.from_config(config)
    controller._active_config = config
    controller._worker_pool = pool
    controller._manual_client = ManualClient(
        pool=pool,
        conductor_provider=lambda: controller._conductor,
    )
    await pool.open()

    async def fire_abort() -> None:
        # Wait for the conductor to reach RUNNING before aborting.
        for _ in range(400):
            conductor = controller.conductor
            if conductor is not None and conductor.state.value == "running":
                break
            await anyio.sleep(0.005)
        controller.request_abort(mode="immediate")

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(fire_abort)
            await controller._run(config)
    finally:
        await pool.close()

    result = finished[0]
    assert result.run_status == "aborted"
    assert result.bundle_status == "sealed"


def test_run_controller_request_abort_with_no_active_run_is_noop(
    qapp: Any,
    tmp_path: Path,
) -> None:
    controller = RunController(runs_root=tmp_path)
    # Should not raise.
    controller.request_abort(mode="safe_shutdown")
    assert controller.conductor is None


@pytest.mark.anyio
async def test_run_controller_config_load_progress_reaches_ready(
    qapp: Any,
    tmp_path: Path,
) -> None:
    config = _make_config(sample_id="CTRL-LOAD", duration_s=0.1, channels=1)
    controller = RunController(runs_root=tmp_path, configure_logging_for_bundle=False)

    started: list[ConfigLoadProgress] = []
    progress: list[ConfigLoadProgress] = []
    finished: list[ConfigLoadProgress] = []
    ready: list[bool] = []
    controller.config_load_started.connect(started.append)
    controller.config_load_progress.connect(progress.append)
    controller.config_load_finished.connect(finished.append)
    controller.hardware_ready_changed.connect(ready.append)

    controller.set_active_config(config, config_path=tmp_path / "sim.toml")
    try:
        for _ in range(200):
            if finished:
                break
            await anyio.sleep(0.01)

        assert started
        assert progress
        assert finished
        assert finished[-1].phase is ConfigLoadPhase.READY
        assert ready[-1] is True
        assert controller.hardware_ready is True
        assert any(snapshot.phase is ConfigLoadPhase.OPENING_DEVICES for snapshot in progress)
        assert any(
            row.name == "heater" and row.status is DeviceInitStatus.READY
            for snapshot in progress
            for row in snapshot.devices
        )
    finally:
        await controller.aclose_pool()


# ---------------------------------------------------------------------------
# MainWindow smoke
# ---------------------------------------------------------------------------


def test_main_window_renders_with_initial_config(qtbot: Any, tmp_path: Path) -> None:
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
    qtbot.waitUntil(lambda: window.numerics_dock is not None, timeout=1000)

    # Setup tab populated.
    setup = window.setup_tab
    assert setup is not None
    # Numerics dock attached.
    nd = window.numerics_dock
    assert nd is not None

    # Events dock present.
    events = window.events_dock
    assert events is not None

    # Run tab knows the config; Start enables once the worker pool's async
    # ``open()`` resolves. In production that runs on the qasync loop via
    # ``schedule_bg``; this test has no running loop, so ``schedule_bg``
    # closes the coroutine without scheduling. Drive ``pool.open()`` →
    # check → ``pool.close()`` in one coroutine so the worker threads are
    # joined before pytest tears down (otherwise the live threads keep
    # the test process hanging at exit).
    pool = window._controller.worker_pool
    assert pool is not None
    run_tab = window.run_tab

    async def _open_then_check_then_close() -> None:
        await pool.open()
        try:
            assert run_tab.can_start() is True
        finally:
            await pool.close()

    anyio.run(_open_then_check_then_close)

    # Operator id reached the status-bar provider.
    assert window._operator_provider.current_operator_id() == "abr"

    window.close()


def test_main_window_open_dialog_path_handling(qtbot: Any, tmp_path: Path) -> None:
    """Constructing MainWindow with no initial config leaves Start disabled
    until a config loads."""
    from capa.ui.main_window import MainWindow

    window = MainWindow(runs_root=tmp_path, configure_logging_for_bundle=False)
    qtbot.addWidget(window)

    # No config loaded → can_start() must be False.
    assert window.run_tab.can_start() is False
    assert window.numerics_dock is None  # built lazily on config load
    window.close()
