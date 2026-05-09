"""End-to-end engine run with the real :class:`WatlowAdapter` (P0d).

Drives :class:`capa.experiment.engine.ExperimentEngine` against an in-process
:class:`StubWatlowController` (no serial port). Verifies that the bundle:

* finalizes with ``run_status == "completed"``, ``bundle_status == "sealed"``,
* contains both ``device_records/watlow.parquet`` (preserving long-format
  rows from :func:`watlowlib.sinks.sample_to_row`) and ``scalars.parquet``
  (calibrated :class:`ChannelSample`\\ s),
* records the device identity in ``status.sqlite`` (DeviceSnapshot).

The stub is wired in by monkey-patching :func:`watlowlib.open_device` — the
real adapter goes through its production code path including
:meth:`Controller.identify` and :func:`watlowlib.streaming.record`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import anyio
import pyarrow.parquet as pq
import pytest
import watlowlib

from capa.channels.calibration import Identity
from capa.channels.spec import ChannelKind, ChannelSpec, WatlowParameter
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
from capa.storage.manifest import BundleManifest
from tests._watlow_stub import StubWatlowController

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def watlow_signals() -> dict[tuple[str, int], float | int | None]:
    return {("process_value", 1): 425.0, ("setpoint", 1): 450.0}


@pytest.fixture
def stub_controller(watlow_signals) -> StubWatlowController:  # type: ignore[no-untyped-def]
    return StubWatlowController(signals=watlow_signals)


@pytest.fixture
def patched_open_device(monkeypatch: pytest.MonkeyPatch, stub_controller: StubWatlowController):  # type: ignore[no-untyped-def]
    """Replace :func:`watlowlib.open_device` with one that returns the stub.

    ``WatlowAdapter._build_controller`` calls ``watlowlib.open_device``; with
    this patch in place the engine constructs a real adapter that talks to
    our in-process stub.
    """

    async def fake_open_device(
        port: str,
        *,
        protocol: Any = None,
        address: int = 1,
        serial_settings: Any = None,
    ) -> Any:
        del port, protocol, address, serial_settings
        return stub_controller

    monkeypatch.setattr(watlowlib, "open_device", fake_open_device)


def _make_config(*, duration_s: float) -> ExperimentConfig:
    return ExperimentConfig(
        hardware=HardwareProfile(
            name="watlow_engine_test",
            devices=(
                DeviceConfig(
                    name="heater",
                    adapter="capa.devices.watlow",
                    params={
                        "port": "fake://stub",
                        "address": 1,
                        "rate_hz": 50.0,
                        "snapshot_period_s": 1e6,
                        "auto_reconnect": False,
                    },
                ),
            ),
            channels=(
                ChannelSpec(
                    name="heater.pv",
                    kind=ChannelKind.PROCESS_VAR,
                    source=WatlowParameter(device="heater", parameter="process_value", instance=1),
                    unit="degC",
                    derived_unit="degC",
                    calibration=Identity(input_unit="degC", output_unit="degC"),
                ),
                ChannelSpec(
                    name="heater.setpoint",
                    kind=ChannelKind.SETPOINT,
                    source=WatlowParameter(device="heater", parameter="setpoint", instance=1),
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
            config={"duration_s": duration_s},
        ),
        calibration_set=CalibrationSetRef(name="default"),
        operator=OperatorRef(id="abr", display_name="A. Researcher"),
        sample=SampleInfo(id="WATLOW-INT-001", material="paint-A", notes="integration"),
        tags=("integration", "watlow", "p0d"),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def sealed_bundle(
    tmp_path: Path,
    patched_open_device: None,
    stub_controller: StubWatlowController,
) -> Path:
    """Drive a short FreeRun against the stubbed Watlow controller.

    Returns the sealed bundle path. The fixture is sync so we bridge with
    :func:`anyio.run`.
    """
    del patched_open_device  # consumed for its side-effect

    async def _go() -> Path:
        config = _make_config(duration_s=0.3)
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        engine = ExperimentEngine()
        result = await engine.run(
            config,
            runs_root=runs_root,
            run_id="2026-05-07_120000_WATLOW-INT-001",
            configure_logging_for_bundle=False,
        )
        assert result.bundle_path is not None, result.exit_reason
        assert result.run_status == "completed", result.exit_reason
        assert result.bundle_status == "sealed", result.exit_reason
        assert result.integrity_status == "ok", result.exit_reason
        return result.bundle_path

    bundle_path = anyio.run(_go)
    # Sanity: the stub was actually exercised.
    assert stub_controller.aentered
    assert stub_controller.identify_calls == 1
    return bundle_path


class TestEndToEnd:
    def test_bundle_files_present(self, sealed_bundle: Path) -> None:
        assert (sealed_bundle / "manifest.json").is_file()
        assert (sealed_bundle / "manifest.sha256").is_file()
        assert (sealed_bundle / "scalars.parquet").is_file()
        assert (sealed_bundle / "device_records" / "watlow.parquet").is_file()
        assert (sealed_bundle / "events.sqlite").is_file()
        assert (sealed_bundle / "status.sqlite").is_file()

    def test_device_records_carry_long_format_rows(self, sealed_bundle: Path) -> None:
        watlow_table = pq.read_table(sealed_bundle / "device_records" / "watlow.parquet")
        # capa-side header columns
        assert {"record_id", "t_mono_ns", "t_utc"} <= set(watlow_table.column_names)
        # watlowlib.sample_to_row schema
        assert {
            "device",
            "address",
            "protocol",
            "parameter",
            "parameter_id",
            "instance",
            "value",
            "unit",
            "requested_at",
            "received_at",
            "midpoint_at",
            "latency_s",
        } <= set(watlow_table.column_names)
        # Both polled parameters must appear
        params = set(watlow_table.column("parameter").to_pylist())
        assert {"process_value", "setpoint"} <= params

    def test_scalars_carry_calibrated_channel_samples(self, sealed_bundle: Path) -> None:
        scalars = pq.read_table(sealed_bundle / "scalars.parquet")
        assert scalars.num_rows > 0
        channels = set(scalars.column("channel").to_pylist())
        assert {"heater.pv", "heater.setpoint"} <= channels
        units = set(scalars.column("unit").to_pylist())
        assert "degC" in units
        # Identity calibration: PV value should equal stub's signal (425.0).
        rows = scalars.to_pylist()
        pv_rows = [r for r in rows if r["channel"] == "heater.pv"]
        assert pv_rows
        assert pv_rows[0]["value"] == pytest.approx(425.0)

    def test_source_record_id_links(self, sealed_bundle: Path) -> None:
        scalars = pq.read_table(sealed_bundle / "scalars.parquet").to_pylist()
        watlow_records = pq.read_table(
            sealed_bundle / "device_records" / "watlow.parquet"
        ).to_pylist()
        record_ids = {row["record_id"] for row in watlow_records}
        scalar_record_ids = {r["source_record_id"] for r in scalars if r.get("source_record_id")}
        assert scalar_record_ids
        assert scalar_record_ids <= record_ids

    def test_status_snapshot_carries_device_identity(self, sealed_bundle: Path) -> None:
        conn = sqlite3.connect(sealed_bundle / "status.sqlite")
        try:
            rows = list(conn.execute("SELECT adapter, device, fields_json FROM status;"))
        finally:
            conn.close()
        assert rows
        adapters = {r[0] for r in rows}
        assert "watlow" in adapters
        # The cached DeviceInfo from identify() must show up in at least one
        # snapshot's fields blob.
        watlow_rows = [r for r in rows if r[0] == "watlow"]
        import json as _json

        identity_seen = False
        for _adapter, _device, fields_json in watlow_rows:
            fields = _json.loads(fields_json)
            if fields.get("part_number") == "PM3C1AJ-AAAAAAA":
                identity_seen = True
                assert fields.get("firmware_id") == 5678
                assert fields.get("hardware_id") == 1234
                break
        assert identity_seen, "no DeviceSnapshot carried the part-number identity"

    def test_manifest_records_real_adapter(self, sealed_bundle: Path) -> None:
        manifest = BundleManifest.read(sealed_bundle / "manifest.json")
        adapters = {r.adapter for r in manifest.data_shape.device_records}
        assert "watlow" in adapters
        layouts = {r.adapter: r.layout for r in manifest.data_shape.device_records}
        assert layouts["watlow"] == "long_row"
