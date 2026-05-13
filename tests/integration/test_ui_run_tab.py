"""``RunTab`` regression tests for the two inline UI fixes from
hardware-day §6 — guarding against:

* the plot pane being bound to the *empty* registry built in
  :meth:`RunTab.load_config` instead of the controller's freshly-rebuilt
  buffers (the rebind must happen on the ``RunUiState.RUNNING``
  transition, not at click time);
* the Start button staying disabled forever after a run seals because
  ``RunController.is_active`` is still ``True`` when the
  ``run_finished`` signal first fires (the re-enable must be deferred
  one event-loop tick).

The full controller drives a real engine task, which is overkill here
and brittle inside pytest-qt. Instead we attach :class:`RunTab` to a
minimal :class:`_FakeController` ``QObject`` that exposes the same
signals + properties.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from PySide6.QtCore import QObject, Signal

from capa.channels.calibration import Identity
from capa.channels.spec import ChannelSpec, WatlowParameter
from capa.core.ringbuffer import RingBufferRegistry
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
from capa.runtime.lifecycle import PoolState
from capa.ui.state import RunUiResult, RunUiState
from capa.ui.tabs.run import RunTab


def _config() -> ExperimentConfig:
    return ExperimentConfig(
        hardware=HardwareProfile(
            name="ui",
            devices=(
                DeviceConfig(
                    name="heater",
                    adapter="capa.devices.sim.watlow_sim",
                    params={
                        "tick_period_s": 0.05,
                        "signals": {
                            ("process_value", 1): Sine(
                                amplitude=2.0, frequency_hz=2.0, offset=400.0
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
                    plot_group="temperature",
                    decimate_to_hz=20.0,
                ),
            ),
        ),
        procedure=ProcedureRef(id="capa.builtin.free_run", config={"duration_s": 0.1}),
        calibration_set=CalibrationSetRef(name="default"),
        operator=OperatorRef(id="op"),
        sample=SampleInfo(id="UI-RT"),
    )


class _FakeController(QObject):
    """Minimal stand-in for :class:`RunController` — exposes the slots
    :class:`RunTab` connects to, plus a writable ``is_active`` property
    so tests can simulate the "task hasn't reached done() yet" race that
    motivated the ``QTimer.singleShot(0, ...)`` defer.
    """

    state_changed = Signal(object)
    event_received = Signal(object)
    run_finished = Signal(object)
    pool_changed = Signal(object)

    def __init__(self, *, channels: tuple[str, ...]) -> None:
        super().__init__()
        # Pre-populate buffers so the test can assert identity-equality
        # against the rebound plot pane registry.
        self._buffers = RingBufferRegistry()
        for ch in channels:
            self._buffers.register(ch, decimate_to_hz=20.0)
        self.is_active = False
        # RunTab.can_start() checks ``worker_pool.state is PoolState.OPEN``
        # before enabling the Start button. Expose a minimal stub that
        # always reports OPEN — these tests aren't exercising the pool
        # readiness race, just the seal / rebind paths.
        self.worker_pool: Any = SimpleNamespace(state=PoolState.OPEN)

    @property
    def buffers(self) -> RingBufferRegistry:
        return self._buffers

    # The real RunController exposes start/abort but RunTab tests don't
    # exercise them — leave as no-ops to keep the seam minimal.
    def start(self, _config: Any) -> None:
        self.is_active = True

    def request_abort(self, *, mode: str) -> None:
        pass


@pytest.fixture
def fake_controller(qapp: Any) -> _FakeController:
    return _FakeController(channels=("heater.pv",))


def test_plot_pane_rebound_to_controller_buffers_on_running(
    qtbot: Any, fake_controller: _FakeController
) -> None:
    """RunTab must replace the placeholder registry built in
    :meth:`load_config` with :attr:`RunController.buffers` once the
    engine reaches ``RUNNING`` — otherwise the live plot stays bound to
    a registry that no producer ever writes to (the §6 inline fix)."""
    tab = RunTab(controller=fake_controller)  # type: ignore[arg-type]
    qtbot.addWidget(tab)
    tab.load_config(_config())

    # Before RUNNING: pane is bound to the empty placeholder built in
    # load_config(), NOT to the controller's buffers.
    assert tab._plot_pane is not None
    placeholder_registry = tab._plot_pane._registry
    assert placeholder_registry is not fake_controller.buffers

    tab._on_state(RunUiState.RUNNING)

    # After RUNNING: pane registry IS the controller's buffers.
    assert tab._plot_pane is not None
    assert tab._plot_pane._registry is fake_controller.buffers


def test_start_button_reenables_after_seal_via_singleshot(
    qtbot: Any, fake_controller: _FakeController
) -> None:
    """Without the deferred re-enable, ``can_start()`` returns ``False``
    at the moment ``_on_run_finished`` fires (the controller's task isn't
    yet ``done()``); the button stays disabled forever (§6 inline fix).

    Simulate that race by leaving ``is_active = True`` while the slot
    runs, then flipping it ``False`` after the signal returns. With the
    ``QTimer.singleShot(0, ...)`` defer, the button re-enables on the
    next event-loop tick.
    """
    tab = RunTab(controller=fake_controller)  # type: ignore[arg-type]
    qtbot.addWidget(tab)
    tab.load_config(_config())

    # Start the run; the click handler disables the button and asks the
    # controller to start — our fake just flips is_active.
    tab._on_start_clicked()
    assert tab._start_btn.isEnabled() is False
    assert fake_controller.is_active is True

    # RunTab inspects ``bundle_path``, ``run_id``, and ``run_status``;
    # everything else is irrelevant for this slot.
    from pathlib import Path as _Path

    sealed = RunUiResult(
        run_id="2026-05-09_000000_UI-RT",
        bundle_path=_Path("/tmp/ui-rt-fake"),
        run_status="completed",
        bundle_status="sealed",
        integrity_status="ok",
    )

    # Simulate the race: the controller's task is still mid-finally
    # block when the signal fires, so is_active stays True.
    fake_controller.is_active = True
    tab._on_run_finished(sealed)

    # Slot returned but the QTimer hasn't fired yet AND is_active still
    # True → button still disabled at this exact instant.
    assert tab._start_btn.isEnabled() is False

    # The controller's task finishes before the next loop tick.
    fake_controller.is_active = False

    # Pump the Qt event loop until the singleShot callback re-enables
    # the button — without the fix this never happens.
    qtbot.waitUntil(lambda: tab._start_btn.isEnabled() is True, timeout=1000)
