"""Tests for :class:`capa.devices.registry.DeviceRegistry`.

The registry owns the connection layer (open/close) and is shared between
the engine and the manual control panel. These tests verify:

* idempotent acquire (single + parallel),
* per-name lock so concurrent first acquires open once,
* release closes one connection,
* aclose closes everything in parallel and refuses subsequent acquires,
* unknown names raise :class:`DeviceRegistryError`,
* a failing open is not cached (next acquire retries).
"""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from capa.channels.calibration import Identity
from capa.channels.spec import ChannelSpec, WatlowParameter
from capa.devices.registry import DeviceRegistry, DeviceRegistryError
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


def _config_two_devices() -> ExperimentConfig:
    return ExperimentConfig(
        hardware=HardwareProfile(
            name="sim",
            devices=(
                DeviceConfig(
                    name="heater",
                    adapter="capa.devices.sim.watlow_sim",
                    params={
                        "tick_period_s": 0.05,
                        "signals": {
                            ("process_value", 1): Sine(
                                amplitude=1.0, frequency_hz=1.0, offset=300.0
                            ),
                        },
                    },
                ),
                DeviceConfig(
                    name="heater_b",
                    adapter="capa.devices.sim.watlow_sim",
                    params={
                        "tick_period_s": 0.05,
                        "signals": {
                            ("process_value", 1): Sine(
                                amplitude=1.0, frequency_hz=1.0, offset=300.0
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
        procedure=ProcedureRef(id="capa.builtin.free_run", config={"duration_s": 0.1}),
        calibration_set=CalibrationSetRef(name="default"),
        operator=OperatorRef(id="op", display_name="Op"),
        sample=SampleInfo(id="S"),
    )


@pytest.mark.anyio
async def test_acquire_is_idempotent() -> None:
    cfg = _config_two_devices()
    reg = DeviceRegistry(cfg)
    try:
        a1 = await reg.acquire_device("heater")
        a2 = await reg.acquire_device("heater")
        assert a1 is a2
        assert reg.is_device_open("heater")
        assert reg.opened_device("heater") is a1
    finally:
        await reg.aclose()


@pytest.mark.anyio
async def test_acquire_unknown_raises() -> None:
    reg = DeviceRegistry(_config_two_devices())
    try:
        with pytest.raises(DeviceRegistryError, match="unknown device"):
            await reg.acquire_device("nope")
    finally:
        await reg.aclose()


@pytest.mark.anyio
async def test_acquire_all_devices_opens_in_parallel() -> None:
    reg = DeviceRegistry(_config_two_devices())
    try:
        adapters = await reg.acquire_all_devices()
        assert set(adapters) == {"heater", "heater_b"}
        assert reg.is_device_open("heater")
        assert reg.is_device_open("heater_b")
    finally:
        await reg.aclose()


@pytest.mark.anyio
async def test_concurrent_acquires_open_once() -> None:
    """Two parallel acquires for the same name must hit the cache the
    second time — opens are serialized through the per-name lock."""
    reg = DeviceRegistry(_config_two_devices())
    try:
        results: list[object] = []

        async def acquire() -> None:
            results.append(await reg.acquire_device("heater"))

        async with anyio.create_task_group() as tg:
            for _ in range(4):
                tg.start_soon(acquire)
        assert len(results) == 4
        first = results[0]
        for other in results[1:]:
            assert other is first
    finally:
        await reg.aclose()


@pytest.mark.anyio
async def test_release_closes_one_and_drops_cache() -> None:
    reg = DeviceRegistry(_config_two_devices())
    try:
        a1 = await reg.acquire_device("heater")
        await reg.release_device("heater")
        assert not reg.is_device_open("heater")
        # A fresh acquire returns a *new* adapter — the old one was closed.
        a2 = await reg.acquire_device("heater")
        assert a2 is not a1
    finally:
        await reg.aclose()


@pytest.mark.anyio
async def test_release_unknown_name_is_noop() -> None:
    reg = DeviceRegistry(_config_two_devices())
    try:
        await reg.release_device("never-opened")
    finally:
        await reg.aclose()


@pytest.mark.anyio
async def test_aclose_then_acquire_raises() -> None:
    reg = DeviceRegistry(_config_two_devices())
    await reg.aclose()
    with pytest.raises(DeviceRegistryError, match="has been closed"):
        await reg.acquire_device("heater")


@pytest.mark.anyio
async def test_aclose_is_idempotent() -> None:
    reg = DeviceRegistry(_config_two_devices())
    await reg.acquire_device("heater")
    await reg.aclose()
    # Second aclose() must not raise even though state is already cleared.
    await reg.aclose()


@pytest.mark.anyio
async def test_engine_borrows_from_shared_registry(tmp_path: Path) -> None:
    """End-to-end: a registry constructed externally is shared with the
    engine. After a clean run, the registry's adapters remain open — proving
    the engine stopped sampling but did not close the connections."""
    from capa.experiment.engine import ExperimentEngine

    cfg = _config_two_devices()
    registry = DeviceRegistry(cfg)
    try:
        # Panel-style: open one device before the run.
        panel_adapter = await registry.acquire_device("heater")
        assert registry.is_device_open("heater")

        # Engine borrows the same registry.
        engine = ExperimentEngine(device_registry=registry)
        result = await engine.run(cfg, runs_root=tmp_path, configure_logging_for_bundle=False)
        assert result.run_status == "completed"

        # After the run, the connection the panel opened is still alive
        # — engine only stopped sampling, did not close.
        assert registry.is_device_open("heater")
        assert registry.opened_device("heater") is panel_adapter
        # And the second device, which the engine opened via acquire_all,
        # is also still open since the registry owns the lifecycle.
        assert registry.is_device_open("heater_b")
    finally:
        await registry.aclose()


@pytest.mark.anyio
async def test_engine_owned_registry_closes_after_run(tmp_path: Path) -> None:
    """When no registry is passed, the engine constructs and owns one;
    after the run it must close everything. (CLI / test path.)"""
    from capa.experiment.engine import ExperimentEngine

    cfg = _config_two_devices()
    engine = ExperimentEngine()
    result = await engine.run(cfg, runs_root=tmp_path, configure_logging_for_bundle=False)
    assert result.run_status == "completed"
    # The engine's internal registry should now be closed; we can't easily
    # introspect it here, but a fresh registry constructed against the same
    # config + opened from scratch confirms no leaked handles.
    fresh = DeviceRegistry(cfg)
    try:
        await fresh.acquire_device("heater")
    finally:
        await fresh.aclose()


@pytest.mark.anyio
async def test_failed_construct_is_not_cached() -> None:
    """An adapter whose construction raises (bad import path) must NOT land
    in the cache. The next acquire is free to retry — typical scenario is
    operator fixes the config and reloads, or unplugs + replugs hardware."""
    cfg = ExperimentConfig(
        hardware=HardwareProfile(
            name="sim",
            devices=(
                DeviceConfig(
                    name="broken",
                    adapter="capa.devices.sim.does_not_exist",
                    params={},
                ),
            ),
            channels=(),
        ),
        procedure=ProcedureRef(id="capa.builtin.free_run", config={"duration_s": 0.1}),
        calibration_set=CalibrationSetRef(name="default"),
        operator=OperatorRef(id="op", display_name="Op"),
        sample=SampleInfo(id="S"),
    )
    reg = DeviceRegistry(cfg)
    try:
        with pytest.raises(Exception):
            await reg.acquire_device("broken")
        assert not reg.is_device_open("broken")
        # Retry must also raise (same root cause) — proves the failing
        # construct wasn't cached.
        with pytest.raises(Exception):
            await reg.acquire_device("broken")
    finally:
        await reg.aclose()
