"""Runtime smoke tests.

A small, fast set of behavioral checks that pin the current shape of the
runtime. Re-run these after runtime/config/device changes to catch
obvious regressions.

Coverage:

* Sim-config validation (with and without cameras) returns no problems.
* A canary that validation does not import ``capa.runtime.build``.
* WorkerPool open/close with sim adapters reaches OPEN and back to CLOSED
  with no errors and one open per adapter.
* Manual dispatch through a pool round-trips a CommandResult.
* CameraDeviceAdapter open/start/stop/close cycle against FlirIrSim.

The headless-run end-to-end gate already lives at
``tests/integration/test_headless_run.py::test_p0c_outcome_gate`` and is
re-exposed here via the ``smoke`` marker so ``pytest -m smoke`` includes it.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from capa.config import ConfigDocument, validate
from capa.devices.registry import _import_builtins
from capa.runtime.lifecycle import PoolState, WorkerState
from capa.runtime.pool import WorkerPool

pytestmark = pytest.mark.smoke


@pytest.fixture(scope="module", autouse=True)
def _ensure_builtins_loaded() -> None:
    _import_builtins()


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_validate_sim_freerun_clean(configs_dir: Path) -> None:
    """sim_freerun.yaml (no camera) validates with zero problems."""
    doc = ConfigDocument.load(configs_dir / "experiments" / "sim_freerun.yaml")
    problems = validate(doc)
    assert problems == [], [
        (p.severity, p.code, p.message) for p in problems if p.severity == "error"
    ]


def test_validate_sim_capa_pyrolysis_clean(configs_dir: Path) -> None:
    """sim_capa_pyrolysis.yaml (richer CAPA recipe) validates with zero problems."""
    doc = ConfigDocument.load(configs_dir / "experiments" / "sim_capa_pyrolysis.yaml")
    problems = validate(doc)
    assert problems == [], [
        (p.severity, p.code, p.message) for p in problems if p.severity == "error"
    ]


def test_validate_does_not_require_runtime_build(configs_dir: Path, tmp_path: Path) -> None:
    """``capa.config.validate`` does not depend on ``capa.runtime.build``.

    Runs the validation pipeline in a subprocess where ``capa.runtime.build``
        is forbidden from being imported. Materialization lives in
        :mod:`capa.devices.materialize`, keeping validation away from the
        worker-building layer.
    """
    config_path = configs_dir / "experiments" / "sim_freerun.yaml"
    script = textwrap.dedent(
        f"""
        import sys

        class _ForbidRuntimeBuild:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "capa.runtime.build" or fullname.startswith(
                    "capa.runtime.build."
                ):
                    raise ImportError(
                        f"capa.runtime.build is forbidden in this canary: {{fullname}}"
                    )
                return None

        sys.meta_path.insert(0, _ForbidRuntimeBuild())

        from capa.config import ConfigDocument, validate
        from capa.devices.registry import _import_builtins

        _import_builtins()
        doc = ConfigDocument.load(r"{config_path}")
        problems = validate(doc)
        errors = [p for p in problems if p.severity == "error"]
        if errors:
            sys.exit(f"validate returned errors: {{errors}}")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"validate failed under runtime-import-forbidden harness:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# WorkerPool open/close
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pool_open_close_with_sim_adapters() -> None:
    """Two sim adapters: pool reaches OPEN, every worker IDLE, then back to CLOSED."""
    from capa.runtime.runner import ThreadedRunner
    from capa.runtime.worker import Worker
    from tests.integration.runtime.fakes import make_fake_adapter

    a1 = make_fake_adapter("dev1", resource_id="sim:dev1")
    a2 = make_fake_adapter("dev2", resource_id="sim:dev2")
    workers = {
        a1.resource_id: Worker(
            resource_id=a1.resource_id,
            adapters=[a1],
            runner=ThreadedRunner(name=f"worker-{a1.resource_id}"),
        ),
        a2.resource_id: Worker(
            resource_id=a2.resource_id,
            adapters=[a2],
            runner=ThreadedRunner(name=f"worker-{a2.resource_id}"),
        ),
    }
    device_to_resource = {a1.name: a1.resource_id, a2.name: a2.resource_id}
    pool = WorkerPool(workers=workers, device_to_resource=device_to_resource)

    assert pool.state is PoolState.CLOSED
    await pool.open()
    try:
        assert pool.state is PoolState.OPEN
        for worker in pool.workers.values():
            assert worker.state is WorkerState.IDLE
        assert a1.open_calls == 1
        assert a2.open_calls == 1
    finally:
        result = await pool.close()
    assert pool.state is PoolState.CLOSED
    assert result.clean is True
    for wr in result.worker_results:
        assert wr.adapter_close_errors == ()


# ---------------------------------------------------------------------------
# Manual dispatch through a pool (no run armed)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_manual_dispatch_through_pool() -> None:
    """Open pool, arm a worker, dispatch one command, disarm, close.

    Mirrors the manual-control surface: a command issued via the worker's
    dispatch() future round-trips a CommandResult with ``accepted=True``.
    """
    from capa.runtime.runner import ThreadedRunner
    from capa.runtime.worker import Worker
    from tests.integration.runtime.fakes import (
        fake_command,
        make_fake_adapter,
        make_run_context,
    )

    adapter = make_fake_adapter("manual-dev", resource_id="sim:manual-dev")
    worker = Worker(
        resource_id=adapter.resource_id,
        adapters=[adapter],
        runner=ThreadedRunner(name=f"worker-{adapter.resource_id}"),
    )
    pool = WorkerPool(
        workers={adapter.resource_id: worker},
        device_to_resource={adapter.name: adapter.resource_id},
    )
    await pool.open()
    try:
        await worker.async_arm(make_run_context())
        future = worker.dispatch(adapter.name, fake_command())
        result = await asyncio.wrap_future(future)
        assert result.accepted is True
        await worker.async_disarm()
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# Camera adapter lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_camera_adapter_open_start_stop_close(tmp_path: Path) -> None:
    """CameraDeviceAdapter drives FlirIrSim through one full lifecycle.

    Validates that the four-method dance (open → start → stop → close)
    completes without raising, exercising the unified
    :class:`AdapterStartContext` lifecycle surface.
    """
    from capa.core.clock import RunClock
    from capa.devices.adapter import AdapterStartContext
    from capa.devices.camera.base import CameraSpec
    from capa.devices.sim.flir_ir_sim import FlirIrSim
    from capa.runtime.camera_adapter import make_camera_adapter

    spec = CameraSpec.model_validate(
        {"name": "ir_smoke", "adapter": "capa.devices.sim.flir_ir_sim", "kind": "ir"}
    )
    adapter = make_camera_adapter(camera_cls=FlirIrSim, spec=spec)
    ctx = AdapterStartContext(
        clock=RunClock.now(),
        run_id="smoke-run",
        bundle_root=tmp_path,
    )

    await adapter.open()
    try:
        await adapter.start(ctx)
        # Give the multiplexer a brief moment to spin up its producers.
        await asyncio.sleep(0.01)
        await adapter.stop()
    finally:
        await adapter.close()
