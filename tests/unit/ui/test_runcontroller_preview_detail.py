""":class:`RunController.set_preview_detail`: a pop-out window's request for
full-size previews reaches the camera through the live pool, and never
raises into the UI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest

from capa.experiment.config import ExperimentConfig
from capa.ui.config_progress import ConfigLoadProgress
from capa.ui.state import RunController

pytestmark = pytest.mark.anyio


@pytest.fixture
async def controller(qapp: Any, tmp_path: Path) -> Any:
    ctrl = RunController(runs_root=tmp_path, configure_logging_for_bundle=False)
    yield ctrl
    await ctrl.aclose_pool()


async def _load(controller: RunController, configs_dir: Path) -> None:
    finished: list[ConfigLoadProgress] = []
    controller.config_load_finished.connect(finished.append)
    controller.set_active_config(
        ExperimentConfig.load(configs_dir / "experiments" / "sim_capa_pyrolysis.yaml")
    )
    with anyio.fail_after(30):
        while not finished:
            await anyio.sleep(0.01)


async def test_request_reaches_the_camera_through_the_pool(
    controller: RunController, configs_dir: Path
) -> None:
    await _load(controller, configs_dir)
    client = controller.manual_client
    assert client is not None
    # The IR simulator has no detail mode: the request reaches it and is declined.
    assert await client.set_preview_detail("ir_cam0", True) is False


async def test_failures_stay_out_of_the_ui(controller: RunController, configs_dir: Path) -> None:
    controller.set_preview_detail("ir_cam0", True)  # no pool yet: a no-op
    await _load(controller, configs_dir)
    client = controller.manual_client
    assert client is not None
    await controller._send_preview_detail(client, "no_such_camera", True)
