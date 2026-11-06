"""Hardware smoke test for the real :class:`Bmp58xAdapter`.

Checks for a BMP58x on an MCP2221 (normally chained after the SHT4x):

1. Discover the sensor and open / identify / configure it.
2. Read one conversion; assert it is a plausible lab reading.
3. Change the oversampling through the authorization gate and read again.
4. Drive a short headless ``capa run`` with the barometer *and* the SHT4x
   on the bridge, and verify the bundle has both device records, both
   sensors' samples in ``scalars.parquet``, and no failed polls.

Skipped unless ``CAPA_HARDWARE_TESTS=1``. ``CAPA_TEST_BMP58X_ADDRESS``
(default ``0x47``) and ``CAPA_TEST_BMP58X_SERIAL`` (the bridge's USB
serial, default unset) pick the sensor; the SHT4x in step 4 uses the
``CAPA_TEST_SHT4X_*`` variables.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import anyio
import pyarrow.parquet as pq
import pytest

from capa.channels.calibration import Identity
from capa.channels.spec import Bmp58xReading, ChannelKind, ChannelSpec, Sht4xReading
from capa.devices._mcp2221 import BUS_POOL
from capa.devices.bmp58x import Bmp58xAdapter, discover, handshake
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


def _bmp58x_params() -> dict[str, Any]:
    params: dict[str, Any] = {
        "i2c_address": int(os.environ.get("CAPA_TEST_BMP58X_ADDRESS", "0x47"), 0),
        "rate_hz": 2.0,
        "snapshot_period_s": 5.0,
    }
    serial = os.environ.get("CAPA_TEST_BMP58X_SERIAL")
    if serial:
        params["serial"] = serial
    return params


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
    return os.environ.get("CAPA_TEST_BMP58X_OPERATOR", "hw-test")


def _plausible(pressure_pa: float, temperature_c: float) -> bool:
    return 70_000.0 < pressure_pa < 110_000.0 and -10.0 < temperature_c < 60.0


class TestRealBmp58x:
    async def test_discover_finds_the_sensor(self) -> None:
        rows = await discover()
        assert rows, "no BMP58x found on any MCP2221"
        assert any(row["i2c_address"] == _bmp58x_params()["i2c_address"] for row in rows)

    async def test_open_identify_close(self) -> None:
        adapter = Bmp58xAdapter(name="baro", **_bmp58x_params())
        await adapter.open()
        try:
            assert adapter.chip in ("BMP580/BMP581", "BMP585")
            snapshot = await adapter.read_state_snapshot()
            assert snapshot is not None and snapshot.bridge_firmware
            assert snapshot.revision is not None
        finally:
            await adapter.close()
        assert BUS_POOL.open_keys() == []

    async def test_read_measurement(self) -> None:
        adapter = Bmp58xAdapter(name="baro", **_bmp58x_params())
        await adapter.open()
        try:
            measurement = await adapter.read_once()
            assert _plausible(measurement.pressure_pa, measurement.temperature_c), measurement
            assert measurement.latency_s < 0.2
        finally:
            await adapter.close()

    async def test_set_oversampling_authorized(self) -> None:
        adapter = Bmp58xAdapter(name="baro", **_bmp58x_params())
        await adapter.open()
        try:
            result = await adapter.set_oversampling(
                pressure=8, temperature=1, issued_by=_operator_id(), confirmed_by=_operator_id()
            )
            assert result.accepted is True
            measurement = await adapter.read_once()
            assert (measurement.pressure_oversampling, measurement.temperature_oversampling) == (
                8,
                1,
            )
            assert _plausible(measurement.pressure_pa, measurement.temperature_c), measurement
        finally:
            await adapter.close()

    async def test_handshake(self) -> None:
        summary = await handshake(_bmp58x_params())
        assert summary.startswith("bmp58x chip=BMP58")


class TestRealAmbientBridgeRun:
    def test_short_freerun_with_both_sensors_writes_bundle(self, tmp_path: Path) -> None:
        config = ExperimentConfig(
            hardware=HardwareProfile(
                name="ambient_bridge_smoke",
                devices=(
                    DeviceConfig(
                        name="ambient", adapter="capa.devices.sht4x", params=_sht4x_params()
                    ),
                    DeviceConfig(
                        name="baro", adapter="capa.devices.bmp58x", params=_bmp58x_params()
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
                        name="ambient.pressure",
                        kind=ChannelKind.PRESSURE,
                        source=Bmp58xReading(device="baro", field="pressure"),
                        unit="Pa",
                        derived_unit="Pa",
                        calibration=Identity(input_unit="Pa", output_unit="Pa"),
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
            sample=SampleInfo(id="HW-SMOKE-BMP58X-001"),
            tags=("hardware", "bmp58x", "sht4x", "smoke"),
        )

        async def _go() -> Path:
            result = await run_headless(config, runs_root=tmp_path / "runs")
            assert result.bundle_path is not None
            assert result.run_status == "completed", result.exit_reason
            assert result.bundle_status == "sealed", result.exit_reason
            assert result.integrity_status == "ok", result.exit_reason
            return result.bundle_path

        bundle = anyio.run(_go)
        assert BUS_POOL.open_keys() == []
        baro = pq.read_table(bundle / "device_records" / "bmp58x.parquet")
        sht = pq.read_table(bundle / "device_records" / "sht4x.parquet")
        assert baro.num_rows >= 5 and sht.num_rows >= 5
        assert {"pressure_pa", "temperature_c", "pressure_raw", "pressure_oversampling"} <= set(
            baro.column_names
        )
        # Sharing the bridge cost neither sensor a poll.
        assert baro.column("error_type").null_count == baro.num_rows
        assert sht.column("error_type").null_count == sht.num_rows
        scalars = pq.read_table(bundle / "scalars.parquet")
        rows = list(
            zip(
                scalars.column("channel").to_pylist(),
                scalars.column("value").to_pylist(),
                strict=True,
            )
        )
        assert {"ambient.temperature", "ambient.pressure"} <= {channel for channel, _ in rows}
        pressure = [value for channel, value in rows if channel == "ambient.pressure"]
        assert pressure and all(70_000.0 < value < 110_000.0 for value in pressure)
