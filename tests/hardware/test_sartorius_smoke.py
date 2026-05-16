"""Hardware smoke test for the real :class:`SartoriusAdapter`.

Checks for Sartorius:

1. Open and identify a real balance.
2. Read mass from an empty pan; assert the numeric is ≈ 0 ± noise.
3. Tare via :meth:`SartoriusAdapter.tare` — exercises the authorization
   gate. Subsequent read should center on 0 (within balance noise).
4. Drive a short headless ``capa run`` and verify the bundle has both
   ``device_records/sartorius.parquet`` and ``scalars.parquet``.

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
from capa.channels.spec import ChannelKind, ChannelSpec, SartoriusReading
from capa.devices.sartorius import SartoriusAdapter
from capa.experiment.config import (
    CalibrationSetRef,
    DeviceConfig,
    ExperimentConfig,
    HardwareProfile,
    OperatorRef,
    ProcedureRef,
    SampleInfo,
)
from capa.runtime.headless import run_headless

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("CAPA_HARDWARE_TESTS") != "1",
        reason="CAPA_HARDWARE_TESTS=1 required",
    ),
]


def _sartorius_params() -> dict[str, Any]:
    port = os.environ.get("CAPA_TEST_SARTORIUS_PORT")
    if port is None:
        pytest.skip("CAPA_TEST_SARTORIUS_PORT not set")
    return {
        "port": port,
        "protocol": os.environ.get("CAPA_TEST_SARTORIUS_PROTOCOL", "xbpi"),
        "baudrate": int(os.environ.get("CAPA_TEST_SARTORIUS_BAUD", "9600")),
        "rate_hz": 2.0,
        "snapshot_period_s": 5.0,
    }


def _operator_id() -> str:
    return os.environ.get("CAPA_TEST_SARTORIUS_OPERATOR", "hw-test")


class TestRealSartorius:
    async def test_open_identify_close(self) -> None:
        adapter = SartoriusAdapter(name="balance", **_sartorius_params())
        await adapter.open()
        try:
            assert adapter.device_info is not None
        finally:
            await adapter.close()

    async def test_read_mass(self) -> None:
        adapter = SartoriusAdapter(name="balance", **_sartorius_params())
        await adapter.open()
        try:
            reading = await adapter.read_mass()
            assert reading.value is not None
            # Empty pan: any consumer-grade balance should be within ±10 g
            # of zero. Wider than spec but generous enough that drift /
            # leftover sample don't false-fail.
            assert abs(float(reading.value)) < 10.0, f"unexpected mass: {reading.value!r}"
        finally:
            await adapter.close()

    async def test_tare_authorized(self) -> None:
        adapter = SartoriusAdapter(name="balance", **_sartorius_params())
        await adapter.open()
        try:
            result = await adapter.tare(
                issued_by=_operator_id(),
                confirmed_by=_operator_id(),
            )
            assert result.accepted is True
            # Brief settle delay before re-reading.
            await anyio.sleep(0.5)
            reading = await adapter.read_mass()
            assert reading.value is not None
            assert abs(float(reading.value)) < 1.0, f"post-tare mass not zero: {reading.value!r}"
        finally:
            await adapter.close()


class TestRealSartoriusEngineRun:
    def test_short_freerun_writes_bundle(self, tmp_path: Path) -> None:
        params = _sartorius_params()
        config = ExperimentConfig(
            hardware=HardwareProfile(
                name="sartorius_smoke",
                devices=(
                    DeviceConfig(
                        name="balance",
                        adapter="capa.devices.sartorius",
                        params=params,
                    ),
                ),
                channels=(
                    ChannelSpec(
                        name="balance.mass",
                        kind=ChannelKind.MASS,
                        source=SartoriusReading(device="balance", field="value"),
                        unit="g",
                        derived_unit="g",
                        calibration=Identity(input_unit="g", output_unit="g"),
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
            sample=SampleInfo(id="HW-SMOKE-SARTORIUS-001"),
            tags=("hardware", "sartorius", "smoke"),
        )

        async def _go() -> Path:
            result = await run_headless(
                config,
                runs_root=tmp_path / "runs",
            )
            assert result.bundle_path is not None
            assert result.run_status == "completed", result.exit_reason
            assert result.bundle_status == "sealed", result.exit_reason
            assert result.integrity_status == "ok", result.exit_reason
            return result.bundle_path

        bundle = anyio.run(_go)
        assert (bundle / "device_records" / "sartorius.parquet").is_file()
        assert (bundle / "scalars.parquet").is_file()
        records = pq.read_table(bundle / "device_records" / "sartorius.parquet")
        assert records.num_rows >= 3
        scalars = pq.read_table(bundle / "scalars.parquet")
        assert scalars.num_rows >= 3
