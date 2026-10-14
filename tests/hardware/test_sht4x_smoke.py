"""Hardware smoke test for the real :class:`Sht4xAdapter`.

Checks for an SHT4x on an MCP2221:

1. Discover the sensor and open / identify it.
2. Read one measurement; assert it is a plausible lab reading.
3. Switch precision through the authorization gate and read again.
4. Drive a short headless ``capa run`` and verify the bundle has both
   ``device_records/sht4x.parquet`` and ``scalars.parquet``.

Skipped unless ``CAPA_HARDWARE_TESTS=1``. ``CAPA_TEST_SHT4X_ADDRESS``
(default ``0x44``) and ``CAPA_TEST_SHT4X_SERIAL`` (the bridge's USB serial,
default unset) pick the sensor.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import anyio
import pyarrow.parquet as pq
import pytest

from capa.channels.calibration import Identity
from capa.channels.spec import ChannelKind, ChannelSpec, Sht4xReading
from capa.devices.sht4x import Sht4xAdapter, discover, handshake
from capa.experiment.config import (
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


def _sht4x_params() -> dict[str, Any]:
    params: dict[str, Any] = {
        "i2c_address": int(os.environ.get("CAPA_TEST_SHT4X_ADDRESS", "0x44"), 0),
        "rate_hz": 2.0,
        "snapshot_period_s": 5.0,
    }
    serial = os.environ.get("CAPA_TEST_SHT4X_SERIAL")
    if serial:
        params["serial"] = serial
    return params


def _operator_id() -> str:
    return os.environ.get("CAPA_TEST_SHT4X_OPERATOR", "hw-test")


def _plausible(temperature_c: float, humidity_pct: float) -> bool:
    return -10.0 < temperature_c < 60.0 and 0.0 <= humidity_pct <= 100.0


class TestRealSht4x:
    async def test_discover_finds_the_sensor(self) -> None:
        rows = await discover()
        assert rows, "no SHT4x found on any MCP2221"
        assert any(row["i2c_address"] == _sht4x_params()["i2c_address"] for row in rows)

    async def test_open_identify_close(self) -> None:
        adapter = Sht4xAdapter(name="ambient", **_sht4x_params())
        await adapter.open()
        try:
            assert adapter.sensor_serial is not None
            assert adapter.sensor_serial.startswith("0x")
            snapshot = await adapter.read_state_snapshot()
            assert snapshot is not None and snapshot.bridge_firmware
        finally:
            await adapter.close()

    async def test_read_measurement(self) -> None:
        adapter = Sht4xAdapter(name="ambient", **_sht4x_params())
        await adapter.open()
        try:
            measurement = await adapter.read_once()
            assert _plausible(measurement.temperature_c, measurement.humidity_pct), measurement
            assert measurement.latency_s < 0.1
        finally:
            await adapter.close()

    async def test_set_precision_authorized(self) -> None:
        adapter = Sht4xAdapter(name="ambient", **_sht4x_params())
        await adapter.open()
        try:
            result = await adapter.set_precision(
                "low", issued_by=_operator_id(), confirmed_by=_operator_id()
            )
            assert result.accepted is True
            measurement = await adapter.read_once()
            assert measurement.precision == "low"
            assert _plausible(measurement.temperature_c, measurement.humidity_pct), measurement
        finally:
            await adapter.close()

    async def test_handshake(self) -> None:
        summary = await handshake(_sht4x_params())
        assert summary.startswith("sht4x serial=0x")


class TestRealSht4xEngineRun:
    def test_short_freerun_writes_bundle(self, tmp_path: Path) -> None:
        params = _sht4x_params()
        config = ExperimentConfig(
            hardware=HardwareProfile(
                name="sht4x_smoke",
                devices=(
                    DeviceConfig(
                        name="ambient",
                        adapter="capa.devices.sht4x",
                        params=params,
                    ),
                ),
                channels=(
                    ChannelSpec(
                        name="ambient.temperature",
                        kind=ChannelKind.TEMPERATURE,
                        source=Sht4xReading(device="ambient", field="temperature"),
                        unit="degC",
                        derived_unit="degC",
                        calibration=Identity(input_unit="degC", output_unit="degC"),
                    ),
                    ChannelSpec(
                        name="ambient.humidity",
                        kind=ChannelKind.HUMIDITY,
                        source=Sht4xReading(device="ambient", field="humidity"),
                        unit="percent",
                        derived_unit="percent",
                        calibration=Identity(input_unit="percent", output_unit="percent"),
                    ),
                ),
            ),
            method=None,
            procedure=ProcedureRef(
                id="capa.builtin.free_run",
                version="0.1",
                config={"duration_s": 5.0},
            ),
            operator=OperatorRef(id=_operator_id(), display_name="Test Operator"),
            sample=SampleInfo(id="HW-SMOKE-SHT4X-001"),
            tags=("hardware", "sht4x", "smoke"),
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
        assert (bundle / "device_records" / "sht4x.parquet").is_file()
        assert (bundle / "scalars.parquet").is_file()
        records = pq.read_table(bundle / "device_records" / "sht4x.parquet")
        assert records.num_rows >= 5
        assert {"temperature_c", "humidity_pct", "temperature_raw", "humidity_raw"} <= set(
            records.column_names
        )
        scalars = pq.read_table(bundle / "scalars.parquet")
        assert scalars.num_rows >= 10
