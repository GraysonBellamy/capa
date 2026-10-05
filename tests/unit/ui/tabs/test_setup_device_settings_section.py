"""Setup tab → Device settings section: forms per device, payload round
trip, edits, orphaned entries and Capture from devices."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QObject, Signal

from capa.config.problems import ConfigProblem
from capa.devices.alicat import AlicatStateSnapshot
from capa.devices.camera.base import WebcamStateSnapshot
from capa.devices.sartorius import SartoriusStateSnapshot
from capa.ui.tabs.setup import SetupTab
from capa.ui.tabs.setup_sections.device_settings import DeviceSettingsSection

REPO_ROOT = Path(__file__).resolve().parents[4]
SIM_CAPA_EXP = REPO_ROOT / "configs" / "experiments" / "sim_capa_pyrolysis.yaml"

DECLARED = {
    "purge_mfc": {"gas": "N2"},
    "balance": {"filter_mode": "very stable", "stability_range": "accurate"},
    "ir_cam0": {
        "temperature_range": {"min_c": 0.0, "max_c": 650.0},
        "emissivity": 0.95,
        "distance_m": 0.5,
    },
}


def _tab(qtbot: Any, controller: Any = None) -> tuple[SetupTab, DeviceSettingsSection]:
    tab = SetupTab(controller=controller)
    qtbot.addWidget(tab)
    tab.load_path(SIM_CAPA_EXP)
    section = tab._sections["device_settings"]
    assert isinstance(section, DeviceSettingsSection)
    return tab, section


def test_one_form_per_device_that_declares_settings(qtbot: Any) -> None:
    _, section = _tab(qtbot)
    # The heater (Watlow) and the NI-DAQ have no declarable settings.
    assert list(section._forms) == ["purge_mfc", "balance", "ir_cam0"]


def test_payload_round_trips_the_declared_settings(qtbot: Any) -> None:
    _, section = _tab(qtbot)
    assert section.payload() == {"device_settings": DECLARED}


def test_edits_flow_into_the_experiment_payload(qtbot: Any) -> None:
    tab, section = _tab(qtbot)
    _spec, form = section._forms["purge_mfc"]
    form._fields["gas"].set_value("Ar")
    _spec, balance = section._forms["balance"]
    balance._fields["stability_range"].set_value(None)  # untick Set
    form.valuesChanged.emit()

    settings = tab.draft.document.experiment_payload["device_settings"]
    assert settings["purge_mfc"] == {"gas": "Ar"}
    assert settings["balance"] == {"filter_mode": "very stable"}
    assert "device_settings" in tab.draft.dirty_sections


def test_a_device_with_nothing_set_is_left_out(qtbot: Any) -> None:
    _, section = _tab(qtbot)
    _spec, form = section._forms["purge_mfc"]
    form._fields["gas"].set_value(None)
    assert "purge_mfc" not in section.payload()["device_settings"]  # type: ignore[operator]


def test_settings_for_unknown_or_unsupported_devices_are_kept_until_removed(qtbot: Any) -> None:
    tab, section = _tab(qtbot)
    settings = tab.draft.document.experiment_payload["device_settings"]
    settings["heater"] = {"setpoint": 600}
    settings["old_mfc"] = {"gas": "Ar"}
    section.refresh()
    payload = section.payload()["device_settings"]
    assert payload["heater"] == {"setpoint": 600}  # type: ignore[index]
    assert payload["old_mfc"] == {"gas": "Ar"}  # type: ignore[index]
    labels = [w.text() for w in section._body.findChildren(type(section._status))]
    assert any("heater" in t and "no settings an experiment can declare" in t for t in labels)
    assert any("old_mfc" in t and "not a device or camera" in t for t in labels)

    section._remove_orphan("old_mfc")
    assert "old_mfc" not in tab.draft.document.experiment_payload["device_settings"]


def test_new_device_in_hardware_gets_a_form(qtbot: Any) -> None:
    tab, section = _tab(qtbot)
    tab.draft.document.hardware_payload["devices"].append(
        {"name": "o2_mfc", "adapter": "capa.devices.sim.alicat_sim"}
    )
    section.refresh()
    assert "o2_mfc" in section._forms


def _add_analyzer(tab: SetupTab, channel_map: dict[str, str]) -> None:
    tab.draft.document.hardware_payload["devices"].append(
        {
            "name": "analyzer",
            "adapter": "capa.devices.sim.fuji_sim",
            "params": {"channel_map": channel_map},
        }
    )


def test_an_analyzer_form_offers_only_the_gases_it_measures(qtbot: Any) -> None:
    tab, section = _tab(qtbot)
    _add_analyzer(tab, {"CH1": "co2", "CH2": "co", "CH3": "o2"})
    section.refresh()
    _spec, form = section._forms["analyzer"]
    gases = {name.split("_", 1)[0] for name in form._fields if name.endswith("_range")}
    assert gases == {"co2", "co", "o2"}
    assert "output_hold" in form._fields

    tab.draft.document.hardware_payload["devices"][-1]["params"]["channel_map"]["CH4"] = "ch4"
    section.refresh()
    _spec, form = section._forms["analyzer"]
    assert "ch4_range" in form._fields


def test_a_declared_setting_for_a_gas_the_analyzer_lacks_is_kept(qtbot: Any) -> None:
    tab, section = _tab(qtbot)
    _add_analyzer(tab, {"CH1": "co2", "CH2": "co", "CH3": "o2"})
    tab.draft.document.experiment_payload["device_settings"]["analyzer"] = {
        "ch4_response_time_s": 10
    }
    section.refresh()
    _spec, form = section._forms["analyzer"]
    assert "ch4_response_time_s" in form._fields
    assert "ch4_range" not in form._fields
    assert section.payload()["device_settings"]["analyzer"] == {  # type: ignore[index]
        "ch4_response_time_s": 10
    }


def test_problem_navigates_to_the_section(qtbot: Any) -> None:
    tab, _ = _tab(qtbot)
    tab._on_problem_activated(
        ConfigProblem(
            severity="error",
            code="device_settings.unknown_device",
            message="x",
            section="device_settings",
            path=("device_settings", "x"),
        )
    )
    assert tab._stack.currentWidget() is tab._section_panes["device_settings"]


# ---------------------------------------------------------------------------
# Capture from devices
# ---------------------------------------------------------------------------


class _Client:
    async def device_readback(self, name: str) -> object:
        if name == "purge_mfc":
            return AlicatStateSnapshot(gas="Ar", gas_list=("Air", "Ar"))
        if name == "balance":
            return SartoriusStateSnapshot(filter_mode="unstable", app_filter="filling")
        raise RuntimeError("camera busy")


class _Controller(QObject):
    hardware_ready_changed = Signal(bool)
    state_changed = Signal(object)
    config_load_finished = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.manual_client: Any = None
        self.state = None
        self.hardware_ready = False
        self.is_active = False


def test_capture_is_disabled_without_hardware(qtbot: Any) -> None:
    _, section = _tab(qtbot, _Controller())
    assert not section._capture_btn.isEnabled()


@pytest.mark.anyio
async def test_capture_fills_the_forms_from_the_devices(qtbot: Any) -> None:
    controller = _Controller()
    controller.manual_client = _Client()
    tab, section = _tab(qtbot, controller)
    section._sync_capture_enabled()
    assert section._capture_btn.isEnabled()

    section._capture_btn.click()
    for _ in range(50):
        await asyncio.sleep(0)
        if section._capture_btn.isEnabled():
            break

    settings = tab.draft.document.experiment_payload["device_settings"]
    assert settings["purge_mfc"] == {"gas": "Ar"}
    assert settings["balance"] == {"filter_mode": "unstable", "app_filter": "filling"}
    # The camera couldn't be read, so its declaration is untouched.
    assert settings["ir_cam0"] == DECLARED["ir_cam0"]
    assert "ir_cam0" in section._status.text()


def _add_webcam(tab: SetupTab, section: DeviceSettingsSection, declared: dict[str, Any]) -> None:
    tab.draft.document.hardware_payload["cameras"].append(
        {"name": "visible_cam0", "adapter": "capa.devices.camera.webcam", "kind": "visible"}
    )
    tab.draft.document.experiment_payload["device_settings"]["visible_cam0"] = declared
    section.refresh()


def test_a_webcam_gets_a_form(qtbot: Any) -> None:
    tab, section = _tab(qtbot)
    _add_webcam(tab, section, {"zoom": 265, "tilt": 3600})
    assert "visible_cam0" in section._forms
    assert section.payload()["device_settings"]["visible_cam0"] == {  # type: ignore[index]
        "zoom": 265,
        "tilt": 3600,
    }


class _ClientWithoutWebcamControls(_Client):
    async def device_readback(self, name: str) -> object:
        if name == "visible_cam0":
            return WebcamStateSnapshot(unavailable="camera controls are only available on Windows")
        return await super().device_readback(name)


@pytest.mark.anyio
async def test_a_capture_that_reads_nothing_keeps_the_declaration(qtbot: Any) -> None:
    controller = _Controller()
    controller.manual_client = _ClientWithoutWebcamControls()
    tab, section = _tab(qtbot, controller)
    _add_webcam(tab, section, {"zoom": 265})
    section._sync_capture_enabled()

    section._capture_btn.click()
    for _ in range(50):
        await asyncio.sleep(0)
        if section._capture_btn.isEnabled():
            break

    settings = tab.draft.document.experiment_payload["device_settings"]
    assert settings["visible_cam0"] == {"zoom": 265}
    assert "visible_cam0 (reported no settings)" in section._status.text()
