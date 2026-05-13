"""Hardware smoke test for the real :class:`NIDAQAdapter` (P2, Windows rig).

Closes the §3.5 acceptance gap on the Windows hardware day. Plan §15.4
contract for NI-DAQ:

1. ``discover()`` returns at least one NI device on the local system.
2. ``handshake()`` validates declared physical channels against the
   live system (read-only — does not allocate the task).
3. ``open() → start() → stream()`` produces a non-empty
   :class:`DaqReading` (one wide row + one :class:`ChannelSample` per
   bound channel).
4. A short headless ``capa run`` lands ``device_records/nidaq_polled.parquet``
   plus matching ``scalars.parquet`` rows.

Skipped unless ``CAPA_HARDWARE_TESTS=1``. NI device + module names come
from envvars (``CAPA_TEST_NIDAQ_DEVICE`` defaults to ``cDAQ1Mod1`` so a
single-module 9171/9214 rig works out of the box).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import anyio
import pyarrow.parquet as pq
import pytest

from capa.channels.calibration import Identity
from capa.channels.spec import ChannelKind, ChannelSpec, NIDAQReadingField
from capa.core.clock import RunClock
from capa.devices.nidaq import NIDAQAdapter
from capa.devices.records import ChannelSample, SourceRecord
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


def _module_name() -> str:
    return os.environ.get("CAPA_TEST_NIDAQ_DEVICE", "cDAQ1Mod1")


def _operator_id() -> str:
    return os.environ.get("CAPA_TEST_NIDAQ_OPERATOR", "hw-test")


def _tc_channels() -> tuple[dict[str, object], ...]:
    """Two K-type thermocouples with built-in CJC, °C output."""
    mod = _module_name()
    return (
        {
            "kind": "thermocouple",
            "physical_channel": f"{mod}/ai0",
            "name": "TC_top_1",
            "thermocouple_type": "K",
            "min_val": 0.0,
            "max_val": 1000.0,
            "cjc_source": "BUILT_IN",
            "units": "DEG_C",
        },
        {
            "kind": "thermocouple",
            "physical_channel": f"{mod}/ai1",
            "name": "TC_top_2",
            "thermocouple_type": "K",
            "min_val": 0.0,
            "max_val": 1000.0,
            "cjc_source": "BUILT_IN",
            "units": "DEG_C",
        },
    )


def _capa_channels() -> tuple[ChannelSpec, ...]:
    return (
        ChannelSpec(
            name="TC_sample_top",
            kind=ChannelKind.THERMOCOUPLE,
            source=NIDAQReadingField(device="cdaq1", task="default_task", field="TC_top_1"),
            unit="degC",
            derived_unit="degC",
            calibration=Identity(input_unit="degC", output_unit="degC"),
        ),
        ChannelSpec(
            name="TC_sample_mid",
            kind=ChannelKind.THERMOCOUPLE,
            source=NIDAQReadingField(device="cdaq1", task="default_task", field="TC_top_2"),
            unit="degC",
            derived_unit="degC",
            calibration=Identity(input_unit="degC", output_unit="degC"),
        ),
    )


class TestRealNIDAQ:
    async def test_discover_returns_local_device(self) -> None:
        from capa.devices.nidaq import discover

        rows = await discover()
        assert len(rows) >= 1, "no NI devices enumerated by nidaqmx"
        names = {row["device"] for row in rows}
        # Either the chassis or the module name must appear.
        assert any(_module_name().split("Mod")[0] in n or _module_name() in n for n in names), (
            f"expected device matching {_module_name()!r} in {names!r}"
        )

    async def test_handshake_against_real_system(self) -> None:
        from capa.devices.nidaq import handshake

        result = await handshake(
            {
                "task_name": "default_task",
                "channels": _tc_channels(),
                "rate_hz": 5.0,
                "snapshot_period_s": 30.0,
            }
        )
        assert "default_task" in result
        assert "channels=2" in result

    async def test_open_stream_emits_readings(self) -> None:
        """``stream_until_stopped`` handles the inner-async-with cleanup so
        the test doesn't have to reach into ``_stop_requested`` /
        ``aclose()`` itself. Hardware-day 2026-05-09 followup #6."""
        clock = RunClock.now()
        adapter = NIDAQAdapter(
            name="cdaq1",
            task_name="default_task",
            channels=_tc_channels(),
            rate_hz=5.0,
            snapshot_period_s=60.0,  # longer than the run; no extra snapshots mid-stream
        )
        adapter.configure_channels(list(_capa_channels()))
        await adapter.open()
        try:
            await adapter.start(clock)
            records: list[SourceRecord] = []
            samples: list[ChannelSample] = []
            async for emission in adapter.stream_until_stopped(max_records=8):
                if isinstance(emission, SourceRecord):
                    records.append(emission)
                elif isinstance(emission, ChannelSample):
                    samples.append(emission)
            assert len(records) >= 5, f"expected ≥5 readings, got {len(records)}"
            assert len(samples) >= 10, f"expected ≥10 channel samples, got {len(samples)}"
            # Thermocouple values should land somewhere in the declared window
            # for connected TCs; open junctions float wildly and may exceed it,
            # which is expected for a bench rig with no probes attached.
            for cs in samples:
                assert isinstance(cs.value, float)
            # Identity probe ran during open() — the manifest collector reads
            # this attribute, so verifying it here catches followup #3 regressions.
            info = adapter.device_info
            assert info is not None, "device_info should be populated against real NI hardware"
            assert info.physical_module is not None
        finally:
            await adapter.close()


class TestRealNIDAQEngineRun:
    def test_short_freerun_writes_bundle(self, tmp_path: Path) -> None:
        config = ExperimentConfig(
            hardware=HardwareProfile(
                name="nidaq_smoke",
                devices=(
                    DeviceConfig(
                        name="cdaq1",
                        adapter="capa.devices.nidaq",
                        params={
                            "task_name": "default_task",
                            "channels": _tc_channels(),
                            "rate_hz": 5.0,
                            "snapshot_period_s": 30.0,
                        },
                    ),
                ),
                channels=_capa_channels(),
            ),
            method=None,
            procedure=ProcedureRef(
                id="capa.builtin.free_run",
                version="0.1",
                config={"duration_s": 5.0},
            ),
            calibration_set=CalibrationSetRef(name="default"),
            operator=OperatorRef(id=_operator_id()),
            sample=SampleInfo(id="HW-SMOKE-NIDAQ-001"),
            tags=("hardware", "nidaq", "smoke"),
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
        assert (bundle / "device_records" / "nidaq_polled.parquet").is_file()
        assert (bundle / "scalars.parquet").is_file()
        records = pq.read_table(bundle / "device_records" / "nidaq_polled.parquet")
        assert records.num_rows >= 5  # 5 Hz × 5 s minus startup ≈ 15
        scalars = pq.read_table(bundle / "scalars.parquet")
        assert scalars.num_rows >= 10  # two channels × 5 ticks
        # Manifest device identity should now be populated (followup #3).
        manifest = json.loads((bundle / "manifest.json").read_text())
        identity = manifest["devices"][0].get("identity")
        assert identity, "manifest.json.devices[0].identity should be populated"
        assert identity.get("physical_module"), identity
        assert identity.get("product_type") or identity.get("serial_number"), identity
