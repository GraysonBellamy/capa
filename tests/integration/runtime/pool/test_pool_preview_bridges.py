""":class:`WorkerPool` preview-bridge integration.

Verifies the pool-level bridge construction and lifetime guarantees:

* ``from_config(preview_consumer_loop=None)`` → empty bridge map (headless).
* ``from_config(preview_consumer_loop=loop)`` → one bridge per camera.
* ``attach_preview_consumers`` must run on the consumer loop and is
  idempotent.
* ``pool.close()`` closes every bridge so UI-side drainers wake on
  ``ThreadBridgeClosedError`` and exit cleanly.
* Multiple arm/disarm cycles do not invalidate the bridges (load-bearing
  for the "open hardware once" acceptance criterion).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from capa.core.clock import RunClock
from capa.devices.camera.base import CameraSpec
from capa.experiment.config import (
    CalibrationSetRef,
    ExperimentConfig,
    HardwareProfile,
    OperatorRef,
    ProcedureRef,
    SampleInfo,
)
from capa.runtime.metrics import DisarmResult
from capa.runtime.pool import WorkerPool
from capa.runtime.runcontext import RunContext
from tests.integration.runtime.fakes import FakeWriterRef


class _PathBundleRef:
    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> object:
        return self._root


def _ctx_with_path(run_id: str, bundle_root: Path) -> RunContext:
    bundle_root.mkdir(parents=True, exist_ok=True)
    return RunContext(
        run_id=run_id,
        clock=RunClock.now(),
        writer=FakeWriterRef(),
        bundle=_PathBundleRef(bundle_root),
    )


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _ir_cam_spec(name: str = "ir_cam0") -> CameraSpec:
    return CameraSpec.model_validate(
        {
            "name": name,
            "adapter": "capa.devices.sim.flir_ir_sim",
            "kind": "ir",
            "params": {"fps": 20},
        }
    )


def _make_config(cameras: tuple[CameraSpec, ...]) -> ExperimentConfig:
    return ExperimentConfig(
        hardware=HardwareProfile(
            name="preview-test",
            devices=(),
            channels=(),
            cameras=cameras,
        ),
        procedure=ProcedureRef(id="capa.builtin.free_run", config={"duration_s": 0.1}),
        calibration_set=CalibrationSetRef(name="default"),
        operator=OperatorRef(id="opA", display_name="Op A"),
        sample=SampleInfo(id="S"),
    )


class TestPoolBridgeConstruction:
    async def test_no_consumer_loop_skips_preview_machinery(self) -> None:
        """Headless path: ``preview_consumer_loop=None`` → empty map.

        Camera adapters' ``start_preview_channel`` then sees no bridge
        passed in and no-ops. Workers iterate an empty preview_bridges
        dict and do nothing extra.
        """
        cfg = _make_config((_ir_cam_spec(),))
        pool = WorkerPool.from_config(cfg, preview_consumer_loop=None)
        assert dict(pool.preview_bridges()) == {}

    async def test_from_config_constructs_one_bridge_per_camera(self) -> None:
        cfg = _make_config((_ir_cam_spec("a"), _ir_cam_spec("b")))
        loop = asyncio.get_running_loop()
        pool = WorkerPool.from_config(cfg, preview_consumer_loop=loop)
        bridges = dict(pool.preview_bridges())
        assert set(bridges) == {"a", "b"}
        # Each bridge has the right name + DROP_OLDEST policy.
        from capa.runtime.bridge import BridgePolicy

        for name, bridge in bridges.items():
            assert bridge.name == f"preview-{name}"
            assert bridge.policy is BridgePolicy.DROP_OLDEST

    async def test_attach_preview_consumers_is_idempotent(self) -> None:
        cfg = _make_config((_ir_cam_spec(),))
        loop = asyncio.get_running_loop()
        pool = WorkerPool.from_config(cfg, preview_consumer_loop=loop)
        pool.attach_preview_consumers()
        # Second call is a no-op (a non-latched second attach on the
        # same bridge would raise).
        pool.attach_preview_consumers()


class TestPoolBridgeLifetime:
    async def test_pool_close_closes_all_preview_bridges(self) -> None:
        cfg = _make_config((_ir_cam_spec(),))
        loop = asyncio.get_running_loop()
        pool = WorkerPool.from_config(cfg, preview_consumer_loop=loop)
        pool.attach_preview_consumers()
        await pool.open()
        try:
            assert pool.workers  # at least one worker built for the camera
        finally:
            await pool.close()
        # All bridges are now closed: get() returns None on closed-empty.
        for bridge in pool.preview_bridges().values():
            assert bridge.closed is True

    async def test_pool_preview_bridges_survive_multiple_runs(self, tmp_path: Path) -> None:
        """Load-bearing for §11 acceptance criterion 5/6: pool stays open
        across many runs, and preview must too. Multiple arm/disarm
        cycles must NOT invalidate the bridges.
        """
        cfg = _make_config((_ir_cam_spec(),))
        loop = asyncio.get_running_loop()
        pool = WorkerPool.from_config(cfg, preview_consumer_loop=loop)
        pool.attach_preview_consumers()
        await pool.open()
        try:
            bridges_before = dict(pool.preview_bridges())
            for run_id in ("r1", "r2"):
                ctx = _ctx_with_path(run_id, tmp_path / run_id)
                await pool.arm_all(ctx)
                outbound = await pool.begin_sampling_all(consumer_loop=asyncio.get_running_loop())
                # Drain a couple of frames so the recording pump actually
                # produces output.
                for bridge in outbound.values():
                    for _ in range(2):
                        await asyncio.wait_for(bridge.get(), timeout=3.0)
                results = await pool.disarm_all(grace_s=3.0)
                assert all(r is DisarmResult.OK for r in results.values())
            # Bridge instances are identical across runs — the pool did
            # not rebuild them.
            assert dict(pool.preview_bridges()) == bridges_before
            assert all(not b.closed for b in bridges_before.values())
        finally:
            await pool.close()
