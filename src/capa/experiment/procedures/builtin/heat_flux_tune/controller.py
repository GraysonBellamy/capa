""":class:`HeatFluxTune` — supervisory tuner for radiant heat flux at the specimen plane.

CAPA's scientific control parameter is the radiant heat flux at the
specimen surface (kW/m²), but the heater coil is driven by a Watlow PM3
PID that closes on the average of three embedded thermocouples
(temperature is just a proxy). This procedure drives a slow supervisory
outer loop on top of the Watlow's existing temperature PID:

* command a heater setpoint
* wait for **measured steady state** (heater PV in band AND flux variance
  low AND rolling flux slope flat, all for ``t_stable_s``)
* read the windowed mean flux
* compute error against the target, apply a damped secant step
* repeat until the error is in tolerance for two consecutive windows
  plus a verification soak

Each accepted ``(target, setpoint, mean-flux)`` point is appended to a
:class:`~capa.calibration.tune_artifact.HeatFluxTuneArtifact` after every
target so a session that aborts mid-way still leaves a usable record.

The procedure is **slow and supervisory** — not a low-level flux PID.
The Schmidt-Boelter gauge is removed before the specimen run; the
experiment-time controller remains temperature-only.

This module owns the async state machine, stream consumers, operator
command handling, artifact persistence, and tick emission. Pure-math
helpers, the pydantic config, and the initial-setpoint heuristics live
in sibling modules.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import anyio

from capa.calibration.tune_artifact import (
    HeatFluxTuneArtifact,
    HeatFluxTunePoint,
    TuneArtifactError,
    load_latest,
    save_artifact,
)
from capa.devices.records import ChannelSample
from capa.experiment.procedures.base import (
    ChannelRequirement,
    OperatorCommand,
    Problem,
    Procedure,
    ProcedureContext,
)
from capa.runtime.emissions import ProcedureTick
from capa.runtime.recording import ResolvedRecordingPlan

from .config import (
    PROCEDURE_ID,
    PROCEDURE_NAME,
    PROCEDURE_VERSION,
    HeatFluxTuneConfig,
    HeatFluxTuneError,
)
from .setpoint import (
    choose_initial_setpoint,
    default_tolerance_kw_m2,
    predicate_strictness,
)
from .signals import (
    RollingWindow,
    RunawayDetector,
    SteadyStatePredicate,
    secant_step,
)


@dataclass(slots=True)
class _SessionState:
    """Mutable per-target bookkeeping shared between async helpers."""

    flux_window: RollingWindow
    pv_window: RollingWindow
    pv_latest: float | None = None
    setpoint_latest: float | None = None
    last_flux_sample_ns: int | None = None
    iterations: list[tuple[float, float]] = field(default_factory=list)
    """``[(setpoint_c, measured_flux_kw_m2), ...]`` for the current
    target — used to compute the in-session secant slope from
    iteration 2 onward."""

    # ----- Tick bookkeeping. Mutated by ``_converge_to`` as iterations
    # advance; read by ``_emit_tick`` to build the live-numerics payload
    # the dock subscribes to. None of these affect control flow — keep
    # them next to the existing fields so the live view and the audit
    # event stream stay in lockstep.
    target_index: int = 0
    """1-based index of the current target within the session."""
    target_count: int = 0
    """How many targets the session is converging through."""
    target_kw_m2: float = 0.0
    tolerance_kw_m2: float = 0.0
    iteration: int = 0
    """1-based iteration within the current target. 0 before the first
    measurement of a target."""
    iteration_max: int = 0
    commanded_setpoint_c: float = 0.0
    in_tol_windows: int = 0
    """Two-in-a-row counter the convergence rule needs (resets on out-
    of-tolerance, fires the verify soak at 2)."""
    runaway_count: int = 0
    df_dt_used: float = 0.0
    df_dt_source: str = "unknown"
    sigma_max_kw_m2: float = 0.0
    """The per-iteration sigma cap, after distance-based predicate
    relaxation. May differ from ``cfg.sigma_flux_floor_kw_m2`` early
    in convergence."""
    slope_max_kw_m2_per_min: float = 0.0
    """The per-iteration slope cap, after relaxation."""


@dataclass(slots=True)
class _Measurement:
    """One predicate-met (or settle-timeout) reading."""

    mean_flux: float
    std_flux: float
    slope_flux: float
    pv_mean: float
    dwell_s: float
    timed_out: bool
    operator_accepted: bool = False
    """``True`` when the measurement is the result of an ``accept_current``
    operator command rather than a predicate fire / settle timeout. The
    caller routes such measurements straight to a
    ``HeatFluxTunePoint(accept_reason='operator_override')`` regardless of
    whether the predicate had held."""


@dataclass(slots=True)
class HeatFluxTune(Procedure):
    """Supervisory heat-flux tuner.

    Instantiated by the engine from
    ``ExperimentConfig.procedure.config`` via :meth:`from_config`.
    """

    id: ClassVar[str] = PROCEDURE_ID
    name: ClassVar[str] = PROCEDURE_NAME
    version: ClassVar[str] = PROCEDURE_VERSION
    config_model: ClassVar[type] = HeatFluxTuneConfig
    required_capabilities: ClassVar[tuple[str, ...]] = ()
    required_channels: ClassVar[tuple[ChannelRequirement, ...]] = (
        ChannelRequirement(name="heat_flux_gauge"),
        ChannelRequirement(name="heater.setpoint"),
        ChannelRequirement(name="heater.pv"),
    )
    uses_method: ClassVar[bool] = False

    cfg: HeatFluxTuneConfig = field(
        default_factory=lambda: HeatFluxTuneConfig(targets_kw_m2=(50.0,), t_set_max_c=900.0)
    )
    _accepted_points: list[HeatFluxTunePoint] = field(default_factory=list, init=False)
    _run_start_ns: int = field(default=0, init=False)
    _paused: bool = field(default=False, init=False)
    """Set by the operator-command consumer on ``pause``; cleared on
    ``resume``. The settle/verify poll loops sleep without re-evaluating
    the predicate while paused — the heater stays at its current
    commanded setpoint and the rolling windows keep filling."""

    _accept_now: bool = field(default=False, init=False)
    """One-shot flag: set to ``True`` when the operator clicks "Accept
    Current"; checked at the top of the settle poll loop and cleared
    when consumed. The resulting :class:`_Measurement` has
    ``operator_accepted=True`` and the caller writes the point with
    ``accept_reason='operator_override'``."""

    @classmethod
    def from_config(cls, raw: dict[str, object] | None) -> HeatFluxTune:
        cfg = HeatFluxTuneConfig.model_validate(raw or {})
        return cls(cfg=cfg)

    # ----------------------------------------------------------------- plan_capture

    def plan_capture(self, default_plan: ResolvedRecordingPlan) -> ResolvedRecordingPlan:
        """Narrow the recording plan to the three channels this procedure
        consumes; suppress all cameras.

        Reads the channel names from :attr:`cfg` (not literal strings) so
        an operator who rebinds ``flux_channel = "flux_b"`` gets the
        rebinding for free — the filter doesn't silently desync from the
        procedure's actual subscriptions.
        """
        return ResolvedRecordingPlan(
            channel_mode="only",
            recorded_channels=(
                self.cfg.flux_channel,
                self.cfg.heater_pv_channel,
                self.cfg.heater_setpoint_channel,
            ),
            camera_mode="none",
            recorded_cameras=(),
            source="procedure_default",
        )

    # ----------------------------------------------------------------- preflight

    async def preflight(self, ctx: ProcedureContext) -> list[Problem]:
        problems: list[Problem] = []
        for required in (
            self.cfg.flux_channel,
            self.cfg.heater_setpoint_channel,
            self.cfg.heater_pv_channel,
        ):
            try:
                ctx.instruments.resolve(required)
            except Exception:
                problems.append(
                    Problem(
                        code="heat_flux_tune.missing_channel",
                        message=(
                            f"channel {required!r} is required for the heat-flux "
                            f"tune procedure but is not bound on the active "
                            f"hardware profile"
                        ),
                        severity="error",
                        blocking=True,
                    )
                )
        if self.cfg.t_set_max_c > 1000.0:
            problems.append(
                Problem(
                    code="heat_flux_tune.setpoint_ceiling_too_high",
                    message=(
                        f"t_set_max_c={self.cfg.t_set_max_c} °C exceeds the "
                        f"rig's documented 1000 °C survival limit"
                    ),
                    severity="error",
                    blocking=True,
                )
            )
        return problems

    # ----------------------------------------------------------------- run

    async def run(self, ctx: ProcedureContext) -> None:
        self._run_start_ns = ctx.clock.t_mono_ns()
        ctx.logger.info(
            "heat_flux_tune.start",
            targets=list(self.cfg.targets_kw_m2),
            t_set_max_c=self.cfg.t_set_max_c,
        )
        self._emit_event(
            ctx,
            kind="heat_flux_tune.started",
            message=(
                f"HeatFluxTune started "
                f"(targets={list(self.cfg.targets_kw_m2)} kW/m², "
                f"t_set_max_c={self.cfg.t_set_max_c} °C)"
            ),
            metadata={
                "targets_kw_m2": list(self.cfg.targets_kw_m2),
                "t_set_max_c": self.cfg.t_set_max_c,
                "initial_guess": self.cfg.initial_guess,
            },
        )

        prior_artifact: HeatFluxTuneArtifact | None = None
        if self.cfg.persist_dir is not None and self.cfg.initial_guess == "lookup":
            try:
                prior_artifact = load_latest(Path(self.cfg.persist_dir))
            except TuneArtifactError as exc:
                ctx.logger.warning("heat_flux_tune.prior_load_failed", error=str(exc))
                prior_artifact = None

        # Session-level task group hosts the operator-command consumer so
        # pause/resume/accept_current survive across per-target loops. The
        # consumer is a no-op when the UI hasn't wired the stream (CLI
        # headless, tests).
        #
        # ``completed_all_targets`` is set **inside** the loop, after the
        # final target's point is appended. This shape — rather than a
        # post-loop ``success = True`` — survives the ``break`` paths
        # below (external_stop, wall-clock exhaustion) which leave the
        # for-loop without raising. A naive post-loop flag would falsely
        # signal "completed" on those paths and hold a heater the
        # operator just told us to stop using.
        completed_all_targets = False
        async with anyio.create_task_group() as session_tg:
            if ctx.operator_commands is not None:
                session_tg.start_soon(self._consume_operator_commands, ctx)
            try:
                await self._gauge_sanity_check(ctx)
                target_count = len(self.cfg.targets_kw_m2)
                for target_index, target in enumerate(self.cfg.targets_kw_m2, start=1):
                    if ctx.external_stop.is_set():
                        break  # completed_all_targets stays False → cool
                    if self._wall_clock_exhausted(ctx):
                        self._emit_event(
                            ctx,
                            kind="heat_flux_tune.aborted",
                            message="total wall-clock budget exhausted before all targets converged",
                            severity="error",
                            metadata={"reason": "t_total_max_s"},
                        )
                        break  # stays False → cool
                    point = await self._converge_to(
                        ctx,
                        target=target,
                        target_index=target_index,
                        target_count=target_count,
                        prior_artifact=prior_artifact,
                    )
                    self._accepted_points.append(point)
                    self._persist_partial(ctx)
                    self._emit_event(
                        ctx,
                        kind="heat_flux_tune.target_accepted",
                        message=(
                            f"target {target:g} kW/m² accepted at "
                            f"setpoint {point.heater_setpoint_c:.2f} °C "
                            f"(measured {point.measured_flux_mean_kw_m2:.2f} kW/m²)"
                        ),
                        metadata={
                            "target_kw_m2": target,
                            "setpoint_c": point.heater_setpoint_c,
                            "measured_flux_kw_m2": point.measured_flux_mean_kw_m2,
                            "accept_reason": point.accept_reason,
                        },
                    )
                    if target_index == target_count:
                        completed_all_targets = True
            except HeatFluxTuneError as exc:
                # Catch only HeatFluxTuneError, not bare Exception —
                # broadening would mask programmer bugs while the loop's
                # last iteration may have appended a point; the hold
                # gate would then see ``completed_all_targets=False`` (good)
                # but at the cost of swallowing a real crash. Let other
                # exceptions propagate to the engine's crash handler.
                self._emit_event(
                    ctx,
                    kind="heat_flux_tune.aborted",
                    message=f"tune aborted: {exc}",
                    severity="error",
                    metadata={"reason": str(exc)},
                )
            finally:
                # Stop the operator-command consumer; no further commands
                # are meaningful once we're cooling down or holding.
                session_tg.cancel_scope.cancel()
                if self._should_hold(completed_all_targets):
                    self._emit_hold_event(ctx)
                    self._emit_holding_tick(ctx)
                else:
                    await self._safe_cool(ctx)
                self._emit_event(
                    ctx,
                    kind="heat_flux_tune.completed",
                    message=(
                        f"HeatFluxTune finished (accepted_points={len(self._accepted_points)})"
                    ),
                    metadata={
                        "accepted_points": len(self._accepted_points),
                        "targets_kw_m2": list(self.cfg.targets_kw_m2),
                        "held": self._should_hold(completed_all_targets),
                    },
                )

    # ----------------------------------------------------------------- hold mode

    def _should_hold(self, completed_all_targets: bool) -> bool:
        """Decide whether the finally block should leave the heater hot.

        Four gates, all required:

        1. The operator opted in (``hold_at_completion=True``).
        2. The loop ran to completion of the final target — every
           abort path (external_stop, wall-clock, HeatFluxTuneError)
           leaves ``completed_all_targets=False`` and cools.
        3. At least one accepted point exists (defensive — if no point
           was appended somehow, there's nothing to hold at).
        4. The last accepted point's ``accept_reason`` is not
           ``warn_proceeded``. The artifact filters non-accepted points,
           so holding at a ``warn_proceeded`` SP would leave the
           operator with a hot heater whose tuned value the rest of
           the system refuses to re-surface via "Apply latest tune" —
           a broken handoff. Cool and let them re-tune.
        """
        if not (completed_all_targets and self.cfg.hold_at_completion):
            return False
        if not self._accepted_points:
            return False
        return self._accepted_points[-1].accepted

    def _emit_hold_event(self, ctx: ProcedureContext) -> None:
        """Emit ``heat_flux_tune.holding`` describing the held setpoint.

        Distinguishes "ended cold" from "ended holding at X" in the
        bundle audit trail; the post-tune apply prompt in the CAPA
        profile UI listens for this kind to know whether to surface
        the suggestion. The accept reason rides along so a later
        analyst can see why the procedure picked this point — even
        the ``operator_override`` case lands here cleanly.
        """
        point = self._accepted_points[-1]
        self._emit_event(
            ctx,
            kind="heat_flux_tune.holding",
            message=(
                f"Tune complete; heater HOLDING at "
                f"{point.heater_setpoint_c:.2f} °C "
                f"({point.target_flux_kw_m2:g} kW/m²). "
                f"Use the heater card's 'Cool to safe' action to drive "
                f"to safe when finished."
            ),
            severity="info",
            metadata={
                "held_setpoint_c": point.heater_setpoint_c,
                "held_target_kw_m2": point.target_flux_kw_m2,
                "held_measured_flux_kw_m2": point.measured_flux_mean_kw_m2,
                "accept_reason": point.accept_reason,
                "gauge_calibration_ref": self.cfg.gauge_calibration_ref,
            },
        )

    def _emit_holding_tick(self, ctx: ProcedureContext) -> None:
        """Publish one final tick with ``phase="holding"``.

        The dock already renders ``payload["phase"]`` directly from the
        existing live-numerics tick path — no new IPC, no state
        machine, just one last frame that latches the dock into the
        HOLDING display before the run leaves :data:`RunUiState.RUNNING`
        and the dock auto-hides. The held SP, target, measured flux,
        and accept reason ride in the payload so a debugger inspecting
        the bridge can recover the same info the event log carries.
        """
        sink = ctx.ui_sink
        if sink is None:
            return
        point = self._accepted_points[-1]
        payload: dict[str, object] = {
            "phase": "holding",
            "target_index": len(self._accepted_points),
            "target_count": len(self._accepted_points),
            "target_kw_m2": point.target_flux_kw_m2,
            "commanded_setpoint_c": point.heater_setpoint_c,
            "mean_flux_kw_m2": point.measured_flux_mean_kw_m2,
            "accept_reason": point.accept_reason,
            "paused": False,
        }
        tick = ProcedureTick(
            procedure_id=PROCEDURE_ID,
            t_mono_ns=ctx.clock.t_mono_ns(),
            payload=payload,
        )
        try:
            sink.publish(tick)
        except Exception as exc:  # pragma: no cover - defensive
            ctx.logger.warning("heat_flux_tune.holding_tick_publish_failed", error=str(exc))

    # ----------------------------------------------------------------- per-target loop

    async def _converge_to(
        self,
        ctx: ProcedureContext,
        *,
        target: float,
        target_index: int = 1,
        target_count: int = 1,
        prior_artifact: HeatFluxTuneArtifact | None,
    ) -> HeatFluxTunePoint:
        tol = self.cfg.tolerance_kw_m2 or default_tolerance_kw_m2(target)
        t_min_c = self.cfg.t_safe_c
        sp_c, _guess_source = choose_initial_setpoint(
            target_kw_m2=target,
            source=self.cfg.initial_guess,
            operator_setpoint_c=self.cfg.operator_initial_setpoint_c,
            prior_artifact=prior_artifact,
            t_min_c=t_min_c,
            t_set_max_c=self.cfg.t_set_max_c,
        )
        runaway = RunawayDetector(trip_threshold=self.cfg.runaway_sign_disagreement_count)

        state = _SessionState(
            flux_window=RollingWindow(
                window_s=self.cfg.t_window_s,
                hampel_k=self.cfg.hampel_k,
            ),
            pv_window=RollingWindow(window_s=self.cfg.t_window_s),
            target_index=target_index,
            target_count=target_count,
            target_kw_m2=target,
            tolerance_kw_m2=tol,
            iteration=0,
            iteration_max=self.cfg.n_iter_max,
            commanded_setpoint_c=sp_c,
        )

        target_start_ns = ctx.clock.t_mono_ns()
        prior_slope = prior_artifact.local_df_dt(target) if prior_artifact is not None else None
        sigma_max = max(
            self.cfg.sigma_flux_floor_kw_m2,
            self.cfg.sigma_flux_max_fraction * target,
        )

        last_delta_t = 0.0
        in_tol_windows = 0
        last_measurement: _Measurement | None = None

        async with anyio.create_task_group() as tg:
            tg.start_soon(self._consume_flux, ctx, state)
            tg.start_soon(self._consume_pv, ctx, state)
            tg.start_soon(self._consume_setpoint, ctx, state)
            try:
                await self._issue_setpoint(ctx, sp_c)

                for iteration in range(1, self.cfg.n_iter_max + 1):
                    if ctx.external_stop.is_set():
                        raise HeatFluxTuneError("external_stop fired during target convergence")
                    if self._wall_clock_exhausted(ctx):
                        raise HeatFluxTuneError(
                            "wall-clock budget exhausted during target convergence"
                        )

                    # Drop samples carried over from the previous
                    # iteration. The setpoint just moved (or is about to,
                    # for iter 1) — keeping pre-step data would dominate
                    # the std/slope statistics for the next ``window_s``
                    # seconds and poison the predicate. Cheap to refill
                    # at the configured gauge rate; the warmup wait is
                    # part of the iteration's settle budget.
                    state.flux_window.clear()
                    state.pv_window.clear()

                    # Distance-based predicate relaxation: loose & fast when
                    # the prior iteration was far from target, tight & full
                    # dwell once we're closing in. Iter 1 has no prior err,
                    # so use ``inf`` → fully relaxed (worst-case assumption).
                    prior_err = (
                        target - state.iterations[-1][1] if state.iterations else float("inf")
                    )
                    k = predicate_strictness(
                        err_kw_m2=prior_err,
                        target_kw_m2=target,
                        tolerance_kw_m2=tol,
                        relax_factor=self.cfg.predicate_relax_factor,
                    )
                    slope_max_iter = self.cfg.slope_max_kw_per_min * k
                    t_stable_iter = self.cfg.t_stable_s / k
                    # Mirror per-iteration knobs onto session state so
                    # the tick payload reports the live values the
                    # predicate is actually using this iteration.
                    state.iteration = iteration
                    state.commanded_setpoint_c = sp_c
                    state.in_tol_windows = in_tol_windows
                    state.sigma_max_kw_m2 = sigma_max
                    state.slope_max_kw_m2_per_min = slope_max_iter
                    state.runaway_count = runaway.count

                    measurement = await self._wait_steady(
                        ctx, state, sp_c, sigma_max, slope_max_iter, t_stable_iter
                    )
                    last_measurement = measurement
                    err = target - measurement.mean_flux
                    state.iterations.append((sp_c, measurement.mean_flux))

                    df_dt, df_dt_source = self._estimate_df_dt(
                        state, prior_slope=prior_slope, default=1.0
                    )
                    state.df_dt_used = df_dt
                    state.df_dt_source = df_dt_source

                    if measurement.operator_accepted:
                        # Operator pressed "Accept Current" — record the
                        # iteration as an override and return immediately.
                        # The verify soak is skipped because the operator
                        # has explicitly opted out of the algorithmic
                        # convergence rule.
                        self._emit_iteration_event(
                            ctx,
                            iteration=iteration,
                            target=target,
                            setpoint_old=sp_c,
                            setpoint_new=sp_c,
                            measurement=measurement,
                            err=err,
                            df_dt_used=df_dt,
                            df_dt_source=df_dt_source,
                            decision="operator_override",
                        )
                        tg.cancel_scope.cancel()
                        return HeatFluxTunePoint(
                            target_flux_kw_m2=target,
                            heater_setpoint_c=sp_c,
                            measured_flux_mean_kw_m2=measurement.mean_flux,
                            measured_flux_std_kw_m2=measurement.std_flux,
                            measured_flux_slope_kw_m2_per_min=measurement.slope_flux,
                            heater_pv_mean_c=measurement.pv_mean,
                            soak_s=(ctx.clock.t_mono_ns() - target_start_ns) / 1e9,
                            accepted=True,
                            accept_reason="operator_override",
                        )

                    if abs(err) <= tol:
                        in_tol_windows += 1
                    else:
                        in_tol_windows = 0
                    state.in_tol_windows = in_tol_windows

                    if in_tol_windows >= 2:
                        self._emit_iteration_event(
                            ctx,
                            iteration=iteration,
                            target=target,
                            setpoint_old=sp_c,
                            setpoint_new=sp_c,
                            measurement=measurement,
                            err=err,
                            df_dt_used=df_dt,
                            df_dt_source=df_dt_source,
                            decision="converged_window",
                        )
                        # Verify-soak runs at full strictness — refresh
                        # the state's slope cap so the tick reports it.
                        state.slope_max_kw_m2_per_min = self.cfg.slope_max_kw_per_min
                        verified = await self._verification_soak(ctx, state, sp_c, sigma_max)
                        if verified is None:
                            in_tol_windows = 0
                            continue
                        tg.cancel_scope.cancel()
                        return HeatFluxTunePoint(
                            target_flux_kw_m2=target,
                            heater_setpoint_c=sp_c,
                            measured_flux_mean_kw_m2=verified.mean_flux,
                            measured_flux_std_kw_m2=verified.std_flux,
                            measured_flux_slope_kw_m2_per_min=verified.slope_flux,
                            heater_pv_mean_c=verified.pv_mean,
                            soak_s=(ctx.clock.t_mono_ns() - target_start_ns) / 1e9,
                            accepted=True,
                            accept_reason=(
                                "operator_override"
                                if verified.operator_accepted
                                else "algorithm_converged"
                            ),
                        )

                    delta_t = secant_step(
                        err_kw_m2=err,
                        df_dt_kw_m2_per_c=df_dt,
                        damping=self.cfg.damping,
                        delta_t_step_max_c=self.cfg.delta_t_step_max_c,
                    )
                    new_sp = max(t_min_c, min(self.cfg.t_set_max_c, sp_c + delta_t))
                    actual_delta = new_sp - sp_c

                    runaway.record(err_kw_m2=err, delta_t_c=last_delta_t)
                    if runaway.tripped():
                        self._emit_iteration_event(
                            ctx,
                            iteration=iteration,
                            target=target,
                            setpoint_old=sp_c,
                            setpoint_new=sp_c,
                            measurement=measurement,
                            err=err,
                            df_dt_used=df_dt,
                            df_dt_source=df_dt_source,
                            decision="abort:runaway",
                        )
                        raise HeatFluxTuneError(
                            f"runaway detector tripped at iteration {iteration} "
                            f"(target={target:g}); sign(err) and sign(ΔT) have "
                            f"disagreed for {runaway.count} iterations"
                        )

                    self._emit_iteration_event(
                        ctx,
                        iteration=iteration,
                        target=target,
                        setpoint_old=sp_c,
                        setpoint_new=new_sp,
                        measurement=measurement,
                        err=err,
                        df_dt_used=df_dt,
                        df_dt_source=df_dt_source,
                        decision="step",
                    )
                    last_delta_t = actual_delta
                    sp_c = new_sp
                    state.commanded_setpoint_c = sp_c
                    state.runaway_count = runaway.count
                    if actual_delta != 0.0:
                        await self._issue_setpoint(ctx, sp_c)

                # iteration cap exhausted; accept the last reading as
                # warn_proceeded so the operator gets a usable artifact for
                # earlier targets in the same session
                if last_measurement is None:
                    raise HeatFluxTuneError(
                        f"iteration cap reached before any measurement (target={target:g})"
                    )
                tg.cancel_scope.cancel()
                return HeatFluxTunePoint(
                    target_flux_kw_m2=target,
                    heater_setpoint_c=sp_c,
                    measured_flux_mean_kw_m2=last_measurement.mean_flux,
                    measured_flux_std_kw_m2=last_measurement.std_flux,
                    measured_flux_slope_kw_m2_per_min=last_measurement.slope_flux,
                    heater_pv_mean_c=last_measurement.pv_mean,
                    soak_s=(ctx.clock.t_mono_ns() - target_start_ns) / 1e9,
                    accepted=False,
                    accept_reason="warn_proceeded",
                )
            finally:
                tg.cancel_scope.cancel()

        # Unreachable: every path inside the task group either returns,
        # raises, or sets the cancel scope before falling off the loop.
        raise HeatFluxTuneError(f"_converge_to ended without producing a point (target={target:g})")

    # ----------------------------------------------------------------- measurement

    async def _wait_steady(
        self,
        ctx: ProcedureContext,
        state: _SessionState,
        commanded_sp_c: float,
        sigma_flux_max: float,
        slope_max_kw_per_min: float,
        t_stable_s: float,
    ) -> _Measurement:
        """Poll the predicate until it fires or settle-timeout elapses.

        Returns one measurement. Raises :class:`HeatFluxTuneError` on
        gauge silence or external_stop.

        While ``self._paused`` is set, the predicate is **not**
        re-evaluated and the settle clock is **not** advanced (paused
        time is excluded from ``t_settle_max_s``). On ``self._accept_now``
        the loop short-circuits and returns the current windowed
        statistics regardless of predicate state, with
        ``operator_accepted=True``.
        """
        predicate = SteadyStatePredicate(
            delta_t_band_c=self.cfg.delta_t_band_c,
            sigma_flux_max=sigma_flux_max,
            slope_max_kw_per_min=slope_max_kw_per_min,
            t_stable_s=t_stable_s,
        )
        start_ns = ctx.clock.t_mono_ns()
        paused_total_s = 0.0
        last_paused_at_ns: int | None = None
        while True:
            if ctx.external_stop.is_set():
                raise HeatFluxTuneError("external_stop fired during settle wait")

            if self._accept_now:
                # One-shot — consume the flag, mark the measurement, and
                # exit. Pause state is also cleared so a subsequent
                # iteration starts fresh; if the operator paused before
                # accepting, the next iteration would otherwise sleep
                # forever.
                self._accept_now = False
                self._paused = False
                now_ns = ctx.clock.t_mono_ns()
                elapsed_s = max(0.0, (now_ns - start_ns) / 1e9 - paused_total_s)
                m = self._take_measurement(state, elapsed_s, timed_out=False)
                m.operator_accepted = True
                return m

            if self._paused:
                if last_paused_at_ns is None:
                    last_paused_at_ns = ctx.clock.t_mono_ns()
                    predicate.reset()
                # Paused tick — elapsed_s and predicate dwell freeze.
                paused_elapsed_s = max(
                    0.0, (ctx.clock.t_mono_ns() - start_ns) / 1e9 - paused_total_s
                )
                self._emit_tick(
                    ctx,
                    state=state,
                    predicate=predicate,
                    phase="settle",
                    elapsed_s=paused_elapsed_s,
                    settle_budget_s=self.cfg.t_settle_max_s,
                )
                await anyio.sleep(self.cfg.poll_interval_s)
                continue
            if last_paused_at_ns is not None:
                paused_total_s += (ctx.clock.t_mono_ns() - last_paused_at_ns) / 1e9
                last_paused_at_ns = None

            now_ns = ctx.clock.t_mono_ns()
            elapsed_s = (now_ns - start_ns) / 1e9 - paused_total_s

            if (
                state.last_flux_sample_ns is not None
                and (now_ns - state.last_flux_sample_ns) / 1e9 > self.cfg.gauge_silence_max_s
            ):
                raise HeatFluxTuneError(
                    f"gauge silent for >{self.cfg.gauge_silence_max_s:.0f}s "
                    f"(channel={self.cfg.flux_channel})"
                )

            window_full = state.flux_window.is_warm(self.cfg.t_window_s)
            pv_mean = state.pv_window.mean() if state.pv_window.count() else None
            predicate.evaluate(
                now_s=elapsed_s,
                pv_mean_c=pv_mean,
                setpoint_c=commanded_sp_c,
                flux_std_kw_m2=state.flux_window.std(),
                flux_slope_kw_per_min=state.flux_window.slope_per_min(),
                window_full=window_full,
            )
            # Live tick once per poll cycle — cheap, drops on closed
            # bridge. Emitted before fire/timeout so the dock catches
            # the last predicate state even on a fire-immediate.
            self._emit_tick(
                ctx,
                state=state,
                predicate=predicate,
                phase="settle",
                elapsed_s=elapsed_s,
                settle_budget_s=self.cfg.t_settle_max_s,
            )

            if predicate.fired(elapsed_s):
                return self._take_measurement(state, elapsed_s, timed_out=False)
            if elapsed_s > self.cfg.t_settle_max_s:
                ctx.logger.warning(
                    "heat_flux_tune.settle_timeout",
                    elapsed_s=elapsed_s,
                    last_reason=predicate.last_reason,
                )
                return self._take_measurement(state, elapsed_s, timed_out=True)
            await anyio.sleep(self.cfg.poll_interval_s)

    async def _verification_soak(
        self,
        ctx: ProcedureContext,
        state: _SessionState,
        commanded_sp_c: float,
        sigma_flux_max: float,
    ) -> _Measurement | None:
        """Sit at the current setpoint and re-check the predicate.

        Returns the soak-end measurement when the predicate holds for
        the full ``t_verify_s``; returns ``None`` if the predicate
        breaks during the soak (the caller resets ``in_tol_windows``).
        An ``accept_current`` operator command during the soak
        short-circuits with ``operator_accepted=True`` so the caller
        records an override point.
        """
        predicate = SteadyStatePredicate(
            delta_t_band_c=self.cfg.delta_t_band_c,
            sigma_flux_max=sigma_flux_max,
            slope_max_kw_per_min=self.cfg.slope_max_kw_per_min,
            t_stable_s=self.cfg.t_verify_s,
        )
        start_ns = ctx.clock.t_mono_ns()
        max_dwell_s = self.cfg.t_verify_s * 2.0  # bounded retry window
        while True:
            if ctx.external_stop.is_set():
                raise HeatFluxTuneError("external_stop fired during verify soak")
            if self._accept_now:
                self._accept_now = False
                self._paused = False
                now_ns = ctx.clock.t_mono_ns()
                elapsed_s = (now_ns - start_ns) / 1e9
                m = self._take_measurement(state, elapsed_s, timed_out=False)
                m.operator_accepted = True
                return m
            if self._paused:
                predicate.reset()
                paused_elapsed_s = (ctx.clock.t_mono_ns() - start_ns) / 1e9
                self._emit_tick(
                    ctx,
                    state=state,
                    predicate=predicate,
                    phase="verify_soak",
                    elapsed_s=paused_elapsed_s,
                    settle_budget_s=max_dwell_s,
                )
                await anyio.sleep(self.cfg.poll_interval_s)
                continue
            now_ns = ctx.clock.t_mono_ns()
            elapsed_s = (now_ns - start_ns) / 1e9
            window_full = state.flux_window.is_warm(self.cfg.t_window_s)
            pv_mean = state.pv_window.mean() if state.pv_window.count() else None
            predicate.evaluate(
                now_s=elapsed_s,
                pv_mean_c=pv_mean,
                setpoint_c=commanded_sp_c,
                flux_std_kw_m2=state.flux_window.std(),
                flux_slope_kw_per_min=state.flux_window.slope_per_min(),
                window_full=window_full,
            )
            self._emit_tick(
                ctx,
                state=state,
                predicate=predicate,
                phase="verify_soak",
                elapsed_s=elapsed_s,
                settle_budget_s=max_dwell_s,
            )
            if predicate.fired(elapsed_s):
                return self._take_measurement(state, elapsed_s, timed_out=False)
            if elapsed_s > max_dwell_s:
                return None
            await anyio.sleep(self.cfg.poll_interval_s)

    def _take_measurement(
        self,
        state: _SessionState,
        elapsed_s: float,
        *,
        timed_out: bool,
    ) -> _Measurement:
        pv_mean = (
            state.pv_window.mean()
            if state.pv_window.count()
            else (state.pv_latest if state.pv_latest is not None else float("nan"))
        )
        return _Measurement(
            mean_flux=state.flux_window.mean(),
            std_flux=state.flux_window.std(),
            slope_flux=state.flux_window.slope_per_min(),
            pv_mean=pv_mean,
            dwell_s=elapsed_s,
            timed_out=timed_out,
        )

    # ----------------------------------------------------------------- consumers

    async def _consume_flux(self, ctx: ProcedureContext, state: _SessionState) -> None:
        sub = ctx.databus.subscribe_channel(
            name=f"heat_flux_tune.flux-{self.cfg.flux_channel}",
            channel=self.cfg.flux_channel,
        )
        try:
            async for emission in sub:
                if not isinstance(emission, ChannelSample):
                    continue
                try:
                    value = float(emission.value)
                except (TypeError, ValueError):
                    continue
                state.last_flux_sample_ns = emission.t_mono_ns
                state.flux_window.push(emission.t_mono_s, value)
        finally:
            ctx.databus.unsubscribe(sub)

    async def _consume_pv(
        self,
        ctx: ProcedureContext,
        state: _SessionState,
    ) -> None:
        sub = ctx.databus.subscribe_channel(
            name=f"heat_flux_tune.pv-{self.cfg.heater_pv_channel}",
            channel=self.cfg.heater_pv_channel,
        )
        try:
            async for emission in sub:
                if not isinstance(emission, ChannelSample):
                    continue
                try:
                    value = float(emission.value)
                except (TypeError, ValueError):
                    continue
                state.pv_latest = value
                state.pv_window.push(emission.t_mono_s, value)
        finally:
            ctx.databus.unsubscribe(sub)

    async def _consume_operator_commands(self, ctx: ProcedureContext) -> None:
        """Translate UI operator commands into ``_paused`` / ``_accept_now``.

        Runs as a session-level task spawned in :meth:`run`. Iterates
        the inbound stream until cancellation (run completion) or the
        UI side closes its send half. Unknown command kinds are
        ignored — the procedure is forward-compatible with new
        :data:`OperatorCommandKind` literals added later.

        Each command also writes an audit event so the bundle records
        every operator intervention with its monotonic timestamp.
        """
        recv = ctx.operator_commands
        if recv is None:
            return
        try:
            async for cmd in recv:
                self._handle_operator_command(ctx, cmd)
        except (anyio.EndOfStream, anyio.ClosedResourceError):
            return

    def _handle_operator_command(self, ctx: ProcedureContext, cmd: OperatorCommand) -> None:
        kind = cmd.kind
        if kind == "pause":
            self._paused = True
        elif kind == "resume":
            self._paused = False
        elif kind == "accept_current":
            self._accept_now = True
        else:
            # Defensive branch — unreachable under the Literal type, but
            # a future plugin (or a runtime-constructed cmd that bypassed
            # validation) could land here. Log and drop rather than
            # crash. mypy correctly flags this as unreachable from the
            # type; the runtime safety guarantee is more valuable.
            ctx.logger.warning(  # type: ignore[unreachable]
                "heat_flux_tune.operator_command.unknown", kind=kind
            )
            return
        self._emit_event(
            ctx,
            kind="heat_flux_tune.operator_command",
            message=f"operator command: {kind}",
            metadata={
                "command": kind,
                "operator_metadata": dict(cmd.metadata),
            },
        )

    async def _consume_setpoint(self, ctx: ProcedureContext, state: _SessionState) -> None:
        sub = ctx.databus.subscribe_channel(
            name=f"heat_flux_tune.sp-{self.cfg.heater_setpoint_channel}",
            channel=self.cfg.heater_setpoint_channel,
        )
        try:
            async for emission in sub:
                if not isinstance(emission, ChannelSample):
                    continue
                try:
                    state.setpoint_latest = float(emission.value)
                except (TypeError, ValueError):
                    continue
        finally:
            ctx.databus.unsubscribe(sub)

    # ----------------------------------------------------------------- helpers

    def _estimate_df_dt(
        self,
        state: _SessionState,
        *,
        prior_slope: float | None,
        default: float,
    ) -> tuple[float, str]:
        """Pick the dF/dT to use this iteration.

        Once two in-session points exist, use the secant across them.
        Until then, fall back to the prior artifact's local slope when
        available, then to a conservative default.
        """
        if len(state.iterations) >= 2:
            (sp_a, f_a), (sp_b, f_b) = state.iterations[-2:]
            if sp_a != sp_b:
                slope = (f_b - f_a) / (sp_b - sp_a)
                if math.isfinite(slope):
                    return slope, "secant"
        if prior_slope is not None and prior_slope > 0:
            return prior_slope, "prior"
        return default, "sigma_t4"

    async def _issue_setpoint(self, ctx: ProcedureContext, value_c: float) -> None:
        """Authorized setpoint write + audit event.

        Mirrors the (private) ``MethodExecutor._command_setpoint`` shape
        rather than reaching into it — the executor's helper is
        internal and the procedure carries its own copy. If this pattern
        needs to be shared across procedures, the follow-up is to lift
        it to a public helper.
        """
        channel_name = self.cfg.heater_setpoint_channel
        try:
            resolved = ctx.instruments.resolve(channel_name)
        except Exception as exc:
            raise HeatFluxTuneError(
                f"setpoint channel {channel_name!r} is not in the registry"
            ) from exc
        device = getattr(resolved.binding, "device", None)
        if device is None or device not in ctx.adapters:
            raise HeatFluxTuneError(f"no adapter bound for setpoint channel {channel_name!r}")
        cmd = ctx.authorization.issue(
            kind="set_setpoint",
            target=channel_name,
            payload={
                "value": value_c,
                "channel": channel_name,
                "device": device,
                "step_kind": "heat_flux_tune",
            },
        )
        result = await ctx.dispatcher.dispatch(device, cmd)
        ctx.bundle_writer.write_event(
            kind="heat_flux_tune.command.issued",
            message=f"setpoint {channel_name}={value_c:.2f} °C",
            severity="info" if result.accepted else "warning",
            source=f"procedure:{PROCEDURE_ID}",
            t_mono_ns=ctx.clock.t_mono_ns(),
            t_utc=datetime.now(UTC),
            metadata={
                "channel": channel_name,
                "device": device,
                "value": value_c,
                "accepted": result.accepted,
                "detail": result.detail,
                "issued_by": cmd.issued_by,
                "authorization_id": cmd.authorization_id,
            },
        )
        if not result.accepted:
            raise HeatFluxTuneError(
                f"adapter rejected setpoint {value_c} on {channel_name}: {result.detail}"
            )

    async def _gauge_sanity_check(self, ctx: ProcedureContext) -> None:
        """Gauge-alive sanity check before the iteration loop.

        Confirms the first flux sample is finite and below
        ``f_gauge_sanity_max_kw_m2``. The check is **not** a "must be
        cold" assertion — starting the tune with the heater already at
        an intermediate setpoint (the typical workflow on this rig) is
        explicitly supported. What we're catching here is the gauge
        returning garbage: NaN/inf (wiring or driver fault) or a
        reading well above the gauge's design full-scale (calibration
        off by 10×, gauge runaway, raw-vs-scaled units mix-up).
        """
        sub = ctx.databus.subscribe_channel(
            name="heat_flux_tune.gauge_sanity",
            channel=self.cfg.flux_channel,
        )
        try:
            with anyio.move_on_after(5.0):
                async for emission in sub:
                    if not isinstance(emission, ChannelSample):
                        continue
                    try:
                        value = float(emission.value)
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(value):
                        raise HeatFluxTuneError(
                            f"gauge reading is non-finite ({emission.value!r}); "
                            f"check wiring and driver"
                        )
                    if value > self.cfg.f_gauge_sanity_max_kw_m2:
                        raise HeatFluxTuneError(
                            f"gauge reading {value:.2f} kW/m² exceeds sanity "
                            f"ceiling {self.cfg.f_gauge_sanity_max_kw_m2} kW/m²; "
                            f"check calibration, wiring, and that the gauge "
                            f"isn't in a runaway condition"
                        )
                    return
            ctx.logger.warning("heat_flux_tune.gauge_sanity_no_sample")
        finally:
            ctx.databus.unsubscribe(sub)

    async def _safe_cool(self, ctx: ProcedureContext) -> None:
        """Best-effort drive to ``t_safe_c`` on shutdown.

        A setpoint failure here is logged but not re-raised — the run
        is already winding down. This is procedure-local cleanup; the
        conductor still disarms workers and finalizes the bundle afterward.
        """
        try:
            await self._issue_setpoint(ctx, self.cfg.t_safe_c)
        except HeatFluxTuneError as exc:
            ctx.logger.warning("heat_flux_tune.safe_cool_failed", error=str(exc))

    def _wall_clock_exhausted(self, ctx: ProcedureContext) -> bool:
        return (ctx.clock.t_mono_ns() - self._run_start_ns) / 1e9 > self.cfg.t_total_max_s

    def _persist_partial(self, ctx: ProcedureContext) -> None:
        """Save the in-progress artifact after each accepted target.

        Crash safety: a session that loses power on target #3 of #4
        still leaves a usable artifact with #1 and #2 on disk. The id
        is the same across all partial saves within a session, so a
        subsequent save unlinks-and-rewrites — the dated-backup
        discipline in :func:`save_artifact` keys off the previous *id*
        in ``latest.toml``, which is unchanged within a session, so no
        same-session backup is taken.
        """
        if self.cfg.persist_dir is None:
            return
        artifact = self._build_artifact(ctx)
        target_dir = Path(self.cfg.persist_dir)
        try:
            file_path = target_dir / f"{artifact.id}.toml"
            if file_path.exists():
                file_path.unlink()
            save_artifact(artifact, target_dir)
        except TuneArtifactError as exc:
            ctx.logger.warning("heat_flux_tune.persist_failed", error=str(exc))

    def _build_artifact(self, ctx: ProcedureContext) -> HeatFluxTuneArtifact:
        rig_name = ctx.config.hardware.name
        device = ctx.config.hardware.devices[0].name if ctx.config.hardware.devices else "unknown"
        try:
            resolved = ctx.instruments.resolve(self.cfg.heater_setpoint_channel)
            bound_device = getattr(resolved.binding, "device", None)
            if isinstance(bound_device, str) and bound_device:
                device = bound_device
        except Exception:
            pass
        id_ = f"{self.cfg.artifact_id_prefix}_{datetime.now(UTC).date().isoformat()}"
        return HeatFluxTuneArtifact(
            id=id_,
            rig=rig_name,
            heater_device=device,
            heater_setpoint_channel=self.cfg.heater_setpoint_channel,
            heater_pv_channel=self.cfg.heater_pv_channel,
            flux_channel=self.cfg.flux_channel,
            gauge_calibration_ref=self.cfg.gauge_calibration_ref,
            geometry=self.cfg.geometry,
            accepted_at=datetime.now(UTC),
            operator_id=self.cfg.operator_id,
            procedure_id=PROCEDURE_ID,
            procedure_version=PROCEDURE_VERSION,
            capa_git_sha=None,
            points=tuple(self._accepted_points),
        )

    # ----------------------------------------------------------------- events

    def _emit_event(
        self,
        ctx: ProcedureContext,
        *,
        kind: str,
        message: str,
        severity: str = "info",
        metadata: dict[str, object] | None = None,
    ) -> None:
        ctx.bundle_writer.write_event(
            kind=kind,
            message=message,
            severity=severity,
            source=f"procedure:{PROCEDURE_ID}",
            t_mono_ns=ctx.clock.t_mono_ns(),
            t_utc=datetime.now(UTC),
            metadata=metadata or {},
        )

    def _emit_iteration_event(
        self,
        ctx: ProcedureContext,
        *,
        iteration: int,
        target: float,
        setpoint_old: float,
        setpoint_new: float,
        measurement: _Measurement,
        err: float,
        df_dt_used: float,
        df_dt_source: str,
        decision: str,
    ) -> None:
        self._emit_event(
            ctx,
            kind="heat_flux_tune.iteration",
            message=(
                f"iter {iteration} target={target:g} sp {setpoint_old:.2f}→"
                f"{setpoint_new:.2f} °C mean={measurement.mean_flux:.2f} "
                f"±{measurement.std_flux:.3f} slope={measurement.slope_flux:.3f} "
                f"err={err:+.2f} decision={decision}"
            ),
            metadata={
                "iteration": iteration,
                "target_kw_m2": target,
                "setpoint_old_c": setpoint_old,
                "setpoint_new_c": setpoint_new,
                "mean_flux_kw_m2": measurement.mean_flux,
                "std_flux_kw_m2": measurement.std_flux,
                "slope_kw_m2_per_min": measurement.slope_flux,
                "error_kw_m2": err,
                "dwell_s": measurement.dwell_s,
                "dF_dT_used": df_dt_used,
                "dF_dT_source": df_dt_source,
                "decision": decision,
                "timed_out": measurement.timed_out,
            },
        )

    # ----------------------------------------------------------------- ticks

    def _emit_tick(
        self,
        ctx: ProcedureContext,
        *,
        state: _SessionState,
        predicate: SteadyStatePredicate | None,
        phase: str,
        elapsed_s: float,
        settle_budget_s: float,
    ) -> None:
        """Publish one live-numerics tick onto the UI sink.

        Cheap by design — a closed bridge or absent sink is a silent
        no-op (the procedure must keep running through a UI
        disconnect). The payload schema is the dock's contract; see
        :class:`~capa.ui.docks.heat_flux_tune.HeatFluxTuneDock` for the
        consumer side.

        ``predicate`` is ``None`` for ticks that emit outside a
        wait-loop (gauge-check, cooling). The dwell/last-reason fields
        fall back to neutral values in that case.
        """
        sink = ctx.ui_sink
        if sink is None:
            return
        mean_flux = state.flux_window.mean()
        std_flux = state.flux_window.std()
        slope_flux = state.flux_window.slope_per_min()
        flux_window_span_s = state.flux_window.span_s()
        flux_samples_in_window = state.flux_window.count()
        window_full = state.flux_window.is_warm(self.cfg.t_window_s)
        if state.last_flux_sample_ns is not None:
            flux_last_sample_age_s: float | None = max(
                0.0, (ctx.clock.t_mono_ns() - state.last_flux_sample_ns) / 1e9
            )
        else:
            flux_last_sample_age_s = None
        if predicate is not None:
            dwell_s = predicate.dwell_s(elapsed_s)
            last_reason = predicate.last_reason
        else:
            dwell_s = 0.0
            last_reason = phase
        error_kw_m2 = state.target_kw_m2 - mean_flux if state.iterations else None
        payload: dict[str, object] = {
            "phase": "paused" if self._paused else phase,
            "target_index": state.target_index,
            "target_count": state.target_count,
            "target_kw_m2": state.target_kw_m2,
            "tolerance_kw_m2": state.tolerance_kw_m2,
            "iteration": state.iteration,
            "iteration_max": state.iteration_max,
            "commanded_setpoint_c": state.commanded_setpoint_c,
            "pv_latest_c": state.pv_latest,
            "pv_mean_c": state.pv_window.mean() if state.pv_window.count() else None,
            "mean_flux_kw_m2": mean_flux,
            "std_flux_kw_m2": std_flux,
            "slope_flux_kw_m2_per_min": slope_flux,
            "window_full": window_full,
            "flux_window_span_s": flux_window_span_s,
            "flux_samples_in_window": flux_samples_in_window,
            "flux_last_sample_age_s": flux_last_sample_age_s,
            "predicate_dwell_s": dwell_s,
            "predicate_last_reason": last_reason,
            "elapsed_s": elapsed_s,
            "settle_budget_s": settle_budget_s,
            "sigma_max_kw_m2": state.sigma_max_kw_m2,
            "slope_max_kw_m2_per_min": state.slope_max_kw_m2_per_min,
            "paused": self._paused,
            "in_tol_windows": state.in_tol_windows,
            "df_dt_used": state.df_dt_used,
            "df_dt_source": state.df_dt_source,
            "runaway_count": state.runaway_count,
            "error_kw_m2": error_kw_m2,
        }
        tick = ProcedureTick(
            procedure_id=PROCEDURE_ID,
            t_mono_ns=ctx.clock.t_mono_ns(),
            payload=payload,
        )
        try:
            sink.publish(tick)
        except Exception as exc:  # pragma: no cover - defensive
            # A misbehaving sink must not crash the procedure. Log
            # once at warning; ticks resume on the next poll cycle.
            ctx.logger.warning("heat_flux_tune.tick_publish_failed", error=str(exc))
