""":class:`DeviceAdapter` Protocol, :class:`Capability` flags, command surface.

Plan §5.2. Adapters wrap each device library's Manager with a uniform API.
``open``/``close`` is the connection layer (USB/serial/IP); ``start``/``stop``
is the sampling layer — separated so hardware-clocked NI tasks can be armed at
run start (not at adapter open) and so a transient I/O hiccup can be recovered
from without renegotiating the device.
"""

from __future__ import annotations

from collections.abc import AsyncIterable
from datetime import datetime
from enum import Flag, auto
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from capa.devices.records import DeviceEmission


class Capability(Flag):
    """Adapter capability flags.

    Used by the UI to gate widgets ("show ramp control only if the adapter
    declares ``HAS_RAMP``") and by :meth:`Procedure.preflight` to validate
    against requirements declared in plugin metadata. Plan §5.2.
    """

    NONE = 0
    HAS_SETPOINT = auto()
    HAS_RAMP = auto()
    HAS_TARE = auto()
    HAS_ZERO = auto()
    HARDWARE_CLOCKED = auto()
    EMITS_BLOCKS = auto()
    SUPPORTS_DISCOVERY = auto()
    READS_PROCESS_VAR = auto()
    WRITES_DIGITAL = auto()
    HAS_GAS_SELECT = auto()
    """Adapter exposes a selectable gas / fluid (Alicat MFC, MFM)."""
    EMITS_STABILITY_FLAG = auto()
    """Per-sample stability/settling flag travels alongside the value (Sartorius
    balance ``stable`` flag). Procedures can preflight on it ("balance must
    report stable for 5 s before ignition")."""
    SUPPORTS_AUTO_RECONNECT = auto()
    """Adapter retries transient connection errors silently and surfaces them
    as a watchdog metric instead of failing the run. Informational; gated by
    the per-adapter ``auto_reconnect`` parameter."""


class DeviceCommand(BaseModel):
    """Generic command issued via :meth:`DeviceAdapter.command`.

    Plan §9: every device-write carries ``issued_by``, ``authorization_id``,
    and ``confirmed_by``. Scheduled method/procedure commands inherit the
    arm/start authorization; manual overrides require an immediate confirmation
    in the UI. The engine refuses to issue a command without one of those.

    Concrete adapters *also* expose typed methods (e.g.
    ``WatlowAdapter.set_setpoint``) for IDE help and refactor safety; this
    generic form exists for plugins that don't know the concrete type.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str
    """Adapter-defined verb. ``"set_setpoint"``, ``"set_parameter"``,
    ``"tare"``, ``"zero"``, ``"start_ramp"``, ..."""
    target: str | None = None
    """Channel name / parameter name / etc. Adapter interprets."""
    payload: dict[str, Any] = Field(default_factory=dict)
    issued_by: str
    """Operator id of the person who initiated this command."""
    authorization_id: str | None = None
    """Run-arm authorization that covers this command. ``None`` means the
    command is a manual override outside any run; the adapter then requires
    :attr:`confirmed_by`."""
    confirmed_by: str | None = None
    """Operator id of the person who explicitly confirmed a manual command at
    the UI. Required when :attr:`authorization_id` is ``None``."""


class CommandResult(BaseModel):
    """What an adapter returns from :meth:`DeviceAdapter.command`."""

    model_config = ConfigDict(extra="forbid")
    accepted: bool
    detail: str = ""
    t_mono_ns: int
    t_utc: datetime


@runtime_checkable
class DeviceAdapter(Protocol):
    """Uniform device surface.

    Plan §5.2. Concrete adapters live under :mod:`capa.devices` (real) and
    :mod:`capa.devices.sim` (simulated, P0a). All four production adapters
    wrap their respective library Manager.
    """

    name: str
    """Adapter-assigned device name. Used as the key in
    :class:`~capa.experiment.config.HardwareProfile` and as
    :attr:`SourceBinding.device`."""

    capabilities: frozenset[Capability]
    """The set of :class:`Capability` flags this adapter declares."""

    async def open(self) -> None:
        """Establish the connection (open serial port, query identity).

        Idempotent against re-entry: a second call on an already-open adapter
        is a no-op rather than an error.
        """
        ...

    async def close(self) -> None:
        """Release the bus / handle. Idempotent."""
        ...

    async def start(self) -> None:
        """Begin sampling / arm hardware-clocked tasks. Requires :meth:`open`
        to have completed."""
        ...

    async def stop(self) -> None:
        """Stop sampling without closing the connection."""
        ...

    async def snapshot(self) -> DeviceEmission:
        """Return a :class:`DeviceSnapshot` capturing current health/config.

        Routed to ``status.sqlite``; never goes through the main fan-out.
        """
        ...

    def stream(self) -> AsyncIterable[DeviceEmission]:
        """Yield emissions while sampling is active.

        The plan's §5.2 wording uses ``poll() -> list[DeviceEmission]``; capa
        uses an async-iterator instead because every underlying library
        already exposes one (``recorder.stream()``), and it composes more
        cleanly with AnyIO task groups. Adapters back this with their library's
        existing recorder.
        """
        ...

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        """Issue a generic command. Concrete adapters layer typed wrappers on top."""
        ...


# ---------------------------------------------------------------------------
# Lifecycle states — used by sim adapters to enforce the open/start ordering
# documented above. Concrete real adapters reuse this.
# ---------------------------------------------------------------------------


AdapterState = Literal["closed", "open", "running"]


class AdapterLifecycle:
    """Shared state machine for adapter lifecycles.

    Used by sim and real adapters to keep the open/close/start/stop dance
    consistent. Not part of the public Protocol — adapters delegate to it
    internally.
    """

    __slots__ = ("_state",)

    def __init__(self) -> None:
        self._state: AdapterState = "closed"

    @property
    def state(self) -> AdapterState:
        return self._state

    def assert_can_open(self) -> bool:
        return self._state == "closed"

    def open(self) -> None:
        if self._state in ("open", "running"):
            return  # idempotent
        self._state = "open"

    def close(self) -> None:
        self._state = "closed"

    def start(self) -> None:
        if self._state == "closed":
            raise RuntimeError("adapter must be open before start()")
        self._state = "running"

    def stop(self) -> None:
        if self._state != "running":
            return
        self._state = "open"


__all__ = [
    "AdapterLifecycle",
    "AdapterState",
    "Capability",
    "CommandResult",
    "DeviceAdapter",
    "DeviceCommand",
]
