"""Unit tests for :mod:`capa.runtime.build` resource validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from capa.devices.adapter import DeviceAdapter, FailurePolicy
from capa.devices.materialize import (
    _check_daqmx_channel_uniqueness,
    _check_webcam_uniqueness,
)
from capa.devices.resolved import ResolvedAdapter


@dataclass
class _AdapterStub:
    """Minimal adapter-like stub used to drive resource validation.

    Carries only the attributes the resource checks read off the
    underlying :class:`DeviceAdapter` — :attr:`physical_channels` for
    DAQmx uniqueness. The :class:`ResolvedAdapter` wrapping it carries
    the ``name`` / ``resource_id`` the checks read off the resolved
    metadata.
    """

    physical_channels: tuple[str, ...] = field(default_factory=tuple)


def _Stub(  # noqa: N802 — keep legacy fixture name
    *,
    name: str,
    resource_id: str,
    physical_channels: tuple[str, ...] = (),
) -> ResolvedAdapter:
    """Build a :class:`ResolvedAdapter` around an adapter stub.

    Wraps the previous ``_Stub`` dataclass shape so the existing test
    bodies keep reading naturally.
    """
    adapter_stub = _AdapterStub(physical_channels=physical_channels)
    return ResolvedAdapter(
        name=name,
        adapter=cast(DeviceAdapter, adapter_stub),
        resource_id=resource_id,
        on_failure=FailurePolicy.ABORT,
        expected_rate_hz=None,
    )


class TestDaqmxChannelUniqueness:
    def test_disjoint_channels_pass(self) -> None:
        a = _Stub(
            name="a",
            resource_id="daqmx:chassis:cDAQ1",
            physical_channels=("cDAQ1Mod1/ai0", "cDAQ1Mod1/ai1"),
        )
        b = _Stub(
            name="b",
            resource_id="daqmx:chassis:cDAQ1",
            physical_channels=("cDAQ1Mod1/ai2",),
        )

        assert _check_daqmx_channel_uniqueness([a, b]) == []

    def test_same_channel_twice_problem(self) -> None:
        a = _Stub(
            name="a",
            resource_id="daqmx:chassis:cDAQ1",
            physical_channels=("cDAQ1Mod1/ai0",),
        )
        b = _Stub(
            name="b",
            resource_id="daqmx:chassis:cDAQ1",
            physical_channels=("cDAQ1Mod1/ai0",),
        )

        problems = _check_daqmx_channel_uniqueness([a, b])

        assert len(problems) == 1
        assert "cDAQ1Mod1/ai0" in problems[0].message
        assert problems[0].severity == "error"
        assert problems[0].code == "devices.daqmx_channel_conflict"

    def test_conflict_carries_both_names(self) -> None:
        a = _Stub(name="a", resource_id="daqmx:chassis:cDAQ1", physical_channels=("ch1",))
        b = _Stub(name="b", resource_id="daqmx:chassis:cDAQ1", physical_channels=("ch1",))

        problems = _check_daqmx_channel_uniqueness([a, b])

        assert len(problems) == 1
        assert set(problems[0].path[1:3]) == {"a", "b"}
        assert problems[0].path[3] == "ch1"

    def test_non_daqmx_adapter_skipped(self) -> None:
        a = _Stub(
            name="serial_thing",
            resource_id="serial:COM6",
            physical_channels=("looks-like-a-channel",),
        )
        b = _Stub(
            name="daqmx_thing",
            resource_id="daqmx:chassis:cDAQ1",
            physical_channels=("looks-like-a-channel",),
        )

        assert _check_daqmx_channel_uniqueness([a, b]) == []

    def test_missing_physical_channels_attribute(self) -> None:
        @dataclass
        class _NoChannels:
            pass

        adapter_stub = _NoChannels()
        a = ResolvedAdapter(
            name="sim_daq",
            adapter=cast(DeviceAdapter, adapter_stub),
            resource_id="daqmx:chassis:cDAQ1",
            on_failure=FailurePolicy.ABORT,
            expected_rate_hz=None,
        )

        assert _check_daqmx_channel_uniqueness([a]) == []


class TestWebcamUniqueness:
    def test_disjoint_webcams_pass(self) -> None:
        a = _Stub(name="cam0", resource_id="webcam:0")
        b = _Stub(name="cam1", resource_id="webcam:1")

        assert _check_webcam_uniqueness([a, b]) == []

    def test_same_webcam_twice_problem(self) -> None:
        a = _Stub(name="cam_a", resource_id="webcam:0")
        b = _Stub(name="cam_b", resource_id="webcam:0")

        problems = _check_webcam_uniqueness([a, b])

        assert len(problems) == 1
        assert "webcam:0" in problems[0].message
        assert problems[0].severity == "error"
        assert problems[0].code == "cameras.webcam_handle_conflict"

    def test_conflict_carries_both_names(self) -> None:
        a = _Stub(name="left", resource_id="webcam:0")
        b = _Stub(name="right", resource_id="webcam:0")

        problems = _check_webcam_uniqueness([a, b])

        assert len(problems) == 1
        assert set(problems[0].path[1:3]) == {"left", "right"}


class TestSchemeIsolation:
    def test_full_config_no_conflicts(self) -> None:
        adapters = [
            _Stub(name="heater", resource_id="serial:COM6"),
            _Stub(name="purge_mfc", resource_id="serial:COM7"),
            _Stub(name="balance", resource_id="serial:COM4"),
            _Stub(
                name="cdaq1",
                resource_id="daqmx:chassis:cDAQ1",
                physical_channels=("cDAQ1Mod1/ai0", "cDAQ1Mod1/ai1"),
            ),
            _Stub(name="visible_cam0", resource_id="webcam:0"),
            _Stub(name="ir_cam0", resource_id="webcam:1"),
        ]

        assert _check_daqmx_channel_uniqueness(adapters) == []
        assert _check_webcam_uniqueness(adapters) == []


class TestOutboundCapacityDerivation:
    """:func:`_outbound_capacity_for` derives the per-worker bridge
    capacity from declared rates. Phase 2 replaces the hardcoded ``64``
    floor with the documented ``max(64, ceil(8 * rate))`` formula."""

    def test_no_rate_falls_back_to_floor(self) -> None:
        from capa.runtime.build import _BRIDGE_MIN_CAPACITY, _outbound_capacity_for

        assert _outbound_capacity_for([None]) == _BRIDGE_MIN_CAPACITY
        assert _outbound_capacity_for([None, None]) == _BRIDGE_MIN_CAPACITY
        assert _outbound_capacity_for([]) == _BRIDGE_MIN_CAPACITY

    def test_low_rate_stays_at_floor(self) -> None:
        # 5 Hz × 8 = 40 → below the 64 floor.
        from capa.runtime.build import _BRIDGE_MIN_CAPACITY, _outbound_capacity_for

        assert _outbound_capacity_for([5.0]) == _BRIDGE_MIN_CAPACITY

    def test_high_rate_scales(self) -> None:
        # 50 Hz × 8 = 400.
        from capa.runtime.build import _outbound_capacity_for

        assert _outbound_capacity_for([50.0]) == 400

    def test_sums_rates_across_adapters(self) -> None:
        # Two adapters on one worker share its outbound bridge — sizing
        # is total emissions/sec.
        from capa.runtime.build import _outbound_capacity_for

        assert _outbound_capacity_for([10.0, 30.0]) == 320  # ceil(8 * 40)

    def test_mixed_known_and_none(self) -> None:
        from capa.runtime.build import _outbound_capacity_for

        # The None contributes nothing; sizing reflects only the
        # rate-declaring adapter.
        assert _outbound_capacity_for([None, 20.0]) == 160


class TestResolveDeviceAdapters:
    """Resolution rules: explicit DeviceConfig.resource_id wins, on_failure
    flows through, expected_rate_hz is captured."""

    def _build_config(
        self,
        *,
        resource_id_override: str | None = None,
        on_failure: FailurePolicy = FailurePolicy.ABORT,
    ) -> ExperimentConfig:  # noqa: F821 — local import below
        from capa.channels.calibration import Identity
        from capa.channels.spec import (
            ChannelKind,
            ChannelSpec,
            WatlowParameter,
        )
        from capa.experiment.config import (
            CalibrationSetRef,
            DeviceConfig,
            ExperimentConfig,
            HardwareProfile,
            OperatorRef,
            ProcedureRef,
            SampleInfo,
        )

        return ExperimentConfig(
            hardware=HardwareProfile(
                name="t",
                devices=(
                    DeviceConfig(
                        name="heater",
                        adapter="capa.devices.sim.watlow_sim",
                        resource_id=resource_id_override,
                        on_failure=on_failure,
                        params={
                            "signals": {
                                "process_value/1": {"kind": "constant", "value": 25.0},
                                "setpoint/1": {"kind": "constant", "value": 25.0},
                            },
                        },
                    ),
                ),
                channels=(
                    ChannelSpec(
                        name="heater.pv",
                        kind=ChannelKind.PROCESS_VAR,
                        source=WatlowParameter(
                            device="heater", parameter="process_value", instance=1
                        ),
                        unit="degC",
                        derived_unit="degC",
                        calibration=Identity(input_unit="degC", output_unit="degC"),
                    ),
                ),
            ),
            procedure=ProcedureRef(id="capa.builtin.recipe_runner"),
            calibration_set=CalibrationSetRef(name="default"),
            operator=OperatorRef(id="op"),
            sample=SampleInfo(id="S1"),
        )

    def test_resource_id_override_wins(self) -> None:
        from capa.devices.materialize import _materialize_devices

        config = self._build_config(resource_id_override="serial:CUSTOM")
        resolved, problems = _materialize_devices(config)
        assert problems == []
        assert len(resolved) == 1
        assert resolved[0].resource_id == "serial:CUSTOM"

    def test_default_uses_adapter_resource_id(self) -> None:
        from capa.devices.materialize import _materialize_devices

        config = self._build_config(resource_id_override=None)
        resolved, problems = _materialize_devices(config)
        assert problems == []
        assert resolved[0].resource_id == resolved[0].adapter.resource_id

    def test_on_failure_propagates(self) -> None:
        from capa.devices.materialize import _materialize_devices

        config = self._build_config(on_failure=FailurePolicy.WARN)
        resolved, problems = _materialize_devices(config)
        assert problems == []
        assert resolved[0].on_failure is FailurePolicy.WARN

    def test_expected_rate_captured(self) -> None:
        from capa.devices.materialize import _materialize_devices

        config = self._build_config()
        resolved, problems = _materialize_devices(config)
        assert problems == []
        # The sim adapter declares a positive emission rate (one
        # SourceRecord plus per-bound-channel samples per tick).
        assert resolved[0].expected_rate_hz is not None
        assert resolved[0].expected_rate_hz > 0.0


class TestBuildWorkersOnFailurePropagation:
    """End-to-end: an explicit DeviceConfig.on_failure must land on the
    constructed Worker's WorkerMetrics.on_failure map."""

    def test_worker_metrics_records_on_failure(self) -> None:
        from capa.channels.calibration import Identity
        from capa.channels.spec import (
            ChannelKind,
            ChannelSpec,
            WatlowParameter,
        )
        from capa.devices.materialize import materialize_adapters
        from capa.experiment.config import (
            CalibrationSetRef,
            DeviceConfig,
            ExperimentConfig,
            HardwareProfile,
            OperatorRef,
            ProcedureRef,
            SampleInfo,
        )
        from capa.runtime.build import build_workers
        from capa.runtime.runner import InlineRunner

        config = ExperimentConfig(
            hardware=HardwareProfile(
                name="t",
                devices=(
                    DeviceConfig(
                        name="heater",
                        adapter="capa.devices.sim.watlow_sim",
                        on_failure=FailurePolicy.WARN,
                        params={
                            "signals": {
                                "process_value/1": {
                                    "kind": "constant",
                                    "value": 25.0,
                                },
                                "setpoint/1": {"kind": "constant", "value": 25.0},
                            },
                        },
                    ),
                ),
                channels=(
                    ChannelSpec(
                        name="heater.pv",
                        kind=ChannelKind.PROCESS_VAR,
                        source=WatlowParameter(
                            device="heater",
                            parameter="process_value",
                            instance=1,
                        ),
                        unit="degC",
                        derived_unit="degC",
                        calibration=Identity(input_unit="degC", output_unit="degC"),
                    ),
                ),
            ),
            procedure=ProcedureRef(id="capa.builtin.recipe_runner"),
            calibration_set=CalibrationSetRef(name="default"),
            operator=OperatorRef(id="op"),
            sample=SampleInfo(id="S1"),
        )

        workers, device_to_resource = build_workers(
            materialize_adapters(config), runner_factory=InlineRunner
        )
        rid = device_to_resource["heater"]
        worker = workers[rid]
        assert worker.metrics.on_failure["heater"] is FailurePolicy.WARN

    def test_default_on_failure_is_abort(self) -> None:
        from capa.channels.calibration import Identity
        from capa.channels.spec import (
            ChannelKind,
            ChannelSpec,
            WatlowParameter,
        )
        from capa.devices.materialize import materialize_adapters
        from capa.experiment.config import (
            CalibrationSetRef,
            DeviceConfig,
            ExperimentConfig,
            HardwareProfile,
            OperatorRef,
            ProcedureRef,
            SampleInfo,
        )
        from capa.runtime.build import build_workers
        from capa.runtime.runner import InlineRunner

        config = ExperimentConfig(
            hardware=HardwareProfile(
                name="t",
                devices=(
                    DeviceConfig(
                        name="heater",
                        adapter="capa.devices.sim.watlow_sim",
                        params={
                            "signals": {
                                "process_value/1": {
                                    "kind": "constant",
                                    "value": 25.0,
                                },
                                "setpoint/1": {"kind": "constant", "value": 25.0},
                            },
                        },
                    ),
                ),
                channels=(
                    ChannelSpec(
                        name="heater.pv",
                        kind=ChannelKind.PROCESS_VAR,
                        source=WatlowParameter(
                            device="heater",
                            parameter="process_value",
                            instance=1,
                        ),
                        unit="degC",
                        derived_unit="degC",
                        calibration=Identity(input_unit="degC", output_unit="degC"),
                    ),
                ),
            ),
            procedure=ProcedureRef(id="capa.builtin.recipe_runner"),
            calibration_set=CalibrationSetRef(name="default"),
            operator=OperatorRef(id="op"),
            sample=SampleInfo(id="S1"),
        )

        workers, device_to_resource = build_workers(
            materialize_adapters(config), runner_factory=InlineRunner
        )
        rid = device_to_resource["heater"]
        assert workers[rid].metrics.on_failure["heater"] is FailurePolicy.ABORT
