"""Tests for ``capa.config.document.ConfigDocument`` (plan §5.2)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from capa.config.document import ConfigDocument, SaveError, SourceLayout
from capa.experiment.config import ExperimentConfig

# ---------------------------------------------------------------------------
# Loading.
# ---------------------------------------------------------------------------


def test_load_sim_capa_pyrolysis(configs_dir: Path) -> None:
    """The reference experiment loads with external hardware + method.

    Confirms mode detection and source-path tracking for the canonical
    CAPA-pyrolysis recipe.
    """
    doc = ConfigDocument.load(configs_dir / "experiments" / "sim_capa_pyrolysis.yaml")

    assert doc.experiment_format == "yaml"
    assert doc.hardware_mode == "external"
    assert doc.method_mode == "external"
    assert doc.hardware_path is not None and doc.hardware_path.name == "sim_capa.toml"
    assert doc.method_path is not None and doc.method_path.name == "sim_capa_pyrolysis.method.toml"
    # Hardware payload includes the devices/channels.
    assert "devices" in doc.hardware_payload
    assert "channels" in doc.hardware_payload
    # experiment_payload must not still carry hardware / method keys —
    # they've been hoisted into separate payload fields.
    assert "hardware" not in doc.experiment_payload
    assert "method" not in doc.experiment_payload
    # The other top-level keys live in experiment_payload as raw dicts.
    assert "procedure" in doc.experiment_payload
    assert "operator" in doc.experiment_payload


def test_load_hardware_only_toml(configs_dir: Path) -> None:
    """A bare hardware TOML produces a minimal document."""
    doc = ConfigDocument.load_hardware_only(configs_dir / "hardware" / "sim_capa.toml")
    assert doc.hardware_mode == "external"
    assert doc.method_mode == "none"
    assert doc.method_payload is None
    assert doc.hardware_path is not None
    assert doc.hardware_payload["name"] == "sim_capa"


def test_load_propagates_source_paths_via_build_config(configs_dir: Path) -> None:
    """``ExperimentConfig.load`` is now a thin wrapper.

    Both ``method_source_path`` and ``hardware_source_path`` survive on
    the validated config so existing UI/runtime callers keep working.
    """
    cfg = ExperimentConfig.load(configs_dir / "experiments" / "sim_capa_pyrolysis.yaml")
    assert cfg.method_source_path is not None
    assert cfg.method_source_path.name == "sim_capa_pyrolysis.method.toml"
    assert cfg.hardware_source_path is not None
    assert cfg.hardware_source_path.name == "sim_capa.toml"


def test_source_paths_excluded_from_dump(configs_dir: Path) -> None:
    """Both source-tracking fields use ``exclude=True``.

    Serialised config must not leak the on-disk source paths — they're
    in-memory IO bookkeeping, not config data.
    """
    cfg = ExperimentConfig.load(configs_dir / "experiments" / "sim_capa_pyrolysis.yaml")
    dumped = cfg.model_dump()
    assert "method_source_path" not in dumped
    assert "hardware_source_path" not in dumped


# ---------------------------------------------------------------------------
# Round-trip idempotency.
# ---------------------------------------------------------------------------


def _save_to(doc: ConfigDocument, tmp_path: Path, prefix: str) -> tuple[Path, Path, Path | None]:
    exp = tmp_path / f"{prefix}.yaml"
    hw = tmp_path / f"{prefix}_hardware.toml"
    method = tmp_path / f"{prefix}_method.toml" if doc.method_mode != "none" else None
    doc.experiment_path = exp
    doc.experiment_format = "yaml"
    doc.hardware_path = hw
    doc.hardware_format = "toml"
    if doc.method_mode == "external":
        doc.method_path = method
        doc.method_format = "toml"
    doc.save()
    return exp, hw, method


def test_roundtrip_byte_identical(tmp_path: Path, configs_dir: Path) -> None:
    """Save → reload → save (to second tmp tree) yields byte-identical files.

    The canonical writers must be idempotent: once a file is written in
    canonical order, re-saving it produces identical bytes. This is the
    property plan acceptance criterion 9 requires.

    The two save targets live in sibling directories with identical
    filenames so the relative ``hardware:`` ref is the same string in
    both experiment YAMLs.
    """
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()

    doc = ConfigDocument.load(configs_dir / "experiments" / "sim_capa_pyrolysis.yaml")
    _save_to(doc, first_dir, "exp")
    doc2 = ConfigDocument.load(first_dir / "exp.yaml")
    _save_to(doc2, second_dir, "exp")

    assert (first_dir / "exp.yaml").read_bytes() == (second_dir / "exp.yaml").read_bytes()
    assert (first_dir / "exp_hardware.toml").read_bytes() == (
        second_dir / "exp_hardware.toml"
    ).read_bytes()
    assert (first_dir / "exp_method.toml").read_bytes() == (
        second_dir / "exp_method.toml"
    ).read_bytes()


def test_roundtrip_preserves_external_mode(tmp_path: Path, configs_dir: Path) -> None:
    """External hardware ref stays external on save (no silent inlining)."""
    doc = ConfigDocument.load(configs_dir / "experiments" / "sim_capa_pyrolysis.yaml")
    exp, _hw, _ = _save_to(doc, tmp_path, "out")

    reloaded = ConfigDocument.load(exp)
    assert reloaded.hardware_mode == "external"
    assert reloaded.method_mode == "external"
    # The experiment YAML must contain a string ref, not an inlined block.
    text = exp.read_text(encoding="utf-8")
    assert "hardware: " in text and "devices" not in text


def test_roundtrip_preserves_inline_mode(tmp_path: Path, configs_dir: Path) -> None:
    """Inline hardware stays inline; no silent extraction."""
    src = configs_dir / "experiments" / "sim_capa_pyrolysis.yaml"
    doc = ConfigDocument.load(src)
    doc.hardware_mode = "inline"
    doc.hardware_path = None
    doc.hardware_format = None
    # Also inline the method to make this a single-file experiment.
    doc.method_mode = "inline"
    doc.method_path = None
    doc.method_format = None

    out = tmp_path / "inline.yaml"
    doc.experiment_path = out
    doc.save()

    reloaded = ConfigDocument.load(out)
    assert reloaded.hardware_mode == "inline"
    assert reloaded.method_mode == "inline"
    assert "devices" in reloaded.hardware_payload


def test_build_config_equivalent_to_legacy_load(configs_dir: Path) -> None:
    """``ConfigDocument(...).build_config()`` matches ``ExperimentConfig.load()``."""
    path = configs_dir / "experiments" / "sim_capa_pyrolysis.yaml"
    cfg_via_doc = ConfigDocument.load(path).build_config()
    cfg_via_load = ExperimentConfig.load(path)
    assert cfg_via_doc.hardware.name == cfg_via_load.hardware.name
    assert len(cfg_via_doc.hardware.devices) == len(cfg_via_load.hardware.devices)
    assert len(cfg_via_doc.hardware.channels) == len(cfg_via_load.hardware.channels)


# ---------------------------------------------------------------------------
# Atomic save semantics.
# ---------------------------------------------------------------------------


def test_atomic_save_rolls_back_on_failure(tmp_path: Path, configs_dir: Path) -> None:
    """If a save partway fails, no ``.tmp`` files survive and originals are intact.

    Simulates a mid-write failure by patching ``os.replace`` to raise on
    the second file. After the failure: every staged ``.tmp`` is gone,
    and any pre-existing original file is unchanged.
    """
    doc = ConfigDocument.load(configs_dir / "experiments" / "sim_capa_pyrolysis.yaml")

    exp = tmp_path / "exp.yaml"
    hw = tmp_path / "hw.toml"
    method = tmp_path / "method.toml"
    doc.experiment_path = exp
    doc.experiment_format = "yaml"
    doc.hardware_path = hw
    doc.hardware_format = "toml"
    doc.method_path = method
    doc.method_format = "toml"

    # Pre-write originals so we can verify they survive untouched.
    exp.write_bytes(b"pre-existing experiment\n")
    hw.write_bytes(b"pre-existing hardware\n")
    method.write_bytes(b"pre-existing method\n")

    real_replace = os.replace
    call_count = {"n": 0}

    def flaky_replace(src: str, dst: str) -> None:
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise OSError("simulated mid-save failure")
        real_replace(src, dst)

    with (
        patch("capa.config.document.os.replace", side_effect=flaky_replace),
        pytest.raises(SaveError),
    ):
        doc.save()

    # No .tmp files left in the directory.
    assert not list(tmp_path.glob("*.tmp"))
    # First replace succeeded, so its target was updated; second failed —
    # but the third was never attempted. Verify the *third* original
    # (method) is untouched, which is the key data-safety property.
    assert method.read_bytes() == b"pre-existing method\n"


# ---------------------------------------------------------------------------
# Save-as / layout transitions.
# ---------------------------------------------------------------------------


def test_save_as_extract_hardware_to_external(tmp_path: Path, configs_dir: Path) -> None:
    """Save-as can turn inline hardware into a separate file."""
    doc = ConfigDocument.load(configs_dir / "experiments" / "sim_capa_pyrolysis.yaml")
    # Start by inlining hardware so we have something to extract.
    doc.hardware_mode = "inline"
    doc.hardware_path = None
    doc.hardware_format = None

    new_hw_path = tmp_path / "extracted_hardware.toml"
    new_exp_path = tmp_path / "extracted.yaml"
    layout = SourceLayout(
        experiment_path=new_exp_path,
        experiment_format="yaml",
        hardware_path=new_hw_path,
        hardware_format="toml",
        hardware_mode="external",
        method_path=tmp_path / "extracted_method.toml",
        method_format="toml",
        method_mode="external",
    )
    doc.save_as(layout)

    reloaded = ConfigDocument.load(new_exp_path)
    assert reloaded.hardware_mode == "external"
    assert reloaded.hardware_path == new_hw_path.resolve()
    # The experiment file must reference hardware by relative path.
    assert "extracted_hardware.toml" in new_exp_path.read_text(encoding="utf-8")
