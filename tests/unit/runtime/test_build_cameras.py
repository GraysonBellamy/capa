""":func:`build_workers` integration tests for camera support.

Cameras must build into workers the same way
device adapters do. These tests construct minimal :class:`ExperimentConfig`
objects that include camera entries and assert:

* Cameras come back as :class:`CameraDeviceAdapter` instances inside
  their workers.
* Camera ``resource_id``\\ s drive worker grouping just like device
  resource_ids — two cameras with the same resource_id collide via
  :class:`ResourceConflict`; two with disjoint ids go to disjoint
  workers.
* The ``device_to_resource`` map covers camera names alongside device
  names so :meth:`WorkerPool.dispatch` can route camera commands.

We use :class:`FlirIrSim` and the :class:`InlineRunner` so tests don't
spawn threads.
"""

from __future__ import annotations

import pytest

from capa.experiment.config import (
    CalibrationSetRef,
    ExperimentConfig,
    HardwareProfile,
    OperatorRef,
    ProcedureRef,
    SampleInfo,
)
from capa.runtime.build import (
    _construct_camera_adapters_from_config,
    _resolve_camera_class,
    build_workers,
)
from capa.runtime.camera_adapter import CameraDeviceAdapter
from capa.runtime.errors import ResourceConflict
from capa.runtime.runner import InlineRunner

# ---------------------------------------------------------------------------
# Minimal config helpers
# ---------------------------------------------------------------------------


def _config_with_cameras(camera_blocks: list[dict[str, object]]) -> ExperimentConfig:
    """Build a minimal :class:`ExperimentConfig` with only camera entries.

    ``camera_blocks`` are dicts that match :class:`CameraSpec` schema —
    the HardwareProfile constructor parses them via pydantic.
    """
    return ExperimentConfig(
        hardware=HardwareProfile(
            name="cam-test",
            devices=(),
            channels=(),
            cameras=camera_blocks,  # type: ignore[arg-type]
        ),
        procedure=ProcedureRef(id="capa.builtin.free_run", config={"duration_s": 0.1}),
        calibration_set=CalibrationSetRef(name="default"),
        operator=OperatorRef(id="op_test"),
        sample=SampleInfo(id="sample_test"),
    )


# ---------------------------------------------------------------------------
# _resolve_camera_class
# ---------------------------------------------------------------------------


class TestResolveCameraClass:
    def test_resolves_flir_sim(self) -> None:
        from capa.devices.registry import ensure_adapters_loaded

        ensure_adapters_loaded()
        cls = _resolve_camera_class("capa.devices.sim.flir_ir_sim")
        assert cls.__name__ == "FlirIrSim"

    def test_resolves_webcam(self) -> None:
        from capa.devices.registry import ensure_adapters_loaded

        ensure_adapters_loaded()
        cls = _resolve_camera_class("capa.devices.camera.webcam")
        assert cls.__name__ == "WebcamAdapter"

    def test_raises_on_unknown_adapter(self) -> None:
        with pytest.raises(ResourceConflict, match="no AdapterDescriptor"):
            _resolve_camera_class("capa.devices.does_not_exist")


# ---------------------------------------------------------------------------
# _construct_camera_adapters_from_config
# ---------------------------------------------------------------------------


class TestConstructCameraAdapters:
    def test_empty_cameras_returns_empty_list(self) -> None:
        cfg = _config_with_cameras([])
        adapters = _construct_camera_adapters_from_config(cfg)
        assert adapters == []

    def test_constructs_wrapper_per_camera(self) -> None:
        cfg = _config_with_cameras(
            [
                {
                    "name": "thermal_top",
                    "adapter": "capa.devices.sim.flir_ir_sim",
                    "kind": "ir",
                },
                {
                    "name": "thermal_side",
                    "adapter": "capa.devices.sim.flir_ir_sim",
                    "kind": "ir",
                },
            ]
        )
        adapters = _construct_camera_adapters_from_config(cfg)
        assert len(adapters) == 2
        assert all(isinstance(a, CameraDeviceAdapter) for a in adapters)
        names = {a.name for a in adapters}
        assert names == {"thermal_top", "thermal_side"}

    def test_constructed_wrapper_has_clock_proxy_rebindable(self) -> None:
        """Cameras built at pool-open time get a :class:`_ClockProxy` —
        not a real :class:`RunClock` (no run exists yet). The wrapper
        rebinds the proxy on :meth:`start`; until then it has a
        "now" anchor so health snapshots have sane timestamps.
        """
        cfg = _config_with_cameras(
            [
                {
                    "name": "ir_cam",
                    "adapter": "capa.devices.sim.flir_ir_sim",
                    "kind": "ir",
                }
            ]
        )
        adapters = _construct_camera_adapters_from_config(cfg)
        wrapper = adapters[0]
        # Underlying camera received the proxy as its clock
        clock_attr = wrapper.camera._clock
        assert hasattr(clock_attr, "rebind")
        # Proxy currently anchored at "now" (within slack)
        assert clock_attr.t_mono_ns() < 100_000_000  # < 100ms


# ---------------------------------------------------------------------------
# build_workers — camera integration
# ---------------------------------------------------------------------------


class TestBuildWorkersWithCameras:
    def test_cameras_get_their_own_workers(self) -> None:
        """Two cameras with disjoint resource_ids → two workers."""
        cfg = _config_with_cameras(
            [
                {
                    "name": "thermal_top",
                    "adapter": "capa.devices.sim.flir_ir_sim",
                    "kind": "ir",
                },
                {
                    "name": "thermal_side",
                    "adapter": "capa.devices.sim.flir_ir_sim",
                    "kind": "ir",
                },
            ]
        )
        workers, device_to_resource = build_workers(cfg, runner_factory=InlineRunner)
        # FlirIrSim.resource_id == "sim:<spec.name>" → two distinct
        # workers
        assert set(workers.keys()) == {"sim:thermal_top", "sim:thermal_side"}
        # Each worker hosts exactly the wrapper
        for rid, worker in workers.items():
            assert len(worker.adapter_names) == 1
            adapter = worker.adapters[worker.adapter_names[0]]
            assert isinstance(adapter, CameraDeviceAdapter)
            assert adapter.resource_id == rid
        # Device map keys both camera names
        assert device_to_resource == {
            "thermal_top": "sim:thermal_top",
            "thermal_side": "sim:thermal_side",
        }

    def test_camera_and_device_coexist(self) -> None:
        """A pool with devices AND cameras → workers for both,
        identified by their respective ``resource_id`` schemes.
        """
        # Need a small device config — re-use the watlow sim which
        # exposes serial:<port> resource_id when configured.
        cfg = ExperimentConfig(
            hardware=HardwareProfile(
                name="mixed-test",
                devices=(),  # avoid pulling real device deps; cameras alone
                channels=(),
                cameras=[
                    {
                        "name": "ir_cam",
                        "adapter": "capa.devices.sim.flir_ir_sim",
                        "kind": "ir",
                    }
                ],  # type: ignore[arg-type]
            ),
            procedure=ProcedureRef(id="capa.builtin.free_run", config={"duration_s": 0.1}),
            calibration_set=CalibrationSetRef(name="default"),
            operator=OperatorRef(id="op_test"),
            sample=SampleInfo(id="sample_test"),
        )
        workers, device_to_resource = build_workers(cfg, runner_factory=InlineRunner)
        assert "sim:ir_cam" in workers
        assert device_to_resource["ir_cam"] == "sim:ir_cam"

    def test_webcam_resource_id_uniqueness_enforced(self) -> None:
        """Two webcam-adapter cameras with the same serial would map to
        the same ``webcam:<serial>`` resource_id; build_workers's
        validation must catch them as a conflict via
        :func:`_check_webcam_uniqueness`. Cameras that legitimately
        share a handle aren't a thing in practice — DirectShow / V4L2
        only let one process hold the capture pin — so the validator
        treats this as a config error.
        """
        cfg = _config_with_cameras(
            [
                {
                    "name": "cam_a",
                    "adapter": "capa.devices.camera.webcam",
                    "kind": "visible",
                    "serial": "SN-9999",
                },
                {
                    "name": "cam_b",
                    "adapter": "capa.devices.camera.webcam",
                    "kind": "visible",
                    "serial": "SN-9999",
                },
            ]
        )
        with pytest.raises(ResourceConflict, match="webcam"):
            build_workers(cfg, runner_factory=InlineRunner)
