"""Test alias — the recording stub with "sartorius" in the import path so
:func:`capa.ui.manual.cards.balance.is_balance_device` recognises it, plus
the balance read-back and internal calibration the manual card consumes."""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime
from typing import cast

from capa.devices.adapter import CommandResult, DeviceCommand
from capa.devices.registry import AdapterDescriptor, register
from capa.devices.sartorius import (
    AdjustmentOutcome,
    BalanceAdjustmentError,
    SartoriusStateSnapshot,
)
from tests.fixtures.stub_recording_adapter import StubRecordingAdapter as _Base

CAL_DONE_DETAIL = "internal calibration done in 24 s at 22.4 °C"
"""What the stub's ``internal_adjust`` reports when it completes."""


class StubSartorius(_Base):
    """:class:`StubRecordingAdapter` that lives in this module so the
    substring ``sartorius`` in the import path satisfies
    :func:`capa.ui.manual.cards.balance.is_balance_device`.

    Extra ``params`` (``filter_mode``, ``auto_zero``, ``display_unit``,
    ``tare_behavior``, ``cal_temperature_c``, ``cal_on_record``) set what
    :meth:`read_state_snapshot` reports. ``adjust_s`` is how long
    ``internal_adjust`` runs (an ``abort_adjustment`` ends it early, as
    aborted) and ``adjust_outcome`` makes it end that way instead of
    completing.
    """

    def __init__(
        self,
        *,
        name: str,
        capabilities: list[str] | None = None,
        accept_kinds: list[str] | None = None,
        filter_mode: str | None = "stable",
        auto_zero: str | None = "on",
        display_unit: str | None = "g",
        tare_behavior: str | None = "with stability",
        cal_temperature_c: float | None = 22.4,
        cal_on_record: bool | None = True,
        adjust_s: float = 0.0,
        adjust_outcome: str | None = None,
    ) -> None:
        super().__init__(name=name, capabilities=capabilities, accept_kinds=accept_kinds)
        self.state = SartoriusStateSnapshot(
            filter_mode=filter_mode,
            auto_zero=auto_zero,
            display_unit=display_unit,
            tare_behavior=tare_behavior,
            cal_temperature_c=cal_temperature_c,
            cal_on_record=cal_on_record,
        )
        self.readback_count = 0
        self._adjust_s = adjust_s
        self._adjust_outcome = cast("AdjustmentOutcome | None", adjust_outcome)
        self._abort: asyncio.Event | None = None

    async def read_state_snapshot(self) -> SartoriusStateSnapshot:
        self.readback_count += 1
        return self.state

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        if cmd.kind == "internal_adjust":
            self.commands_received.append(cmd)
            return await self._calibrate()
        if cmd.kind == "abort_adjustment" and self._abort is not None:
            self._abort.set()
        return await super().command(cmd)

    async def _calibrate(self) -> CommandResult:
        abort = self._abort = asyncio.Event()
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(abort.wait(), self._adjust_s)
        finally:
            self._abort = None
        outcome: AdjustmentOutcome | None = "aborted" if abort.is_set() else self._adjust_outcome
        if outcome is not None:
            raise BalanceAdjustmentError(f"stub calibration {outcome}", outcome=outcome)
        return CommandResult(
            accepted=True,
            detail=CAL_DONE_DETAIL,
            t_mono_ns=time.monotonic_ns(),
            t_utc=datetime.now(UTC),
        )


DESCRIPTOR = AdapterDescriptor(
    id="tests.fixtures.stub_sartorius",
    label="Stub Sartorius (test fixture)",
    family="sim",
    adapter_factory=StubSartorius,
)
register(DESCRIPTOR)


__all__ = ["CAL_DONE_DETAIL", "DESCRIPTOR", "StubSartorius"]
