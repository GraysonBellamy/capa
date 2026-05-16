"""Cameras section + camera descriptors."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from capa.config import ConfigDocument
from capa.devices.registry import ADAPTERS, ensure_adapters_loaded
from capa.ui.tabs.setup_sections.cameras import (
    CamerasSection,
    _camera_descriptors,
    _human_disk_fill,
)
from capa.ui.tabs.setup_state import SetupDraft

REPO_ROOT = Path(__file__).resolve().parents[4]
WEBCAM_EXP = REPO_ROOT / "configs" / "experiments" / "webcam_real_freerun.yaml"


# ---------------------------------------------------------------------------
# Descriptor registration.
# ---------------------------------------------------------------------------


def test_webcam_descriptor_is_registered() -> None:
    ensure_adapters_loaded()
    assert "capa.devices.camera.webcam" in ADAPTERS
    descriptor = ADAPTERS["capa.devices.camera.webcam"]
    assert descriptor.family == "camera_visible"
    assert descriptor.params_model is not None
    assert "fps" in descriptor.default_params


def test_flir_ir_sim_descriptor_is_registered() -> None:
    ensure_adapters_loaded()
    assert "capa.devices.sim.flir_ir_sim" in ADAPTERS
    descriptor = ADAPTERS["capa.devices.sim.flir_ir_sim"]
    assert descriptor.family == "camera_ir"
    assert descriptor.params_model is not None


def test_camera_descriptors_helper_filters_to_camera_families() -> None:
    ensure_adapters_loaded()
    descriptors = _camera_descriptors()
    families = {d.family for d in descriptors}
    assert families <= {"camera_visible", "camera_ir"}
    assert len(descriptors) >= 2  # webcam + flir_ir_sim


# ---------------------------------------------------------------------------
# Disk-fill helper.
# ---------------------------------------------------------------------------


def test_disk_fill_renders_mb_per_minutes() -> None:
    # 4 MB/s × 30 min = 7200 MB.
    assert _human_disk_fill(4 * 1024 * 1024, 30 * 60) == "~7200.0 MB / 30 min"


# ---------------------------------------------------------------------------
# Section behaviour.
# ---------------------------------------------------------------------------


def _make_section(qtbot: Any) -> tuple[CamerasSection, SetupDraft]:
    document = ConfigDocument.load(WEBCAM_EXP)
    draft = SetupDraft(document=document)
    section = CamerasSection()
    qtbot.addWidget(section)
    section.set_draft(draft)
    return section, draft


def test_cameras_section_lists_cameras_from_fixture(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    rows = section._model.cameras()
    assert len(rows) >= 1
    assert rows[0]["adapter"] == "capa.devices.camera.webcam"


def test_cameras_section_payload_under_cameras_key(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    payload = section.payload()
    assert "cameras" in payload
    assert isinstance(payload["cameras"], list)


def test_cameras_section_add_camera_appends_row(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    descriptor = ADAPTERS["capa.devices.camera.webcam"]
    starting = len(section._model.cameras())
    section._on_add_camera(descriptor)
    assert len(section._model.cameras()) == starting + 1
    added = section._model.cameras()[-1]
    assert added["adapter"] == descriptor.id
    assert added["kind"] == "visible"


def test_cameras_section_edit_name_round_trips(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    section._table.selectRow(0)
    section._name_edit.setText("front_cam")
    section._on_name_changed("front_cam")
    assert section._model.cameras()[0]["name"] == "front_cam"


def test_cameras_section_estimated_bps_updates_disk_fill_label(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    section._table.selectRow(0)
    section._estimated_bps_edit.setText("8000000")
    section._on_estimated_bps_changed("8000000")
    # 8 MB/s × 30 min computed in MiB → ~13732.9 MB.
    assert "13732.9 MB" in section._disk_fill_label.text()
    assert "30 min" in section._disk_fill_label.text()


def test_cameras_section_remove_drops_row(qtbot: Any) -> None:
    section, _ = _make_section(qtbot)
    starting = len(section._model.cameras())
    section._table.selectRow(0)
    section._on_remove()
    assert len(section._model.cameras()) == starting - 1
