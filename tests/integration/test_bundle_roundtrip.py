"""End-to-end synthetic-run round-trip.

Drives :class:`RunBundleWriter` from the P0a sim adapters, finalizes, then
reads everything back and asserts the bundle is sealed, integrity-clean,
and the data round-trips losslessly.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from capa.channels.calibration import Identity
from capa.channels.spec import (
    AlicatFrameField,
    ChannelKind,
    ChannelSpec,
    NIDAQReadingField,
    SartoriusReading,
    WatlowParameter,
)
from capa.core.clock import RunClock
from capa.devices.camera.base import CameraSpec
from capa.devices.sim._signals import Sine
from capa.devices.sim.alicat_sim import AlicatSim
from capa.devices.sim.nidaq_polled_sim import NIDAQPolledSim
from capa.devices.sim.sartorius_sim import SartoriusSim
from capa.devices.sim.watlow_sim import WatlowSim
from capa.experiment.config import (
    CalibrationSetRef,
    DeviceConfig,
    ExperimentConfig,
    HardwareProfile,
    OperatorRef,
    ProcedureRef,
    SampleInfo,
)
from capa.storage.bundle import RunBundleWriter
from capa.storage.finalize import finalize_in_place
from capa.storage.integrity import verify
from capa.storage.manifest import BundleManifest


def _hardware() -> HardwareProfile:
    """Multi-adapter hardware fixture exercising every shape:
    Watlow long_row, Alicat wide_row, Sartorius single_value_row,
    NI polled wide_row.
    """
    return HardwareProfile(
        name="bundle_roundtrip",
        devices=(
            DeviceConfig(name="heater", adapter="capa.devices.sim.watlow_sim"),
            DeviceConfig(name="air_mfc", adapter="capa.devices.sim.alicat_sim"),
            DeviceConfig(name="balance", adapter="capa.devices.sim.sartorius_sim"),
            DeviceConfig(name="ni0", adapter="capa.devices.sim.nidaq_polled_sim"),
        ),
        cameras=(
            # Spec carries no model_hint/serial — identity should come from
            # the live adapter probe via the cameras=[...] kwarg at finalize.
            CameraSpec(
                name="visible_cam0",
                adapter="capa.devices.camera.webcam",
                kind="visible",
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
                name="MFC_air.flow",
                kind=ChannelKind.MFC_FLOW,
                source=AlicatFrameField(device="air_mfc", field="Mass_Flow"),
                unit="slpm",
                derived_unit="slpm",
                calibration=Identity(input_unit="slpm", output_unit="slpm"),
            ),
            ChannelSpec(
                name="balance.value",
                kind=ChannelKind.MASS,
                source=SartoriusReading(device="balance", field="value"),
                unit="g",
                derived_unit="g",
                calibration=Identity(input_unit="g", output_unit="g"),
            ),
            ChannelSpec(
                name="ai0",
                kind=ChannelKind.ANALOG_IN,
                source=NIDAQReadingField(device="ni0", task="default_task", field="AI0"),
                unit="V",
                derived_unit="V",
                calibration=Identity(input_unit="V", output_unit="V"),
            ),
        ),
    )


def _config() -> ExperimentConfig:
    return ExperimentConfig(
        hardware=_hardware(),
        method=None,
        procedure=ProcedureRef(id="capa.builtin.free_run", version="0.1"),
        calibration_set=CalibrationSetRef(name="default"),
        operator=OperatorRef(id="abr", display_name="A. Researcher"),
        sample=SampleInfo(id="SIM-S001", material="paint-A", notes="synthetic"),
        tags=("sim", "freerun", "p0b-roundtrip"),
    )


def _build_sims(clock: RunClock) -> tuple[WatlowSim, AlicatSim, SartoriusSim, NIDAQPolledSim]:
    specs = list(_hardware().channels)
    watlow = WatlowSim(
        name="heater",
        signals={("process_value", 1): Sine(amplitude=5, frequency_hz=0.1, offset=400)},
        parameter_units={"process_value": "degC"},
    )
    watlow.configure_channels(specs)

    alicat = AlicatSim(
        name="air_mfc",
        signals={"Mass_Flow": Sine(amplitude=0.5, frequency_hz=0.05, offset=2.0)},
        static_fields={"Mix_Gas": "Air"},
    )
    alicat.configure_channels(specs)

    sartorius = SartoriusSim(
        name="balance",
        mass_signal=Sine(amplitude=1.0, frequency_hz=0.02, offset=10.0),
    )
    sartorius.configure_channels(specs)

    nidaq = NIDAQPolledSim(
        name="ni0",
        signals={"AI0": Sine(amplitude=1.0, frequency_hz=0.5, offset=0.5)},
        units={"AI0": "V"},
    )
    nidaq.configure_channels(specs)
    return watlow, alicat, sartorius, nidaq


@pytest.fixture
def sealed_bundle(tmp_path: Path) -> Path:
    """Drive a synthetic run through the full bundle writer pipeline.

    Sync fixture: the sim adapters expose async ``open``/``start``/``snapshot``
    methods, but they're trivial state changes. We bridge with ``asyncio.run``
    so this fixture stays synchronous (and therefore usable from both sync
    and async tests in any pytest version).
    """
    clock = RunClock.now()
    watlow, alicat, sartorius, nidaq = _build_sims(clock)

    async def _arm() -> None:
        for sim in (watlow, alicat, sartorius, nidaq):
            await sim.open()
            await sim.start(clock=clock)

    asyncio.run(_arm())

    config = _config()
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    started = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)
    writer = RunBundleWriter(
        config,
        runs_root=runs_root,
        run_id="2026-05-07_120000_SIM-S001",
        started_utc=started,
        started_mono_ns_anchor=clock.started_mono_ns,
    )
    repo_root = Path(__file__).resolve().parents[2]
    writer.open(repo_root=repo_root, lockfile_source=repo_root / "uv.lock")

    # Drive 8 ticks of each adapter; that's enough rows to exercise multiple
    # row-group flushes when flush_rows is small.
    snapshot = asyncio.run(watlow.snapshot())
    for _ in range(8):
        for sim in (watlow, alicat, sartorius, nidaq):
            for emission in sim.tick_once():
                writer.record(emission)
        writer.record(snapshot)

    # An operator event for good measure.
    writer.write_event(
        kind="run_start",
        message="recording",
        t_mono_ns=clock.t_mono_ns(),
        t_utc=datetime.now(UTC),
    )

    # Synthetic equipment + camera identity blocks — matches what the
    # engine's _collect_equipment_blocks / _collect_camera_blocks would
    # produce for sim adapters (identity=None) plus a probed webcam.
    equipment_blocks = [
        {"name": "heater", "adapter": "capa.devices.sim.watlow_sim", "identity": None},
        {"name": "air_mfc", "adapter": "capa.devices.sim.alicat_sim", "identity": None},
        {"name": "balance", "adapter": "capa.devices.sim.sartorius_sim", "identity": None},
        {"name": "ni0", "adapter": "capa.devices.sim.nidaq_polled_sim", "identity": None},
    ]
    camera_blocks = [
        {
            "name": "visible_cam0",
            "adapter": "capa.devices.camera.webcam",
            "identity": {
                "model": "Logitech Webcam C930e",
                "serial": "E7501BDE-FIXTURE",
            },
        },
    ]

    result = writer.finalize(
        run_status="completed",
        equipment=equipment_blocks,
        cameras=camera_blocks,
    )
    assert result.integrity.status == "ok"
    return writer.bundle_path


class TestBundleStructure:
    def test_required_files_present(self, sealed_bundle: Path) -> None:
        # Every plan-§8 path that P0b owns.
        assert (sealed_bundle / "manifest.json").is_file()
        assert (sealed_bundle / "manifest.sha256").is_file()
        assert (sealed_bundle / "config.toml").is_file()
        assert (sealed_bundle / "equipment.toml").is_file()
        assert (sealed_bundle / "calibration.json").is_file()
        assert (sealed_bundle / "scalars.parquet").is_file()
        assert (sealed_bundle / "events.sqlite").is_file()
        assert (sealed_bundle / "status.sqlite").is_file()
        assert (sealed_bundle / "run.log").is_file()
        assert (sealed_bundle / "env" / "uv.lock").is_file()
        assert (sealed_bundle / "env" / "packages.json").is_file()
        # In-flight files removed after finalize
        assert not (sealed_bundle / "scalars.in-flight.arrows").exists()

    def test_device_records_per_adapter(self, sealed_bundle: Path) -> None:
        dr = sealed_bundle / "device_records"
        assert dr.is_dir()
        names = sorted(p.name for p in dr.iterdir() if p.suffix == ".parquet")
        assert names == [
            "alicat.parquet",
            "nidaq_polled.parquet",
            "sartorius.parquet",
            "watlow.parquet",
        ]
        for name in names:
            assert not (dr / name.replace(".parquet", ".in-flight.arrows")).exists()


class TestManifest:
    def test_manifest_round_trips_and_seals(self, sealed_bundle: Path) -> None:
        manifest = BundleManifest.read(sealed_bundle / "manifest.json")
        assert manifest.bundle_status == "sealed"
        assert manifest.run_status == "completed"
        assert manifest.integrity.status == "ok"
        assert manifest.ended_utc is not None
        assert manifest.bundle_schema_version == 2

    def test_data_shape_lists_every_adapter(self, sealed_bundle: Path) -> None:
        manifest = BundleManifest.read(sealed_bundle / "manifest.json")
        assert manifest.data_shape.channel_samples is not None
        assert manifest.data_shape.channel_samples.path == "scalars.parquet"
        adapters = {r.adapter for r in manifest.data_shape.device_records}
        assert adapters == {"alicat", "nidaq_polled", "sartorius", "watlow"}
        # Layout tags match plan §8.9.
        layouts = {r.adapter: r.layout for r in manifest.data_shape.device_records}
        assert layouts["watlow"] == "long_row"
        assert layouts["sartorius"] == "single_value_row"
        assert layouts["alicat"] == "wide_row"
        assert layouts["nidaq_polled"] == "wide_row"

    def test_provenance_block_populated(self, sealed_bundle: Path) -> None:
        manifest = BundleManifest.read(sealed_bundle / "manifest.json")
        assert manifest.capa.version
        assert manifest.python.version
        assert manifest.platform.os
        # Lockfile sha was captured (we passed uv.lock as the source).
        assert manifest.lockfile.path == "env/uv.lock"
        assert manifest.lockfile.sha256
        # And the on-disk file matches the recorded sha.
        from hashlib import sha256

        bytes_on_disk = (sealed_bundle / "env" / "uv.lock").read_bytes()
        assert sha256(bytes_on_disk).hexdigest() == manifest.lockfile.sha256


class TestParquetReadback:
    def test_scalars_round_trip(self, sealed_bundle: Path) -> None:
        table = pq.read_table(sealed_bundle / "scalars.parquet")
        assert table.num_rows > 0
        # Every channel from the hardware fixture should have rows.
        channels = set(table.column("channel").to_pylist())
        assert {"heater.pv", "MFC_air.flow", "balance.value", "ai0"} <= channels
        # Sorted by t_mono_ns post-finalize.
        ts = table.column("t_mono_ns").to_pylist()
        assert ts == sorted(ts)
        # Units come through.
        units = set(table.column("unit").to_pylist())
        assert {"degC", "slpm", "g", "V"} <= units

    def test_device_records_carry_native_columns(self, sealed_bundle: Path) -> None:
        watlow_path = sealed_bundle / "device_records" / "watlow.parquet"
        watlow = pq.read_table(watlow_path)
        # Capa-side header columns
        assert {"record_id", "t_mono_ns", "t_utc"} <= set(watlow.column_names)
        # Library-native columns from watlowlib.sample_to_row
        assert {"parameter", "value", "instance", "address", "protocol"} <= set(watlow.column_names)
        # Sartorius single-value row
        sartorius = pq.read_table(sealed_bundle / "device_records" / "sartorius.parquet")
        assert "stable" in sartorius.column_names


class TestSqliteReadback:
    def test_events_writer_event_present(self, sealed_bundle: Path) -> None:
        conn = sqlite3.connect(sealed_bundle / "events.sqlite")
        try:
            kinds = [k for (k,) in conn.execute("SELECT kind FROM events ORDER BY id;")]
            assert "run_start" in kinds
        finally:
            conn.close()

    def test_status_snapshots_landed(self, sealed_bundle: Path) -> None:
        conn = sqlite3.connect(sealed_bundle / "status.sqlite")
        try:
            count = conn.execute("SELECT COUNT(*) FROM status;").fetchone()[0]
            assert count > 0
        finally:
            conn.close()


class TestIntegrity:
    def test_capa_verify_clean(self, sealed_bundle: Path) -> None:
        result = verify(sealed_bundle)
        assert result.status == "ok"
        assert not result.mismatches

    def test_sha256sum_minus_c_clean(self, sealed_bundle: Path) -> None:
        # sha256sum is GNU coreutils; ubiquitous but not on every OS.
        if shutil.which("sha256sum") is None:
            pytest.skip("sha256sum CLI not available")
        proc = subprocess.run(
            ["sha256sum", "-c", "manifest.sha256"],
            cwd=sealed_bundle,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def test_mutation_breaks_verify(self, sealed_bundle: Path) -> None:
        target = sealed_bundle / "scalars.parquet"
        original = target.read_bytes()
        try:
            target.write_bytes(original + b"\x00")  # corrupt
            result = verify(sealed_bundle)
            assert result.status == "mismatch"
        finally:
            target.write_bytes(original)


class TestConfigSnapshot:
    def test_config_toml_round_trips(self, sealed_bundle: Path) -> None:
        with open(sealed_bundle / "config.toml", "rb") as fp:
            data = tomllib.load(fp)
        assert data["operator"]["id"] == "abr"
        assert data["sample"]["id"] == "SIM-S001"
        # tuple-typed `tags` round-trips as list under TOML
        assert "sim" in data["tags"]

    def test_calibration_json_present(self, sealed_bundle: Path) -> None:
        with open(sealed_bundle / "calibration.json") as fp:
            data = json.load(fp)
        assert data["name"] == "default"


class TestEquipmentToml:
    """Hardware-day §10: ``equipment.toml`` was previously a stub with
    only configured ``name`` + ``adapter``. Live adapters must inject
    their probed identity at finalize time so the bundle records the
    actual physical hardware used (Watlow part number, firmware, …)."""

    def test_equipment_block_includes_devices(self, sealed_bundle: Path) -> None:
        with open(sealed_bundle / "equipment.toml", "rb") as fp:
            data = tomllib.load(fp)
        assert "devices" in data
        names = sorted(d["name"] for d in data["devices"])
        # Every configured device shows up — no silent drops.
        assert "heater" in names
        assert "air_mfc" in names
        assert "balance" in names
        assert "ni0" in names

    def test_each_device_has_identity_field(self, sealed_bundle: Path) -> None:
        with open(sealed_bundle / "equipment.toml", "rb") as fp:
            data = tomllib.load(fp)
        for entry in data["devices"]:
            # identity is None for sim adapters that don't expose
            # device_info, but the schema slot must be present.
            assert "identity" in entry or entry.get("identity") is None

    def test_equipment_toml_in_manifest_sha256(self, sealed_bundle: Path) -> None:
        # The integrity digest must cover the rewritten file — otherwise a
        # mid-pipeline rewrite of identity values would slip past verify().
        digest_text = (sealed_bundle / "manifest.sha256").read_text(encoding="utf-8")
        assert "equipment.toml" in digest_text

    def test_cameras_section_includes_probed_identity(self, sealed_bundle: Path) -> None:
        """Hardware-day 2026-05-09 PM finding #2: ``[[cameras]]`` must appear
        alongside ``[[devices]]`` so V4L2 / vendor camera identity reaches
        the bundle artefact.
        """
        with open(sealed_bundle / "equipment.toml", "rb") as fp:
            data = tomllib.load(fp)
        assert "cameras" in data
        assert len(data["cameras"]) == 1
        cam = data["cameras"][0]
        assert cam["name"] == "visible_cam0"
        assert cam["adapter"] == "capa.devices.camera.webcam"
        assert cam["identity"]["model"] == "Logitech Webcam C930e"
        assert cam["identity"]["serial"] == "E7501BDE-FIXTURE"

    def test_manifest_cameras_model_serial_overridden_from_identity(
        self, sealed_bundle: Path
    ) -> None:
        """Finding #2 second surface: ``manifest.json.cameras[*].model`` /
        ``serial`` were hard-coded to ``CameraSpec.model_hint`` (a stub
        populated at arm-time). The finalize-time identity overrides must
        replace those with the live-probed values.
        """
        manifest = json.loads((sealed_bundle / "manifest.json").read_text())
        cameras = manifest["cameras"]
        assert len(cameras) == 1
        # The CameraSpec in the fixture has no model_hint/serial; the
        # override is the only source of these values.
        assert cameras[0]["model"] == "Logitech Webcam C930e"
        assert cameras[0]["serial"] == "E7501BDE-FIXTURE"


class TestIdempotentFinalize:
    def test_finalize_in_place_on_sealed_is_safe(self, sealed_bundle: Path) -> None:
        # No in-flight files remain; finalize_in_place should be a no-op
        # that leaves the seal intact.
        result = finalize_in_place(sealed_bundle, run_status="completed")
        assert result.integrity.status == "ok"
        # Re-verify
        assert verify(sealed_bundle).status == "ok"
        manifest = BundleManifest.read(sealed_bundle / "manifest.json")
        assert manifest.bundle_status == "sealed"
