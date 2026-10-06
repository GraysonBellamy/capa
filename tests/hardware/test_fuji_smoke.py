"""Hardware smoke test for the real :class:`FujiAdapter`. Reads only.

Checks for a Fuji ZP-series gas analyzer:

1. Open and identify a real analyzer, and read its settings.
2. Read the operator-facing snapshot: one reading per mapped channel.
3. The ``capa validate --strict`` handshake line.
4. Discovery finds the analyzer on its port.
5. Drive a short headless ``capa run`` and verify the bundle has both
   ``device_records/fuji.parquet`` and ``scalars.parquet``.

Nothing is written to the analyzer: no setting, no front-panel key and no
calibration.

Skipped unless ``CAPA_HARDWARE_TESTS=1`` and ``CAPA_TEST_FUJI_PORT`` is set.
``CAPA_TEST_FUJI_ADDRESS`` (default ``1``) is the station number, and
``CAPA_TEST_FUJI_CHANNEL_MAP`` (default ``CH1=co2,CH2=co,CH3=o2``) the gas on
each analyzer channel.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import anyio
import pyarrow.parquet as pq
import pytest

from capa.channels.calibration import Identity
from capa.channels.spec import ChannelKind, ChannelSpec, FujiChannel
from capa.devices.fuji import FujiAdapter, discover, handshake
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


def _channel_map() -> dict[str, str]:
    text = os.environ.get("CAPA_TEST_FUJI_CHANNEL_MAP", "CH1=co2,CH2=co,CH3=o2")
    pairs = (item.split("=", 1) for item in text.split(",") if item.strip())
    return {channel.strip().upper(): gas.strip().lower() for channel, gas in pairs}


def _fuji_params() -> dict[str, Any]:
    port = os.environ.get("CAPA_TEST_FUJI_PORT")
    if port is None:
        pytest.skip("CAPA_TEST_FUJI_PORT not set")
    return {
        "port": port,
        "address": int(os.environ.get("CAPA_TEST_FUJI_ADDRESS", "1")),
        "channel_map": _channel_map(),
        "rate_hz": 2.0,
        "snapshot_period_s": 5.0,
    }


def _operator_id() -> str:
    return os.environ.get("CAPA_TEST_FUJI_OPERATOR", "hw-test")


class TestRealFuji:
    async def test_open_identify_close(self) -> None:
        adapter = FujiAdapter(name="analyzer", **_fuji_params())
        await adapter.open()
        try:
            info = adapter.device_info
            assert info is not None
            assert info.model
            assert adapter.metadata is not None
            snap = await adapter.snapshot()
            assert snap.fields["model"] == info.model
            assert "response_time_o2_s" in snap.fields
        finally:
            await adapter.close()

    async def test_read_state_snapshot(self) -> None:
        params = _fuji_params()
        adapter = FujiAdapter(name="analyzer", **params)
        await adapter.open()
        try:
            snapshot = await adapter.read_state_snapshot()
            assert snapshot is not None
            assert {r.channel for r in snapshot.readings} >= set(params["channel_map"])
            assert snapshot.calibration.state == "idle"
            for reading in snapshot.readings:
                assert reading.value is not None, f"{reading.channel} did not decode"
        finally:
            await adapter.close()

    async def test_handshake(self) -> None:
        line = await handshake(_fuji_params())
        assert line.startswith("fuji model=")
        assert "channels=[" in line

    async def test_discover_finds_the_analyzer(self) -> None:
        params = _fuji_params()
        rows = await discover(ports=[params["port"]], addresses=(params["address"],))
        assert len(rows) == 1, rows
        assert rows[0]["adapter"] == "fuji"
        assert rows[0]["address"] == params["address"]
        assert rows[0]["model"]


class TestRealFujiEngineRun:
    def test_short_freerun_writes_bundle(self, tmp_path: Path) -> None:
        params = _fuji_params()
        channels = tuple(
            ChannelSpec(
                name=f"gas.{gas}",
                kind=ChannelKind.GAS_CONCENTRATION,
                source=FujiChannel(device="analyzer", channel=channel),  # type: ignore[arg-type]
                unit="percent",
                derived_unit="percent",
                calibration=Identity(input_unit="percent", output_unit="percent"),
            )
            for channel, gas in params["channel_map"].items()
        )
        config = ExperimentConfig(
            hardware=HardwareProfile(
                name="fuji_smoke",
                devices=(
                    DeviceConfig(
                        name="analyzer",
                        adapter="capa.devices.fuji",
                        params=params,
                    ),
                ),
                channels=channels,
            ),
            method=None,
            procedure=ProcedureRef(
                id="capa.builtin.free_run",
                version="0.1",
                config={"duration_s": 5.0},
            ),
            operator=OperatorRef(id=_operator_id(), display_name="Test Operator"),
            sample=SampleInfo(id="HW-SMOKE-FUJI-001"),
            tags=("hardware", "fuji", "smoke"),
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
        assert (bundle / "device_records" / "fuji.parquet").is_file()
        assert (bundle / "scalars.parquet").is_file()
        records = pq.read_table(bundle / "device_records" / "fuji.parquet").to_pylist()
        assert len(records) >= 3
        assert all(row["error_type"] is None for row in records), records
        scalars = pq.read_table(bundle / "scalars.parquet").to_pylist()
        # A channel whose unit is not percent on the analyzer is quarantined
        # rather than recorded wrong, so the count is of the channels that are.
        assert {row["channel"] for row in scalars} <= {spec.name for spec in channels}
        assert len(scalars) >= 3
