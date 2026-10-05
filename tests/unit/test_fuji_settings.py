""":mod:`capa.devices.fuji_settings` — what an experiment can declare for a
Fuji analyzer under ``device_settings:``, planned and applied against the
simulator through :mod:`capa.runtime.device_settings`."""

from __future__ import annotations

from typing import Any

import pytest
from fujilib import Gas
from pydantic import ValidationError

from capa.devices.adapter import CommandResult, DeviceCommand
from capa.devices.fuji import FujiStateSnapshot
from capa.devices.fuji_settings import FUJI_SETTINGS, GASES, FujiSettings
from capa.devices.registry import ensure_adapters_loaded, get_descriptor
from capa.devices.settings import capture_settings, plan_settings
from capa.devices.sim.fuji_sim import FujiSim
from capa.experiment.config import DeviceConfig, ExperimentConfig
from capa.runtime.device_settings import Outcome, apply_device_settings, plan_device_settings
from tests.unit.test_manual_control_cards import _make_config

pytestmark = pytest.mark.anyio

SIM = "capa.devices.sim.fuji_sim"


async def _snapshot(sim: FujiSim) -> FujiStateSnapshot:
    snapshot = await sim.read_state_snapshot()
    assert snapshot is not None
    return snapshot


async def _opened(**params: Any) -> FujiSim:
    sim = FujiSim(name="analyzer", **params)
    await sim.open()
    return sim


def _config(settings: dict[str, Any]) -> ExperimentConfig:
    config = _make_config((DeviceConfig(name="analyzer", adapter=SIM, params={}),))
    return config.model_copy(update={"device_settings": {"analyzer": settings}})


class _Rig:
    """Readback and dispatch over the simulator in process."""

    def __init__(self, sim: FujiSim) -> None:
        self.sim = sim
        self.sent: list[DeviceCommand] = []

    async def readback(self, name: str) -> Any:
        assert name == "analyzer"
        return await self.sim.read_state_snapshot()

    async def dispatch(self, name: str, cmd: DeviceCommand) -> CommandResult:
        assert name == "analyzer"
        self.sent.append(cmd)
        return await self.sim.command(cmd)


def test_every_gas_a_channel_map_can_assert_has_fields() -> None:
    assert {gas for gas, _group in GASES} == {g.value for g in Gas if g is not Gas.UNKNOWN}
    for gas, _group in GASES:
        for suffix in ("response_time_s", "range_method", "range"):
            assert f"{gas}_{suffix}" in FujiSettings.model_fields


def test_both_adapters_declare_the_settings() -> None:
    ensure_adapters_loaded()
    for adapter in ("capa.devices.fuji", SIM):
        descriptor = get_descriptor(adapter)
        assert descriptor is not None
        assert descriptor.settings is FUJI_SETTINGS


@pytest.mark.parametrize(
    "entry",
    [
        {"o2_range_method": "auto", "o2_range": {"full_scale": 25, "unit": "vol%"}},
        {"co2_response_time_s": 61},
        {"o2_range": {"full_scale": 25, "unit": "percent"}},
        {"hold_mode": "last_value"},
        {"ch1_range": 2},
    ],
)
def test_what_the_model_refuses(entry: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        FujiSettings.model_validate(entry)


async def test_capture_names_everything_by_gas_and_span() -> None:
    sim = await _opened()
    captured = capture_settings(FUJI_SETTINGS, await _snapshot(sim))
    assert captured == {
        "output_hold": False,
        "hold_mode": "last reading",
        "co2_response_time_s": 15,
        "co2_range_method": "manual",
        "co2_range": {"full_scale": 10.0, "unit": "vol%"},
        "co_response_time_s": 15,
        "co_range_method": "manual",
        "co_range": {"full_scale": 1.0, "unit": "vol%"},
        "o2_response_time_s": 15,
        "o2_range_method": "manual",
        "o2_range": {"full_scale": 25.0, "unit": "vol%"},
    }
    # What was captured validates, and plans nothing against the same analyzer.
    desired = FujiSettings.model_validate(captured)
    assert plan_settings(FUJI_SETTINGS, desired, await _snapshot(sim)) == ((), ())


async def test_a_gas_or_range_the_analyzer_lacks_is_an_issue() -> None:
    sim = await _opened()
    desired = FujiSettings.model_validate(
        {"nox_response_time_s": 10, "o2_range": {"full_scale": 5, "unit": "vol%"}}
    )
    changes, issues = plan_settings(FUJI_SETTINGS, desired, await _snapshot(sim))
    assert changes == ()
    assert {issue.label: issue.message for issue in issues} == {
        "NOx response time": "the channel map asserts no measured NOx channel",
        "O2 range": "no O2 range is 0–5 vol% (the analyzer offers: 0–25 vol%; 0–10 vol%)",
    }


async def test_declared_settings_are_applied_and_verified() -> None:
    rig = _Rig(await _opened())
    config = _config(
        {
            "output_hold": True,
            "hold_mode": "preset value",
            "co2_response_time_s": 20,
            "o2_range_method": "manual",
            "o2_range": {"full_scale": 10, "unit": "vol%"},
        }
    )
    plan = await plan_device_settings(config, rig.readback)
    (device,) = plan.devices
    assert [(c.label, c.current, c.desired) for c in device.changes] == [
        ("Output hold", "off", "on"),
        ("Hold mode", "last reading", "preset value"),
        ("CO2 response time", "15 s", "20 s"),
        ("O2 range", "0–25 vol%", "0–10 vol%"),
    ]
    report = await apply_device_settings(
        plan, None, dispatch=rig.dispatch, readback=rig.readback, operator_id="abr"
    )
    assert report.ok
    assert [r.outcome for r in report.results] == [Outcome.VERIFIED] * 4
    assert [(cmd.kind, cmd.payload) for cmd in rig.sent] == [
        ("set_output_hold", {"enabled": True}),
        ("set_hold_mode", {"mode": "setting"}),
        ("set_response_time", {"target": "CH1", "seconds": 20}),
        ("set_range", {"channel": "CH3", "range": 2}),
    ]
    assert not report.plan_after.needs_attention


async def test_a_range_method_goes_before_the_range() -> None:
    sim = await _opened()
    rig = _Rig(sim)
    _ = await sim.command(
        DeviceCommand(
            kind="set_range_method",
            payload={"channel": "CH3", "method": "auto"},
            issued_by="op",
            confirmed_by="op",
        )
    )
    config = _config({"o2_range_method": "manual", "o2_range": {"full_scale": 10, "unit": "vol%"}})
    plan = await plan_device_settings(config, rig.readback)
    report = await apply_device_settings(
        plan, None, dispatch=rig.dispatch, readback=rig.readback, operator_id="abr"
    )
    assert report.ok, [r.detail for r in report.results]
    assert [cmd.kind for cmd in rig.sent] == ["set_range_method", "set_range"]
