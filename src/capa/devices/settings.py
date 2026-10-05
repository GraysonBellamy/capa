"""Declarative device settings — what an experiment wants a device set to.

An adapter opts in through :attr:`AdapterDescriptor.settings`: a
:class:`DeviceSettingsSpec` naming a Pydantic model (every field optional;
an absent field means "leave unchanged") and a table of
:class:`SettingField`\\ s. Each row says how to read that setting off the
adapter's ``read_state_snapshot()`` result and which command sets it.

:func:`plan_settings` compares a desired model against a snapshot and
returns the commands that would close the gap; :func:`capture_settings`
turns a snapshot back into a settings payload ("save what the device is
set to now"). Both walk the same table, so they can't disagree.

Qt-free and I/O-free: the runtime orchestrator
(:mod:`capa.runtime.device_settings`) does the reading and dispatching.
"""

from __future__ import annotations

import math
import operator
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError


class SettingRefusedError(Exception):
    """A desired value the device can't take, e.g. a temperature range the
    camera doesn't offer. Raised by :attr:`SettingField.command`."""


@dataclass(frozen=True, slots=True)
class SettingField:
    """One declarative setting: how to read it and how to change it."""

    name: str
    """Field name on the spec's model."""

    label: str
    """Operator-facing name, e.g. ``"Gas"``."""

    current: Callable[[Any], Any]
    """``snapshot → value`` in the model's terms; ``None`` when the device
    didn't report it."""

    command: Callable[[Any, Any], tuple[str, dict[str, Any]]]
    """``(desired, snapshot) → (kind, payload)`` for the command that sets
    the value. Raises :class:`SettingRefusedError` when the device can't take it."""

    same: Callable[[Any, Any], bool] = operator.eq
    """``(current, desired) → bool``. Floats use :func:`close_to`."""

    show: Callable[[Any], str] = str
    """Formats a value for the confirmation dialog."""

    note: str | None = None
    """Shown next to a pending change, e.g. a recalibration warning."""


@dataclass(frozen=True, slots=True)
class DeviceSettingsSpec:
    """An adapter's settings model plus its field table."""

    model: type[BaseModel]
    """Every field ``X | None = None``; ``None`` means leave unchanged."""

    fields: tuple[SettingField, ...]
    """In apply order (e.g. an IR camera's range before its radiometric
    parameters)."""

    snapshot_type: type[Any]
    """What the adapter's ``read_state_snapshot()`` returns."""

    unused: Callable[[Mapping[str, Any]], frozenset[str]] | None = None
    """``device params → fields this device can't take``, for settings that
    depend on how the device is configured, e.g. an analyzer's settings for
    gases its channel map doesn't assert. The Setup form leaves them out.
    ``None`` when every field applies to every device."""


@dataclass(frozen=True, slots=True)
class SettingChange:
    """One setting that differs from what the experiment declares."""

    field: str
    label: str
    current: str | None
    """Display text of the device's value; ``None`` if it didn't report one."""
    desired: str
    kind: str
    payload: Mapping[str, Any]
    note: str | None = None


@dataclass(frozen=True, slots=True)
class SettingIssue:
    """A declared setting that can't be applied as written."""

    field: str
    label: str
    message: str


def plan_settings(
    spec: DeviceSettingsSpec,
    desired: BaseModel,
    snapshot: Any,
) -> tuple[tuple[SettingChange, ...], tuple[SettingIssue, ...]]:
    """Changes that would bring the device from ``snapshot`` to ``desired``.

    Fields left unset in ``desired`` are skipped. A field whose current
    value is unknown counts as a change: the write is harmless and the
    verification read says whether it took.
    """
    changes: list[SettingChange] = []
    issues: list[SettingIssue] = []
    for field in spec.fields:
        want = getattr(desired, field.name)
        if want is None:
            continue
        have = field.current(snapshot)
        if have is not None and field.same(have, want):
            continue
        try:
            kind, payload = field.command(want, snapshot)
        except SettingRefusedError as exc:
            issues.append(SettingIssue(field=field.name, label=field.label, message=str(exc)))
            continue
        changes.append(
            SettingChange(
                field=field.name,
                label=field.label,
                current=None if have is None else field.show(have),
                desired=field.show(want),
                kind=kind,
                payload=payload,
                note=field.note,
            )
        )
    return tuple(changes), tuple(issues)


def capture_settings(spec: DeviceSettingsSpec, snapshot: Any) -> dict[str, Any]:
    """The device's current settings as a ``device_settings`` entry.

    JSON-safe; settings the device didn't report, or reported as a value
    the model rejects (e.g. a custom gas mixture), are left out.
    """
    captured: dict[str, Any] = {}
    for field in spec.fields:
        value = field.current(snapshot)
        if value is None:
            continue
        try:
            one = spec.model.model_validate({field.name: value})
        except ValidationError:
            continue
        captured.update(one.model_dump(mode="json", include={field.name}))
    return captured


def close_to(abs_tol: float) -> Callable[[Any, Any], bool]:
    """Float equality within ``abs_tol``, for read-backs that come back
    through single-precision storage or a unit conversion."""

    def _same(have: Any, want: Any) -> bool:
        return math.isclose(float(have), float(want), rel_tol=1e-9, abs_tol=abs_tol)

    return _same


__all__ = [
    "DeviceSettingsSpec",
    "SettingChange",
    "SettingField",
    "SettingIssue",
    "SettingRefusedError",
    "capture_settings",
    "close_to",
    "plan_settings",
]
