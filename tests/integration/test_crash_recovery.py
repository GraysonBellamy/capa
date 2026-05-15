"""Crash-recovery integration test (graceful-shutdown shim).

Plan §13.3: a bundle whose engine crashed mid-run leaves
``*.in-flight.arrows`` files plus an open manifest. ``finalize_in_place``
walks the directory and produces a sealed bundle marked ``run_status="crashed"``.

This test exercises the *soft* crash path (``close_sinks()`` without
``finalize()``). Real ``SIGKILL`` mid-write is covered by
``test_crash_recovery_sigkill.py``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from capa.channels.calibration import Identity
from capa.channels.spec import (
    ChannelKind,
    ChannelSpec,
    WatlowParameter,
)
from capa.core.clock import RunClock
from capa.devices.sim._signals import Sine
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
from capa.storage.channel_samples_sink import (
    FINAL_FILENAME,
    INFLIGHT_FILENAME,
)
from capa.storage.finalize import finalize_in_place
from capa.storage.integrity import verify
from capa.storage.manifest import BundleManifest
from tests._adapter_helpers import make_start_ctx


def _config() -> ExperimentConfig:
    hardware = HardwareProfile(
        name="crash_recovery",
        devices=(DeviceConfig(name="heater", adapter="capa.devices.sim.watlow_sim"),),
        channels=(
            ChannelSpec(
                name="heater.pv",
                kind=ChannelKind.PROCESS_VAR,
                source=WatlowParameter(device="heater", parameter="process_value", instance=1),
                unit="degC",
                derived_unit="degC",
                calibration=Identity(input_unit="degC", output_unit="degC"),
            ),
        ),
    )
    return ExperimentConfig(
        hardware=hardware,
        method=None,
        procedure=ProcedureRef(id="capa.builtin.free_run"),
        calibration_set=CalibrationSetRef(name="default"),
        operator=OperatorRef(id="abr"),
        sample=SampleInfo(id="CRASH-001"),
    )


def _crashed_bundle(tmp_path: Path) -> Path:
    """Drive a synthetic run, then drop the writer mid-stream by closing
    sinks without finalizing. Mirrors what happens after a SIGKILL or a
    hard hang followed by a process restart.
    """
    clock = RunClock.now()
    watlow = WatlowSim(
        name="heater",
        signals={("process_value", 1): Sine(amplitude=2, frequency_hz=0.1, offset=300)},
        parameter_units={"process_value": "degC"},
    )
    watlow.configure_channels(list(_config().hardware.channels))

    asyncio.run(watlow.open())
    asyncio.run(watlow.start(make_start_ctx(clock=clock)))

    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    writer = RunBundleWriter(
        _config(),
        runs_root=runs_root,
        run_id="2026-05-07_999999_CRASH-001",
        started_utc=datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC),
        started_mono_ns_anchor=clock.started_mono_ns,
    )
    writer.open()
    for _ in range(4):
        for emission in watlow.tick_once():
            writer.record(emission)
    writer.write_event(
        kind="abort_pending",
        message="simulating crash",
        t_mono_ns=clock.t_mono_ns(),
        t_utc=datetime.now(UTC),
    )
    # Crash simulation: close sinks without calling finalize().
    writer.close_sinks()
    return writer.bundle_path


class TestCrashRecovery:
    def test_inflight_files_present_pre_recovery(self, tmp_path: Path) -> None:
        bundle = _crashed_bundle(tmp_path)
        # Before recovery, in-flight files exist and final ones don't.
        assert (bundle / INFLIGHT_FILENAME).is_file()
        assert not (bundle / FINAL_FILENAME).exists()
        assert (bundle / "device_records" / "watlow.in-flight.arrows").is_file()
        # Manifest is still in 'open' state.
        manifest = BundleManifest.read(bundle / "manifest.json")
        assert manifest.bundle_status == "open"
        assert manifest.run_status == "running"
        assert manifest.ended_utc is None

    def test_finalize_in_place_recovers_to_sealed(self, tmp_path: Path) -> None:
        bundle = _crashed_bundle(tmp_path)
        result = finalize_in_place(
            bundle,
            run_status="crashed",
            exit_reason="simulated crash",
            inferred_ended_utc=True,
        )
        assert result.integrity.status == "ok"
        assert (bundle / FINAL_FILENAME).is_file()
        assert not (bundle / INFLIGHT_FILENAME).exists()
        assert (bundle / "manifest.sha256").is_file()

        manifest = BundleManifest.read(bundle / "manifest.json")
        assert manifest.run_status == "crashed"
        assert manifest.bundle_status == "sealed"
        assert manifest.exit_reason == "simulated crash"
        assert manifest.ended_utc is not None
        assert manifest.custom.get("inferred_ended_utc") is True
        assert manifest.integrity.status == "ok"

        # Recovered Parquet is readable and has the expected rows.
        table = pq.read_table(bundle / FINAL_FILENAME)
        assert table.num_rows > 0
        # Sorted by t_mono_ns post-rewrite.
        ts = table.column("t_mono_ns").to_pylist()
        assert ts == sorted(ts)

        # And sha256 verification clean.
        assert verify(bundle).status == "ok"

    def test_finalize_idempotent_on_recovered_bundle(self, tmp_path: Path) -> None:
        bundle = _crashed_bundle(tmp_path)
        finalize_in_place(bundle, run_status="crashed")
        # Running twice must not corrupt the seal.
        result = finalize_in_place(bundle, run_status="crashed")
        assert result.integrity.status == "ok"
        assert verify(bundle).status == "ok"
        manifest = BundleManifest.read(bundle / "manifest.json")
        assert manifest.bundle_status == "sealed"


class TestIllegalCombinations:
    def test_running_status_at_finalize_rejected(self, tmp_path: Path) -> None:
        bundle = _crashed_bundle(tmp_path)
        from capa.storage.finalize import FinalizeError

        with pytest.raises(FinalizeError):
            # ``run_status="running"`` combined with bundle going to sealed
            # is illegal per is_legal_finalize_combination.
            finalize_in_place(bundle, run_status="running")
