""":class:`Procedure` Protocol and :class:`ProcedureContext`.

A procedure is a state machine that walks through one run. Plugins
register a class implementing :class:`Procedure`; the engine instantiates,
calls :meth:`Procedure.preflight` (returns a list of :class:`Problem`
warnings/errors; blocking errors abort arming), then
:meth:`Procedure.run` inside the engine task group.

The context is a small dataclass of references to engine-owned services —
clock, config, bundle writer (for events), data bus (for subscriptions),
logger, an external-stop event, the channel registry (for value lookups), an
authorization handle (so every device write carries the run's audit stamps),
the per-adapter command surface, and the :class:`MethodExecutor` for
method-bearing procedures.

"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, runtime_checkable

import anyio
import anyio.abc
import structlog
from pydantic import BaseModel, ConfigDict

from capa.channels.registry import ChannelRegistry
from capa.core.clock import RunClock
from capa.core.databus import DataBus
from capa.core.errors import CapaError
from capa.devices.adapter import DeviceAdapter
from capa.experiment.authorization import Authorization
from capa.experiment.config import ExperimentConfig
from capa.storage.bundle import RunBundleWriter

if TYPE_CHECKING:
    from capa.experiment.executor import MethodExecutor
    from capa.runtime.dispatch import CommandDispatcher
    from capa.runtime.emissions import ProcedureUiSink
    from capa.runtime.recording import ResolvedRecordingPlan


class ProcedureError(CapaError):
    """Raised when a procedure refuses to run (preflight failure) or when a
    procedure plugin cannot be loaded.

    Distinct from :class:`~capa.core.errors.PluginTrustError` (which is the
    plugins.lock policy refusal) so the engine can treat preflight failures
    as configuration errors and trust failures as security errors.
    """


# ---------------------------------------------------------------------------
# Problem — the value preflight returns. # ---------------------------------------------------------------------------


ProblemSeverity = Literal["info", "warning", "error"]


class Problem(BaseModel):
    """One preflight problem.

    The engine collects every problem returned by procedure + profile
    preflight, surfaces them to the caller, and refuses to arm if any has
    ``blocking=True``. Non-blocking problems land in the bundle as warnings.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    """Stable identifier (e.g. ``"capa.balance.unstable"``). Useful for
    machine-readable filtering and for cross-run analysis."""

    message: str
    severity: ProblemSeverity = "error"
    blocking: bool = True
    """If ``True``, the engine refuses to arm. Profile-level preflights
    typically use ``blocking=True`` for missing channel groups; per-run hints
    use ``blocking=False`` for warnings."""

    metadata: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# ChannelRequirement — what a procedure declares it needs from the rig.
# # ---------------------------------------------------------------------------


class ChannelRequirement(BaseModel):
    """Procedure-declared channel requirement.

    Distinct from :class:`capa.experiment.profiles.base.ChannelRequirement`
    which is *profile*-level. A procedure can demand specific named channels
    (``"heater.setpoint"``) or a kind (``"setpoint"``). The runtime preflight
    matches against the active :class:`HardwareProfile`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str | None = None
    """Exact channel name to require. ``None`` means "any channel matching
    ``kind``"."""

    kind: str | None = None
    """Acceptable :class:`ChannelKind` value. ``None`` = any."""

    min_count: int = 1
    """How many channels must satisfy the requirement."""


# ---------------------------------------------------------------------------
# OperatorCommand — UI-issued mid-run control message.
# ---------------------------------------------------------------------------


OperatorCommandKind = Literal["pause", "resume", "accept_current"]
"""Inbound UI commands a long-running procedure can react to.

* ``pause`` / ``resume`` — freeze the iteration loop without changing
  commanded setpoints; the procedure keeps subscribing to live samples
  so the operator can inspect a stable state.
* ``accept_current`` — force the current iteration to terminate with
  whatever the most recent rolling-window statistics are. Procedures
  that emit per-target results (e.g.
  :class:`~capa.experiment.procedures.builtin.heat_flux_tune.HeatFluxTune`)
  mark the resulting point with ``accept_reason="operator_override"``.

``abort`` is intentionally **not** in this list: a UI abort is
delivered through the existing ``external_stop`` event, which every
procedure already polls.
"""


@dataclass(frozen=True, slots=True)
class OperatorCommand:
    """One inbound operator command.

    The procedure consumes these from
    :attr:`ProcedureContext.operator_commands` (if wired). Cheap and
    cancellable — the consumer task closes its end of the stream on
    cancellation and the UI side sees an exception on next send.
    """

    kind: OperatorCommandKind
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ProcedureContext — what the engine hands to preflight() and run().
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ProcedureContext:
    """References handed to :meth:`Procedure.run` and :meth:`Procedure.preflight`.

    Engine-owned; the procedure must not store these past its own lifetime.
    """

    clock: RunClock
    """Single monotonic timebase for the run ()."""

    config: ExperimentConfig
    """Frozen run recipe. The procedure reads ``config.method``,
    ``config.procedure.config``, ``config.sample`` etc. but never mutates."""

    bundle_writer: RunBundleWriter
    """For ``write_event`` from the procedure layer. The data fan-out (sink
    writes) is the engine's job — the procedure does not call ``record_*``
    directly."""

    databus: DataBus
    """In-process pub/sub. Procedures subscribe to channels they care about
    via :meth:`DataBus.subscribe_channel` etc."""

    logger: structlog.stdlib.BoundLogger
    """Procedure-bound logger. The engine sets ``procedure_id`` in the
    contextvars before constructing this context, so log lines carry it
    automatically."""

    external_stop: anyio.Event
    """The CLI's SIGINT handler / UI's abort button sets this. Procedures
    that loop should poll it (or ``await`` on it) so a stop request actually
    stops them."""

    instruments: ChannelRegistry
    """Frozen channel registry. Used by procedures and the
    :class:`MethodExecutor` to resolve a channel name to its bound device +
    binding."""

    adapters: dict[str, DeviceAdapter]
    """Adapter handles keyed by device name (matches
    :attr:`SourceBinding.device`). Provided for **introspection only** —
    procedures that need adapter metadata (capabilities, resource_id) can
    read here. Sending commands directly through ``adapter.command`` is
    deprecated; use :attr:`dispatcher` instead so the
    concurrency layer (single-loop Engine vs per-resource-worker
    Conductor) is transparent to procedures."""

    dispatcher: CommandDispatcher
    """Command dispatch surface. Procedures and the :class:`MethodExecutor`
    issue every device write through :meth:`CommandDispatcher.dispatch`.
    The engine wires :class:`~capa.runtime.dispatch.AdapterDispatcher` here
    for the single-loop path; the conductor wires
    :class:`~capa.runtime.dispatch.ConductorDispatcher` for per-resource-
    worker runs."""

    authorization: Authorization
    """Run-arm authorization handle. Every device command goes through
    :meth:`Authorization.issue` so ``issued_by`` / ``authorization_id`` are
    stamped consistently."""

    method_executor: MethodExecutor | None = None
    """Reusable method-walking service. ``None`` for procedures that don't
    need it (FreeRun); set by the engine when ``config.method`` is present."""

    metadata: dict[str, Any] | None = None
    """Optional procedure-private scratchpad. Not snapshotted into the bundle
    — for cross-step state during a single run."""

    operator_commands: anyio.abc.ObjectReceiveStream[OperatorCommand] | None = None
    """Inbound UI control stream. ``None`` when no UI is attached (CLI
    headless, test harness) — procedures that consume this MUST check for
    ``None`` before subscribing. The conductor/runner creates a paired
    send/receive stream and stores the receive end here; the UI calls
    into the runner to push commands."""

    ui_sink: ProcedureUiSink | None = None
    """Outbound UI-only telemetry sink. ``None`` when no UI is attached
    (CLI headless, tests) — procedures that emit
    :class:`~capa.runtime.emissions.ProcedureTick`\\ s MUST null-check
    before publishing.

    The conductor's :meth:`~capa.runtime.conductor.Conductor.procedure_ui_sink`
    wraps the same UI bridge that carries device emissions. Ticks
    short-circuit through :meth:`~capa.runtime.conductor.Conductor._publish_ui`
    — they never hit the writer or the data bus, by design: ticks are
    pure operator-facing mirror, not durable artifacts. Use
    :meth:`ProcedureContext.bundle_writer.write_event` for events that
    should land in the bundle (e.g. heat-flux tune's per-iteration audit
    events)."""


# ---------------------------------------------------------------------------
# Procedure protocol.
# ---------------------------------------------------------------------------


@runtime_checkable
class Procedure(Protocol):
    """The procedure plugin contract.

    A class implementing this Protocol can be registered on the
    ``capa.procedures`` entry-point group. Plugin loading checks the contract
    at registration time (:mod:`capa.core.plugins_runtime`).
    """

    id: ClassVar[str]
    """Plugin identifier (e.g. ``"capa.builtin.recipe_runner"``). Matched
    against ``ProcedureRef.id`` and against ``plugins.lock``."""

    name: ClassVar[str]
    """Human-readable display name used by the UI procedure picker."""

    version: ClassVar[str]
    """PEP 440 version string. Pinned in ``plugins.lock``."""

    config_model: ClassVar[type[BaseModel]]
    """Pydantic model that validates ``ExperimentConfig.procedure.config``.
    The auto-form generator (:mod:`capa.ui.forms.from_model`) renders this
    as an editable form on the Run tab."""

    required_capabilities: ClassVar[tuple[str, ...]]
    """:class:`Capability` flag names the procedure requires. The engine
    checks every adapter declares each flag at preflight time."""

    required_channels: ClassVar[tuple[ChannelRequirement, ...]]
    """Channels (by name or by kind) the procedure must have access to."""

    uses_method: ClassVar[bool]
    """Whether this procedure consumes ``ExperimentConfig.method``.

    ``True`` for procedures that walk a :class:`Method` via
    :class:`MethodExecutor` (the standard :class:`RecipeRunner`); ``False``
    for self-driving procedures that ignore or reject methods
    (:class:`FreeRun`, :class:`HeatFluxTune`). The UI reads this to
    decide whether the Method tab is meaningful for the currently
    selected procedure — when ``False``, the tab is disabled so the
    operator doesn't waste time editing steps that will never run.

    Defaults to ``True`` via :func:`procedure_uses_method` for plugins
    that don't declare it, matching the pre-existing always-visible
    behaviour.
    """

    async def preflight(self, ctx: ProcedureContext) -> list[Problem]:
        """Return a list of preflight :class:`Problem` records.

        An empty list means "good to go". The engine refuses to arm if any
        returned problem has ``blocking=True``. Non-blocking problems land
        in the bundle as warnings.

        Runs *before* :meth:`RunBundleWriter.open`, so a preflight refusal
        leaves no bundle on disk. Most procedures do not raise — they
        return problems instead — but a raised :class:`ProcedureError` is
        treated as a single blocking problem with code ``"procedure.error"``.
        """
        ...

    async def run(self, ctx: ProcedureContext) -> None:
        """Drive the run to completion (or until ``ctx.external_stop`` is
        set).

        Returning normally signals the engine to mark ``run_status="completed"``
        and finalize. Raising propagates as a crash.
        """
        ...

    def plan_capture(self, default_plan: ResolvedRecordingPlan) -> ResolvedRecordingPlan | None:
        """Return a narrower :class:`ResolvedRecordingPlan`, or ``None`` to
        inherit the default (record everything declared in the hardware
        profile).

        Optional — procedures that don't implement this get full-rig
        recording, preserving today's behaviour. Calibration / self-driving
        procedures override to declare exactly what they need: e.g.
        :class:`~capa.experiment.procedures.builtin.heat_flux_tune.HeatFluxTune`
        returns a plan with three channels and no cameras.

        Called once at arm time on the conductor loop, before
        :class:`~capa.runtime.runcontext.RunContext` is frozen and before
        any adapter's :meth:`start` is invoked. The procedure has not yet
        received its :class:`ProcedureContext`, so this method must
        derive its return value from the procedure's own ``cfg`` fields
        rather than channel-name string constants.
        """
        ...


def procedure_uses_method(cls: type[object]) -> bool:
    """Look up ``cls.uses_method`` with a safe default.

    Returns ``True`` for any class that does not declare ``uses_method``,
    preserving today's always-visible Method-tab behaviour for plugins
    written before this attribute was added to the Protocol.
    """
    value = getattr(cls, "uses_method", True)
    return bool(value)


__all__ = [
    "ChannelRequirement",
    "OperatorCommand",
    "OperatorCommandKind",
    "Problem",
    "ProblemSeverity",
    "Procedure",
    "ProcedureContext",
    "ProcedureError",
    "procedure_uses_method",
]
