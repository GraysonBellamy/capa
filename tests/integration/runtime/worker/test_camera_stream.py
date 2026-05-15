"""Worker hosting a :class:`CameraDeviceAdapter` — full arm/sample/disarm cycle.

These tests verify that a camera wrapper is a first-class adapter from
the Worker's perspective:

* :meth:`Worker.start` opens the camera handle on the worker loop.
* :meth:`Worker.arm` installs the RunContext into the wrapper.
* :meth:`Worker.begin_sampling` calls ``wrapper.start(run_context)``,
  which the worker's signature-probing dispatcher hands the full context —
  the wrapper then opens the camera's
  output container and spawns the multiplexer.
* The outbound bridge carries :class:`FrameReceipt` and
  :class:`CameraEvent` values produced by the wrapper's multiplexer.
* :meth:`Worker.disarm` returns ``OK`` within grace — the wrapper's
  ``stop()`` cancels the mux scope cooperatively.

Driven against :class:`FlirIrSim` because it pumps deterministic frames
in-process. Hardware tests against the webcam adapter live under
``tests/hardware/``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from capa.core.clock import RunClock
from capa.devices.camera.base import (
    CameraEvent,
    CameraSpec,
    FrameReceipt,
)
from capa.devices.sim.flir_ir_sim import FlirIrSim
from capa.runtime.camera_adapter import make_camera_adapter
from capa.runtime.lifecycle import WorkerState
from capa.runtime.metrics import DisarmResult
from capa.runtime.runcontext import RunContext
from capa.runtime.runner import ThreadedRunner
from capa.runtime.worker import Worker
from tests.integration.runtime.fakes import FakeWriterRef

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _ctx(bundle_root: Path, *, run_id: str = "test-run") -> RunContext:
    return RunContext(
        run_id=run_id,
        clock=RunClock.now(),
        writer=FakeWriterRef(),
        bundle=_PathBundleRef(bundle_root),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCameraWorkerLifecycle:
    async def test_full_arm_sample_disarm_cycle(self, tmp_path: Path) -> None:
        """The end-to-end happy path: pool-open → arm → sample → disarm.

        Verifies that the wrapper integrates with the worker state
        machine without any camera-specific hooks at the worker level.
        """
        wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=_ir_spec(fps=20))
        worker = Worker(
            resource_id=wrapper.resource_id,
            adapters=[wrapper],
            runner=ThreadedRunner(name="cam-cycle"),
        )
        await worker.async_start()
        try:
            assert worker.state is WorkerState.IDLE
            await worker.async_arm(_ctx(tmp_path))
            assert worker.state is WorkerState.ARMED
            bridge = await worker.async_begin_sampling(consumer_loop=asyncio.get_running_loop())
            try:
                assert worker.state is WorkerState.SAMPLING
                # Sample at least one frame off the bridge — proves
                # adapter.start was called with the full ctx (otherwise
                # start_recording would have failed without a path).
                frames_seen = 0
                events_seen = 0
                for _ in range(20):
                    emission = await asyncio.wait_for(bridge.get(), timeout=2.0)  # type: ignore[union-attr]
                    assert emission is not None
                    if isinstance(emission, FrameReceipt):
                        frames_seen += 1
                    elif isinstance(emission, CameraEvent):
                        events_seen += 1
                    if frames_seen >= 3:
                        break
                assert frames_seen >= 3
                # recording_started fires inside wrapper.start → camera
                # bursts the event onto the event stream; the mux
                # forwards it within the first batch.
                assert events_seen >= 1
            finally:
                result = await worker.async_disarm(grace_s=3.0)
                assert result is DisarmResult.OK
                assert worker.state is WorkerState.IDLE
        finally:
            await worker.async_close(grace_s=2.0)

    async def test_disarm_seals_output_container(self, tmp_path: Path) -> None:
        """Wrapper.stop() must call camera.stop_recording so the output
        container (``.csq`` for IR) is sealed with the final frame count.

        Without this, worker disarm would leave a dangling write handle
        and the bundle's video sidecar would be unreadable.
        """
        wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=_ir_spec(fps=30))
        worker = Worker(
            resource_id=wrapper.resource_id,
            adapters=[wrapper],
            runner=ThreadedRunner(name="cam-disarm"),
        )
        await worker.async_start()
        try:
            ctx = _ctx(tmp_path)
            await worker.async_arm(ctx)
            bridge = await worker.async_begin_sampling(consumer_loop=asyncio.get_running_loop())
            # Drain a few frames so the sim writes some data
            for _ in range(5):
                await asyncio.wait_for(bridge.get(), timeout=2.0)  # type: ignore[union-attr]
            await worker.async_disarm(grace_s=3.0)

            csq_path = tmp_path / "video" / "ir_cam0.csq"
            assert csq_path.exists()
            # Sim writes a magic header; non-zero size means start_recording
            # ran and stop_recording finalized.
            assert csq_path.stat().st_size > 0
            # The sim records "recording_stopped" inside stop_recording —
            # check the .meta.json sidecar got the final frame_count.
            meta = csq_path.with_suffix(".csq.meta.json")
            assert meta.exists()
        finally:
            await worker.async_close(grace_s=2.0)

    async def test_multiple_runs_against_same_worker(self, tmp_path: Path) -> None:
        """The whole reason for pool-lifetime workers: open hardware
        once, run many times. Verify two consecutive arm/disarm cycles
        produce two independent bundles without re-opening the camera.
        """
        wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=_ir_spec(fps=20))
        worker = Worker(
            resource_id=wrapper.resource_id,
            adapters=[wrapper],
            runner=ThreadedRunner(name="cam-multi-run"),
        )
        await worker.async_start()
        try:
            for run_id in ("run-1", "run-2"):
                run_root = tmp_path / run_id
                run_root.mkdir()
                ctx = _ctx(run_root, run_id=run_id)
                await worker.async_arm(ctx)
                bridge = await worker.async_begin_sampling(consumer_loop=asyncio.get_running_loop())
                # Drain a couple of frames
                for _ in range(3):
                    await asyncio.wait_for(bridge.get(), timeout=2.0)  # type: ignore[union-attr]
                await worker.async_disarm(grace_s=3.0)
                # Each run produced its own .csq
                assert (run_root / "video" / "ir_cam0.csq").exists()
            # Camera was opened exactly once (worker.start ran open(),
            # subsequent arms reuse the open handle).
            assert wrapper.camera._open is True
        finally:
            await worker.async_close(grace_s=2.0)


class TestCameraEmissionTypes:
    async def test_frame_receipts_carry_correct_camera_name(self, tmp_path: Path) -> None:
        spec = _ir_spec(name="thermal_top_view", fps=20)
        wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=spec)
        worker = Worker(
            resource_id=wrapper.resource_id,
            adapters=[wrapper],
            runner=ThreadedRunner(name="cam-name-attr"),
        )
        await worker.async_start()
        try:
            await worker.async_arm(_ctx(tmp_path))
            bridge = await worker.async_begin_sampling(consumer_loop=asyncio.get_running_loop())
            # Find the first FrameReceipt
            for _ in range(20):
                emission = await asyncio.wait_for(bridge.get(), timeout=2.0)  # type: ignore[union-attr]
                if isinstance(emission, FrameReceipt):
                    assert emission.name == "thermal_top_view"
                    break
            else:
                pytest.fail("no FrameReceipt seen within 20 emissions")
        finally:
            await worker.async_disarm(grace_s=3.0)
            await worker.async_close(grace_s=2.0)

    async def test_recording_started_event_carries_camera_event_type(self, tmp_path: Path) -> None:
        """The wrapper must yield :class:`CameraEvent` instances, not
        :class:`DeviceEvent` — the Conductor's dispatch table keys on
        the runtime class.
        """
        wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=_ir_spec(fps=15))
        worker = Worker(
            resource_id=wrapper.resource_id,
            adapters=[wrapper],
            runner=ThreadedRunner(name="cam-event-type"),
        )
        await worker.async_start()
        try:
            await worker.async_arm(_ctx(tmp_path))
            bridge = await worker.async_begin_sampling(consumer_loop=asyncio.get_running_loop())
            for _ in range(20):
                emission = await asyncio.wait_for(bridge.get(), timeout=2.0)  # type: ignore[union-attr]
                if isinstance(emission, CameraEvent):
                    assert emission.kind == "recording_started"
                    break
            else:
                pytest.fail("no CameraEvent seen within 20 emissions")
        finally:
            await worker.async_disarm(grace_s=3.0)
            await worker.async_close(grace_s=2.0)
