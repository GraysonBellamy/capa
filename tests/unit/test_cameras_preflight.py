"""``disk_space_preflight_problems`` — duration resolution + tmpfs handling.

Hardware-day §3 / §4 regressions: free-runs were always projected against
the ``DEFAULT_FALLBACK_DURATION_S`` (3600 s) because the procedure's own
``duration_s`` config was never consulted, and tmpfs runs-roots looked
plentiful even though the bytes evaporate under memory pressure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from capa.devices.camera.base import CameraSpec
from capa.experiment import cameras as cameras_module
from capa.experiment.cameras import (
    DEFAULT_DISK_FREE_MARGIN,
    DEFAULT_FALLBACK_DURATION_S,
    disk_space_preflight_problems,
)
from capa.experiment.config import (
    CalibrationSetRef,
    ExperimentConfig,
    HardwareProfile,
    OperatorRef,
    ProcedureRef,
    SampleInfo,
)


@pytest.fixture(autouse=True)
def _stub_filesystem_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default ``_filesystem_type`` to ``None`` so tests that don't care
    about filesystem-type detection aren't affected by the host's tmpfs
    layout (pytest's ``tmp_path`` lives on ``/tmp``, which is tmpfs on
    several dev boxes). Tests that exercise the volatile-fs path override
    this fixture by patching ``_filesystem_type`` themselves.
    """
    monkeypatch.setattr(cameras_module, "_filesystem_type", lambda _p: None)
    monkeypatch.setattr(cameras_module, "_mem_available_bytes", lambda: None)


def _config(
    *,
    procedure_config: dict[str, object] | None = None,
    estimated_bps: int = 1_000_000,
) -> ExperimentConfig:
    return ExperimentConfig(
        hardware=HardwareProfile(
            name="cam-only",
            devices=(),
            cameras=(
                CameraSpec.model_validate(
                    {
                        "name": "visible_cam0",
                        "adapter": "capa.devices.camera.webcam",
                        "kind": "visible",
                        "estimated_bps": estimated_bps,
                    }
                ),
            ),
        ),
        procedure=ProcedureRef(
            id="capa.builtin.free_run",
            config=procedure_config or {},
        ),
        calibration_set=CalibrationSetRef(name="default"),
        operator=OperatorRef(id="op"),
        sample=SampleInfo(id="S-1"),
    )


class TestProcedureDurationResolution:
    """Free-run procedures expose ``duration_s`` in ``procedure.config``;
    preflight must use it instead of the 3600 s fallback."""

    def test_free_run_duration_used_when_method_absent(self, tmp_path: Path) -> None:
        # 30 s × 1 MB/s = 30 MB projected; 100 MB free → fits with margin.
        config = _config(procedure_config={"duration_s": 30.0})
        free = int(30 * 1_000_000 * DEFAULT_DISK_FREE_MARGIN * 2)
        problems = disk_space_preflight_problems(
            config, bundle_root=tmp_path, free_bytes_override=free
        )
        assert problems == []

    def test_fallback_kicks_in_when_no_duration(self, tmp_path: Path) -> None:
        # No duration anywhere -> 3600 s fallback. 30 s of headroom now blocks.
        config = _config(procedure_config={})
        free = int(30 * 1_000_000 * DEFAULT_DISK_FREE_MARGIN * 2)
        problems = disk_space_preflight_problems(
            config, bundle_root=tmp_path, free_bytes_override=free
        )
        assert len(problems) == 1
        assert problems[0].code == "disk_space_insufficient"
        assert problems[0].metadata["duration_inferred"] is True
        assert problems[0].metadata["duration_s"] == DEFAULT_FALLBACK_DURATION_S

    def test_invalid_duration_falls_back(self, tmp_path: Path) -> None:
        # Non-numeric or non-positive values must not be trusted as durations.
        for bad in (None, "30", -5.0, 0.0):
            config = _config(procedure_config={"duration_s": bad})
            problems = disk_space_preflight_problems(
                config,
                bundle_root=tmp_path,
                free_bytes_override=int(30 * 1_000_000 * DEFAULT_DISK_FREE_MARGIN * 2),
            )
            assert len(problems) == 1, f"expected fallback for duration_s={bad!r}"
            assert problems[0].metadata["duration_inferred"] is True

    def test_duration_inferred_false_when_procedure_supplies_it(self, tmp_path: Path) -> None:
        # Even when the projection blocks, the metadata must mark the
        # duration as caller-supplied rather than fallback-inferred.
        config = _config(
            procedure_config={"duration_s": 30.0},
            estimated_bps=1_000_000_000,  # 1 GB/s -> 30 GB projected
        )
        problems = disk_space_preflight_problems(
            config, bundle_root=tmp_path, free_bytes_override=1_000_000
        )
        assert len(problems) == 1
        assert problems[0].code == "disk_space_insufficient"
        assert problems[0].metadata["duration_inferred"] is False
        assert problems[0].metadata["duration_s"] == 30.0


class TestVolatileFilesystemDetection:
    """tmpfs / ramfs targets get a non-blocking warning AND a tightened
    free-bytes budget — the kernel's reported free is RAM, which can
    evaporate under memory pressure."""

    def test_tmpfs_emits_warning_problem(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cameras_module, "_filesystem_type", lambda _p: "tmpfs")
        monkeypatch.setattr(cameras_module, "_mem_available_bytes", lambda: 16 * 1024**3)
        config = _config(procedure_config={"duration_s": 30.0})
        problems = disk_space_preflight_problems(
            config,
            bundle_root=tmp_path,
            free_bytes_override=16 * 1024**3,
        )
        codes = [p.code for p in problems]
        assert "disk_target_volatile" in codes
        warning = next(p for p in problems if p.code == "disk_target_volatile")
        assert warning.severity == "warning"
        assert warning.blocking is False
        assert warning.metadata["filesystem_type"] == "tmpfs"

    def test_persistent_fs_no_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cameras_module, "_filesystem_type", lambda _p: "ext4")
        config = _config(procedure_config={"duration_s": 30.0})
        problems = disk_space_preflight_problems(
            config,
            bundle_root=tmp_path,
            free_bytes_override=int(30 * 1_000_000 * DEFAULT_DISK_FREE_MARGIN * 2),
        )
        assert problems == []

    def test_unknown_fs_does_not_fire_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Non-Linux platforms (or read failures) return None — silently skip.
        monkeypatch.setattr(cameras_module, "_filesystem_type", lambda _p: None)
        config = _config(procedure_config={"duration_s": 30.0})
        problems = disk_space_preflight_problems(
            config,
            bundle_root=tmp_path,
            free_bytes_override=int(30 * 1_000_000 * DEFAULT_DISK_FREE_MARGIN * 2),
        )
        assert problems == []

    def test_tmpfs_tightens_budget_to_half_mem_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Reported free is huge (16 GB tmpfs) but MemAvailable is tight (1 GB).
        # Tightened budget = min(16 GB, 0.5 GB) = 0.5 GB. A 1 GB projection
        # then blocks where the un-tightened path would've passed.
        monkeypatch.setattr(cameras_module, "_filesystem_type", lambda _p: "tmpfs")
        monkeypatch.setattr(cameras_module, "_mem_available_bytes", lambda: 1024**3)
        config = _config(
            procedure_config={"duration_s": 1.0},
            estimated_bps=1024**3,  # 1 GB/s × 1 s = 1 GB
        )
        problems = disk_space_preflight_problems(
            config,
            bundle_root=tmp_path,
            free_bytes_override=16 * 1024**3,
        )
        codes = [p.code for p in problems]
        assert "disk_target_volatile" in codes
        assert "disk_space_insufficient" in codes  # tightened budget triggers block
        blocker = next(p for p in problems if p.code == "disk_space_insufficient")
        assert blocker.metadata["free_bytes"] == 1024**3 // 2
        assert blocker.metadata["filesystem_type"] == "tmpfs"

    def test_tmpfs_without_meminfo_keeps_reported_free(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If /proc/meminfo isn't readable, fall back to the reported free
        # bytes. Warning still fires; the blocking check uses raw free.
        monkeypatch.setattr(cameras_module, "_filesystem_type", lambda _p: "tmpfs")
        monkeypatch.setattr(cameras_module, "_mem_available_bytes", lambda: None)
        config = _config(procedure_config={"duration_s": 30.0})
        free = int(30 * 1_000_000 * DEFAULT_DISK_FREE_MARGIN * 2)
        problems = disk_space_preflight_problems(
            config, bundle_root=tmp_path, free_bytes_override=free
        )
        codes = [p.code for p in problems]
        assert codes == ["disk_target_volatile"]  # warning, no blocker
        warning = problems[0]
        assert warning.metadata["mem_available_bytes"] is None
        assert warning.metadata["free_bytes_tightened"] == free
