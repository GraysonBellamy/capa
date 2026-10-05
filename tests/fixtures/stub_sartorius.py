"""Test alias — the recording stub with "sartorius" in the import path so
:func:`capa.ui.manual.cards.balance.is_balance_device` recognises it, plus
the balance read-back the manual card consumes."""

from __future__ import annotations

from capa.devices.registry import AdapterDescriptor, register
from capa.devices.sartorius import SartoriusStateSnapshot
from tests.fixtures.stub_recording_adapter import StubRecordingAdapter as _Base


class StubSartorius(_Base):
    """:class:`StubRecordingAdapter` that lives in this module so the
    substring ``sartorius`` in the import path satisfies
    :func:`capa.ui.manual.cards.balance.is_balance_device`.

    Extra ``params`` (``filter_mode``, ``auto_zero``, ``display_unit``,
    ``tare_behavior``, ``cal_temperature_c``, ``cal_on_record``) set what
    :meth:`read_state_snapshot` reports.
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

    async def read_state_snapshot(self) -> SartoriusStateSnapshot:
        self.readback_count += 1
        return self.state


DESCRIPTOR = AdapterDescriptor(
    id="tests.fixtures.stub_sartorius",
    label="Stub Sartorius (test fixture)",
    family="sim",
    adapter_factory=StubSartorius,
)
register(DESCRIPTOR)


__all__ = ["DESCRIPTOR", "StubSartorius"]
