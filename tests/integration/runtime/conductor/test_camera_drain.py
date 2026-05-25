"""Conductor drain emission-type dispatch tests.

Verifies that the Conductor's per-worker drain dispatches by runtime type
so cameras land on the right writer paths.

* :class:`FrameReceipt` → :meth:`WriterRef.record_frame` (the FakeWriterRef
  collects them in ``frames``).
* :class:`CameraEvent` → :meth:`WriterRef.write_camera_event` (collected
  in ``camera_events`` with full attribution: ``camera.<kind>`` /
  ``camera:<name>``).
* Non-camera :data:`DeviceEmission` → :meth:`WriterRef.submit` +
  :meth:`DataBus.publish` (unchanged from the pre-camera-unification path).

Cameras do NOT participate in the procedure-side databus — this matches
today's engine behavior where ``FrameReceipt`` never reached the bus.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from capa.core.clock import RunClock
from capa.devices.adapter import DeviceAdapter
from capa.devices.camera.base import CameraEvent, CameraSpec, FrameReceipt
from capa.devices.sim.flir_ir_sim import FlirIrSim
from capa.runtime.camera_adapter import make_camera_adapter
from capa.runtime.conductor import Conductor, NoOpRunner, RunOutcome
from capa.runtime.pool import WorkerPool
from capa.runtime.runner import ThreadedRunner
from capa.runtime.state import ConductorState
from capa.runtime.worker import Worker
from tests.integration.runtime.conductor.fakes import FakeRunSession
from tests.integration.runtime.fakes import FakeWriterRef, make_fake_adapter

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _PathBundleRef:
    """Bundle ref whose ``root`` is a real :class:`pathlib.Path`.

    The default :class:`FakeBundleRef` returns a string root; cameras
    need a real directory so the wrapper's path resolution works.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> object:
        return self._root


def _ir_spec(name: str = "ir_cam0", *, fps: int = 20) -> CameraSpec:
    return CameraSpec.model_validate(
        {
            "name": name,
            "adapter": "capa.devices.sim.flir_ir_sim",
            "kind": "ir",
            "params": {"fps": fps},
        }
    )


def _make_camera_pool(spec: CameraSpec) -> WorkerPool:
    wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=spec)
    rid = wrapper.resource_id
    worker = Worker(
        resource_id=rid,
        adapters=[cast(DeviceAdapter, wrapper)],
        runner=ThreadedRunner(name=f"worker-{rid}"),
    )
    return WorkerPool(
        workers={rid: worker},
        device_to_resource={spec.name: rid},
    )


def _make_mixed_pool(
    camera_spec: CameraSpec, device_resource_id: str = "sim:fake_dev"
) -> tuple[WorkerPool, object]:
    """Build a pool with one camera + one fake device adapter."""
    wrapper = make_camera_adapter(camera_cls=FlirIrSim, spec=camera_spec)
    cam_rid = wrapper.resource_id
    cam_worker = Worker(
        resource_id=cam_rid,
        adapters=[cast(DeviceAdapter, wrapper)],
        runner=ThreadedRunner(name=f"worker-{cam_rid}"),
    )
    dev = make_fake_adapter("fake_dev", resource_id=device_resource_id, tick_period_s=0.02)
    dev_worker = Worker(
        resource_id=device_resource_id,
        adapters=[dev],
        runner=ThreadedRunner(name=f"worker-{device_resource_id}"),
    )
    pool = WorkerPool(
        workers={cam_rid: cam_worker, device_resource_id: dev_worker},
        device_to_resource={
            camera_spec.name: cam_rid,
            "fake_dev": device_resource_id,
        },
    )
    return pool, dev


def _session(bundle_root: Path) -> FakeRunSession:
    """Build a FakeRunSession whose bundle ref points to a real path."""
    return FakeRunSession(
        run_id="cam-test-run",
        bundle_path=bundle_root,
        clock=RunClock.now(),
        writer_ref=FakeWriterRef(),
        bundle_ref=_PathBundleRef(bundle_root),  # type: ignore[arg-type]
    )


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    """Poll predicate until it returns truthy or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise TimeoutError("predicate never became true")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCameraDrainDispatch:
    async def test_frame_receipts_routed_to_record_frame(self, tmp_path: Path) -> None:
        """The conductor must call ``writer.record_frame`` for every
        :class:`FrameReceipt` and never ``writer.submit`` for them.
        """
        pool = _make_camera_pool(_ir_spec(fps=25))
        await pool.open()
        try:
            session = _session(tmp_path)
            # Park procedure forever; we'll stop the conductor manually
            # after draining a few frames.
            runner = NoOpRunner()
            cond = Conductor(pool=pool, session=session, runner=runner)
            cond.start()
            try:
                # Wait for at least 3 frames to land on the writer
                _wait_until(lambda: len(session.writer_ref.frames) >= 3, timeout=5.0)
            finally:
                cond.stop(reason="test_done")
                result = cond.result_future.result(timeout=5.0)
                cond.join(timeout=2.0)
            assert result.outcome in (RunOutcome.ABORTED, RunOutcome.COMPLETED)
            assert result.final_state is ConductorState.SEALED
            # FrameReceipts went to record_frame
            assert len(session.writer_ref.frames) >= 3
            for f in session.writer_ref.frames:
                assert isinstance(f, FrameReceipt)
                assert f.name == "ir_cam0"
            # And NONE of them ended up on writer.submit (which would
            # crash WriterThread._dispatch — the union doesn't include
            # bare FrameReceipt)
            for emission in session.writer_ref.submitted:
                # Defensive runtime check: mypy already proves DeviceEmission
                # excludes FrameReceipt, but the test guards against
                # regressions that would widen the union.
                assert not isinstance(emission, FrameReceipt)  # type: ignore[unreachable]
        finally:
            await pool.close()

    async def test_camera_events_routed_to_write_camera_event(self, tmp_path: Path) -> None:
        """The conductor must call ``writer.write_camera_event`` with
        ``kind=camera.<event.kind>`` and ``source=camera:<name>`` so the
        bundle's events.sqlite carries the same attribution as today's
        engine path.
        """
        pool = _make_camera_pool(_ir_spec(fps=20))
        await pool.open()
        try:
            session = _session(tmp_path)
            runner = NoOpRunner()
            cond = Conductor(pool=pool, session=session, runner=runner)
            cond.start()
            try:
                # Wait for the recording_started event to appear
                _wait_until(
                    lambda: any(
                        ev["kind"] == "camera.recording_started"
                        for ev in session.writer_ref.camera_events
                    ),
                    timeout=5.0,
                )
            finally:
                cond.stop(reason="test_done")
                cond.result_future.result(timeout=5.0)
                cond.join(timeout=2.0)
            # The event has the camera.<kind> prefix
            kinds = {ev["kind"] for ev in session.writer_ref.camera_events}
            assert "camera.recording_started" in kinds
            # And source attribution
            sources = {ev["source"] for ev in session.writer_ref.camera_events}
            assert "camera:ir_cam0" in sources
            # And carries CameraEvent's t_mono_ns + severity verbatim
            for ev in session.writer_ref.camera_events:
                assert isinstance(ev["t_mono_ns"], int)
                assert ev["severity"] in {"info", "warning", "error"}
        finally:
            await pool.close()

    async def test_camera_only_pool_never_calls_submit(self, tmp_path: Path) -> None:
        """A pool containing only cameras must never call
        ``writer.submit`` — all camera emissions route through
        ``record_frame`` and ``write_camera_event``.

        This is the structural check that camera emissions don't leak
        onto the device path. The databus-publish bypass is part of the
        same dispatch branch; if ``submit`` is not called, neither is
        ``bus.publish`` (the dispatch code reaches them together).
        """
        pool = _make_camera_pool(_ir_spec(fps=20))
        await pool.open()
        try:
            session = _session(tmp_path)
            runner = NoOpRunner()
            cond = Conductor(pool=pool, session=session, runner=runner)
            cond.start()
            try:
                _wait_until(lambda: len(session.writer_ref.frames) >= 5, timeout=5.0)
            finally:
                cond.stop(reason="test_done")
                cond.result_future.result(timeout=5.0)
                cond.join(timeout=2.0)
            # Camera path was active
            assert len(session.writer_ref.frames) >= 5
            # Device path was NOT — no FrameReceipt / CameraEvent / any
            # other emission ended up on the device submit path.
            assert session.writer_ref.submitted == []
        finally:
            await pool.close()


class TestMixedPoolDispatch:
    async def test_device_and_camera_routed_independently(self, tmp_path: Path) -> None:
        """A pool with one camera and one device verifies dispatch picks
        the right writer method per emission type:

        * Camera frames → record_frame
        * Camera events → write_camera_event
        * Device emissions → submit + databus.publish
        """
        pool, _dev_adapter = _make_mixed_pool(_ir_spec(fps=20))
        await pool.open()
        try:
            session = _session(tmp_path)
            runner = NoOpRunner()
            cond = Conductor(pool=pool, session=session, runner=runner)
            cond.start()
            try:
                # Wait until both sides have produced output
                _wait_until(
                    lambda: (
                        len(session.writer_ref.frames) >= 2
                        and len(session.writer_ref.submitted) >= 2
                    ),
                    timeout=5.0,
                )
            finally:
                cond.stop(reason="test_done")
                cond.result_future.result(timeout=5.0)
                cond.join(timeout=2.0)
            # Camera side
            assert all(isinstance(f, FrameReceipt) for f in session.writer_ref.frames)
            assert all(
                ev["source"].startswith("camera:") for ev in session.writer_ref.camera_events
            )
            # Device side — the FakeAdapter emits DeviceSnapshot; none of
            # the camera types should have leaked through .submit
            for emission in session.writer_ref.submitted:
                # Defensive runtime check: DeviceEmission excludes both
                # camera types per mypy, but the assertion guards against
                # union widening regressions.
                assert not isinstance(emission, FrameReceipt)  # type: ignore[unreachable]
                assert not isinstance(emission, CameraEvent)  # type: ignore[unreachable]
        finally:
            await pool.close()
