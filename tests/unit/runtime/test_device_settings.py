""":mod:`capa.runtime.device_settings` — plan and apply an experiment's
``device_settings`` against the simulators, in memory and through a real
:class:`WorkerPool`."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from capa.core.clock import RunClock
from capa.devices.adapter import CommandResult, DeviceCommand
from capa.devices.camera.base import CameraSpec
from capa.devices.sim._signals import Constant
from capa.devices.sim.alicat_sim import AlicatSim
from capa.devices.sim.flir_ir_sim import FlirIrSim
from capa.devices.sim.sartorius_sim import SartoriusSim
from capa.experiment.config import ExperimentConfig
from capa.runtime.device_settings import (
    ChangeResult,
    Outcome,
    apply_device_settings,
    plan_device_settings,
    pool_readback,
)
from capa.runtime.dispatch import PoolDispatcher
from capa.runtime.pool import WorkerPool

pytestmark = pytest.mark.anyio

SETTINGS: dict[str, dict[str, Any]] = {
    "purge_mfc": {"gas": "N2"},
    "balance": {"filter_mode": "very stable", "stability_range": "accurate"},
    "ir_cam0": {
        "temperature_range": {"min_c": 0.0, "max_c": 650.0},
        "emissivity": 0.95,
        "distance_m": 0.5,
    },
}


@pytest.fixture
def config(configs_dir: Path) -> ExperimentConfig:
    cfg = ExperimentConfig.load(configs_dir / "experiments" / "sim_capa_pyrolysis.yaml")
    return cfg.model_copy(update={"device_settings": SETTINGS})


def _with(config: ExperimentConfig, settings: dict[str, dict[str, Any]]) -> ExperimentConfig:
    return config.model_copy(update={"device_settings": settings})


class _Rig:
    """Readback and dispatch over in-process simulators — the pool's
    surface without its worker threads."""

    def __init__(self) -> None:
        spec = CameraSpec(name="ir_cam0", adapter="capa.devices.sim.flir_ir_sim", kind="ir")
        self.devices: dict[str, Any] = {
            "purge_mfc": AlicatSim(name="purge_mfc"),
            "balance": SartoriusSim(name="balance", mass_signal=Constant(5.0)),
            "ir_cam0": FlirIrSim(spec=spec, clock=RunClock.now()),
        }
        self.sent: list[tuple[str, DeviceCommand]] = []

    async def open(self) -> None:
        for device in self.devices.values():
            await device.open()

    async def readback(self, name: str) -> Any:
        return await self.devices[name].read_state_snapshot()

    async def dispatch(self, name: str, cmd: DeviceCommand) -> CommandResult:
        self.sent.append((name, cmd))
        result: CommandResult = await self.devices[name].command(cmd)
        return result


@pytest.fixture
async def rig() -> _Rig:
    rig = _Rig()
    await rig.open()
    return rig


def _result(accepted: bool, detail: str = "") -> CommandResult:
    return CommandResult(accepted=accepted, detail=detail, t_mono_ns=0, t_utc=datetime.now(UTC))


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


class TestPlan:
    async def test_compares_each_declared_device(self, config: ExperimentConfig, rig: _Rig) -> None:
        plan = await plan_device_settings(config, rig.readback)
        assert [d.name for d in plan.devices] == ["purge_mfc", "balance", "ir_cam0"]
        changes = {
            d.name: [(c.field, c.current, c.desired) for c in d.changes] for d in plan.devices
        }
        assert changes == {
            "purge_mfc": [("gas", "Air", "N2")],
            "balance": [
                ("filter_mode", "stable", "very stable"),
                ("stability_range", "very accurate", "accurate"),
            ],
            # The simulator starts at emissivity 0.95, so only two differ.
            "ir_cam0": [
                ("temperature_range", "-20 to 120 °C", "0 to 650 °C"),
                ("distance_m", "1 m", "0.5 m"),
            ],
        }
        assert plan.needs_attention
        assert plan.change_count == 5
        assert plan.observed()["purge_mfc"] == {"gas": "Air"}

    async def test_nothing_declared_reads_nothing(self, config: ExperimentConfig) -> None:
        async def readback(name: str) -> Any:
            raise AssertionError("no device should be read")

        plan = await plan_device_settings(_with(config, {}), readback)
        assert plan.devices == ()
        assert not plan.needs_attention

    async def test_bad_entries_become_errors_not_exceptions(
        self, config: ExperimentConfig, rig: _Rig
    ) -> None:
        settings = {
            "purge_mcf": {"gas": "N2"},
            "heater": {"setpoint": 600},
            "purge_mfc": {"gas": "N3"},
        }
        plan = await plan_device_settings(_with(config, settings), rig.readback)
        errors = {d.name: d.error for d in plan.devices}
        assert "not a device or camera" in (errors["purge_mcf"] or "")
        assert "no settings an experiment can declare" in (errors["heater"] or "")
        assert "did you mean" in (errors["purge_mfc"] or "")
        assert rig.sent == []

    @pytest.mark.parametrize(
        ("snapshot", "expected"),
        [
            (None, "didn't report its settings"),
            ("not a snapshot", "unexpected read-back str"),
        ],
    )
    async def test_unusable_readback_is_an_error(
        self, config: ExperimentConfig, snapshot: object, expected: str
    ) -> None:
        async def readback(name: str) -> object:
            return snapshot

        plan = await plan_device_settings(_with(config, {"purge_mfc": {"gas": "N2"}}), readback)
        (device,) = plan.devices
        assert expected in (device.error or "")
        assert device.observed is None

    async def test_failed_or_hung_readback_is_an_error(self, config: ExperimentConfig) -> None:
        async def readback(name: str) -> object:
            if name == "purge_mfc":
                raise RuntimeError("port gone")
            await asyncio.sleep(10)
            return None

        settings = {"purge_mfc": {"gas": "N2"}, "balance": {"filter_mode": "stable"}}
        plan = await plan_device_settings(_with(config, settings), readback, timeout_s=0.05)
        errors = {d.name: d.error for d in plan.devices}
        assert errors == {
            "purge_mfc": "read-back failed: port gone",
            "balance": "no read-back within 0.05 s",
        }


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


class TestApply:
    async def test_applies_everything_and_verifies(
        self, config: ExperimentConfig, rig: _Rig
    ) -> None:
        plan = await plan_device_settings(config, rig.readback)
        progress: list[ChangeResult] = []
        report = await apply_device_settings(
            plan,
            None,
            dispatch=rig.dispatch,
            readback=rig.readback,
            operator_id="abr",
            on_progress=progress.append,
        )
        assert report.ok
        assert [r.outcome for r in report.results] == [Outcome.VERIFIED] * 5
        assert progress == list(report.results)
        assert not report.plan_after.needs_attention
        assert report.plan_after.observed()["ir_cam0"]["distance_m"] == 0.5
        # Manual overrides confirmed by the operator; the gas isn't saved.
        for _, cmd in rig.sent:
            assert (cmd.issued_by, cmd.confirmed_by, cmd.authorization_id) == ("abr", "abr", None)
        gas_cmd = next(cmd for _, cmd in rig.sent if cmd.kind == "set_gas")
        assert gas_cmd.payload == {"gas": "N2", "save": False}
        # The range goes before the radiometric parameters.
        ir_kinds = [cmd.kind for name, cmd in rig.sent if name == "ir_cam0"]
        assert ir_kinds == ["set_temperature_range", "set_distance_m"]

    async def test_sends_only_the_selected_changes(
        self, config: ExperimentConfig, rig: _Rig
    ) -> None:
        plan = await plan_device_settings(config, rig.readback)
        report = await apply_device_settings(
            plan,
            {("balance", "stability_range")},
            dispatch=rig.dispatch,
            readback=rig.readback,
            operator_id="abr",
        )
        assert [(r.device, r.change.field, r.outcome) for r in report.results] == [
            ("balance", "stability_range", Outcome.VERIFIED)
        ]
        remaining = {d.name: [c.field for c in d.changes] for d in report.plan_after.devices}
        assert remaining == {
            "purge_mfc": ["gas"],
            "balance": ["filter_mode"],
            "ir_cam0": ["temperature_range", "distance_m"],
        }

    async def test_refused_failed_and_hung_commands(
        self, config: ExperimentConfig, rig: _Rig
    ) -> None:
        async def dispatch(name: str, cmd: DeviceCommand) -> CommandResult:
            if cmd.kind == "set_filter_mode":
                return _result(False, "menu locked")
            if cmd.kind == "set_stability_range":
                raise RuntimeError("timeout on the wire")
            await asyncio.sleep(10)
            return _result(True)

        balance_only = _with(
            config, {"balance": SETTINGS["balance"], "purge_mfc": SETTINGS["purge_mfc"]}
        )
        plan = await plan_device_settings(balance_only, rig.readback)
        report = await apply_device_settings(
            plan,
            None,
            dispatch=dispatch,
            readback=rig.readback,
            operator_id="abr",
            timeout_s=0.05,
        )
        assert not report.ok
        assert [(r.change.field, r.outcome, r.detail) for r in report.results] == [
            ("filter_mode", Outcome.REFUSED, "menu locked"),
            ("stability_range", Outcome.FAILED, "timeout on the wire"),
            ("gas", Outcome.FAILED, "no reply within 0.05 s"),
        ]

    async def test_accepted_but_unchanged_still_differs(
        self, config: ExperimentConfig, rig: _Rig
    ) -> None:
        async def dispatch(name: str, cmd: DeviceCommand) -> CommandResult:
            return _result(True, "ack")  # accepted, but nothing changes

        plan = await plan_device_settings(_with(config, {"purge_mfc": {"gas": "N2"}}), rig.readback)
        report = await apply_device_settings(
            plan, None, dispatch=dispatch, readback=rig.readback, operator_id="abr"
        )
        (result,) = report.results
        assert result.outcome is Outcome.STILL_DIFFERS
        assert result.detail == "the device reports Air"
        assert not report.ok

    async def test_accepted_but_unreported_is_unverified(self, config: ExperimentConfig) -> None:
        from capa.devices.alicat import AlicatStateSnapshot

        async def readback(name: str) -> AlicatStateSnapshot:
            return AlicatStateSnapshot()  # legacy firmware: no gas query

        async def dispatch(name: str, cmd: DeviceCommand) -> CommandResult:
            return _result(True)

        plan = await plan_device_settings(_with(config, {"purge_mfc": {"gas": "N2"}}), readback)
        report = await apply_device_settings(
            plan, None, dispatch=dispatch, readback=readback, operator_id="abr"
        )
        assert [r.outcome for r in report.results] == [Outcome.UNVERIFIED]
        assert report.ok


# ---------------------------------------------------------------------------
# Through a real worker pool
# ---------------------------------------------------------------------------


async def test_through_a_worker_pool(config: ExperimentConfig) -> None:
    pool = WorkerPool.from_config(config)
    await pool.open()
    try:
        readback = pool_readback(pool)
        plan = await plan_device_settings(config, readback)
        assert plan.change_count == 5
        report = await apply_device_settings(
            plan,
            None,
            dispatch=PoolDispatcher(pool).dispatch,
            readback=readback,
            operator_id="abr",
        )
        assert report.ok, report.results
        again = await plan_device_settings(config, readback)
        assert not again.needs_attention
    finally:
        await pool.close()
