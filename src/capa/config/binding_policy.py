"""Channel-kind ↔ binding-type ordering policy.

The Channels section's "binding type" combobox keeps every variant
available — a thermocouple value occasionally arrives via Watlow rather
than via NI-DAQ — but reorders by likely match for the channel's kind.
The same lookup also powers Layer-2 referential checks when an adapter
family is known: see :mod:`capa.config.validate`.

Pure data, ~30 lines: no schema enforcement, no dispatch.
"""

from __future__ import annotations

from capa.channels.spec import ChannelKind

# Every known source-binding discriminator value, ordered by general
# usefulness. Variants the policy table doesn't mention for a given
# channel kind fall back to this order.
ALL_BINDING_SOURCES: tuple[str, ...] = (
    "watlow_parameter",
    "alicat_frame_field",
    "sartorius_reading",
    "nidaq_reading_field",
    "nidaq_block_channel",
    "derived",
)


# Per-kind preferred order. Variants outside the listed tuple are
# appended in :data:`ALL_BINDING_SOURCES` order so the combobox is
# never sparse.
KIND_TO_PREFERRED_BINDINGS: dict[ChannelKind, tuple[str, ...]] = {
    ChannelKind.THERMOCOUPLE: ("nidaq_reading_field", "watlow_parameter", "nidaq_block_channel"),
    ChannelKind.PROCESS_VAR: ("watlow_parameter", "nidaq_reading_field"),
    ChannelKind.SETPOINT: ("watlow_parameter", "alicat_frame_field"),
    ChannelKind.MASS: ("sartorius_reading",),
    ChannelKind.MFC_FLOW: ("alicat_frame_field",),
    ChannelKind.ANALOG_IN: ("nidaq_reading_field", "nidaq_block_channel"),
    ChannelKind.ANALOG_OUT: ("nidaq_reading_field",),
    ChannelKind.COUNTER: ("nidaq_reading_field",),
    ChannelKind.DERIVED: ("derived",),
}


def ordered_bindings_for_kind(kind: ChannelKind | str | None) -> tuple[str, ...]:
    """Return every binding-source value, ordered for the given kind.

    Unknown kinds fall back to :data:`ALL_BINDING_SOURCES`. ``None``
    means "no kind selected yet" — also falls back to the canonical
    order so the combobox is populated.
    """
    if kind is None:
        return ALL_BINDING_SOURCES
    # Accept the StrEnum value or the StrEnum itself.
    if isinstance(kind, str):
        try:
            kind = ChannelKind(kind)
        except ValueError:
            return ALL_BINDING_SOURCES
    preferred = KIND_TO_PREFERRED_BINDINGS.get(kind, ())
    seen = set(preferred)
    tail = tuple(s for s in ALL_BINDING_SOURCES if s not in seen)
    return preferred + tail


def filter_bindings_for_family(
    bindings: tuple[str, ...],
    supported: tuple[str, ...] | None,
) -> tuple[str, ...]:
    """Restrict ``bindings`` to ``supported`` (from
    :attr:`AdapterDescriptor.supported_binding_sources`).

    ``supported=None`` means "device has no descriptor" — we don't
    filter in that case so plugin adapters still see every binding.
    ``DerivedBinding`` is always preserved at the end (it has no
    device, so adapter family is irrelevant).
    """
    if not supported:
        return bindings
    allowed = set(supported)
    out = tuple(b for b in bindings if b in allowed)
    if "derived" in bindings and "derived" not in out:
        out = (*out, "derived")
    return out


__all__ = [
    "ALL_BINDING_SOURCES",
    "KIND_TO_PREFERRED_BINDINGS",
    "filter_bindings_for_family",
    "ordered_bindings_for_kind",
]
