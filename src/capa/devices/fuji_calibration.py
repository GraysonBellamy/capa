"""A manual zero or span of a Fuji analyzer, driven across capa's commands.

fujilib drives the analyzer's front panel with the calibration keys
(:class:`fujilib.devices.keys.RemoteCalibration`): it selects the channel,
waits on the panel's wait step while the operator's gas settles, and sends
the key that calibrates only on an explicit request. That object is an async
context manager, and it must be entered and left in one task: leaving it is
what returns the panel to the measurement screen, however the run ends.

capa runs every :meth:`DeviceAdapter.command` in a task of its own. So one
background task owns a run from its first key to its cleanup
(:class:`CalibrationRun`), and the commands only start it, signal it and read
what it publishes:

* ``begin`` starts the task and returns once the panel is on the wait step,
  or raises what stopped it getting there.
* While it waits, the task reads the panel every ``interval`` and judges
  whether the reading is steady on the gas the operator named. The port is
  free between reads, so a recording goes on, its rows ``calibrating``.
* ``commit`` asks the task to send the key that calibrates. fujilib refuses
  it unless the reading is steady; the run then stays on the wait step.
* ``cancel`` asks the task to leave the wait step with ESC.
* Left alone for ``timeout_s``, the task cancels the run itself.
* ``close`` cancels the task; fujilib's cleanup still runs, bounded by
  ``cleanup_timeout_s``. The two together stay inside the time the worker
  gives an adapter to close, so the port is still released after them.

When the task ends it has the run's ``fujilib-calibration/1`` record.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Literal

import structlog
from fujilib import Analyzer, ChannelId, DeviceInfo, FujiAnalyzerStateError, FujiError, RangeInfo
from fujilib.devices.keys import CalibrationGas, RemoteCalibration, RunState
from fujilib.devices.panel import ManualCalibrationPlan
from fujilib.devices.steadiness import SteadinessRule, SteadinessVerdict

from capa.devices.fuji_labels import range_of

__all__ = [
    "IDLE",
    "CalibrationRun",
    "CalibrationState",
    "CalibrationStatus",
    "plan_summary",
]

CalibrationState = Literal["idle", "starting", "waiting", "calibrating", "ended"]

CLEANUP_TIMEOUT_S: Final = 1.5
"""How long fujilib may take to return the panel to measurement when a run is
stopped. Short, because a closing adapter waits for it."""

_CLOSE_MARGIN_S: Final = 0.5
"""What :meth:`CalibrationRun.close` waits beyond the cleanup itself."""

_logger = structlog.get_logger("capa.devices.fuji")


@dataclass(frozen=True, slots=True)
class CalibrationStatus:
    """What a calibration run shows the operator.

    Immutable, so the manual-control card can read it on another thread.
    """

    state: CalibrationState = "idle"
    channel: str | None = None
    channel_name: str | None = None
    """The channel by its gas, as the operator reads it: ``"O2"``."""
    kind: str | None = None
    """``"zero"`` or ``"span"``."""
    gas_value: float | None = None
    gas_unit: str | None = None
    gas_label: str | None = None
    plan: str | None = None
    """What the run calibrates: every channel and range, and its gas."""
    steady: bool | None = None
    """Whether the last read found the gas steady; ``None`` before the first."""
    reasons: tuple[str, ...] = ()
    """Per channel, by its gas, why the gas is or is not steady."""
    readings: Mapping[str, float | None] = field(default_factory=lambda: MappingProxyType({}))
    """The last reading of each channel the run calibrates, by channel id."""
    waited_s: float | None = None
    outcome: str | None = None
    """How the pass ended: ``completed``, ``failed``, ``cancelled`` or
    ``ambiguous``; ``None`` while it runs, or when no key opened one."""
    error: str | None = None
    """What stopped the run, when something did."""
    clean: bool | None = None
    """Whether the panel was left on the measurement screen with no
    calibration flag set."""
    record: Mapping[str, object] | None = None
    """The run's ``fujilib-calibration/1`` document, once it has ended."""


IDLE: Final = CalibrationStatus()
"""The status when no calibration has been begun."""


def plan_summary(
    plan: ManualCalibrationPlan,
    names: Mapping[str, str] | None = None,
    ranges: Sequence[RangeInfo] = (),
) -> str:
    """``"span of O2: O2 0–25 vol% against 20.95 vol%"``.

    Each channel by its gas in ``names`` and each range by its span in
    ``ranges``; by the analyzer's numbers where they do not say.
    """
    named = names or {}
    tables = {info.channel: info for info in ranges}
    parts: list[str] = []
    for target in plan.targets:
        gases = target.zero_gas if plan.kind.value == "zero" else target.span_gas
        channel = named.get(target.channel.value, target.channel.value)
        for number, gas, unit in zip(target.ranges, gases, target.units, strict=False):
            shown = "an unreadable gas setting" if gas is None else f"{gas:g} {unit}"
            note = "" if target.established else " (not established)"
            span = range_of(tables.get(target.channel), number)
            parts.append(f"{channel} {span} against {shown}{note}")
    selected = named.get(plan.channel.value, plan.channel.value)
    return f"{plan.kind.value} of {selected}: " + "; ".join(parts)


class CalibrationRun:
    """One manual zero or span, owned by one task (see the module docstring)."""

    def __init__(
        self,
        analyzer: Analyzer,
        plan: ManualCalibrationPlan,
        gas: CalibrationGas,
        *,
        operator: str,
        info: DeviceInfo | None,
        port: str,
        address: int,
        rule: SteadinessRule | None = None,
        interval_s: float = 0.5,
        timeout_s: float = 900.0,
        cleanup_timeout_s: float = CLEANUP_TIMEOUT_S,
        on_end: Callable[[CalibrationStatus], None] | None = None,
        names: Mapping[str, str] | None = None,
        ranges: Sequence[RangeInfo] = (),
    ) -> None:
        """Prepare a run of ``plan`` against ``gas``; nothing is sent yet.

        ``operator`` goes into the record. ``timeout_s`` is how long the run
        may sit on the wait step before it cancels itself. ``on_end`` is
        called once, on the event loop, with the final status. ``names``
        (channel id to gas) and ``ranges`` word what the status shows.
        """
        self._analyzer = analyzer
        self._plan = plan
        self._gas = gas
        self._names: Mapping[str, str] = MappingProxyType(dict(names or {}))
        self._ranges = tuple(ranges)
        self._operator = operator
        self._info = info
        self._port = port
        self._address = address
        self._rule = rule
        self._interval_s = interval_s
        self._timeout_s = timeout_s
        self._cleanup_timeout_s = cleanup_timeout_s
        self._on_end = on_end
        self._task: asyncio.Task[None] | None = None
        self._state: CalibrationState = "starting"
        self._request: Literal["commit", "cancel"] | None = None
        self._cancel_reason: str | None = None
        self._wake = asyncio.Event()
        self._ready = asyncio.Event()
        self._refused = asyncio.Event()
        self._done = asyncio.Event()
        self._refusal: FujiError | None = None
        self._begin_error: FujiError | None = None
        self._verdict: SteadinessVerdict | None = None
        self._readings: Mapping[str, float | None] = MappingProxyType({})
        self._outcome: str | None = None
        self._error: str | None = None
        self._clean: bool | None = None
        self._record: Mapping[str, object] | None = None

    # ------------------------------------------------------------------ what is known

    @property
    def active(self) -> bool:
        """Whether the owner task is still running: the panel is not yet released."""
        return self._task is not None and not self._task.done()

    @property
    def status(self) -> CalibrationStatus:
        """The run as it stands."""
        verdict = self._verdict
        unit = self._gas.unit
        channel = self._plan.channel.value
        return CalibrationStatus(
            state=self._state,
            channel=channel,
            channel_name=self._names.get(channel, channel),
            kind=self._plan.kind.value,
            gas_value=self._gas.value,
            gas_unit=str(unit) if unit is not None else None,
            gas_label=self._gas.label,
            plan=plan_summary(self._plan, self._names, self._ranges),
            steady=verdict.steady if verdict is not None else None,
            reasons=self._reasons(verdict),
            readings=self._readings,
            waited_s=verdict.elapsed_s if verdict is not None else None,
            outcome=self._outcome,
            error=self._error,
            clean=self._clean,
            record=self._record,
        )

    # ------------------------------------------------------------------ the commands' side

    async def begin(self) -> CalibrationStatus:
        """Start the owner task and wait for the panel to reach the wait step.

        Raises:
            FujiError: the run never got there: fujilib refused it (the panel
                not on measurement, key lock or output hold on, the gas not
                the calibration-gas setting, ...) or a read or key failed.
                The panel has been returned to measurement and the task has
                ended.
        """
        self._task = asyncio.get_running_loop().create_task(
            self._own(), name=f"fuji-calibration-{self._plan.channel.value}"
        )
        _ = await self._ready.wait()
        if self._begin_error is not None:
            raise self._begin_error
        if self._state == "ended":
            # The task ended between reaching the wait step and here, or for
            # a reason that is not fujilib's.
            raise FujiAnalyzerStateError(
                f"the calibration ended before its wait step: {self._error or 'no reason given'}"
            )
        return self.status

    async def commit(self) -> CalibrationStatus:
        """Send the key that calibrates, and wait for the run to end.

        Raises:
            FujiAnalyzerStateError: the run is not on the wait step, or the
                reading is not steady on the gas. Nothing was sent, and a run
                on the wait step stays there.
        """
        if not self.active or self._state != "waiting":
            raise FujiAnalyzerStateError("no calibration is on its wait step")
        verdict = self._verdict
        if verdict is None or not verdict.steady:
            reasons = "; ".join(self._reasons(verdict)) or "no read yet"
            raise FujiAnalyzerStateError(f"the reading is not steady on the gas: {reasons}")
        self._refusal = None
        self._refused.clear()
        self._request = "commit"
        self._wake.set()
        waiters = [
            asyncio.ensure_future(self._done.wait()),
            asyncio.ensure_future(self._refused.wait()),
        ]
        try:
            _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                _ = waiter.cancel()
        refusal = self._refused_with()
        if refusal is not None and not self._done.is_set():
            raise refusal
        return self.status

    def _reasons(self, verdict: SteadinessVerdict | None) -> tuple[str, ...]:
        """Each channel's reason, prefixed with the channel's gas."""
        if verdict is None:
            return ()
        return tuple(
            f"{self._name(channel)}: {steadiness.reason}"
            for channel, steadiness in verdict.channels.items()
        )

    def _name(self, channel: ChannelId) -> str:
        return self._names.get(channel.value, channel.value)

    def _refused_with(self) -> FujiError | None:
        """What the owner task refused the last request with, if it did.

        Read through a method because the owner task sets it while
        :meth:`commit` waits, which a type checker cannot see.
        """
        return self._refusal

    async def cancel(self, *, reason: str | None = None) -> CalibrationStatus:
        """Leave the wait step with ESC, and wait for the run to end.

        ``reason`` says why, when it was not the operator who asked; it is
        kept as the run's ``error``. A run already calibrating is not
        interrupted: this waits for it to end.
        """
        if self.active:
            self._request = "cancel"
            self._cancel_reason = reason
            self._wake.set()
            _ = await self._done.wait()
        return self.status

    async def close(self) -> None:
        """Stop the owner task and wait for its cleanup; for an adapter that is closing.

        fujilib returns the panel to measurement, shielded from the
        cancellation, within ``cleanup_timeout_s``. A cleanup that takes
        longer is left behind: closing the analyzer ends it.
        """
        task = self._task
        if task is None or task.done():
            return
        _ = task.cancel()
        _ = await asyncio.wait({task}, timeout=self._cleanup_timeout_s + _CLOSE_MARGIN_S)

    # ------------------------------------------------------------------ the owner task

    async def _own(self) -> None:
        run: RemoteCalibration | None = None
        try:
            run = self._analyzer.manual_calibration(
                self._plan,
                gas=self._gas,
                confirm=True,
                rule=self._rule,
                interval=self._interval_s,
                cleanup_timeout=self._cleanup_timeout_s,
            )
            async with run:
                self._state = "waiting"
                self._ready.set()
                await self._wait(run)
        except asyncio.CancelledError:
            self._error = "the adapter closed while the calibration was under way"
            raise
        except FujiError as exc:
            if not self._ready.is_set():
                self._begin_error = exc
            self._error = str(exc)
        except Exception as exc:
            # Nothing awaits this task, so an error that is not fujilib's
            # would otherwise be lost; fujilib's cleanup has already run.
            self._error = f"{type(exc).__name__}: {exc}"
            _logger.exception(
                "fuji.calibration_task_failed",
                channel=self._plan.channel.value,
                kind=self._plan.kind.value,
            )
        finally:
            self._finish(run)

    async def _wait(self, run: RemoteCalibration) -> None:
        """Sit on the wait step, judging the gas, until asked to calibrate or cancel."""
        loop = asyncio.get_running_loop()
        give_up_at = loop.time() + self._timeout_s
        while True:
            self._wake.clear()
            request, self._request = self._request, None
            if request is None and loop.time() >= give_up_at:
                self._error = (
                    f"no decision within {self._timeout_s:g} s on the wait step; "
                    "the calibration was cancelled"
                )
                request = "cancel"
            if request == "cancel":
                if self._error is None:
                    self._error = self._cancel_reason
                _ = await run.cancel()
                return
            if request == "commit":
                self._state = "calibrating"
                try:
                    _ = await run.calibrate(confirm=True)
                except FujiAnalyzerStateError as exc:
                    if run.state is not RunState.WAITING:
                        raise
                    # Refused before the key: the gas moved, or a check failed.
                    self._state = "waiting"
                    self._refusal = exc
                    self._refused.set()
                    continue
                return
            self._judge(run, await run.read())
            with contextlib.suppress(TimeoutError):
                _ = await asyncio.wait_for(self._wake.wait(), timeout=self._interval_s)

    def _judge(self, run: RemoteCalibration, verdict: SteadinessVerdict) -> None:
        self._verdict = verdict
        observation = run.observation
        if observation is None:
            return
        self._readings = MappingProxyType(
            {
                channel.value: (reading.value if reading is not None else None)
                for channel in verdict.channels
                for reading in (observation.readings.get(channel),)
            }
        )

    def _finish(self, run: RemoteCalibration | None) -> None:
        result = run.result if run is not None else None
        if result is not None:
            outcome = result.outcome
            self._outcome = outcome.value if outcome is not None else None
            self._clean = result.cleanup.clean
            if self._verdict is None:
                self._verdict = result.steadiness
            self._record = MappingProxyType(
                result.as_record(
                    info=self._info,
                    port=self._port,
                    address=self._address,
                    operator=self._operator,
                )
            )
            if self._error is None and result.error is not None:
                self._error = result.error
        self._state = "ended"
        self._done.set()
        self._ready.set()
        if self._on_end is not None:
            self._on_end(self.status)
