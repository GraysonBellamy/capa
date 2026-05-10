"""Real-OS-kill crash-recovery — closes the §7 acceptance gap from the
2026-05-09 hardware day.

Spawns a child writer process, lets it cross several flush boundaries,
forces termination via :meth:`multiprocessing.Process.kill` (which maps to
``SIGKILL`` on POSIX and ``TerminateProcess`` on Windows — both are
uncatchable), then verifies ``finalize_in_place`` recovers a sealed bundle
with every fsync'd batch.

Uses ``mp.get_context("spawn")`` so the test runner isn't ``fork()``\\ d —
forking pytest leaks threads/locks/fds into the child and produces flaky
behavior. Spawn re-imports the module in a fresh interpreter.

Hardware-day 2026-05-09 followup #5: the prior implementation referenced
``signal.SIGKILL`` directly, which doesn't exist on Windows; the cross-
platform ``proc.kill()`` covers both rigs in a single test.
"""

from __future__ import annotations

import multiprocessing as mp
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

from capa.storage.finalize import finalize_in_place
from capa.storage.manifest import (
    BundleManifest,
    CapaBlock,
    LockfileBlock,
    OperatorBlock,
    PlatformBlock,
    ProcedureBlock,
    PythonBlock,
    SampleBlock,
)


# Top-level so it's pickleable for spawn-mode multiprocessing.
def _writer_child(bundle_dir: str, ready_path: str, n_batches: int) -> None:
    from capa.devices.records import ChannelSample
    from capa.storage.channel_samples_sink import ChannelSamplesSink

    sink = ChannelSamplesSink(Path(bundle_dir), flush_rows=4)
    for batch in range(n_batches):
        for i in range(4):
            t_ns = batch * 4_000_000 + i * 1_000_000
            sink.write(
                ChannelSample(
                    channel="heater.pv",
                    t_mono_ns=t_ns,
                    t_mono_s=t_ns / 1e9,
                    value=float(batch * 4 + i),
                    raw=None,
                    unit="degC",
                    status="ok",
                )
            )
        # flush_rows=4 above triggers an automatic flush per batch — the
        # explicit call here is belt-and-suspenders to guarantee the fsync
        # finishes before we touch the ready signal.
        sink.flush()
    Path(ready_path).touch()
    while True:
        time.sleep(1)


def _write_minimal_manifest(bundle: Path) -> None:
    """Lay down a manifest.json that finalize_in_place will accept."""
    manifest = BundleManifest(
        run_id="2026-05-07_120000_SIGKILL",
        started_utc=datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC),
        started_mono_ns_anchor=1_000_000_000,
        operator=OperatorBlock(id="abr"),
        sample=SampleBlock(id="SIGKILL-1"),
        procedure=ProcedureBlock(id="capa.builtin.free_run"),
        capa=CapaBlock(version="0.7.3"),
        python=PythonBlock(version="3.13.0", implementation="CPython", executable="/usr/bin/py"),
        platform=PlatformBlock(os="Linux-test", machine="x86_64", node="testrig"),
        lockfile=LockfileBlock(path=None, sha256=None),
    )
    manifest.write(bundle / "manifest.json")


def _spawn_and_kill(
    target,  # type: ignore[no-untyped-def]
    args: tuple[object, ...],
    ready: Path,
) -> None:
    ctx = mp.get_context("spawn")
    proc = ctx.Process(target=target, args=args)
    proc.start()
    try:
        deadline = time.monotonic() + 30.0
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "child never reported ready"
        assert proc.pid is not None
        # Cross-platform forced termination: SIGKILL on POSIX,
        # TerminateProcess on Windows.
        proc.kill()
        proc.join(timeout=5)
        # Exit-code semantics differ by platform:
        # * POSIX: exitcode is the negative signal number (``-9`` for SIGKILL).
        # * Windows: TerminateProcess yields exit code 1 (or whatever was
        #   passed; mp uses 1).
        # Either way, the child must have terminated with a non-zero exit.
        if sys.platform == "win32":
            assert proc.exitcode is not None and proc.exitcode != 0, (
                f"child exit code was {proc.exitcode}, expected non-zero"
            )
        else:
            assert proc.exitcode is not None and proc.exitcode < 0, (
                f"child exit code was {proc.exitcode}, expected negative (signal)"
            )
    finally:
        if proc.is_alive():
            proc.kill()
            proc.join()


def test_sigkill_mid_write_recovers(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_minimal_manifest(bundle)
    ready = tmp_path / "ready"

    _spawn_and_kill(_writer_child, (str(bundle), str(ready), 5), ready)

    result = finalize_in_place(bundle, run_status="crashed", inferred_ended_utc=True)
    assert result.integrity.status == "ok"

    final = pq.read_table(bundle / "scalars.parquet")
    # 5 batches × 4 rows, all flushed before SIGKILL.
    assert final.num_rows >= 20
    ts = final.column("t_mono_ns").to_pylist()
    assert ts == sorted(ts)

    manifest = BundleManifest.read(bundle / "manifest.json")
    assert manifest.run_status == "crashed"
    assert manifest.bundle_status == "sealed"
    assert manifest.custom.get("inferred_ended_utc") is True


def test_unrecoverable_inflight_seals_with_warning(tmp_path: Path) -> None:
    """In-flight file torn before the schema message decodes → bundle seals
    'crashed' with a ``finalize_warnings`` entry. Triggered here by dropping
    a few junk bytes at the in-flight path (a SIGKILL between open() and the
    first write would produce the same on-disk shape, but is hard to time
    deterministically — the warning logic is what matters).
    """
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_minimal_manifest(bundle)
    # Junk bytes that aren't a valid Arrow IPC schema message.
    (bundle / "scalars.in-flight.arrows").write_bytes(b"\x00\x01\x02\x03\x04")

    result = finalize_in_place(bundle, run_status="crashed", inferred_ended_utc=True)
    assert result.integrity.status == "ok"
    assert not (bundle / "scalars.parquet").exists()
    assert not (bundle / "scalars.in-flight.arrows").exists()

    manifest = BundleManifest.read(bundle / "manifest.json")
    assert manifest.run_status == "crashed"
    assert manifest.bundle_status == "sealed"
    warnings = manifest.custom.get("finalize_warnings")
    assert isinstance(warnings, list)
    assert len(warnings) == 1
    assert "scalars.in-flight.arrows" in warnings[0]["path"]
