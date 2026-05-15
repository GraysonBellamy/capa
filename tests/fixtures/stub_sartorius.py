"""Test alias — same recording stub but with "sartorius" in the import path
so :func:`capa.ui.manual.cards.balance.is_balance_device` recognises it."""

from __future__ import annotations

from capa.devices.registry import AdapterDescriptor, register
from tests.fixtures.stub_recording_adapter import StubRecordingAdapter as _Base


class StubSartorius(_Base):
    """Identical to :class:`StubRecordingAdapter`; lives in this module so
    the substring ``sartorius`` in the import path satisfies
    :func:`capa.ui.manual.cards.balance.is_balance_device`."""


DESCRIPTOR = AdapterDescriptor(
    id="tests.fixtures.stub_sartorius",
    label="Stub Sartorius (test fixture)",
    family="sim",
    adapter_factory=StubSartorius,
)
register(DESCRIPTOR)


__all__ = ["DESCRIPTOR", "StubSartorius"]
