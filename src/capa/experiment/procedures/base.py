""":class:`Procedure` Protocol and :class:`ProcedureContext`.

Plan §11. A procedure is a state machine that walks through one run. Plugins
register a class implementing :class:`Procedure`; the engine instantiates,
calls :meth:`Procedure.preflight` (returns a list of :class:`Problem`
warnings/errors; blocking errors abort the arm phase), then
:meth:`Procedure.run` inside the engine task group.

The context is a small dataclass of references to engine-owned services —
clock, config, bundle writer (for events), data bus (for subscriptions),
logger, an external-stop event, the channel registry (for value lookups), an
authorization handle (so every device write carries the run's audit stamps),
the per-adapter command surface, and the :class:`MethodExecutor` for
method-bearing procedures.

P0c shipped a tiny prefix; P3 promotes this to the full §11 contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, runtime_checkable

import anyio
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


class ProcedureError(CapaError):
    """Raised when a procedure refuses to run (preflight failure) or when a
    procedure plugin cannot be loaded.

    Distinct from :class:`~capa.core.errors.PluginTrustError` (which is the
    plugins.lock policy refusal) so the engine can treat preflight failures
    as configuration errors and trust failures as security errors.
    """


# ---------------------------------------------------------------------------
# Problem — the value preflight returns. Plan §11 line 915.
# ---------------------------------------------------------------------------


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
# Plan §11 line 913.
# ---------------------------------------------------------------------------


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
# ProcedureContext — what the engine hands to preflight() and run().
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ProcedureContext:
    """References handed to :meth:`Procedure.run` and :meth:`Procedure.preflight`.

    Engine-owned; the procedure must not store these past its own lifetime.
    """

    clock: RunClock
    """Single monotonic timebase for the run (plan §6)."""

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
    :attr:`SourceBinding.device`). Procedures and executors send commands
    through these."""

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


# ---------------------------------------------------------------------------
# Procedure protocol — full §11 contract.
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


__all__ = [
    "ChannelRequirement",
    "Problem",
    "ProblemSeverity",
    "Procedure",
    "ProcedureContext",
    "ProcedureError",
]
