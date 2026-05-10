"""Test alias — same recording stub but with "alicat" in the import path
so :func:`capa.ui.manual.cards.alicat.is_alicat_device` recognises it."""

from __future__ import annotations

from tests.fixtures.stub_recording_adapter import StubRecordingAdapter as _Base


class StubAlicat(_Base):
    """Identical to :class:`StubRecordingAdapter`; the class name follows
    the resolver convention (CamelCase of the module leaf) so
    ``_import_adapter_class`` picks it up."""


__all__ = ["StubAlicat"]
