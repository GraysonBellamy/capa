""":class:`MethodExecutor` — reusable segmented-profile walker.

Plan §5.3 / §11. The executor is a *service*, not an abstract class. The
builtin :class:`~capa.experiment.procedures.builtin.recipe_runner.RecipeRunner`
is a one-line wrapper that calls :meth:`run_to_completion`; custom procedures
can call :meth:`advance_until` / :meth:`run_segment` and interleave their own
phases.

The executor:

* Walks the steps of a :class:`Method` in order.
* Issues device commands through :class:`Authorization` so every write
  carries ``issued_by`` + ``authorization_id``.
* Writes ``method.step.entered`` / ``method.step.exited`` events into the
  bundle, plus ``method.command.issued`` for every device write.
* Subscribes to the data bus for ``wait`` / stability / end-condition checks.
* Honors ``ctx.external_stop`` — every loop polls or awaits it so the
  operator's abort button stops execution promptly.
* Raises :class:`MethodExecutorError` on a step that cannot be executed
  (e.g. unknown channel, no adapter for the bound device); the engine
  classifies that as a crashed run.

Step-kind dispatch lives in private ``_run_<kind>`` methods. ``custom`` steps
look up a handler in :attr:`custom_handlers` (populated by the loading
procedure); an unmapped ``handler_id`` raises.

The executor is **not** thread-safe; it expects to be called from the
procedure's task only.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import anyio

from capa.core.errors import CapaError
from capa.devices.records import ChannelSample
from capa.experiment.method import (
    AcquireStep,
    CustomStep,
    EndCondition,
    HoldStep,
    Method,
    PromptStep,
    RampStep,
    SafeShutdownStep,
    SetpointStep,
    Step,
    WaitStep,
)

if TYPE_CHECKING:
    from capa.experiment.procedures.base import ProcedureContext


# Custom-step handler signature: receives the executor + the step + the run
# context. The handler is responsible for any device commands; it returns
# normally on success, raises to abort.
CustomHandler = Callable[["MethodExecutor", CustomStep, "ProcedureContext"], Awaitable[None]]


class MethodExecutorError(CapaError):
    """Raised when a step cannot be executed (unknown channel, no adapter,
    unmapped custom handler, end-condition references a missing channel).

    Distinct from :class:`~capa.experiment.procedures.base.ProcedureError`
    because the failure is *during* execution, not at preflight."""


# ---------------------------------------------------------------------------
# Tunables. Plan §15 — these belong as named constants so a perf regression
# test can pin behaviour.
# ---------------------------------------------------------------------------

DEFAULT_RAMP_TICK_HZ: float = 10.0
"""Setpoint-update cadence for ``ramp`` steps. Hardware closes the loop;
capa just drips setpoints. 10 Hz is plenty for a 2 K/s ramp on a controller
with a 1 s integration window."""

DEFAULT_WAIT_POLL_HZ: float = 20.0
"""Channel-condition evaluation rate inside :meth:`_run_wait`. Faster than
the typical 1–10 Hz scientific channel so a step exits within ~50 ms of the
condition becoming true."""

PROMPT_HEADLESS_DEFAULT_TIMEOUT_S: float = 30.0
"""Soft default when a headless run hits a ``prompt`` step without
``auto_acknowledge_prompts``. The executor logs a warning and aborts the
step rather than blocking forever."""


# ---------------------------------------------------------------------------
# MethodExecutor
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MethodExecutor:
    """Reusable segmented-profile walker.

    Bound to one run via the :class:`ProcedureContext`. Construct once per
    run; do not reuse across runs.
    """

    ctx: ProcedureContext
    """The procedure context this executor commands through."""

    custom_handlers: dict[str, CustomHandler] = field(default_factory=dict)
    """``handler_id`` → coroutine. Procedures that consume ``custom`` steps
    populate this before calling :meth:`run_to_completion`."""

    auto_acknowledge_prompts: bool = False
    """When ``True``, ``prompt`` steps auto-acknowledge after a small delay
    instead of blocking on operator input. Used by headless tests and by the
    ``Batch`` driver — never by an interactive run."""

    _current_step_index: int | None = field(default=None, init=False)

    # ---------------------------------------------------------------- public

    @property
    def current_step_index(self) -> int | None:
        return self._current_step_index

    async def run_to_completion(self, method: Method) -> None:
        """Walk every step in order. Returns when the last step finishes
        or ``ctx.external_stop`` fires.

        This is the recipe-runner pattern. Custom procedures that want to
        interleave can call :meth:`advance_until` instead.
        """
        await self._run_range(method, start=0, stop=len(method.steps))

    async def advance_until(self, method: Method, step_id: int) -> None:
        """Run steps ``[current..step_id)``.

        ``step_id`` is exclusive — call again with the same id to resume
        from where you left off. Useful for procedures that want to insert
        custom phases between method steps.
        """
        if step_id < 0 or step_id > len(method.steps):
            raise MethodExecutorError(
                f"advance_until: step_id {step_id} out of range [0, {len(method.steps)})"
            )
        start = (self._current_step_index or -1) + 1
        await self._run_range(method, start=start, stop=step_id)

    async def run_segment(self, step: Step) -> None:
        """Run a single step. Useful for one-off invocation of
        ``safe_shutdown`` from a fault handler.
        """
        await self._dispatch(step, index=-1)

    # ---------------------------------------------------------------- internals

    async def _run_range(self, method: Method, *, start: int, stop: int) -> None:
        for idx in range(start, stop):
            if self.ctx.external_stop.is_set():
                self.ctx.logger.info("method.executor.stopped", at=idx)
                return
            step = method.steps[idx]
            self._current_step_index = idx
            await self._dispatch(step, index=idx)

    async def _dispatch(self, step: Step, *, index: int) -> None:
        self._write_step_event(
            kind="method.step.entered",
            step=step,
            index=index,
            severity="info",
        )
        try:
            match step:
                case HoldStep():
                    await self._run_hold(step)
                case RampStep():
                    await self._run_ramp(step)
                case SetpointStep():
                    await self._run_setpoint(step)
                case WaitStep():
                    await self._run_wait(step)
                case PromptStep():
                    await self._run_prompt(step)
                case AcquireStep():
                    await self._run_acquire(step)
                case SafeShutdownStep():
                    await self._run_safe_shutdown(step)
                case CustomStep():
                    await self._run_custom(step)
        except Exception as exc:
            self._write_step_event(
                kind="method.step.failed",
                step=step,
                index=index,
                severity="error",
                extra={"error": str(exc), "error_type": type(exc).__name__},
            )
            raise
        else:
            self._write_step_event(
                kind="method.step.exited",
                step=step,
                index=index,
                severity="info",
            )

    # ---------- step kinds ------------------------------------------------

    async def _run_hold(self, step: HoldStep) -> None:
        """Command a setpoint and wait for ``duration_s`` or
        ``end_condition``."""
        await self._command_setpoint(step.target.name, step.value, kind="hold")
        await self._wait_for(
            duration_s=step.duration_s,
            end_condition=step.end_condition,
        )

    async def _run_ramp(self, step: RampStep) -> None:
        """Drip setpoints at :data:`DEFAULT_RAMP_TICK_HZ` from
        ``start_value`` to ``end_value``.

        ``rate_per_second`` and ``duration_s`` are alternative ways of
        specifying the slope; if both are given they must agree (Pydantic
        validates this on the model). ``start_value=None`` means "start
        from the current setpoint" — we read it from the latest data-bus
        value if available, otherwise we issue end_value immediately.
        """
        end = step.end_value
        start = step.start_value
        if start is None:
            start = self._latest_value(step.target.name)
            if start is None:
                self.ctx.logger.warning(
                    "method.ramp.no_start_value",
                    channel=step.target.name,
                    reason="no live sample on databus; jumping to end_value",
                )
                await self._command_setpoint(step.target.name, end, kind="ramp")
                return

        duration_s = step.duration_s
        if duration_s is None:
            assert step.rate_per_second is not None  # validated by Pydantic
            slope = step.rate_per_second
            if slope == 0:
                raise MethodExecutorError("ramp step has rate_per_second==0")
            duration_s = abs(end - start) / abs(slope)

        if duration_s <= 0 or math.isclose(start, end):
            await self._command_setpoint(step.target.name, end, kind="ramp")
            return

        tick_dt = 1.0 / DEFAULT_RAMP_TICK_HZ
        n_ticks = max(1, math.ceil(duration_s / tick_dt))
        actual_dt = duration_s / n_ticks
        for k in range(1, n_ticks + 1):
            if self.ctx.external_stop.is_set():
                return
            frac = k / n_ticks
            target_value = start + (end - start) * frac
            await self._command_setpoint(step.target.name, target_value, kind="ramp")
            # Sleep with cancellation respect so an external_stop wakes us.
            with anyio.move_on_after(actual_dt):
                await self.ctx.external_stop.wait()
            if self.ctx.external_stop.is_set():
                return

    async def _run_setpoint(self, step: SetpointStep) -> None:
        await self._command_setpoint(step.target.name, step.value, kind="setpoint")

    async def _run_wait(self, step: WaitStep) -> None:
        await self._wait_for(
            duration_s=step.duration_s,
            end_condition=step.end_condition,
            timeout_s=step.timeout_s,
            on_timeout=step.on_timeout,
        )

    async def _run_prompt(self, step: PromptStep) -> None:
        """Block until operator confirms.

        In headless mode (``auto_acknowledge_prompts``) we sleep a short
        interval and proceed. In UI mode the engine wires a confirm pathway
        that flips ``ctx.metadata['_prompt_confirmed']`` (set by the run
        tab); we poll it and write a ``method.prompt.acknowledged`` event.
        """
        self.ctx.bundle_writer.write_event(
            kind="method.prompt.shown",
            message=step.message,
            severity="info",
            source="method_executor",
            t_mono_ns=self.ctx.clock.t_mono_ns(),
            t_utc=datetime.now(UTC),
            metadata={"title": step.title, "timeout_s": step.timeout_s},
        )
        if self.auto_acknowledge_prompts:
            await anyio.sleep(0)
            self.ctx.bundle_writer.write_event(
                kind="method.prompt.acknowledged",
                message=f"auto-ack: {step.message}",
                severity="info",
                source="method_executor",
                t_mono_ns=self.ctx.clock.t_mono_ns(),
                t_utc=datetime.now(UTC),
                metadata={"by": "auto_acknowledge"},
            )
            return

        timeout_s = step.timeout_s or PROMPT_HEADLESS_DEFAULT_TIMEOUT_S
        confirmed = False
        with anyio.move_on_after(timeout_s) as scope:
            while not self.ctx.external_stop.is_set():
                if self._prompt_was_confirmed():
                    confirmed = True
                    return
                await anyio.sleep(0.1)
        if not confirmed:
            severity = "warning" if scope.cancelled_caught else "error"
            reason = "timeout" if scope.cancelled_caught else "external_stop"
            self.ctx.bundle_writer.write_event(
                kind="method.prompt.unanswered",
                message=f"prompt not acknowledged: {step.message}",
                severity=severity,
                source="method_executor",
                t_mono_ns=self.ctx.clock.t_mono_ns(),
                t_utc=datetime.now(UTC),
                metadata={"reason": reason},
            )
            if reason == "timeout":
                raise MethodExecutorError(
                    f"prompt step timed out after {timeout_s}s without confirmation"
                )

    async def _run_acquire(self, step: AcquireStep) -> None:
        """Pure record-window: hold for ``duration_s`` without changing any
        control output."""
        with anyio.move_on_after(step.duration_s):
            await self.ctx.external_stop.wait()

    async def _run_safe_shutdown(self, step: SafeShutdownStep) -> None:
        """Drive each ``cool_target`` channel to its commanded value, then
        wait ``duration_s`` (or until external_stop)."""
        for channel_name, value in step.cool_target.items():
            try:
                await self._command_setpoint(channel_name, value, kind="safe_shutdown")
            except MethodExecutorError as exc:
                # A missing channel during cooldown is logged but does not
                # abort the rest of the cooldown — we still want the other
                # targets to receive their safe values.
                self.ctx.logger.warning(
                    "method.safe_shutdown.command_failed",
                    channel=channel_name,
                    error=str(exc),
                )
        if step.duration_s is not None and step.duration_s > 0:
            with anyio.move_on_after(step.duration_s):
                await self.ctx.external_stop.wait()

    async def _run_custom(self, step: CustomStep) -> None:
        handler = self.custom_handlers.get(step.handler_id)
        if handler is None:
            raise MethodExecutorError(
                f"custom step references unknown handler_id {step.handler_id!r}; "
                f"loading procedure must register it before run"
            )
        await handler(self, step, self.ctx)

    # ---------- helpers ---------------------------------------------------

    async def _command_setpoint(self, channel_name: str, value: float, *, kind: str) -> None:
        """Resolve ``channel_name`` → device, build a :class:`DeviceCommand`
        through :class:`Authorization`, and dispatch it via the procedure
        context's :attr:`CommandDispatcher`.

        Plan §18 #12: every device write is attributable. The command's
        ``issued_by`` / ``authorization_id`` come from the run-arm
        :class:`Authorization`.

        Migration doc §3.5: the dispatch surface is an abstraction over
        the concurrency layer — engine-style ``AdapterDispatcher`` (direct
        in-loop call) and conductor-style ``ConductorDispatcher`` (worker-
        thread routed) both satisfy the same async contract, so this
        method is loop-shape-agnostic."""
        try:
            resolved = self.ctx.instruments.resolve(channel_name)
        except Exception as exc:
            raise MethodExecutorError(f"channel {channel_name!r} not in registry") from exc

        device = getattr(resolved.binding, "device", None)
        if device is None:
            raise MethodExecutorError(
                f"channel {channel_name!r} has no device binding (derived?); cannot command"
            )
        # We still validate that the device exists in the adapter map so a
        # misconfigured channel binding fails with the same MethodExecutorError
        # as before — the dispatcher would raise a different exception type
        # for an unknown device, but the executor's error vocabulary stays
        # stable for procedures that catch MethodExecutorError.
        if device not in self.ctx.adapters:
            raise MethodExecutorError(
                f"no adapter registered for device {device!r} (channel {channel_name!r})"
            )

        cmd = self.ctx.authorization.issue(
            kind="set_setpoint",
            target=channel_name,
            payload={
                "value": value,
                "channel": channel_name,
                "device": device,
                "step_kind": kind,
            },
        )
        result = await self.ctx.dispatcher.dispatch(device, cmd)
        self.ctx.bundle_writer.write_event(
            kind="method.command.issued",
            message=f"setpoint {channel_name}={value} (step={kind})",
            severity="info" if result.accepted else "warning",
            source="method_executor",
            t_mono_ns=self.ctx.clock.t_mono_ns(),
            t_utc=datetime.now(UTC),
            metadata={
                "channel": channel_name,
                "device": device,
                "value": value,
                "step_kind": kind,
                "accepted": result.accepted,
                "detail": result.detail,
                "issued_by": cmd.issued_by,
                "authorization_id": cmd.authorization_id,
            },
        )
        if not result.accepted:
            self.ctx.logger.warning(
                "method.command.rejected",
                channel=channel_name,
                value=value,
                detail=result.detail,
            )

    async def _wait_for(
        self,
        *,
        duration_s: float | None,
        end_condition: EndCondition | None,
        timeout_s: float | None = None,
        on_timeout: str = "warn",
    ) -> None:
        """Wait for ``duration_s`` and/or ``end_condition``.

        * Both ``None`` is a no-op (model validation prevents this for the
          ``wait``/``hold`` step kinds).
        * ``end_condition`` alone: poll the latest databus value at
          :data:`DEFAULT_WAIT_POLL_HZ`. ``timeout_s`` (if set) bounds the
          maximum wait.
        * ``duration_s`` alone: sleep, breakable by external_stop.
        * Both: whichever fires first wins."""
        if duration_s is None and end_condition is None:
            return

        condition_event = anyio.Event()
        if end_condition is not None:
            sub = self.ctx.databus.subscribe_channel(
                name=f"method-wait-{end_condition.channel}",
                channel=end_condition.channel,
            )
        else:
            sub = None

        async def watch_condition() -> None:
            assert sub is not None
            assert end_condition is not None
            async for emission in sub:
                if not isinstance(emission, ChannelSample):
                    continue
                if _condition_holds(end_condition, float(emission.value)):
                    condition_event.set()
                    return

        async def watch_duration() -> None:
            assert duration_s is not None
            await anyio.sleep(duration_s)
            condition_event.set()

        async def watch_external() -> None:
            await self.ctx.external_stop.wait()
            condition_event.set()

        timeout_fired = False
        try:
            async with anyio.create_task_group() as tg:
                if end_condition is not None:
                    tg.start_soon(watch_condition)
                if duration_s is not None:
                    tg.start_soon(watch_duration)
                tg.start_soon(watch_external)

                if timeout_s is not None and timeout_s > 0:

                    async def watch_timeout() -> None:
                        nonlocal timeout_fired
                        await anyio.sleep(timeout_s)
                        timeout_fired = True
                        condition_event.set()

                    tg.start_soon(watch_timeout)

                await condition_event.wait()
                tg.cancel_scope.cancel()
        finally:
            if sub is not None:
                self.ctx.databus.unsubscribe(sub)

        if timeout_fired and not self.ctx.external_stop.is_set():
            self.ctx.bundle_writer.write_event(
                kind="method.wait.timeout",
                message=f"wait timed out after {timeout_s}s",
                severity="warning" if on_timeout == "warn" else "error",
                source="method_executor",
                t_mono_ns=self.ctx.clock.t_mono_ns(),
                t_utc=datetime.now(UTC),
                metadata={"on_timeout": on_timeout, "timeout_s": timeout_s},
            )
            if on_timeout == "abort":
                raise MethodExecutorError(f"wait timed out (timeout_s={timeout_s})")
            if on_timeout == "safe_shutdown":
                self.ctx.external_stop.set()

    def _latest_value(self, channel_name: str) -> float | None:
        """Read the most recent value seen on the databus for ``channel_name``,
        or ``None`` if nothing has been published yet."""
        try:
            return self.ctx.databus.last_value(channel_name)
        except Exception:
            return None

    def _prompt_was_confirmed(self) -> bool:
        meta = self.ctx.metadata
        if meta is None:
            return False
        return bool(meta.pop("_prompt_confirmed", False))

    def _write_step_event(
        self,
        *,
        kind: str,
        step: Step,
        index: int,
        severity: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        meta: dict[str, Any] = {
            "step_index": index,
            "step_kind": step.kind,
        }
        target = getattr(step, "target", None)
        if target is not None:
            meta["target"] = target.name
        if extra is not None:
            meta.update(extra)
        self.ctx.bundle_writer.write_event(
            kind=kind,
            message=f"{kind}: idx={index} kind={step.kind}",
            severity=severity,
            source="method_executor",
            t_mono_ns=self.ctx.clock.t_mono_ns(),
            t_utc=datetime.now(UTC),
            metadata=meta,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _condition_holds(cond: EndCondition, value: float) -> bool:
    op = cond.op
    threshold = cond.value
    if op == ">":
        return value > threshold
    if op == ">=":
        return value >= threshold
    if op == "<":
        return value < threshold
    if op == "<=":
        return value <= threshold
    if op == "==":
        return value == threshold
    raise MethodExecutorError(f"unknown end-condition op {op!r}")


__all__ = [
    "DEFAULT_RAMP_TICK_HZ",
    "DEFAULT_WAIT_POLL_HZ",
    "PROMPT_HEADLESS_DEFAULT_TIMEOUT_S",
    "CustomHandler",
    "MethodExecutor",
    "MethodExecutorError",
]
