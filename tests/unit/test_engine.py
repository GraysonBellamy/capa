"""Tests for :class:`capa.experiment.engine.ExperimentEngine`.

Drives the engine against simulated adapters end-to-end inside a temp dir.
Asserts:

* clean ``completed`` + ``sealed`` outcome,
* ``aborted`` when ``external_stop`` fires,
* ``crashed`` recovery when an adapter raises during streaming,
* manifest fields propagated to bundle + catalog.
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
from capa.experiment.engine import (
    ENGINE_VERSION,
    EngineResult,
    EngineState,
    ExperimentEngine,
    make_run_id,
)
from capa.storage.catalog import RunCatalog
from capa.storage.manifest import BundleManifest


def _make_config(
    *,
    sample_id: str = "S-1",
    duration_s: float | None = 0.15,
    tick_period_s: float = 0.02,
    procedure_id: str = "capa.builtin.free_run",
) -> ExperimentConfig:
    return ExperimentConfig(
        hardware=HardwareProfile(
            name="sim",
            devices=(
                DeviceConfig(
                    name="heater",
                    adapter="capa.devices.sim.watlow_sim",
                    params={
                        "tick_period_s": tick_period_s,
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
                ),
            ),
        ),
        procedure=ProcedureRef(
            id=procedure_id,
            config={"duration_s": duration_s} if duration_s is not None else {},
        ),
        calibration_set=CalibrationSetRef(name="default"),
        operator=OperatorRef(id="abr", display_name="A. Researcher"),
        sample=SampleInfo(id=sample_id),
    )


def test_make_run_id_slugs_unsafe_chars() -> None:
    rid = make_run_id(sample_id="SAMPLE 001/x")
    assert "/" not in rid
    assert " " not in rid
    assert rid.endswith("_SAMPLE-001-x")


@pytest.mark.anyio
async def test_engine_completed_run_seals_bundle(tmp_path: Path) -> None:
    config = _make_config(sample_id="ENGINE-1", duration_s=0.1)
    engine = ExperimentEngine()
    result = await engine.run(
        config,
        runs_root=tmp_path,
        configure_logging_for_bundle=False,
    )
    assert result.run_status == "completed"
    assert result.bundle_status == "sealed"
    assert result.integrity_status == "ok"
    assert result.exit_code() == 0
    assert result.bundle_path is not None and result.bundle_path.is_dir()

    manifest = BundleManifest.read(result.bundle_path / "manifest.json")
    assert manifest.run_status == "completed"
    assert manifest.bundle_status == "sealed"
    assert manifest.capa.engine_version == ENGINE_VERSION
    assert manifest.queue_health, "expected queue_health populated by engine"


@pytest.mark.anyio
async def test_engine_external_stop_aborts(tmp_path: Path) -> None:
    config = _make_config(sample_id="ABORT", duration_s=None)
    stop = anyio.Event()
    engine = ExperimentEngine()

    async def fire_stop() -> None:
        await anyio.sleep(0.05)
        stop.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(fire_stop)
        result_holder: list[EngineResult] = []

        async def run_engine() -> None:
            r = await engine.run(
                config,
                runs_root=tmp_path,
                external_stop=stop,
                configure_logging_for_bundle=False,
            )
            result_holder.append(r)

        tg.start_soon(run_engine)

    result = result_holder[0]
    assert result.run_status == "aborted"
    assert result.bundle_status == "sealed"
    assert result.exit_code() == 1


@pytest.mark.anyio
async def test_engine_writes_catalog_row(tmp_path: Path) -> None:
    config = _make_config(sample_id="CAT-1", duration_s=0.1)
    with RunCatalog(tmp_path) as cat:
        engine = ExperimentEngine()
        result = await engine.run(
            config,
            runs_root=tmp_path,
            catalog=cat,
            configure_logging_for_bundle=False,
        )
        row = cat.get(result.run_id)
        assert row is not None
        assert row.run_status == "completed"
        assert row.bundle_status == "sealed"
        assert row.integrity_status == "ok"
        assert row.operator_id == "abr"


@pytest.mark.anyio
async def test_engine_unknown_procedure_id_is_preflight_refusal(tmp_path: Path) -> None:
    config = _make_config(procedure_id="capa.builtin.does_not_exist")
    engine = ExperimentEngine()
    result = await engine.run(
        config,
        runs_root=tmp_path,
        configure_logging_for_bundle=False,
    )
    assert result.run_status == "aborted"
    assert result.bundle_path is None
    assert "not in the trusted registry" in (result.exit_reason or "")


@pytest.mark.anyio
async def test_engine_crashed_when_adapter_raises_during_stream(tmp_path: Path) -> None:
    """A producer that raises mid-stream should still produce a sealed
    bundle marked ``run_status="crashed"`` (plan §13.3)."""
    config = _make_config(sample_id="CRASH-1", duration_s=1.0)
    engine = ExperimentEngine()

    async def crash_after_first_tick() -> None:
        # Patch the engine's adapter list after construction so the test
        # adapter raises after one tick. We do this via a wrapper that
        # mutates the constructed adapters list once they exist.
        pass

    # Easier path: instantiate the engine ourselves, monkey-patch the first
    # adapter's stream() to raise. We use the public surface — engine.run —
    # but before running, we override _construct_adapters via a subclass.

    class _CrashingEngine(ExperimentEngine):
        async def _producer_task(self, adapter, queue, metrics, producers_alive):  # type: ignore[override]
            raise RuntimeError("synthetic adapter crash")

    crashing = _CrashingEngine()
    result = await crashing.run(
        config,
        runs_root=tmp_path,
        configure_logging_for_bundle=False,
    )
    assert result.run_status == "crashed"
    # The bundle still seals.
    assert result.bundle_status in ("sealed", "verification_failed")
    assert result.exit_code() in (2, 3)


# ---------------------------------------------------------------------------
# UI-facing seam: EngineState transitions, request_abort, eager DataBus.
# Plan §10 / P1 — the UI subscribes before run() and observes lifecycle live.
# ---------------------------------------------------------------------------


def test_engine_idle_at_construction() -> None:
    engine = ExperimentEngine()
    assert engine.state is EngineState.IDLE
    # Eager-construction surface: UI can grab these immediately.
    assert engine.databus is not None
    assert engine.metrics is not None
    assert engine.external_stop is not None


@pytest.mark.anyio
async def test_engine_state_transitions_during_completed_run(tmp_path: Path) -> None:
    """Plan §10.1 Run-tab states: PREPARING → RUNNING → FINALIZING → SEALED."""
    states: list[EngineState] = []
    engine = ExperimentEngine(on_state_changed=states.append)
    config = _make_config(sample_id="STATE-1", duration_s=0.05)
    result = await engine.run(config, runs_root=tmp_path, configure_logging_for_bundle=False)
    assert result.run_status == "completed"
    assert states[0] is EngineState.PREPARING
    assert EngineState.RUNNING in states
    assert EngineState.FINALIZING in states
    assert states[-1] is EngineState.SEALED
    # No ABORTING transition for a clean run.
    assert EngineState.ABORTING not in states
    assert engine.state is EngineState.SEALED


@pytest.mark.anyio
async def test_engine_request_abort_transitions_to_aborting(tmp_path: Path) -> None:
    states: list[EngineState] = []
    engine = ExperimentEngine(on_state_changed=states.append)
    config = _make_config(sample_id="ABORT-STATE", duration_s=None)

    async def fire_abort() -> None:
        # Wait until engine is RUNNING before aborting so we exercise the
        # RUNNING→ABORTING edge.
        for _ in range(200):
            if engine.state is EngineState.RUNNING:
                break
            await anyio.sleep(0.005)
        engine.request_abort(mode="immediate")

    async with anyio.create_task_group() as tg:
        tg.start_soon(fire_abort)
        result_holder: list[EngineResult] = []

        async def run_engine() -> None:
            r = await engine.run(
                config,
                runs_root=tmp_path,
                configure_logging_for_bundle=False,
            )
            result_holder.append(r)

        tg.start_soon(run_engine)

    result = result_holder[0]
    assert result.run_status == "aborted"
    assert engine.abort_mode == "immediate"
    assert EngineState.ABORTING in states
    assert states[-1] is EngineState.SEALED


def test_engine_request_abort_idempotent_first_mode_wins() -> None:
    engine = ExperimentEngine()
    engine.request_abort(mode="safe_shutdown")
    engine.request_abort(mode="immediate")
    assert engine.abort_mode == "safe_shutdown"
    assert engine.external_stop.is_set()


def test_engine_set_state_callback_swap_at_runtime() -> None:
    engine = ExperimentEngine()
    received: list[EngineState] = []
    engine.set_state_callback(received.append)
    engine._set_state(EngineState.PREPARING)
    assert received == [EngineState.PREPARING]
    engine.set_state_callback(None)
    engine._set_state(EngineState.RUNNING)
    assert received == [EngineState.PREPARING]


def test_engine_state_callback_failure_does_not_crash_engine() -> None:
    """A misbehaving UI callback must not propagate into the engine task."""

    def explode(_: EngineState) -> None:
        raise RuntimeError("ui bug")

    engine = ExperimentEngine(on_state_changed=explode)
    # Should not raise.
    engine._set_state(EngineState.PREPARING)
    assert engine.state is EngineState.PREPARING


@pytest.mark.anyio
async def test_engine_databus_subscription_before_run_observes_samples(tmp_path: Path) -> None:
    """The UI must be able to subscribe before run() and see emissions."""
    config = _make_config(sample_id="SUB-1", duration_s=0.1)
    engine = ExperimentEngine()
    sub = engine.databus.subscribe_all("test-pre-run")

    received: list[object] = []

    async def consume() -> None:
        # databus.close() at finalize ends the subscription queue, which
        # raises RuntimeError out of get(). The UI consumer expects this and
        # treats it as the natural end-of-run signal.
        try:
            async for emission in sub:
                received.append(emission)
        except RuntimeError:
            pass

    async with anyio.create_task_group() as tg:
        tg.start_soon(consume)
        await engine.run(config, runs_root=tmp_path, configure_logging_for_bundle=False)

    assert received, "expected at least one emission to reach the pre-run subscriber"
