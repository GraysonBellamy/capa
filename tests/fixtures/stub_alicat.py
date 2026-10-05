"""Test alias — the recording stub with "alicat" in the import path so
:func:`capa.ui.manual.cards.alicat.is_alicat_device` recognises it, plus the
Alicat read-back the manual card consumes."""

from __future__ import annotations

from capa.devices.adapter import Capability
from capa.devices.alicat import AlicatStateSnapshot
from capa.devices.registry import AdapterDescriptor, register
from tests.fixtures.stub_recording_adapter import StubRecordingAdapter as _Base


class StubAlicat(_Base):
    """:class:`StubRecordingAdapter` that lives in this module so the
    substring ``alicat`` in the import path satisfies
    :func:`capa.ui.manual.cards.alicat.is_alicat_device`.

    Extra ``params``:

    * ``gas`` / ``gas_list`` / ``setpoint`` / ``setpoint_unit`` — what
      :meth:`read_state_snapshot` reports.
    * ``capabilities_after_open`` — flag names that replace
      ``capabilities`` on :meth:`open`, the way the real adapter only learns
      whether it fronts a controller once ``open()`` identifies the device.
    """

    def __init__(
        self,
        *,
        name: str,
        capabilities: list[str] | None = None,
        accept_kinds: list[str] | None = None,
        gas: str | None = "Air",
        gas_list: list[str] | None = None,
        setpoint: float | None = None,
        setpoint_unit: str | None = None,
        capabilities_after_open: list[str] | None = None,
    ) -> None:
        super().__init__(name=name, capabilities=capabilities, accept_kinds=accept_kinds)
        self.state = AlicatStateSnapshot(
            gas=gas,
            gas_list=tuple(gas_list if gas_list is not None else ["Air", "N2", "Ar"]),
            setpoint=setpoint,
            setpoint_unit=setpoint_unit,
        )
        self._capabilities_after_open = capabilities_after_open
        self.readback_count = 0

    async def open(self) -> None:
        await super().open()
        if self._capabilities_after_open is not None:
            self.capabilities = frozenset(Capability[n] for n in self._capabilities_after_open)

    async def read_state_snapshot(self) -> AlicatStateSnapshot:
        self.readback_count += 1
        return self.state


DESCRIPTOR = AdapterDescriptor(
    id="tests.fixtures.stub_alicat",
    label="Stub Alicat (test fixture)",
    family="sim",
    adapter_factory=StubAlicat,
)
register(DESCRIPTOR)


__all__ = ["DESCRIPTOR", "StubAlicat"]
