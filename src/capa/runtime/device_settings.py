"""Apply an experiment's ``device_settings`` to open devices.

Shared by the GUI (on config load, and before Start) and the headless
runner. Reading and dispatching are passed in as callables, so the same
code runs against a :class:`~capa.runtime.pool.WorkerPool` directly or
through the UI's :class:`~capa.runtime.dispatch.ManualClient`.

Two steps:

* :func:`plan_device_settings` reads every device the experiment names
  and compares it with the declared settings (see
  :mod:`capa.devices.settings`). It never raises: an entry that doesn't
  validate, or a device whose read-back fails, times out or has the wrong
  shape, becomes a :class:`DevicePlan` carrying an ``error``. Config
  validation catches most of the former, but File → Open and headless
  runs don't stop on validation problems.
* :func:`apply_device_settings` sends the selected changes, in each
  device's field order, then reads each device it touched again and
  re-plans it to see what took.

Commands go out as manual overrides — the operator who confirmed them is
both ``issued_by`` and ``confirmed_by`` — exactly as a manual-card write
would. Writes are whatever the adapter's settings spec sends; the built-in
specs never save to device EEPROM.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ValidationError

from capa.devices.registry import require_descriptor
from capa.devices.settings import (
    DeviceSettingsSpec,
    SettingChange,
    SettingIssue,
    capture_settings,
    plan_settings,
)
from capa.experiment.authorization import Authorization

if TYPE_CHECKING:
    from capa.devices.adapter import CommandResult, DeviceCommand
    from capa.experiment.config import ExperimentConfig
    from capa.runtime.pool import WorkerPool

READ_TIMEOUT_S: Final[float] = 15.0
"""Per-device read-back deadline. A real Alicat or balance answers in well
under a second; this only bounds a device that has stopped answering."""

COMMAND_TIMEOUT_S: Final[float] = 30.0
"""Per-command deadline. An IR range switch alone takes a few seconds."""

Readback = Callable[[str], Awaitable[Any]]
"""``device name → read_state_snapshot()`` result (``None`` if unsupported)."""

Dispatch = Callable[[str, "DeviceCommand"], Awaitable["CommandResult"]]
"""``(device name, command) → CommandResult``."""


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DevicePlan:
    """One device's declared settings compared with what it reports."""

    name: str
    changes: tuple[SettingChange, ...] = ()
    issues: tuple[SettingIssue, ...] = ()
    error: str | None = None
    """Why the device's settings couldn't be compared at all: an invalid
    entry, an unknown device, or a failed read-back."""
    observed: Mapping[str, Any] | None = None
    """The device's settings as read, in ``device_settings`` form; ``None``
    when it wasn't read."""
    spec: DeviceSettingsSpec | None = field(default=None, repr=False, compare=False)
    desired: BaseModel | None = field(default=None, repr=False, compare=False)

    @property
    def needs_attention(self) -> bool:
        """``True`` when the operator has something to apply or read."""
        return bool(self.changes or self.issues or self.error)


@dataclass(frozen=True, slots=True)
class SettingsPlan:
    """Every declared device's :class:`DevicePlan`, in declaration order."""

    devices: tuple[DevicePlan, ...] = ()

    @property
    def needs_attention(self) -> bool:
        """``True`` when any device differs, can't be set, or wasn't read."""
        return any(device.needs_attention for device in self.devices)

    @property
    def change_count(self) -> int:
        """How many settings differ, across every device."""
        return sum(len(device.changes) for device in self.devices)

    def observed(self) -> dict[str, dict[str, Any]]:
        """``{device: settings}`` for every device that was read — what goes
        into the bundle as the settings the run actually started with."""
        return {d.name: dict(d.observed) for d in self.devices if d.observed is not None}


async def plan_device_settings(
    config: ExperimentConfig,
    readback: Readback,
    *,
    timeout_s: float = READ_TIMEOUT_S,
) -> SettingsPlan:
    """Read every device named in ``config.device_settings`` and compare.

    Devices are read concurrently; each read has its own deadline.
    """
    adapter_ids = {dev.name: dev.adapter for dev in config.hardware.devices}
    adapter_ids.update({cam.name: cam.adapter for cam in config.hardware.cameras})
    plans = await asyncio.gather(
        *(
            _plan_one(name, raw, adapter_ids.get(name), readback, timeout_s)
            for name, raw in config.device_settings.items()
        )
    )
    return SettingsPlan(devices=tuple(plans))


async def _plan_one(
    name: str,
    raw: Mapping[str, Any],
    adapter_id: str | None,
    readback: Readback,
    timeout_s: float,
) -> DevicePlan:
    resolved = _resolve(name, raw, adapter_id)
    if isinstance(resolved, str):
        return DevicePlan(name=name, error=resolved)
    spec, desired = resolved
    return await _compare(name, spec, desired, readback, timeout_s)


def _resolve(
    name: str, raw: Mapping[str, Any], adapter_id: str | None
) -> tuple[DeviceSettingsSpec, BaseModel] | str:
    """The spec and validated settings for one entry, or why there are none."""
    if adapter_id is None:
        return f"{name!r} is not a device or camera in this hardware"
    try:
        descriptor = require_descriptor(adapter_id)
    except KeyError:
        return f"no adapter {adapter_id!r} is registered"
    spec = descriptor.settings
    if spec is None:
        return f"{descriptor.label} has no settings an experiment can declare"
    try:
        desired = spec.model.model_validate(raw)
    except ValidationError as exc:
        reasons = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'settings'}: {err['msg']}"
            for err in exc.errors()
        )
        return f"invalid settings: {reasons}"
    return spec, desired


async def _compare(
    name: str,
    spec: DeviceSettingsSpec,
    desired: BaseModel,
    readback: Readback,
    timeout_s: float,
) -> DevicePlan:
    try:
        snapshot = await asyncio.wait_for(readback(name), timeout_s)
    except TimeoutError:
        error = f"no read-back within {timeout_s:g} s"
    except Exception as exc:
        error = f"read-back failed: {exc}"
    else:
        if snapshot is None:
            error = "the device didn't report its settings"
        elif not isinstance(snapshot, spec.snapshot_type):
            error = f"unexpected read-back {type(snapshot).__name__}"
        else:
            changes, issues = plan_settings(spec, desired, snapshot)
            return DevicePlan(
                name=name,
                changes=changes,
                issues=issues,
                observed=capture_settings(spec, snapshot),
                spec=spec,
                desired=desired,
            )
    return DevicePlan(name=name, error=error, spec=spec, desired=desired)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


class Outcome(StrEnum):
    """What became of one sent change."""

    VERIFIED = "verified"
    """The device now reports the declared value."""
    UNVERIFIED = "unverified"
    """Accepted, but the device doesn't report the value back."""
    STILL_DIFFERS = "still_differs"
    """Accepted, but the device still reports something else."""
    REFUSED = "refused"
    """The device refused the command."""
    FAILED = "failed"
    """The command raised or timed out."""


@dataclass(frozen=True, slots=True)
class ChangeResult:
    """One sent change and its outcome."""

    device: str
    change: SettingChange
    outcome: Outcome
    detail: str = ""

    @property
    def ok(self) -> bool:
        """``True`` unless the change was refused, failed, or didn't take."""
        return self.outcome in (Outcome.VERIFIED, Outcome.UNVERIFIED)


@dataclass(frozen=True, slots=True)
class SettingsReport:
    """Outcome of :func:`apply_device_settings`."""

    results: tuple[ChangeResult, ...]
    plan_after: SettingsPlan
    """The plan as it stands now: devices that were sent changes are read
    again; the rest carry over unchanged."""

    @property
    def ok(self) -> bool:
        """``True`` when every sent change took (or can't be checked)."""
        return all(result.ok for result in self.results)


async def apply_device_settings(
    plan: SettingsPlan,
    selected: Collection[tuple[str, str]] | None,
    *,
    dispatch: Dispatch,
    readback: Readback,
    operator_id: str,
    on_progress: Callable[[ChangeResult], None] | None = None,
    timeout_s: float = COMMAND_TIMEOUT_S,
    read_timeout_s: float = READ_TIMEOUT_S,
) -> SettingsReport:
    """Send the ``selected`` ``(device, field)`` changes; ``None`` sends all.

    Each device's changes go out one at a time in its spec's field order;
    the device is then read again to verify. ``on_progress`` is called with
    each result once its device has been verified.
    """
    authorization = Authorization(operator_id=operator_id, run_id="manual")
    results: list[ChangeResult] = []
    after: list[DevicePlan] = []
    for device in plan.devices:
        picked = [
            change
            for change in device.changes
            if selected is None or (device.name, change.field) in selected
        ]
        if not picked or device.spec is None or device.desired is None:
            after.append(device)
            continue
        sent: list[tuple[SettingChange, str]] = []
        failed: list[ChangeResult] = []
        for change in picked:
            cmd = authorization.issue_manual(
                kind=change.kind,
                payload=dict(change.payload),
                issued_by=operator_id,
                confirmed_by=operator_id,
            )
            try:
                result = await asyncio.wait_for(dispatch(device.name, cmd), timeout_s)
            except TimeoutError:
                failed.append(
                    ChangeResult(
                        device.name, change, Outcome.FAILED, f"no reply within {timeout_s:g} s"
                    )
                )
            except Exception as exc:
                failed.append(ChangeResult(device.name, change, Outcome.FAILED, str(exc)))
            else:
                if result.accepted:
                    sent.append((change, result.detail))
                else:
                    failed.append(ChangeResult(device.name, change, Outcome.REFUSED, result.detail))
        replanned = await _compare(
            device.name, device.spec, device.desired, readback, read_timeout_s
        )
        after.append(replanned)
        remaining = {change.field: change for change in replanned.changes}
        device_results = [*failed]
        for change, detail in sent:
            device_results.append(_verify(device.name, change, detail, replanned, remaining))
        device_results.sort(key=lambda r: picked.index(r.change))
        for change_result in device_results:
            results.append(change_result)
            if on_progress is not None:
                on_progress(change_result)
    return SettingsReport(results=tuple(results), plan_after=SettingsPlan(devices=tuple(after)))


def _verify(
    device: str,
    change: SettingChange,
    detail: str,
    replanned: DevicePlan,
    remaining: Mapping[str, SettingChange],
) -> ChangeResult:
    if replanned.error is not None:
        return ChangeResult(device, change, Outcome.UNVERIFIED, replanned.error)
    still = remaining.get(change.field)
    if still is None:
        return ChangeResult(device, change, Outcome.VERIFIED, detail)
    if still.current is None:
        return ChangeResult(
            device, change, Outcome.UNVERIFIED, "the device doesn't report this setting"
        )
    return ChangeResult(
        device, change, Outcome.STILL_DIFFERS, f"the device reports {still.current}"
    )


def pool_readback(pool: WorkerPool) -> Readback:
    """A :data:`Readback` over a pool's ``device_readback``."""

    async def read(name: str) -> Any:
        return await asyncio.wrap_future(pool.device_readback(name))

    return read


__all__ = [
    "COMMAND_TIMEOUT_S",
    "READ_TIMEOUT_S",
    "ChangeResult",
    "DevicePlan",
    "Dispatch",
    "Outcome",
    "Readback",
    "SettingsPlan",
    "SettingsReport",
    "apply_device_settings",
    "plan_device_settings",
    "pool_readback",
]
