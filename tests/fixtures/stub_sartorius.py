"""Test alias — same recording stub but with "sartorius" in the import path
so :func:`capa.ui.manual.cards.balance.is_balance_device` recognises it."""

from __future__ import annotations

from tests.fixtures.stub_recording_adapter import StubRecordingAdapter as _Base


class StubSartorius(_Base):
    """Identical to :class:`StubRecordingAdapter`; the class name follows
    the resolver convention (CamelCase of the module leaf) so
    ``_import_adapter_class`` picks it up.

    The module path also contains the substring ``sartorius`` so
    :func:`capa.ui.manual.cards.balance.is_balance_device` recognises it.
    """


__all__ = ["StubSartorius"]
