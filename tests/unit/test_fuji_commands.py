"""The command surface of :class:`capa.devices.fuji.FujiAdapter`.

Every write goes to fujilib's simulated analyzer (``MockAnalyzer``), which
accepts only the documented writes and fails the test on any other, so these
tests check what reaches the analyzer as well as what the adapter answers.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import fujilib
import pytest
from fujilib import (
    Analyzer,
    FujiConnectionError,
    FujiModbusTimeoutError,
    FujiWriteOutcomeUnknownError,
)
from fujilib.devices.settings import SETTINGS_FORMAT
from fujilib.testing import DEFAULT_ZPA_BANK, FaultKind, MockAnalyzer, mock_transport

from capa.core.errors import AdapterError
from capa.devices.adapter import Capability, CommandResult, DeviceCommand
from capa.devices.fuji import FujiAdapter
from capa.devices.records import DeviceEvent
from tests._adapter_helpers import make_start_ctx

pytestmark = pytest.mark.anyio

CHANNEL_MAP = {"CH1": "co2", "CH2": "co", "CH3": "o2"}
FC06 = 6
ZERO_KEY = 0x40
AUTO = fujilib.Capability.AUTO_CALIBRATION | fujilib.Capability.AUTO_ZERO


@asynccontextmanager
async def _adapter_on(
    mock: MockAnalyzer, *, options: fujilib.Capability = fujilib.Capability.NONE
) -> AsyncIterator[FujiAdapter]:
    async with mock_transport(mock) as (transport, _line):

        async def factory() -> Analyzer:
            return await fujilib.open_device(
                transport, channel_map=CHANNEL_MAP, timeout=0.25, options=options
            )

        adapter = FujiAdapter(
            name="analyzer", port="mock://zp", channel_map=CHANNEL_MAP, analyzer_factory=factory
        )
        await adapter.open()
        mock.clear()
        try:
            yield adapter
        finally:
            await adapter.close()


def _by_person(kind: str, **payload: Any) -> DeviceCommand:
    """A manual command a person confirmed at the interface."""
    return DeviceCommand(kind=kind, payload=payload, issued_by="op", confirmed_by="op")


def _by_run(kind: str, **payload: Any) -> DeviceCommand:
    """A command covered by a run's authorization only."""
    return DeviceCommand(kind=kind, payload=payload, issued_by="op", authorization_id="run-1")


def _writes(mock: MockAnalyzer) -> list[tuple[int, int | None, int | None]]:
    return [t for t in mock.transactions() if t[0] in (6, 16)]


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


def test_capabilities_follow_the_asserted_options() -> None:
    plain = FujiAdapter(name="a", port="COM8", channel_map=CHANNEL_MAP)
    assert Capability.HAS_PARAMETER_CONFIG in plain.capabilities
    assert Capability.HAS_INTERNAL_CAL not in plain.capabilities
    plumbed = FujiAdapter(name="a", port="COM8", channel_map=CHANNEL_MAP, options=["auto_zero"])
    assert Capability.HAS_INTERNAL_CAL in plumbed.capabilities


# ---------------------------------------------------------------------------
# Settings: PERSISTENT, either authorization
# ---------------------------------------------------------------------------


class TestSettings:
    @pytest.mark.parametrize("build", [_by_person, _by_run])
    async def test_a_setting_is_written_read_back_and_cached(self, build: Any) -> None:
        mock = MockAnalyzer(DEFAULT_ZPA_BANK)
        async with _adapter_on(mock) as adapter:
            result = await adapter.command(build("set_response_time", target="o2", seconds=10))
            snap = await adapter.snapshot()
        assert result.accepted
        assert result.detail == "O2 response time: 15 s -> 10 s"
        assert mock.register("response_time.o2") == (10,)
        assert len(_writes(mock)) == 1
        # The cached settings are read again, so the next snapshot shows the change.
        assert snap.fields["response_time_o2_s"] == 10

    async def test_every_setter_reaches_its_register(self) -> None:
        mock = MockAnalyzer(DEFAULT_ZPA_BANK)
        async with _adapter_on(mock) as adapter:
            results = [
                await adapter.set_response_time("CH1", 20, issued_by="op", confirmed_by="op"),
                await adapter.set_output_hold(True, issued_by="op", confirmed_by="op"),
                await adapter.set_hold_mode("setting", issued_by="op", authorization_id="run-1"),
                await adapter.set_hold_value("CH3", 40, issued_by="op", confirmed_by="op"),
                await adapter.set_range_method("CH3", "manual", issued_by="op", confirmed_by="op"),
                await adapter.set_range("CH3", 2, issued_by="op", confirmed_by="op"),
            ]
        assert [r.accepted for r in results] == [True] * 6, [r.detail for r in results]
        assert mock.register("response_time.ndir1") == (20,)
        assert mock.register("output_hold.enabled") == (1,)
        assert mock.register("hold.mode") == (1,)
        assert mock.register("hold.ch3.value") == (40,)
        assert mock.register("range.ch3.selected") == (1,)

    async def test_write_parameter_takes_its_tier_from_the_register(self) -> None:
        mock = MockAnalyzer(DEFAULT_ZPA_BANK)
        async with _adapter_on(mock) as adapter:
            persistent = await adapter.command(
                _by_run("write_parameter", name="response_time.o2", value=12)
            )
            dangerous = await adapter.command(
                _by_run(
                    "write_parameter",
                    name="calibration_gas.ch3.range1.span",
                    value=20.0,
                    unit="vol%",
                )
            )
            writes_after_refusal = len(_writes(mock))
            confirmed = await adapter.command(
                _by_person(
                    "write_parameter",
                    name="calibration_gas.ch3.range1.span",
                    value=20.0,
                    unit="vol%",
                )
            )
            unknown = await adapter.command(_by_person("write_parameter", name="nope", value=1))
        assert persistent.accepted
        assert not dangerous.accepted
        assert "person's confirmation" in dangerous.detail
        assert writes_after_refusal == 1  # only the persistent write went out
        assert confirmed.accepted, confirmed.detail
        assert not unknown.accepted
        assert "unknown register" in unknown.detail

    async def test_apply_settings(self) -> None:
        mock = MockAnalyzer(DEFAULT_ZPA_BANK)
        document = {"format": SETTINGS_FORMAT, "settings": {"response_time.ndir2": 20}}
        async with _adapter_on(mock) as adapter:
            first = await adapter.command(_by_run("apply_settings", document=document))
            again = await adapter.command(_by_run("apply_settings", document=document))
            bad = await adapter.command(_by_run("apply_settings", document="response_time"))
        assert first.accepted, first.detail
        assert first.detail == "apply_settings: wrote 1 (response_time.ndir2)"
        assert mock.register("response_time.ndir2") == (20,)
        assert again.accepted
        assert again.detail == "apply_settings: nothing to change"
        assert not bad.accepted

    async def test_apply_settings_that_fails_part_way_says_what_was_written(self) -> None:
        mock = MockAnalyzer(DEFAULT_ZPA_BANK)
        document = {
            "format": SETTINGS_FORMAT,
            "settings": {"response_time.ndir2": 20, "response_time.o2": 10},
        }
        writes: list[int | None] = []

        def second_write(request: Any) -> bool:
            if request.function != FC06:
                return False
            writes.append(request.address)
            return len(writes) == 2

        async with _adapter_on(mock) as adapter:
            await adapter.start(make_start_ctx())
            # The analyzer acknowledges the second write and does not keep it.
            _ = mock.inject(FaultKind.IGNORE, when=second_write)
            result = await adapter.command(_by_run("apply_settings", document=document))
            queued = list(adapter._events)
            snap = await adapter.snapshot()
        assert not result.accepted
        assert "outcome not verified" in result.detail
        assert "applied 1 settings, then response_time." in result.detail
        assert "not attempted: none" in result.detail
        # The error's context is given once, not once per layer that reported it.
        assert result.detail.count("command=") <= 1
        # The write that did go through is reported and cached; the other is an error.
        assert [(e.kind, e.severity) for e in queued] == [
            ("setting_changed", "info"),
            ("write_uncertain", "error"),
        ]
        kept = queued[0].metadata["setting"]
        cached = {
            "response_time.ndir2": snap.fields["response_time_ndir2_s"],
            "response_time.o2": snap.fields["response_time_o2_s"],
        }
        assert cached[kept] == document["settings"][kept]

    async def test_apply_settings_keeps_to_the_tier_the_authorization_allows(self) -> None:
        mock = MockAnalyzer(DEFAULT_ZPA_BANK)
        document = {
            "format": SETTINGS_FORMAT,
            "settings": {
                "response_time.ndir2": 20,
                "calibration_gas.ch3.range1.span": {"value": 20.0, "unit": "vol%"},
            },
        }
        async with _adapter_on(mock) as adapter:
            by_run = await adapter.command(_by_run("apply_settings", document=document))
            written = len(_writes(mock))
            by_person = await adapter.command(_by_person("apply_settings", document=document))
        # A document with a DANGEROUS write is refused whole: nothing of it is written.
        assert not by_run.accepted
        assert "DANGEROUS" in by_run.detail
        assert written == 0
        assert by_person.accepted, by_person.detail
        assert mock.register("response_time.ndir2") == (20,)


# ---------------------------------------------------------------------------
# DANGEROUS and STATEFUL verbs
# ---------------------------------------------------------------------------


class TestGuardedVerbs:
    async def test_a_calibration_gas_needs_a_person(self) -> None:
        mock = MockAnalyzer(DEFAULT_ZPA_BANK)
        async with _adapter_on(mock) as adapter:
            by_run = await adapter.set_calibration_gas(
                "CH3", 1, "span", 20.0, unit="vol%", issued_by="op", authorization_id="run-1"
            )
            assert mock.transactions() == []  # refused before anything was sent
            by_person = await adapter.set_calibration_gas(
                "CH3", 1, "span", 20.0, unit="vol%", issued_by="op", confirmed_by="op"
            )
            wrong_unit = await adapter.set_calibration_gas(
                "CH3", 1, "span", 20.0, unit="ppm", issued_by="op", confirmed_by="op"
            )
        assert not by_run.accepted
        assert "person's confirmation" in by_run.detail
        assert by_person.accepted, by_person.detail
        assert by_person.detail == "O2 0–21 vol% span gas: 20.95 vol% -> 20 vol%"
        assert not wrong_unit.accepted

    async def test_return_to_measurement(self) -> None:
        mock = MockAnalyzer(DEFAULT_ZPA_BANK)
        async with _adapter_on(mock) as adapter:
            result = await adapter.return_to_measurement(issued_by="op", authorization_id="run-1")
        assert result.accepted
        assert result.detail == "return_to_measurement: done"

    async def test_auto_calibration_needs_the_option_and_a_person(self) -> None:
        mock = MockAnalyzer(DEFAULT_ZPA_BANK)
        async with _adapter_on(mock) as adapter:
            by_run = await adapter.command(_by_run("start_auto_calibration"))
            unplumbed = await adapter.command(_by_person("start_auto_calibration"))
            assert _writes(mock) == []
        assert not by_run.accepted
        assert "person's confirmation" in by_run.detail
        # The bench analyzer has no calibration valves: fujilib refuses the command.
        assert not unplumbed.accepted
        assert "refused" in unplumbed.detail

    async def test_auto_calibration_with_the_option_asserted(self) -> None:
        mock = MockAnalyzer(dataclasses.replace(DEFAULT_ZPA_BANK, time_scale=0.0001))
        async with _adapter_on(mock, options=AUTO) as adapter:
            started = await adapter.command(_by_person("start_auto_zero_calibration"))
        assert started.accepted, started.detail
        assert started.detail.startswith("start_auto_zero_calibration: ")
        assert len(_writes(mock)) == 1


# ---------------------------------------------------------------------------
# Refusals, uncertain outcomes, failures
# ---------------------------------------------------------------------------


class TestOutcomes:
    @pytest.mark.parametrize(
        ("kind", "payload", "match"),
        [
            ("set_response_time", {"target": "o2"}, "payload is missing seconds"),
            ("set_response_time", {"target": "o2", "seconds": 99}, "outside"),
            ("set_response_time", {"target": "CH9", "seconds": 5}, "refused"),
            ("set_hold_mode", {"mode": "sideways"}, "refused"),
            ("set_range", {"channel": "CH3", "range": 3}, "range_number"),
            ("set_calibration_gas", {"channel": "CH3"}, "payload is missing"),
        ],
    )
    async def test_a_refusal_is_a_result_and_sends_no_write(
        self, kind: str, payload: dict[str, Any], match: str
    ) -> None:
        mock = MockAnalyzer(DEFAULT_ZPA_BANK)
        async with _adapter_on(mock) as adapter:
            result = await adapter.command(_by_person(kind, **payload))
        assert isinstance(result, CommandResult)
        assert not result.accepted
        assert match in result.detail
        assert _writes(mock) == []

    async def test_the_analyzer_being_calibrated_is_a_refusal(self) -> None:
        mock = MockAnalyzer(dataclasses.replace(DEFAULT_ZPA_BANK, panel_channels=(1, 2, 3)))
        async with _adapter_on(mock) as adapter:
            mock.press(ZERO_KEY)  # an operator starts a zero at the front panel
            result = await adapter.command(_by_person("set_output_hold", enabled=True))
        assert not result.accepted
        assert _writes(mock) == []

    async def test_a_write_the_analyzer_did_not_keep_is_not_accepted(self) -> None:
        mock = MockAnalyzer(DEFAULT_ZPA_BANK)
        async with _adapter_on(mock) as adapter:
            await adapter.start(make_start_ctx())
            mock.inject(FaultKind.IGNORE, when=lambda request: request.function == FC06)
            result = await adapter.command(_by_person("set_response_time", target="o2", seconds=9))
            queued = list(adapter._events)
        assert not result.accepted
        assert "outcome not verified" in result.detail
        assert mock.register("response_time.o2") == (15,)
        # During a run the uncertainty is also an error event in the bundle.
        assert [(e.kind, e.severity) for e in queued] == [("write_uncertain", "error")]
        assert queued[0].metadata == {"command": "set_response_time", "outcome": "not verified"}

    async def test_an_unknown_outcome_is_not_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockAnalyzer(DEFAULT_ZPA_BANK)
        async with _adapter_on(mock) as adapter:
            analyzer = adapter._analyzer
            assert analyzer is not None

            async def lost(*args: Any, **kwargs: Any) -> Any:
                del args, kwargs
                raise FujiWriteOutcomeUnknownError("neither the reply nor the read-back arrived")

            monkeypatch.setattr(analyzer, "set_output_hold", lost)
            result = await adapter.command(_by_person("set_output_hold", enabled=True))
            # Outside a run there is no stream to carry an event.
            assert adapter._events == []
        assert not result.accepted
        assert "outcome unknown" in result.detail

    @pytest.mark.parametrize(
        "error", [FujiModbusTimeoutError("no reply"), FujiConnectionError("the port is gone")]
    )
    async def test_a_dead_line_is_an_adapter_error(
        self, monkeypatch: pytest.MonkeyPatch, error: fujilib.FujiError
    ) -> None:
        mock = MockAnalyzer(DEFAULT_ZPA_BANK)
        async with _adapter_on(mock) as adapter:
            analyzer = adapter._analyzer
            assert analyzer is not None

            async def dead(*args: Any, **kwargs: Any) -> Any:
                del args, kwargs
                raise error

            monkeypatch.setattr(analyzer, "set_output_hold", dead)
            with pytest.raises(AdapterError, match="command 'set_output_hold' failed"):
                await adapter.command(_by_person("set_output_hold", enabled=True))

    async def test_a_change_made_during_a_run_is_an_event(self) -> None:
        mock = MockAnalyzer(DEFAULT_ZPA_BANK)
        async with _adapter_on(mock) as adapter:
            await adapter.start(make_start_ctx())
            result = await adapter.command(_by_run("set_response_time", target="o2", seconds=10))
            queued = list(adapter._events)
        assert result.accepted
        assert len(queued) == 1
        event = queued[0]
        assert isinstance(event, DeviceEvent)
        assert (event.kind, event.severity) == ("setting_changed", "info")
        assert event.message == "O2 response time: 15 s -> 10 s"
        assert event.metadata == {"setting": "response_time.o2", "previous": 15, "written": 10}


# ---------------------------------------------------------------------------
# Read-only helpers
# ---------------------------------------------------------------------------


class TestReads:
    async def test_read_settings_and_plan(self) -> None:
        mock = MockAnalyzer(DEFAULT_ZPA_BANK)
        async with _adapter_on(mock) as adapter:
            settings = await adapter.read_settings()
            plan = await adapter.plan_calibration("CH3", "span")
            assert _writes(mock) == []
        assert settings["response_time.o2"].value == 15
        assert plan.kind.value == "span"
        assert [c.value for c in plan.channels] == ["CH3"]

    async def test_reads_need_an_open_adapter(self) -> None:
        adapter = FujiAdapter(name="analyzer", port="COM8", channel_map=CHANNEL_MAP)
        with pytest.raises(AdapterError, match="requires open"):
            await adapter.read_settings()
        with pytest.raises(AdapterError, match="requires open"):
            await adapter.plan_calibration("CH3", "zero")

    async def test_a_failed_read_is_an_adapter_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock = MockAnalyzer(DEFAULT_ZPA_BANK)
        async with _adapter_on(mock) as adapter:
            analyzer = adapter._analyzer
            assert analyzer is not None

            async def dead(*args: Any, **kwargs: Any) -> Any:
                del args, kwargs
                raise FujiModbusTimeoutError("no reply")

            monkeypatch.setattr(analyzer, "read_settings", dead)
            monkeypatch.setattr(analyzer, "plan_manual_calibration", dead)
            with pytest.raises(AdapterError, match="read_settings failed"):
                await adapter.read_settings()
            with pytest.raises(AdapterError, match="plan_calibration failed"):
                await adapter.plan_calibration("CH3", "zero")
