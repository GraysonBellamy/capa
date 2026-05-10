"""Hardware smoke test for the real :class:`AlicatAdapter` (P2).

Plan §15.4 contract for Alicat:

1. Open and identify a real Alicat device.
2. Run a short engine free-run; assert the bundle has both
   ``device_records/alicat.parquet`` and ``scalars.parquet`` populated.
3. Echo the current setpoint as a no-op write — exercises the
   authorization gate + ``confirm=True`` path without changing the
   physical state. (Skipped automatically if the device is a meter, not a
   controller.)

Skipped unless ``CAPA_HARDWARE_TESTS=1``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import anyio
import pyarrow.parquet as pq
import pytest

from capa.channels.calibration import Identity
from capa.channels.spec import AlicatFrameField, ChannelKind, ChannelSpec
from capa.devices.alicat import AlicatAdapter
from capa.experiment.config import (
    CalibrationSetRef,
    DeviceConfig,
    ExperimentConfig,
    HardwareProfile,
    OperatorRef,
    ProcedureRef,
    SampleInfo,
)
from capa.experiment.engine import ExperimentEngine

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("CAPA_HARDWARE_TESTS") != "1",
        reason="CAPA_HARDWARE_TESTS=1 required",
    ),
]


def _alicat_params() -> dict[str, Any]:
    port = os.environ.get("CAPA_TEST_ALICAT_PORT")
    if port is None:
        pytest.skip("CAPA_TEST_ALICAT_PORT not set")
    return {
        "port": port,
        "unit_id": os.environ.get("CAPA_TEST_ALICAT_UNIT_ID", "A"),
        "baudrate": int(os.environ.get("CAPA_TEST_ALICAT_BAUD", "19200")),
        "rate_hz": 2.0,
        "snapshot_period_s": 5.0,
    }


def _operator_id() -> str:
    return os.environ.get("CAPA_TEST_ALICAT_OPERATOR", "hw-test")


class TestRealAlicat:
    async def test_open_identify_close(self) -> None:
        adapter = AlicatAdapter(name="purge_mfc", **_alicat_params())
        await adapter.open()
        try:
            assert adapter.device_info is not None
        finally:
            await adapter.close()

    async def test_setpoint_echo_noop(self) -> None:
        """Echo the current setpoint back to the device — no physical
        change but exercises the authorize → confirm=True round-trip.
        Skipped automatically if the underlying device is a meter."""
        from alicatlib.devices.flow_controller import FlowController
        from alicatlib.devices.pressure_controller import PressureController

        adapter = AlicatAdapter(name="purge_mfc", **_alicat_params())
        await adapter.open()
        try:
            if not isinstance(adapter._device, FlowController | PressureController):
                pytest.skip("alicat device is a meter; no setpoint to echo")
            frame = await adapter._device.poll()  # current setpoint snapshot
            current_sp = float(getattr(frame, "Mass_Flow_Setpt", 0.0) or 0.0)
            result = await adapter.set_setpoint(
                current_sp,
                issued_by=_operator_id(),
                confirmed_by=_operator_id(),
            )
            assert result.accepted is True
        finally:
            await adapter.close()


class TestRealAlicatEngineRun:
    def test_short_freerun_writes_bundle(self, tmp_path: Path) -> None:
        params = _alicat_params()
        config = ExperimentConfig(
            hardware=HardwareProfile(
                name="alicat_smoke",
                devices=(
                    DeviceConfig(
                        name="purge_mfc",
                        adapter="capa.devices.alicat",
                        params=params,
                    ),
                ),
                channels=(
                    ChannelSpec(
                        name="purge.flow",
                        kind=ChannelKind.MFC_FLOW,
                        source=AlicatFrameField(device="purge_mfc", field="Mass_Flow"),
                        unit="slpm",
                        derived_unit="slpm",
                        calibration=Identity(input_unit="slpm", output_unit="slpm"),
                    ),
                ),
            ),
            method=None,
            procedure=ProcedureRef(
                id="capa.builtin.free_run",
                version="0.1",
                config={"duration_s": 5.0},
            ),
            calibration_set=CalibrationSetRef(name="default"),
            operator=OperatorRef(id=_operator_id()),
            sample=SampleInfo(id="HW-SMOKE-ALICAT-001"),
            tags=("hardware", "alicat", "smoke"),
        )

        async def _go() -> Path:
            engine = ExperimentEngine()
            result = await engine.run(
                config,
                runs_root=tmp_path / "runs",
                configure_logging_for_bundle=False,
            )
            assert result.bundle_path is not None
            assert result.run_status == "completed", result.exit_reason
            assert result.bundle_status == "sealed", result.exit_reason
            assert result.integrity_status == "ok", result.exit_reason
            return result.bundle_path

        bundle = anyio.run(_go)
        assert (bundle / "device_records" / "alicat.parquet").is_file()
        assert (bundle / "scalars.parquet").is_file()
        records = pq.read_table(bundle / "device_records" / "alicat.parquet")
        assert records.num_rows >= 3
        scalars = pq.read_table(bundle / "scalars.parquet")
        assert scalars.num_rows >= 3
