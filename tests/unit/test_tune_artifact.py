"""Tests for :mod:`capa.calibration.tune_artifact`."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from capa.calibration.tune_artifact import (
    HeatFluxTuneArtifact,
    HeatFluxTunePoint,
    TuneArtifactError,
    from_toml,
    load_artifact,
    load_latest,
    save_artifact,
    to_toml,
)


def _point(target: float, sp: float, *, accepted: bool = True) -> HeatFluxTunePoint:
    return HeatFluxTunePoint(
        target_flux_kw_m2=target,
        heater_setpoint_c=sp,
        measured_flux_mean_kw_m2=target,
        measured_flux_std_kw_m2=0.02,
        measured_flux_slope_kw_m2_per_min=0.0,
        heater_pv_mean_c=sp,
        soak_s=600.0,
        accepted=accepted,
        accept_reason="algorithm_converged",
    )


def _artifact(*, id_: str = "capa_flux_2026-05-17") -> HeatFluxTuneArtifact:
    return HeatFluxTuneArtifact(
        id=id_,
        rig="capa_real_full",
        heater_device="heater",
        heater_setpoint_channel="heater.setpoint",
        heater_pv_channel="heater.pv",
        flux_channel="heat_flux_gauge",
        gauge_calibration_ref="SB-SN1234 cert 2026-04-12",
        geometry="40mm below heater, centerline",
        accepted_at=datetime(2026, 5, 17, 14, 30, tzinfo=UTC),
        operator_id="gbellamy",
        procedure_id="capa.builtin.heat_flux_tune",
        procedure_version="0.1.0",
        capa_git_sha="deadbeef",
        points=(
            _point(25.0, 480.0),
            _point(50.0, 620.0),
            _point(75.0, 740.0),
        ),
    )


def test_round_trip_preserves_equality() -> None:
    artifact = _artifact()
    text = to_toml(artifact)
    restored = from_toml(text)
    assert restored == artifact


def test_round_trip_with_optional_fields_unset() -> None:
    artifact = HeatFluxTuneArtifact(
        id="capa_flux_2026-05-18",
        rig="capa_real_full",
        heater_device="heater",
        heater_setpoint_channel="heater.setpoint",
        heater_pv_channel="heater.pv",
        flux_channel="heat_flux_gauge",
        geometry="40mm below heater, centerline",
        accepted_at=datetime(2026, 5, 18, 9, 0, tzinfo=UTC),
        procedure_id="capa.builtin.heat_flux_tune",
        procedure_version="0.1.0",
        points=(_point(50.0, 620.0),),
    )
    restored = from_toml(to_toml(artifact))
    assert restored == artifact
    assert restored.gauge_calibration_ref is None
    assert restored.operator_id is None
    assert restored.capa_git_sha is None


def test_malformed_toml_raises_tune_artifact_error() -> None:
    with pytest.raises(TuneArtifactError, match="malformed"):
        from_toml("[this is = not = valid")


def test_missing_required_field_raises_tune_artifact_error() -> None:
    incomplete = """
id = "x"
rig = "r"
"""
    with pytest.raises(TuneArtifactError, match="validation"):
        from_toml(incomplete)


def test_save_and_load_latest(tmp_path: Path) -> None:
    artifact = _artifact()
    path = save_artifact(artifact, tmp_path)
    assert path == tmp_path / f"{artifact.id}.toml"
    assert path.is_file()
    assert (tmp_path / "latest.toml").is_file()

    loaded = load_latest(tmp_path)
    assert loaded == artifact


def test_save_refuses_overwrite_same_id(tmp_path: Path) -> None:
    artifact = _artifact()
    save_artifact(artifact, tmp_path)
    with pytest.raises(TuneArtifactError, match="already exists"):
        save_artifact(artifact, tmp_path)


def test_save_dated_backup_of_previous_latest(tmp_path: Path) -> None:
    first = _artifact(id_="capa_flux_2026-05-17")
    save_artifact(first, tmp_path)

    second = _artifact(id_="capa_flux_2026-05-18")
    save_artifact(second, tmp_path)

    backups = list(tmp_path.glob("capa_flux_2026-05-17.toml.bak-*"))
    assert len(backups) == 1, f"expected one dated backup, got: {backups}"
    backup_text = backups[0].read_text(encoding="utf-8")
    # The backup is a verbatim copy of the prior artifact, not a re-serialise
    assert "capa_flux_2026-05-17" in backup_text

    latest_loaded = load_latest(tmp_path)
    assert latest_loaded is not None
    assert latest_loaded.id == "capa_flux_2026-05-18"


def test_load_latest_returns_none_when_no_pointer(tmp_path: Path) -> None:
    assert load_latest(tmp_path) is None


def test_load_latest_returns_none_when_pointer_target_missing(tmp_path: Path) -> None:
    (tmp_path / "latest.toml").write_text('id = "ghost"\n', encoding="utf-8")
    assert load_latest(tmp_path) is None


def test_load_latest_raises_on_malformed_pointer(tmp_path: Path) -> None:
    (tmp_path / "latest.toml").write_text("not a toml file\n[[broken\n", encoding="utf-8")
    with pytest.raises(TuneArtifactError, match="latest pointer"):
        load_latest(tmp_path)


def test_load_latest_raises_when_pointer_id_missing(tmp_path: Path) -> None:
    (tmp_path / "latest.toml").write_text("# no id key\n", encoding="utf-8")
    with pytest.raises(TuneArtifactError, match="'id'"):
        load_latest(tmp_path)


def test_load_artifact_missing_file(tmp_path: Path) -> None:
    with pytest.raises(TuneArtifactError, match="cannot read"):
        load_artifact(tmp_path / "missing.toml")


def test_setpoint_for_target_interpolates_within_bracket() -> None:
    art = _artifact()
    # Bracket is 50→620 to 75→740: target 60 → 620 + 0.4*120 = 668
    result = art.setpoint_for_target(60.0)
    assert result is not None
    assert result == pytest.approx(668.0, abs=1e-6)


def test_setpoint_for_target_refuses_extrapolation() -> None:
    art = _artifact()
    assert art.setpoint_for_target(10.0) is None  # below
    assert art.setpoint_for_target(120.0) is None  # above


def test_setpoint_for_target_returns_none_when_no_accepted_points() -> None:
    art = _artifact()
    # Override with all-rejected points to test the "no accepted" path
    rejected = tuple(
        _point(p.target_flux_kw_m2, p.heater_setpoint_c, accepted=False) for p in art.points
    )
    art2 = art.model_copy(update={"points": rejected})
    assert art2.setpoint_for_target(50.0) is None


def test_local_df_dt_secant_slope() -> None:
    art = _artifact()
    # Between (50, 620) and (75, 740): dF/dT = 25 / 120 ≈ 0.2083
    slope = art.local_df_dt(60.0)
    assert slope is not None
    assert slope == pytest.approx(25.0 / 120.0, rel=1e-9)


def test_local_df_dt_requires_two_accepted_points() -> None:
    one = HeatFluxTuneArtifact(
        id="capa_flux_2026-05-19",
        rig="capa_real_full",
        heater_device="heater",
        heater_setpoint_channel="heater.setpoint",
        heater_pv_channel="heater.pv",
        flux_channel="heat_flux_gauge",
        geometry="40mm below heater, centerline",
        accepted_at=datetime(2026, 5, 19, tzinfo=UTC),
        procedure_id="capa.builtin.heat_flux_tune",
        procedure_version="0.1.0",
        points=(_point(50.0, 620.0),),
    )
    assert one.local_df_dt(50.0) is None
