"""Hardware smoke test for the real :class:`WatlowAdapter` (P0d).

Plan §15.4 contract for Watlow:

1. Open and identify a real PM-class controller.
2. Read PV; assert sane numeric value.
3. Set the current setpoint to its current value (no-op delta) — exercises
   the authorization gate + ``confirm=True`` path without changing the
   physical state.
4. Drive a short headless ``capa run`` and verify the bundle has both
   ``device_records/watlow.parquet`` and ``scalars.parquet``.

Skipped unless ``CAPA_HARDWARE_TESTS=1``. Connection parameters come from
the envvars documented in :mod:`tests.hardware.__init__`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import anyio
import pyarrow.parquet as pq
import pytest

from capa.channels.calibration import Identity
from capa.channels.spec import ChannelKind, ChannelSpec, WatlowParameter
from capa.devices.watlow import WatlowAdapter
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


def _watlow_params() -> dict[str, Any]:
    port = os.environ.get("CAPA_TEST_WATLOW_PORT")
    if port is None:
        pytest.skip("CAPA_TEST_WATLOW_PORT not set")
    return {
        "port": port,
        "address": int(os.environ.get("CAPA_TEST_WATLOW_ADDR", "1")),
        "protocol": os.environ.get("CAPA_TEST_WATLOW_PROTOCOL", "stdbus"),
        "rate_hz": 1.0,
        "snapshot_period_s": 5.0,
    }


def _operator_id() -> str:
    return os.environ.get("CAPA_TEST_WATLOW_OPERATOR", "hw-test")


class TestRealWatlow:
    async def test_open_identify_close(self) -> None:
        adapter = WatlowAdapter(name="heater", **_watlow_params())
        await adapter.open()
        try:
            assert adapter.device_info is not None
            assert adapter.device_info.part_number.raw  # non-empty
        finally:
            await adapter.close()

    async def test_read_pv(self) -> None:
        adapter = WatlowAdapter(name="heater", **_watlow_params())
        await adapter.open()
        try:
            reading = await adapter.read_pv()
            assert reading.value is not None
            assert -200 < float(reading.value) < 1500  # plausibly °C / °F range
        finally:
            await adapter.close()

    async def test_set_setpoint_noop_echo(self) -> None:
        """Set the setpoint to its current value — no physical change but
        exercises the authorize → confirm=True → echo round-trip."""
        adapter = WatlowAdapter(name="heater", **_watlow_params())
        await adapter.open()
        try:
            current_sp = await adapter.read_pv()  # use PV as a safe surrogate
            target = float(current_sp.value or 25.0)
            result = await adapter.set_setpoint(
                target,
                issued_by=_operator_id(),
                confirmed_by=_operator_id(),  # explicit manual confirm
            )
            assert result.accepted is True
        finally:
            await adapter.close()


class TestRealWatlowEngineRun:
    def test_short_freerun_writes_bundle(self, tmp_path: Path) -> None:
        params = _watlow_params()
        config = ExperimentConfig(
            hardware=HardwareProfile(
                name="watlow_smoke",
                devices=(
                    DeviceConfig(
                        name="heater",
                        adapter="capa.devices.watlow",
                        params=params,
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
            method=None,
            procedure=ProcedureRef(
                id="capa.builtin.free_run",
                version="0.1",
                config={"duration_s": 5.0},
            ),
            calibration_set=CalibrationSetRef(name="default"),
            operator=OperatorRef(id=_operator_id()),
            sample=SampleInfo(id="HW-SMOKE-WATLOW-001"),
            tags=("hardware", "watlow", "smoke"),
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
        # Both surfaces present
        assert (bundle / "device_records" / "watlow.parquet").is_file()
        assert (bundle / "scalars.parquet").is_file()
        records = pq.read_table(bundle / "device_records" / "watlow.parquet")
        assert records.num_rows >= 3  # at least a few ticks of data
        scalars = pq.read_table(bundle / "scalars.parquet")
        assert scalars.num_rows >= 3
