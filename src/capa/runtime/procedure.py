""":class:`ProcedureRunner` — adapter between the procedure plugin layer
and the :class:`ConductorRunner` contract.

The conductor doesn't know about
:class:`~capa.experiment.procedures.base.Procedure` plugins; it only knows
about the :class:`~capa.runtime.conductor.ConductorRunner` protocol (one
``preflight``, one ``run``). :class:`ProcedureRunner` is the thin adapter:

* Wraps a :class:`Procedure` instance.
* Constructs the :class:`ProcedureContext` from the conductor-supplied
  :class:`RunContext` plus the per-run pieces that the conductor doesn't
  carry (adapters dict for introspection, dispatcher, authorization,
  method executor).
* Translates ``Procedure.preflight``'s :class:`Problem` list into either a
  silent pass (no blocking problems) or a :class:`ProcedureError` raise
  (the conductor will surface this as ``run_status="crashed"``).
* Routes ``Procedure.run`` through with the constructed context.

What it does **not** do:

* Build the procedure plugin itself. The caller resolves the procedure
  via :class:`~capa.experiment.procedures.registry.ProcedureRegistry` and
  passes the instance in.
* Resolve channels, build adapters, open the bundle. All of that is the
  session/factory's job.
* Hard-stop a procedure. The conductor's normal cancel scope handles
  that — :meth:`run` propagates :class:`asyncio.CancelledError`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import anyio
import structlog

from capa.experiment.procedures.base import (
    OperatorCommand,
    ProcedureContext,
    ProcedureError,
)

if TYPE_CHECKING:
    from anyio.streams.memory import (
        MemoryObjectReceiveStream,
        MemoryObjectSendStream,
    )

    from capa.channels.registry import ChannelRegistry
    from capa.core.databus import DataBus
    from capa.devices.adapter import DeviceAdapter
    from capa.experiment.authorization import Authorization
    from capa.experiment.config import ExperimentConfig
    from capa.experiment.executor import MethodExecutor
    from capa.experiment.procedures.base import Problem, Procedure
    from capa.runtime.dispatch import CommandDispatcher
    from capa.runtime.emissions import ProcedureUiSink
    from capa.runtime.runcontext import RunContext
    from capa.storage.bundle import RunBundleWriter


_logger = structlog.get_logger("capa.runtime.procedure")


class ProcedureRunner:
    """Adapt a :class:`Procedure` to :class:`ConductorRunner`.

    The runner is **per-run** — construct one each time you start a
    conductor. State carried across runs would be a bug; the procedure
    plugin layer already has its own per-instance lifecycle.

    :param procedure: The resolved procedure plugin instance.
    :param config: The frozen run recipe.
    :param channel_registry: Frozen channel registry the procedure resolves
        names against.
    :param dispatcher: Command-dispatch surface (typically a
        :class:`~capa.runtime.dispatch.ConductorDispatcher` for runs through
        the conductor; an :class:`AdapterDispatcher` for engine-path tests).
    :param authorization: Run-arm authorization handle.
    :param adapters: Adapter handles keyed by device name (introspection
        only — the dispatcher does the commanding).
    :param external_stop: Procedures that loop poll this; the conductor
        sets it during shutdown so blocking procedures exit promptly.
    :param bundle_writer: For procedure-side event recording (via
        ``ctx.bundle_writer.write_event(...)``). The conductor's drain
        tasks own all *data* writes — procedures only record structured
        events.
    :param method_executor: Optional. Procedures that walk a method (recipe
        runner, etc.) get the executor preconstructed against the same
        context.
    :param ui_sink: Optional UI-only telemetry sink — when wired, the
        procedure can publish :class:`~capa.runtime.emissions.ProcedureTick`
        payloads through ``ctx.ui_sink.publish(tick)`` and they land
        on the UI bridge without touching the writer or the data bus.
        ``None`` for headless / test paths; the conductor's
        :meth:`~capa.runtime.conductor.Conductor.procedure_ui_sink`
        is the production source.
    """

    __slots__ = (
        "_adapters",
        "_authorization",
        "_bundle_writer",
        "_channel_registry",
        "_config",
        "_dispatcher",
        "_external_stop",
        "_logger",
        "_method_executor",
        "_op_cmd_recv",
        "_op_cmd_send",
        "_proc_ctx",
        "_procedure",
        "_stop_signal",
        "_ui_sink",
    )

    def __init__(
        self,
        *,
        procedure: Procedure,
        config: ExperimentConfig,
        channel_registry: ChannelRegistry,
        dispatcher: CommandDispatcher,
        authorization: Authorization,
        adapters: dict[str, DeviceAdapter],
        bundle_writer: RunBundleWriter,
        method_executor: MethodExecutor | None = None,
        stop_signal: asyncio.Event | None = None,
        ui_sink: ProcedureUiSink | None = None,
    ) -> None:
        self._procedure = procedure
        self._config = config
        self._channel_registry = channel_registry
        self._dispatcher = dispatcher
        self._authorization = authorization
        self._adapters = adapters
        # ``stop_signal`` is the conductor's loop-local completion event;
        # ``external_stop`` (the anyio.Event the procedure sees in its
        # ProcedureContext) is created on the conductor's loop inside
        # :meth:`_build_proc_ctx` and bridged to ``stop_signal`` by a
        # small linker task spawned in :meth:`run`. This avoids the cross-
        # loop binding error you'd get if the caller's external_stop event
        # (created on the calling loop) were passed straight through.
        self._stop_signal = stop_signal
        self._external_stop: anyio.Event | None = None
        self._bundle_writer = bundle_writer
        self._method_executor = method_executor
        self._proc_ctx: ProcedureContext | None = None
        # Operator-command stream — paired send/receive channel, created
        # lazily in :meth:`_build_proc_ctx` on the conductor loop so the
        # streams are loop-local. The UI side calls
        # :meth:`send_operator_command` to push messages onto the send
        # end; the procedure consumes the receive end via
        # ``ctx.operator_commands``.
        self._op_cmd_send: MemoryObjectSendStream[OperatorCommand] | None = None
        self._op_cmd_recv: MemoryObjectReceiveStream[OperatorCommand] | None = None
        # UI-only telemetry sink — wired by the conductor so the
        # procedure can publish ProcedureTicks without learning about
        # the bridge. ``None`` for headless / test paths; the procedure
        # must null-check before calling .publish().
        self._ui_sink: ProcedureUiSink | None = ui_sink
        self._logger = structlog.get_logger("capa.runtime.procedure").bind(
            procedure=getattr(procedure, "id", "<unknown>"),
        )

    @property
    def procedure(self) -> Procedure:
        """The wrapped procedure plugin instance. Read by the conductor
        at arm time to invoke :meth:`Procedure.plan_capture` for the
        run's recording-plan resolution."""
        return self._procedure

    async def preflight(self, ctx: RunContext, bus: DataBus) -> None:
        """Conductor preflight hook.

        Constructs the :class:`ProcedureContext` once (re-used in
        :meth:`run`) and invokes :meth:`Procedure.preflight`. Blocking
        problems raise :class:`ProcedureError`; non-blocking problems are
        logged and written to the bundle as warnings.

        The conductor catches the raised error and turns it into a
        ``RunOutcome.CRASHED`` with the failure reason — the bundle still
        seals so the operator can inspect what went wrong.
        """
        self._proc_ctx = self._build_proc_ctx(ctx, bus)
        problems: list[Problem] = await self._procedure.preflight(self._proc_ctx)
        # ``blocking=True`` refuses the run regardless of severity;
        # non-blocking is a warning regardless of severity.
        blocking = [p for p in problems if p.blocking]
        warnings = [p for p in problems if not p.blocking]
        for p in warnings:
            self._logger.warning(
                "procedure.preflight.warning",
                code=p.code,
                message=p.message,
                severity=str(p.severity),
            )
            # Best-effort bundle record; do not let a write failure mask
            # the procedure's preflight result.
            try:
                self._bundle_writer.write_event(
                    kind="procedure.preflight.warning",
                    message=p.message,
                    severity="warning",
                    source="procedure",
                    t_mono_ns=ctx.clock.t_mono_ns(),
                    t_utc=datetime.now(UTC),
                    metadata={"code": p.code},
                )
            except BaseException as exc:
                self._logger.warning(
                    "procedure.preflight.warning_record_failed",
                    error=str(exc),
                )
        if blocking:
            messages = "; ".join(f"{p.code}: {p.message}" for p in blocking)
            raise ProcedureError(f"procedure preflight refused: {messages}")

    async def run(self, ctx: RunContext, bus: DataBus) -> None:
        """Conductor run hook.

        Re-uses the :class:`ProcedureContext` built in :meth:`preflight`;
        falls back to a fresh build if preflight wasn't called (defensive,
        but the conductor always calls preflight first).

        Spawns a "stop linker" task that mirrors the conductor's loop-
        local completion event into the procedure's ``external_stop``,
        so a procedure that ``await ctx.external_stop.wait()``s exits
        cleanly when the conductor signals shutdown — instead of
        bubbling up a :class:`CancelledError` mid-handler.

        Returning normally signals "procedure complete"; raising signals
        "procedure crashed". The conductor's task-group cancel scope is
        the cancellation channel.
        """
        proc_ctx = self._proc_ctx or self._build_proc_ctx(ctx, bus)
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._link_stop_signal)
            try:
                await self._procedure.run(proc_ctx)
            finally:
                # Ensure the linker exits whatever the outcome — without
                # this it'd wait forever on the stop signal and stall the
                # surrounding task group.
                tg.cancel_scope.cancel()

    async def _link_stop_signal(self) -> None:
        """Mirror the conductor's completion event into the procedure's
        loop-local ``external_stop`` event.

        No-op when ``stop_signal`` is unwired (tests / non-conductor
        callers). Exits via task-group cancel when the procedure returns.
        """
        if self._stop_signal is None or self._external_stop is None:
            await anyio.sleep_forever()
            return
        try:
            await asyncio.wait_for(self._stop_signal.wait(), timeout=None)
            self._external_stop.set()
        except asyncio.CancelledError:
            raise

    def send_operator_command(self, cmd: OperatorCommand) -> bool:
        """Push one operator command onto the procedure's inbound stream.

        Returns ``True`` if the command was queued, ``False`` if the
        stream is unwired (procedure hasn't been built yet or the
        runner predates the command-stream feature for this run). The
        UI side calls this from its loop; the procedure consumer runs
        on the conductor loop and picks the command up via
        ``ctx.operator_commands``.

        Non-blocking. A buffer-full condition (deeply unusual — the
        consumer is a tight async loop) is treated as a drop and
        logged; the UI should not freeze on operator-command delivery.
        """
        if self._op_cmd_send is None:
            return False
        try:
            self._op_cmd_send.send_nowait(cmd)
        except (anyio.WouldBlock, anyio.BrokenResourceError, anyio.ClosedResourceError) as exc:
            self._logger.warning("operator_command.drop", kind=cmd.kind, error=str(exc))
            return False
        return True

    def _build_proc_ctx(self, ctx: RunContext, bus: DataBus) -> ProcedureContext:
        # The external_stop event must be loop-local — anyio.Event is bound
        # to whatever loop creates it. _build_proc_ctx runs on the
        # conductor loop, so the event we make here is safe for the
        # procedure (also on the conductor loop) to await on.
        if self._external_stop is None:
            self._external_stop = anyio.Event()
        # Operator-command stream is loop-local for the same reason. The
        # buffer size of 8 is generous for a human-driven UI — operator
        # clicks come at human speed but a pause→resume burst could
        # arrive while the consumer is parked on a databus message.
        if self._op_cmd_send is None or self._op_cmd_recv is None:
            send_stream, recv_stream = anyio.create_memory_object_stream[OperatorCommand](
                max_buffer_size=8
            )
            self._op_cmd_send = send_stream
            self._op_cmd_recv = recv_stream
        return ProcedureContext(
            clock=ctx.clock,
            config=self._config,
            bundle_writer=self._bundle_writer,
            databus=bus,
            logger=self._logger.bind(component="procedure"),
            external_stop=self._external_stop,
            instruments=self._channel_registry,
            adapters=self._adapters,
            dispatcher=self._dispatcher,
            authorization=self._authorization,
            method_executor=self._method_executor,
            metadata={},
            operator_commands=self._op_cmd_recv,
            ui_sink=self._ui_sink,
        )


__all__ = ["ProcedureRunner"]
