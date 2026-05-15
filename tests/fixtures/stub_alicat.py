"""Test alias — same recording stub but with "alicat" in the import path
so :func:`capa.ui.manual.cards.alicat.is_alicat_device` recognises it."""

from __future__ import annotations

from capa.devices.registry import AdapterDescriptor, register
from tests.fixtures.stub_recording_adapter import StubRecordingAdapter as _Base


class StubAlicat(_Base):
    """Identical to :class:`StubRecordingAdapter`; lives in this module so
    the substring ``alicat`` in the import path satisfies
    :func:`capa.ui.manual.cards.alicat.is_alicat_device`."""


DESCRIPTOR = AdapterDescriptor(
    id="tests.fixtures.stub_alicat",
    label="Stub Alicat (test fixture)",
    family="sim",
    adapter_factory=StubAlicat,
)
register(DESCRIPTOR)


__all__ = ["DESCRIPTOR", "StubAlicat"]
