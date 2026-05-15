"""Shared adapter helpers used by both sim and real adapters.

Both sim and real adapters need:

* the same channel-routing and calibration-application logic when turning a
  library record into one or more
  :class:`~capa.devices.records.ChannelSample`\\ s,
* the same authorization gate on every :meth:`DeviceAdapter.command` (plan
  §9: a command without either ``authorization_id`` or ``confirmed_by`` is
  refused at the adapter boundary regardless of the underlying device's own
  gates),
* a uniform "last-sample-emitted" timestamp so the engine's safety
  watchdog (plan §13.2 / §9 day-1 rules) can detect a silent producer.

Lifting these here keeps the four real adapters from re-implementing the exact same logic.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime

from capa.channels.spec import ChannelSpec
from capa.core.clock import RunClock
from capa.core.errors import AdapterError
from capa.devices.adapter import CommandResult, DeviceCommand
from capa.devices.records import ChannelSample


def make_record_id(adapter: str, device: str, seq: int) -> str:
    """Stable record id within a run.

    Format ``"<adapter>:<device>:<seq>"``. ``seq`` is monotonically incremented
    per device per adapter; the engine never reads the inner structure so the
    format is for human/diagnostic eyes only.
    """
    return f"{adapter}:{device}:{seq}"


def channels_for_device(
    specs: Iterable[ChannelSpec],
    *,
    device: str,
    binding_source: str,
) -> list[ChannelSpec]:
    """Filter ``specs`` to channels whose binding matches ``device`` and
    ``binding_source`` (e.g. ``"alicat_frame_field"``, ``"watlow_parameter"``)."""
    out: list[ChannelSpec] = []
    for spec in specs:
        binding = spec.source
        if getattr(binding, "device", None) != device:
            continue
        if binding.source != binding_source:
            continue
        out.append(spec)
    return out


def build_channel_sample(
    *,
    spec: ChannelSpec,
    raw_value: float,
    t_mono_ns: int,
    source_record_id: str,
    source_field: str,
    status: str = "ok",
) -> ChannelSample:
    """Apply the channel's calibration and produce a :class:`ChannelSample`.

    Adapters call this once per declared channel per tick (or per derived
    sample) so calibration application is centralized rather than redone in
    every adapter. Plan §7.2: "calibration application is the only non-trivial
    CPU step in the normalized pipeline; happens before fan-out so all
    consumers see calibrated ``ChannelSample``\\ s."
    """
    cal = spec.calibration
    # CustomCallable's evaluate() requires the plugin runtime; surface the
    # gap loudly rather than silently fall back.
    if cal.kind == "custom_callable":
        raise AdapterError(
            f"adapter cannot evaluate custom_callable calibration on channel {spec.name!r}",
            device=spec.source.device if hasattr(spec.source, "device") else None,
        )
    value, uncertainty = cal.evaluate_with_uncertainty(raw_value)
    return ChannelSample(
        channel=spec.name,
        t_mono_ns=t_mono_ns,
        t_mono_s=t_mono_ns / 1e9,
        value=value,
        raw=raw_value if spec.keep_raw else None,
        unit=spec.output_unit(),
        uncertainty=uncertainty,
        status=status,
        source_record_id=source_record_id,
        source_field=source_field,
    )


# ---------------------------------------------------------------------------
# Authorization gate — plan §9.
#
# Every command issued through a real adapter carries ``issued_by`` plus
# either ``authorization_id`` (run-arm cover) or ``confirmed_by`` (manual UI
# confirmation). The adapter refuses anything without one of those — a
# scheduled-method command without an arm authorization is just as
# attributable as a manual override without a confirmation.
# ---------------------------------------------------------------------------


def make_unauthorized_result(
    *,
    adapter_id: str,
    device_name: str,
    clock: RunClock,
) -> CommandResult:
    """Build the ``accepted=False`` result for a command lacking authorization.

    Centralized so every adapter renders the same message and the same
    timestamp shape (the engine's audit trail is uniform across adapters).
    """
    return CommandResult(
        accepted=False,
        detail=f"{adapter_id} {device_name!r} refuses unauthorized commands",
        t_mono_ns=clock.t_mono_ns(),
        t_utc=datetime.now(UTC),
    )


def make_not_open_result(
    *,
    adapter_id: str,
    device_name: str,
    clock: RunClock,
) -> CommandResult:
    """Build the ``accepted=False`` result for a command on an unopened adapter."""
    return CommandResult(
        accepted=False,
        detail=f"{adapter_id} {device_name!r} not open",
        t_mono_ns=clock.t_mono_ns(),
        t_utc=datetime.now(UTC),
    )


def make_accepted_result(
    *,
    detail: str,
    clock: RunClock,
) -> CommandResult:
    """Build the success result for a dispatched command."""
    return CommandResult(
        accepted=True,
        detail=detail,
        t_mono_ns=clock.t_mono_ns(),
        t_utc=datetime.now(UTC),
    )


def reject_unless_authorized(
    cmd: DeviceCommand,
    *,
    adapter_id: str,
    device_name: str,
    clock: RunClock,
) -> CommandResult | None:
    """Return a rejection :class:`CommandResult` when ``cmd`` lacks authorization.

    Returns ``None`` to mean "proceed". Intended use::

        async def command(self, cmd):
            clock = self._clock or RunClock.now()
            rejection = reject_unless_authorized(cmd, ...)
            if rejection is not None:
                return rejection
            ...
    """
    if cmd.authorization_id is None and cmd.confirmed_by is None:
        return make_unauthorized_result(adapter_id=adapter_id, device_name=device_name, clock=clock)
    return None


# ---------------------------------------------------------------------------
# Watchdog support — plan §9 / §13.2.
#
# Each producer task (one per adapter) updates ``LastSampleTracker`` whenever
# it emits. The engine's :class:`SafetyMonitor` reads the tracker on its own
# cadence and raises a ``device_silent`` fault if any adapter has gone quiet
# past ``2 / sample_rate_hz``. The tracker is intentionally tiny so adapters
# pay zero cost on the hot path.
# ---------------------------------------------------------------------------


class LastSampleTracker:
    """Per-adapter "time of last emission" tracker.

    Adapters call :meth:`mark` on every successful poll tick (after the
    library returns a sample). The engine reads :meth:`age_ns` to drive the
    silent-device rule. Threadsafe by virtue of being simple int writes
    under the GIL — adapters live on the same anyio task group, so there is
    no real cross-thread access.

    A fresh tracker reports an "infinite" age so the watchdog gives a
    just-started adapter one tick of grace before complaining.
    """

    __slots__ = ("_t_mono_ns",)

    def __init__(self) -> None:
        self._t_mono_ns: int | None = None

    def mark(self, t_mono_ns: int) -> None:
        """Record that an emission was produced at ``t_mono_ns``."""
        self._t_mono_ns = t_mono_ns

    @property
    def last_t_mono_ns(self) -> int | None:
        """The most recent recorded timestamp, or ``None`` if never marked."""
        return self._t_mono_ns

    def age_ns(self, *, now_t_mono_ns: int) -> int | None:
        """Nanoseconds since the last :meth:`mark`. ``None`` if never marked."""
        if self._t_mono_ns is None:
            return None
        return now_t_mono_ns - self._t_mono_ns

    def reset(self) -> None:
        """Clear the timestamp — used at adapter ``start()`` to re-grace
        the watchdog window."""
        self._t_mono_ns = None


class WatchdogState:
    """Read-only view of an adapter's watchdog-relevant state.

    The engine's watchdog task constructs one of these per tick by calling
    :meth:`adapter.watchdog_state()` on every real adapter. The struct is
    plain attributes (no dependency on Pydantic) so the engine can call it
    inside a hot loop without paying for model validation. Plan §13.2.

    ``lifecycle_state`` is the adapter's own ``open``/``running``/``closed``
    label; the watchdog uses it to suppress ``device_silent`` warnings when
    the adapter has already been told to stop. Without that grace, a clean
    shutdown that races the 1 s watchdog sweep produces a spurious warning
    (hardware-day 2026-05-09 followup #4).
    """

    __slots__ = ("device", "expected_period_ns", "last_t_mono_ns", "lifecycle_state")

    def __init__(
        self,
        *,
        device: str,
        last_t_mono_ns: int | None,
        expected_period_ns: int,
        lifecycle_state: str | None = None,
    ) -> None:
        self.device = device
        self.last_t_mono_ns = last_t_mono_ns
        self.expected_period_ns = expected_period_ns
        self.lifecycle_state = lifecycle_state

    def is_silent(self, *, now_t_mono_ns: int, slack: float = 2.0) -> bool:
        """Return ``True`` when the producer hasn't emitted in
        ``slack * expected_period_ns``. Returns ``False`` if the adapter
        has not yet emitted at all (start-up grace), or if the adapter's
        :attr:`lifecycle_state` is anything other than ``"running"`` —
        a stopped or closed adapter is *expected* to be silent."""
        if self.lifecycle_state is not None and self.lifecycle_state != "running":
            return False
        if self.last_t_mono_ns is None:
            return False
        elapsed = now_t_mono_ns - self.last_t_mono_ns
        return elapsed > int(slack * self.expected_period_ns)


_DAQMX_MODULE_SUFFIX_RE = re.compile(r"Mod\d+$")
"""Strips a trailing ``ModN`` from a cDAQ module name to derive the chassis
name (``cDAQ1Mod1`` → ``cDAQ1``)."""


def serial_resource_id(port: str) -> str:
    """Return the ``resource_id`` for a serial-port adapter.

    Per ``docs/per-resource-worker-migration.md`` §4.10: two adapters sharing
    a serial port (multi-drop RS-485 bus) must share a worker. Windows COM
    names are case-insensitive at the OS layer, so the body is normalized to
    upper case — ``"com6"`` and ``"COM6"`` collapse to the same worker.
    """
    return f"serial:{port.strip().upper()}"


def daqmx_resource_id_from_channels(physical_channels: Iterable[str]) -> str:
    """Return the ``resource_id`` for a DAQmx adapter, derived from its
    physical-channel strings (e.g. ``"cDAQ1Mod1/ai0"``).

    The contention domain is the chassis (cDAQ) or the device (single-board
    card). Channel strings are parsed locally — no call into ``nidaqmx`` —
    so the property is safe to read before :meth:`open` and in sim/CI where
    the NI runtime is absent. Per the migration doc §4.10: ``cDAQ1Mod1`` and
    ``cDAQ1Mod3`` collapse to ``daqmx:cDAQ1``; a single-board ``Dev1`` stays
    ``daqmx:Dev1``.
    """
    for raw in physical_channels:
        head = raw.split("/", 1)[0] if "/" in raw else raw
        if not head:
            continue
        stripped = _DAQMX_MODULE_SUFFIX_RE.sub("", head)
        return f"daqmx:{stripped}"
    return "daqmx:unknown"


__all__ = [
    "LastSampleTracker",
    "WatchdogState",
    "build_channel_sample",
    "channels_for_device",
    "daqmx_resource_id_from_channels",
    "make_accepted_result",
    "make_not_open_result",
    "make_record_id",
    "make_unauthorized_result",
    "reject_unless_authorized",
    "serial_resource_id",
]
