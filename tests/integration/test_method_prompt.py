"""A method ``prompt`` step answered from the UI side of a real run.

Drives :meth:`RunController._run` on the sim Watlow with the recipe
runner: the prompt's ``open`` tick must reach
:attr:`RunController.procedure_tick_received` through the conductor's
UI bridge, and :meth:`RunController.send_operator_command` must release
it from there. Before the dialog existed nothing could answer a prompt,
so an interactive run timed out on its first prompt.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from capa.channels.calibration import Identity
from capa.channels.spec import ChannelSpec, WatlowParameter
from capa.devices.sim._signals import Sine
from capa.experiment.config import (
    DeviceConfig,
    ExperimentConfig,
    HardwareProfile,
    OperatorRef,
    ProcedureRef,
    SampleInfo,
)
from capa.experiment.executor import PROMPT_TICK_KIND
from capa.experiment.method import AcquireStep, Method, PromptStep
from capa.experiment.procedures.base import OperatorCommand
from capa.runtime.emissions import ProcedureTick
from capa.ui.state import RunController, RunUiResult


def _config() -> ExperimentConfig:
    return ExperimentConfig(
        hardware=HardwareProfile(
            name="sim",
            devices=(
                DeviceConfig(
                    name="heater",
                    adapter="capa.devices.sim.watlow_sim",
                    params={
                        "tick_period_s": 0.02,
                        "signals": {
                            ("process_value", 1): Sine(
                                amplitude=1.0, frequency_hz=1.0, offset=300.0
                            )
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
        method=Method(
            name="prompt-e2e",
            steps=(
                PromptStep(title="Move", message="Slide the heater to stop B, then Continue."),
                AcquireStep(duration_s=0.05),
                PromptStep(title="Move back", message="Slide back to stop A, then Continue."),
            ),
        ),
        procedure=ProcedureRef(id="capa.builtin.recipe_runner"),
        operator=OperatorRef(id="abr", display_name="A. Researcher"),
        sample=SampleInfo(id="PROMPT-E2E"),
    )


@pytest.mark.anyio
async def test_prompts_are_confirmed_from_the_ui(qapp: Any, tmp_path: Path) -> None:
    config = _config()
    controller = RunController(runs_root=tmp_path, configure_logging_for_bundle=False)
    finished: list[RunUiResult] = []
    controller.run_finished.connect(finished.append)
    confirmed: list[str] = []

    def on_tick(tick: object) -> None:
        # What the dialog's Continue does, once per prompt.
        if not isinstance(tick, ProcedureTick) or tick.payload.get("kind") != PROMPT_TICK_KIND:
            return
        prompt_id = tick.payload["prompt_id"]
        if tick.payload["state"] != "open" or prompt_id in confirmed:
            return
        assert isinstance(prompt_id, str)
        confirmed.append(prompt_id)
        cmd = OperatorCommand(kind="confirm_prompt", metadata={"prompt_id": prompt_id})
        assert controller.send_operator_command(cmd)

    controller.procedure_tick_received.connect(on_tick)

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
        await controller._run(config)
    finally:
        await pool.close()

    result = finished[0]
    assert result.run_status == "completed", result.exit_reason
    assert result.bundle_path is not None
    assert len(confirmed) == 2
    with sqlite3.connect(result.bundle_path / "events.sqlite") as con:
        kinds = [row[0] for row in con.execute("select kind from events order by id")]
    assert kinds.count("method.prompt.acknowledged") == 2
    assert "method.prompt.unanswered" not in kinds
