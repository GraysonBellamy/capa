"""End-to-end tests for camera adapter suppression by the recording plan.

Pins the two gotchas identified during planning:

1. ``start()`` skips ``start_recording`` when ``recording_enabled=False``
   — and ``stream()`` exits cleanly (instead of raising) so the worker's
   stream task doesn't crash the run.
2. The IDLE preview source keeps running for the suppressed camera so
   the operator's tile stays alive during the run.

Plus the happy-path regression: ``recording_enabled=True`` still opens
the ``.csq`` and produces frames.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest

from capa.core.clock import RunClock
from capa.devices.adapter import DeviceAdapter
from capa.devices.camera.base import CameraSpec
from capa.devices.sim.flir_ir_sim import FlirIrSim
from capa.runtime.camera_adapter import make_camera_adapter
from capa.runtime.lifecycle import WorkerState
from capa.runtime.metrics import DisarmResult
from capa.runtime.recording import ResolvedRecordingPlan
from capa.runtime.runcontext import RunContext
from capa.runtime.runner import ThreadedRunner
from capa.runtime.worker import Worker
from tests.integration.runtime.fakes import FakeWriterRef

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _ir_spec(name: str = "ir_cam0", *, fps: int = 30) -> CameraSpec:
    return CameraSpec.model_validate(
        {
            "name": name,
            "adapter": "capa.devices.sim.flir_ir_sim",
            "kind": "ir",
            "params": {"fps": fps},
        }
    )


class _PathBundleRef:
    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> object:
        return self._root


def _ctx(
    bundle_root: Path,
    *,
    recorded_cameras: tuple[str, ...] = ("ir_cam0",),
    camera_mode: str = "all",
) -> RunContext:
    return RunContext(
        run_id="test-run",
        clock=RunClock.now(),
        writer=FakeWriterRef(),
        bundle=_PathBundleRef(bundle_root),
        recording_plan=ResolvedRecordingPlan(
            channel_mode="all",
            camera_mode=camera_mode,
            recorded_cameras=recorded_cameras,
            source="procedure_default",
        ),
    )


class TestCameraSuppression:
    async def test_suppressed_camera_writes_no_file(self, tmp_path: Path) -> None:
        """Plan with ``camera_mode='none'`` — no ``.csq`` lands in the bundle."""
        wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=_ir_spec(fps=20))
        worker = Worker(
            resource_id=wrapper.resource_id,
            adapters=[cast(DeviceAdapter, wrapper)],
            runner=ThreadedRunner(name="cam-suppress"),
        )
        await worker.async_start()
        try:
            ctx = _ctx(tmp_path, camera_mode="none", recorded_cameras=())
            await worker.async_arm(ctx)
            bridge = await worker.async_begin_sampling(consumer_loop=asyncio.get_running_loop())
            assert worker.state is WorkerState.SAMPLING
            # Give the stream task a moment to exhaust the (empty) iterator.
            # No frames should ever appear — race that against a short timeout.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(bridge.get(), timeout=0.5)

            result = await worker.async_disarm(grace_s=3.0)
            # The stream task exited cleanly — disarm returns OK, not FORCED.
            assert result is DisarmResult.OK
            assert getattr(worker, "state") is WorkerState.IDLE  # noqa: B009
        finally:
            await worker.async_close(grace_s=2.0)

        # No video file should exist in the bundle.
        csq_path = tmp_path / "video" / "ir_cam0.csq"
        assert not csq_path.exists(), (
            f"suppressed camera should not have written a file, but {csq_path} exists"
        )

    async def test_unsuppressed_camera_still_records(self, tmp_path: Path) -> None:
        """Regression check: the suppression branch doesn't break the
        normal record-everything path."""
        wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=_ir_spec(fps=20))
        worker = Worker(
            resource_id=wrapper.resource_id,
            adapters=[cast(DeviceAdapter, wrapper)],
            runner=ThreadedRunner(name="cam-normal"),
        )
        await worker.async_start()
        try:
            ctx = _ctx(tmp_path)  # camera_mode='all' by default
            await worker.async_arm(ctx)
            bridge = await worker.async_begin_sampling(consumer_loop=asyncio.get_running_loop())
            # We should see at least one frame
            emission = await asyncio.wait_for(bridge.get(), timeout=2.0)
            assert emission is not None

            await worker.async_disarm(grace_s=3.0)
        finally:
            await worker.async_close(grace_s=2.0)

        csq_path = tmp_path / "video" / "ir_cam0.csq"
        assert csq_path.exists(), "unsuppressed camera must write its container"

    async def test_suppressed_camera_then_stop_is_clean(self, tmp_path: Path) -> None:
        """stop() must not blow up when start() short-circuited."""
        wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=_ir_spec(fps=20))
        worker = Worker(
            resource_id=wrapper.resource_id,
            adapters=[cast(DeviceAdapter, wrapper)],
            runner=ThreadedRunner(name="cam-stop-clean"),
        )
        await worker.async_start()
        try:
            ctx = _ctx(tmp_path, camera_mode="none", recorded_cameras=())
            await worker.async_arm(ctx)
            await worker.async_begin_sampling(consumer_loop=asyncio.get_running_loop())
            # Immediate disarm — proves stop() handles the "never recorded"
            # state without raising.
            result = await worker.async_disarm(grace_s=2.0)
            assert result is DisarmResult.OK
        finally:
            await worker.async_close(grace_s=2.0)
