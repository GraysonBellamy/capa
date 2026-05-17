"""The NI ↔ capa channel join, made first-class.

An NI-DAQ input row in ``devices.params.channels`` (NI-side: physical
channel, thermocouple type, NI ADC mode) and a capa channel row in
top-level ``[[channels]]`` (capa-side: name, units, plot group, calibration)
are joined today by **string equality** on the NI ``name`` ↔ the binding's
``field``/``channel``. The join has no validator and a silent runtime
failure (:meth:`capa.devices.nidaq.NIDAQAdapter._channel_samples_for`
``continue``\\ s when the binding's field isn't in the polled values
dict — no log, no problem reported, the channel is silently absent from
``scalars.parquet``).

:class:`DeclaredNIDAQChannel` materialises that join as a value so every
consumer (Layer-2 validator, Devices-pane widget, Channels Add menu, live
section validator) can read from one source of truth. The two entry
points — :func:`declared_channels_from_payload` (raw dict, for live UI)
and :func:`declared_channels_from_config` (validated HardwareProfile,
for the validator) — return the same shape.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DeclaredNIDAQChannel:
    """One NI input row, with the join state to its capa channel.

    ``field_name`` is the NI display name the polled :class:`DaqReading`
    is keyed by — the underlying ``ChannelSpec.display_name`` derived from
    the NI row's ``name`` (defaulting to ``physical_channel`` when ``name``
    is absent). This is the value an :class:`NIDAQReadingField.field` or
    :class:`NIDAQBlockChannel.channel` binding must equal.
    """

    device_name: str
    """``DeviceConfig.name`` of the owning NI device (e.g. ``"cdaq1"``)."""

    task_name: str
    """``NIDAQAdapterParams.task_name``. Bindings join on this as well as
    on the device name — two NI devices may declare different tasks with
    overlapping field names without colliding."""

    field_name: str
    """The NI display name — ``channel.name`` if set, else
    ``physical_channel``. The join key."""

    physical_channel: str
    """``cDAQ1Mod1/ai0`` and friends. Operator-facing detail; not part of
    the join."""

    kind: str
    """NI channel kind discriminator: ``"thermocouple"``, ``"ai_voltage"``,
    or ``"raw"`` (anything not typed by :mod:`capa.devices.nidaq_channels`)."""

    units: str | None
    """Best-effort engineering unit. For thermocouples, the
    :attr:`NIDAQThermocoupleConfig.units` NI enum name (``"DEG_C"``,
    ``"K"``, ``"DEG_F"``). For voltage, the optional ``unit`` field.
    ``None`` when the row carries no declared unit."""

    is_bound_to_capa: bool
    """``True`` iff at least one ``[[channels]]`` row's binding resolves to
    this declared NI channel via the ``(device, task, field)`` triple."""


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def declared_channels_from_payload(
    hardware_payload: Mapping[str, Any],
) -> list[DeclaredNIDAQChannel]:
    """Parse declared NI channels from a raw, pre-validation hardware payload.

    ``hardware_payload`` is the dict held on ``SetupDraft.document.hardware_payload`` —
    a TOML-shaped mapping with ``"devices"`` (list of device dicts) and
    ``"channels"`` (list of channel dicts) keys. Non-NI devices and
    devices with malformed params are silently skipped; this helper has
    to stay usable while the operator is mid-edit and the payload may be
    partially invalid.
    """
    devices = hardware_payload.get("devices")
    channels = hardware_payload.get("channels")
    device_iter: Iterable[Mapping[str, Any]] = devices if isinstance(devices, list) else ()
    channel_iter: Iterable[Mapping[str, Any]] = channels if isinstance(channels, list) else ()
    declared = _parse_devices_dict(device_iter)
    return _attach_bound_flags(declared, _binding_keys_from_dict_channels(channel_iter))


def nidaq_task_keys_from_payload(
    hardware_payload: Mapping[str, Any],
) -> set[tuple[str, str]]:
    """Return ``(device, task)`` keys for NI devices in a raw payload.

    Unlike :func:`declared_channels_from_payload`, this still returns a
    task key when the NI device has no parseable channels. Validators use
    that to report "no fields declared" instead of silently opting out.
    """
    devices = hardware_payload.get("devices")
    device_iter: Iterable[Mapping[str, Any]] = devices if isinstance(devices, list) else ()
    return _parse_task_keys_dict(device_iter)


def declared_channels_from_config(
    hardware_profile: Any,
) -> list[DeclaredNIDAQChannel]:
    """Parse declared NI channels from a validated :class:`HardwareProfile`.

    ``hardware_profile`` is the post-Pydantic object held on
    ``ExperimentConfig.hardware``. Devices' ``params`` are still untyped
    dicts at this layer (the adapter parses them at construction time),
    so the same dict-walker as the payload path applies — the only thing
    that's typed here is the ``channels`` tuple, where bindings are
    :class:`ChannelSpec` instances with concrete :class:`SourceBinding`
    variants.
    """
    devices = getattr(hardware_profile, "devices", ()) or ()
    channels = getattr(hardware_profile, "channels", ()) or ()
    device_dicts: list[Mapping[str, Any]] = []
    for dev in devices:
        device_dicts.append(
            {
                "name": getattr(dev, "name", None),
                "adapter": getattr(dev, "adapter", None),
                "params": getattr(dev, "params", {}) or {},
            }
        )
    declared = _parse_devices_dict(device_dicts)
    return _attach_bound_flags(declared, _binding_keys_from_typed_channels(channels))


def nidaq_task_keys_from_config(
    hardware_profile: Any,
) -> set[tuple[str, str]]:
    """Return ``(device, task)`` keys for NI devices in a validated profile."""
    devices = getattr(hardware_profile, "devices", ()) or ()
    device_dicts: list[Mapping[str, Any]] = []
    for dev in devices:
        device_dicts.append(
            {
                "name": getattr(dev, "name", None),
                "adapter": getattr(dev, "adapter", None),
                "params": getattr(dev, "params", {}) or {},
            }
        )
    return _parse_task_keys_dict(device_dicts)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _is_nidaq_adapter(adapter_id: object) -> bool:
    """Heuristic family check: any adapter id whose descriptor lives in the
    NI family. Triggers builtin-adapter import on first call so the helper
    doesn't depend on the caller having pre-loaded the registry.
    """
    if not isinstance(adapter_id, str):
        return False
    try:
        from capa.devices.registry import (  # noqa: PLC0415
            ensure_adapters_loaded,
            get_descriptor,
        )

        ensure_adapters_loaded()
    except Exception:  # pragma: no cover — registry import always succeeds at runtime
        return False
    descriptor = get_descriptor(adapter_id)
    if descriptor is None:
        return False
    return getattr(descriptor, "family", None) == "nidaq"


_TYPED_KIND_UNITS_FIELD = {
    "thermocouple": "units",
    "ai_voltage": "unit",
}


def _kind_and_units(channel_dict: Mapping[str, Any]) -> tuple[str, str | None]:
    """Best-effort kind + units extraction from a raw NI channel dict.

    Falls back to ``"raw"`` for any kind not in the typed-model set so the
    join still surfaces unfamiliar channels (digital lines, counters,
    pass-through specs) instead of dropping them.
    """
    raw_kind = channel_dict.get("kind")
    kind = raw_kind if isinstance(raw_kind, str) else "raw"
    if kind not in _TYPED_KIND_UNITS_FIELD:
        kind_tag = "raw"
        units_field = "unit"
    else:
        kind_tag = kind
        units_field = _TYPED_KIND_UNITS_FIELD[kind]
    raw_units = channel_dict.get(units_field)
    units = raw_units if isinstance(raw_units, str) and raw_units else None
    return kind_tag, units


def _parse_devices_dict(
    devices: Iterable[Mapping[str, Any]],
) -> list[DeclaredNIDAQChannel]:
    out: list[DeclaredNIDAQChannel] = []
    for dev in devices:
        if not isinstance(dev, Mapping):
            continue
        if not _is_nidaq_adapter(dev.get("adapter")):
            continue
        device_name = dev.get("name")
        if not isinstance(device_name, str) or not device_name:
            continue
        params = dev.get("params")
        if not isinstance(params, Mapping):
            continue
        task_name = params.get("task_name")
        if not isinstance(task_name, str) or not task_name:
            continue
        channels = params.get("channels")
        if not isinstance(channels, (list, tuple)):
            continue
        for ch in channels:
            if not isinstance(ch, Mapping):
                continue
            physical = ch.get("physical_channel")
            if not isinstance(physical, str) or not physical:
                continue
            raw_name = ch.get("name")
            field_name = raw_name if isinstance(raw_name, str) and raw_name else physical
            kind, units = _kind_and_units(ch)
            out.append(
                DeclaredNIDAQChannel(
                    device_name=device_name,
                    task_name=task_name,
                    field_name=field_name,
                    physical_channel=physical,
                    kind=kind,
                    units=units,
                    is_bound_to_capa=False,
                )
            )
    return out


def _parse_task_keys_dict(
    devices: Iterable[Mapping[str, Any]],
) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for dev in devices:
        if not isinstance(dev, Mapping):
            continue
        if not _is_nidaq_adapter(dev.get("adapter")):
            continue
        device_name = dev.get("name")
        if not isinstance(device_name, str) or not device_name:
            continue
        params = dev.get("params")
        if not isinstance(params, Mapping):
            continue
        task_name = params.get("task_name")
        if isinstance(task_name, str) and task_name:
            keys.add((device_name, task_name))
    return keys


def _binding_keys_from_dict_channels(
    channels: Iterable[Mapping[str, Any]],
) -> set[tuple[str, str, str]]:
    """Walk pre-validation ``[[channels]]`` dicts and collect ``(device,
    task, field)`` triples for NI bindings.

    Polled (:class:`NIDAQReadingField`) uses ``field``; block
    (:class:`NIDAQBlockChannel`) uses ``channel`` — both name the same NI
    display name, so they merge into one binding-key set.
    """
    keys: set[tuple[str, str, str]] = set()
    for ch in channels:
        if not isinstance(ch, Mapping):
            continue
        source = ch.get("source")
        if not isinstance(source, Mapping):
            continue
        kind = source.get("source")
        if kind not in ("nidaq_reading_field", "nidaq_block_channel"):
            continue
        device = source.get("device")
        task = source.get("task")
        field = source.get("field") if kind == "nidaq_reading_field" else source.get("channel")
        if isinstance(device, str) and isinstance(task, str) and isinstance(field, str):
            keys.add((device, task, field))
    return keys


def _binding_keys_from_typed_channels(
    channels: Sequence[Any],
) -> set[tuple[str, str, str]]:
    """Walk validated ``ChannelSpec`` instances and collect the same triples.

    Concrete binding variants come from :mod:`capa.channels.spec` —
    :class:`NIDAQReadingField` exposes ``device, task, field``;
    :class:`NIDAQBlockChannel` exposes ``device, task, channel``.
    """
    keys: set[tuple[str, str, str]] = set()
    for ch in channels:
        binding = getattr(ch, "source", None)
        source = getattr(binding, "source", None)
        if source not in ("nidaq_reading_field", "nidaq_block_channel"):
            continue
        device = getattr(binding, "device", None)
        task = getattr(binding, "task", None)
        if source == "nidaq_reading_field":
            field = getattr(binding, "field", None)
        else:
            field = getattr(binding, "channel", None)
        if isinstance(device, str) and isinstance(task, str) and isinstance(field, str):
            keys.add((device, task, field))
    return keys


def _attach_bound_flags(
    declared: list[DeclaredNIDAQChannel],
    binding_keys: set[tuple[str, str, str]],
) -> list[DeclaredNIDAQChannel]:
    """Return a copy of ``declared`` with ``is_bound_to_capa`` resolved
    against ``binding_keys`` — frozen dataclasses can't be mutated in place.
    """
    out: list[DeclaredNIDAQChannel] = []
    for d in declared:
        bound = (d.device_name, d.task_name, d.field_name) in binding_keys
        out.append(
            DeclaredNIDAQChannel(
                device_name=d.device_name,
                task_name=d.task_name,
                field_name=d.field_name,
                physical_channel=d.physical_channel,
                kind=d.kind,
                units=d.units,
                is_bound_to_capa=bound,
            )
        )
    return out


__all__ = [
    "DeclaredNIDAQChannel",
    "declared_channels_from_config",
    "declared_channels_from_payload",
    "nidaq_task_keys_from_config",
    "nidaq_task_keys_from_payload",
]
