"""Domain-profile preflight runtime — id → callable check registry.

The :class:`~capa.experiment.profiles.base.PreflightCheck`
schema declares *what* should be checked; this module is the registry that
maps each ``id`` to a concrete callable. The engine resolves the active
profile, walks its ``preflight_checks``, runs each callable, and collects
:class:`~capa.experiment.procedures.base.Problem` entries.

A check function signature:

    async def check(ctx: ProfilePreflightContext) -> Problem | None

Return ``None`` when the check passes; return a :class:`Problem` to surface
a warning/error. Raising is treated as an unexpected failure (logged and
recorded as a blocking error problem).

Builtin checks are registered at import time via the :func:`register`
decorator. Profile authors can register their own checks the same way; the
plugin runtime imports the profile module which triggers registration.
"""

from __future__ import annotations

import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import anyio

from capa.devices.records import ChannelSample
from capa.experiment.procedures.base import Problem
from capa.experiment.profiles.base import ChannelRequirement

if TYPE_CHECKING:
    from capa.channels.registry import ChannelRegistry
    from capa.core.databus import DataBus
    from capa.experiment.config import ExperimentConfig


@dataclass(slots=True)
class ProfilePreflightContext:
    """What a profile preflight check sees.

    Deliberately smaller than :class:`ProcedureContext` — preflight runs
    *before* the bundle is opened, so there's no writer or executor, only
    config/registry/databus and an event loop where the check can do
    short-running observations (e.g. listen for 5 s of mass samples to
    assess balance stability)."""

    config: ExperimentConfig
    instruments: ChannelRegistry
    databus: DataBus
    profile_metadata: dict[str, Any]
    """The ``DomainProfileRef.metadata`` dict for the active profile —
    e.g. CAPA's :class:`CapaPyrolysisMetadata` after model_dump."""
    adapters_started: bool = False
    """``True`` when the engine has run ``adapter.start()`` for every
    declared device. Dynamic checks that observe live samples flip their
    silent-channel handling on this flag: pre-start, no samples means
    "stream not open yet" (warning); post-start, no samples means the
    device is broken (blocking error)."""


Category = Literal["static", "dynamic"]
"""Static checks read only config / filesystem state and run before adapters
are opened. Dynamic checks read live samples and must run inside the engine
task group, after every ``adapter.start()`` has returned."""


CheckFn = Callable[[ProfilePreflightContext], Awaitable[Problem | None]]
"""Signature of a registered preflight check."""

_REGISTRY: dict[str, tuple[CheckFn, Category]] = {}


def register(check_id: str, *, category: Category = "static") -> Callable[[CheckFn], CheckFn]:
    """Decorator: register a check callable under ``check_id``.

    Re-registration is allowed and replaces the previous binding — useful
    for tests that want to swap a slow check for a fast stub. Production
    plugin loading does not re-register a builtin id; collisions there are
    surfaced by the plugin trust check, not here.

    ``category`` defaults to ``"static"`` so plugins that registered before
    this argument existed keep their pre-task-group execution order."""

    def _wrap(fn: CheckFn) -> CheckFn:
        _REGISTRY[check_id] = (fn, category)
        return fn

    return _wrap


def get(check_id: str) -> CheckFn | None:
    """Look up a registered check. Returns ``None`` if unknown."""
    entry = _REGISTRY.get(check_id)
    return entry[0] if entry is not None else None


def get_category(check_id: str) -> Category | None:
    """Return the registered category for ``check_id`` or ``None`` if
    unknown."""
    entry = _REGISTRY.get(check_id)
    return entry[1] if entry is not None else None


def registered_ids() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY.keys()))


def filter_by_category(check_ids: tuple[str, ...], category: Category) -> tuple[str, ...]:
    """Return ``check_ids`` restricted to those registered under ``category``.

    Unknown ids are dropped — they're surfaced separately by
    :func:`run_profile_preflight` as ``profile.unknown_check`` problems."""
    return tuple(cid for cid in check_ids if get_category(cid) == category)


async def run_profile_preflight(
    ctx: ProfilePreflightContext,
    check_ids: tuple[str, ...],
) -> list[Problem]:
    """Run every check in ``check_ids`` and collect results.

    A missing check id is recorded as a blocking problem so a typo in a
    profile's ``preflight_checks`` list is surfaced loudly. Exceptions are
    caught and converted to blocking problems with code
    ``"profile.check_raised"``.
    """
    problems: list[Problem] = []
    for check_id in check_ids:
        entry = _REGISTRY.get(check_id)
        if entry is None:
            problems.append(
                Problem(
                    code="profile.unknown_check",
                    message=f"profile referenced unknown preflight check {check_id!r}",
                    severity="error",
                    blocking=True,
                )
            )
            continue
        fn = entry[0]
        try:
            result = await fn(ctx)
        except Exception as exc:
            problems.append(
                Problem(
                    code="profile.check_raised",
                    message=f"preflight check {check_id!r} raised: {exc}",
                    severity="error",
                    blocking=True,
                    metadata={"error_type": type(exc).__name__},
                )
            )
            continue
        if result is not None:
            problems.append(result)
    return problems


# ---------------------------------------------------------------------------
# Builtin checks — generic + CAPA profile.
# ---------------------------------------------------------------------------


@register("capa.required_channel_mappings")
@register("cone.required_channel_mappings")
async def _required_channel_mappings(ctx: ProfilePreflightContext) -> Problem | None:
    """Verify every required channel group has at least min_count members.

    Both the CAPA and cone profiles register this id (the implementation is
    identical — both use :attr:`ChannelSpec.metadata['<profile>_group']` /
    similar markers, validated against :attr:`required_channel_groups`).
    """
    profile_id = ctx.config.domain_profile.id if ctx.config.domain_profile else ""
    group_key = "capa_group" if "capa" in profile_id else "cone_group"
    required = _resolve_required_groups(profile_id)
    if not required:
        return None

    by_group: dict[str, list[str]] = {}
    for ch in ctx.config.hardware.channels:
        group = ch.metadata.get(group_key)
        if not group:
            continue
        by_group.setdefault(group, []).append(ch.name)

    missing: list[str] = []
    for req in required:
        members = by_group.get(req.group, [])
        if len(members) < req.min_count:
            missing.append(f"{req.group} (have {len(members)}, need {req.min_count})")

    if missing:
        return Problem(
            code="profile.missing_channel_groups",
            message="missing required channel groups: " + "; ".join(missing),
            severity="error",
            blocking=True,
            metadata={"missing": missing, "group_key": group_key},
        )
    return None


@register("capa.atmosphere_consistency")
async def _atmosphere_consistency(ctx: ProfilePreflightContext) -> Problem | None:
    """Oxidative / reactive_blend modes must declare a reactive_gas_flow channel."""
    mode = ctx.profile_metadata.get("atmosphere", {}).get("mode")
    if mode not in ("oxidative", "reactive_blend"):
        return None
    has_reactive = any(
        ch.metadata.get("capa_group") == "reactive_gas_flow" for ch in ctx.config.hardware.channels
    )
    if not has_reactive:
        return Problem(
            code="capa.atmosphere_inconsistent",
            message=(
                f"atmosphere.mode={mode!r} requires a channel tagged capa_group='reactive_gas_flow'"
            ),
            severity="error",
            blocking=True,
            metadata={"mode": mode},
        )
    return None


@register("capa.heater_pv_in_safe_range", category="dynamic")
async def _heater_pv_safe(ctx: ProfilePreflightContext) -> Problem | None:
    """Heater PV is below a sane startup limit before arming.

    Default limit is 200 °C — a CAPA reactor at room temperature is
    expected. The limit can be overridden in
    ``profile_metadata['_safe_arm']['max_heater_pv_c']`` for hot-swap or
    rapid-cycle routines."""
    safe = ctx.profile_metadata.get("_safe_arm", {})
    limit_c: float = float(safe.get("max_heater_pv_c", 200.0))
    sample = await _sample_one(ctx, group_key="capa_group", group_value="heater_pv")
    if sample is None:
        # Silent post-start = device broken; pre-start = stream not yet open.
        return Problem(
            code="capa.heater_pv_silent",
            message="no heater_pv sample observed within preflight window",
            severity="error" if ctx.adapters_started else "warning",
            blocking=ctx.adapters_started,
        )
    value = float(sample.value)
    if value > limit_c:
        return Problem(
            code="capa.heater_pv_too_hot",
            message=(
                f"heater PV is {value:.1f} {sample.unit}, above safe-arm limit "
                f"{limit_c:.1f} °C; let the reactor cool before arming"
            ),
            severity="error",
            blocking=True,
            metadata={"observed_c": value, "limit_c": limit_c},
        )
    return None


@register("capa.purge_flow_established", category="dynamic")
async def _purge_flow_established(ctx: ProfilePreflightContext) -> Problem | None:
    """Purge flow has been seen at >= target * 0.5 for >=3 s."""
    target = ctx.profile_metadata.get("atmosphere", {}).get("purge", {}).get("target_flow_sccm")
    if target is None:
        return Problem(
            code="capa.purge_target_missing",
            message="atmosphere.purge.target_flow_sccm not declared",
            severity="warning",
            blocking=False,
        )
    if float(target) <= 0:
        # Explicit zero = operator declared a no-flow run (purge wired but
        # intentionally not flowed). Nothing to verify.
        return None
    threshold = float(target) * 0.5
    samples = await _sample_for(
        ctx,
        group_key="capa_group",
        group_value="purge_gas_flow",
        seconds=3.0,
    )
    if not samples:
        # Pre-adapter-start, silent = "stream not open yet" → warning.
        # Post-adapter-start, silent = device broken → blocking.
        return Problem(
            code="capa.purge_silent",
            message="no purge_gas_flow samples observed within preflight window",
            severity="error" if ctx.adapters_started else "warning",
            blocking=ctx.adapters_started,
        )
    last_below = [s for s in samples if float(s.value) < threshold]
    if last_below:
        return Problem(
            code="capa.purge_below_target",
            message=(
                f"purge flow held below {threshold:.2f} sccm for "
                f"{len(last_below)}/{len(samples)} samples in last 3 s"
            ),
            severity="error",
            blocking=True,
            metadata={"threshold_sccm": threshold},
        )
    return None


@register("capa.leak_test_recency")
async def _leak_test_recency(ctx: ProfilePreflightContext) -> Problem | None:

    leak = ctx.profile_metadata.get("atmosphere", {}).get("leak_check_at")
    if leak is None:
        return Problem(
            code="capa.leak_test_missing",
            message="atmosphere.leak_check_at is not set; recency cannot be verified",
            severity="warning",
            blocking=False,
        )
    if isinstance(leak, str):
        try:
            leak_dt = datetime.fromisoformat(leak)
        except ValueError:
            return Problem(
                code="capa.leak_test_unparseable",
                message=f"atmosphere.leak_check_at is not ISO-8601: {leak!r}",
                severity="warning",
                blocking=False,
            )
    else:
        leak_dt = leak
    if leak_dt.tzinfo is None:
        leak_dt = leak_dt.replace(tzinfo=UTC)
    age = datetime.now(UTC) - leak_dt
    window = timedelta(days=int(ctx.profile_metadata.get("_leak_window_days", 7)))
    if age > window:
        return Problem(
            code="capa.leak_test_stale",
            message=f"leak check is {age.days} days old (>{window.days} day window)",
            severity="warning",
            blocking=False,
            metadata={"age_days": age.days, "window_days": window.days},
        )
    return None


@register("capa.flux_calibration_freshness")
async def _flux_calibration_freshness(ctx: ProfilePreflightContext) -> Problem | None:
    """Warn when a flux target is declared without a recent calibration.

    Implements the proposal §14 Phase 3 step: a specimen run that declares
    a ``target_heat_flux_kw_m2`` but no ``flux_calibration_ref`` lacks the
    flux↔setpoint mapping that justifies its chosen heater setpoint. The
    check is non-blocking — the operator can knowingly run with a stale
    calibration if they accept the consequences. Recency is only enforced
    when the ref resolves to an on-disk tune artifact; free-form refs
    (lab notebook entries, etc.) pass without recency checks.

    Overrides via ``profile_metadata`` mirror the leak-test recency knob:

    * ``_flux_calibration_window_days`` (default ``7``)
    * ``_flux_calibration_dir`` (default ``configs/calibrations/flux``)
    """
    program = ctx.profile_metadata.get("heater_program") or {}
    if not isinstance(program, dict):
        return None
    target = program.get("target_heat_flux_kw_m2")
    try:
        target_value = float(target) if target is not None else 0.0
    except (TypeError, ValueError):
        target_value = 0.0
    if target_value <= 0:
        return None

    ref = program.get("flux_calibration_ref")
    if not isinstance(ref, str) or not ref.strip():
        return Problem(
            code="capa.flux_calibration_missing",
            message=(
                f"target_heat_flux_kw_m2={target_value:g} declared but no "
                f"flux_calibration_ref is set — run a heat-flux tune or pick "
                f"an existing artifact"
            ),
            severity="warning",
            blocking=False,
            metadata={"target_kw_m2": target_value},
        )
    ref = ref.strip()

    flux_dir = Path(
        str(ctx.profile_metadata.get("_flux_calibration_dir") or "configs/calibrations/flux")
    )
    if not flux_dir.is_absolute():
        flux_dir = (Path.cwd() / flux_dir).resolve()
    artifact_path = flux_dir / f"{ref}.toml"
    if not artifact_path.is_file():
        # Free-form ref (lab notebook entry, external doc) — nothing to
        # date-check. Pass; the operator owns the cross-reference.
        return None

    try:
        from capa.calibration.tune_artifact import (  # noqa: PLC0415
            TuneArtifactError,
            load_artifact,
        )

        artifact = load_artifact(artifact_path)
    except TuneArtifactError as exc:
        return Problem(
            code="capa.flux_calibration_unreadable",
            message=(
                f"flux_calibration_ref={ref!r} points at an on-disk artifact that "
                f"failed to load: {exc}"
            ),
            severity="warning",
            blocking=False,
            metadata={"ref": ref, "path": str(artifact_path)},
        )

    window_days = int(ctx.profile_metadata.get("_flux_calibration_window_days", 7))
    accepted_at = artifact.accepted_at
    if accepted_at.tzinfo is None:
        accepted_at = accepted_at.replace(tzinfo=UTC)
    age = datetime.now(UTC) - accepted_at
    if age > timedelta(days=window_days):
        return Problem(
            code="capa.flux_calibration_stale",
            message=(
                f"flux calibration {ref!r} is {age.days} days old "
                f"(>{window_days} day window) — consider re-tuning before this run"
            ),
            severity="warning",
            blocking=False,
            metadata={
                "ref": ref,
                "age_days": age.days,
                "window_days": window_days,
            },
        )
    return None


@register("capa.balance_stability", category="dynamic")
@register("cone.balance_stability", category="dynamic")
async def _balance_stability(ctx: ProfilePreflightContext) -> Problem | None:
    """When a mass channel is declared, it must report stable for >=5 s."""
    profile_id = ctx.config.domain_profile.id if ctx.config.domain_profile else ""
    group_key = "capa_group" if "capa" in profile_id else "cone_group"
    has_mass = any(ch.metadata.get(group_key) == "mass" for ch in ctx.config.hardware.channels)
    if not has_mass:
        return None  # nothing to check
    samples = await _sample_for(ctx, group_key=group_key, group_value="mass", seconds=5.0)
    if not samples:
        return Problem(
            code="profile.balance_silent",
            message="no mass samples observed within preflight window",
            severity="error" if ctx.adapters_started else "warning",
            blocking=ctx.adapters_started,
        )
    values = [float(s.value) for s in samples]
    spread = max(values) - min(values)
    if spread > 0.05:  # 50 mg
        return Problem(
            code="profile.balance_unstable",
            message=f"mass channel spread is {spread * 1000:.1f} mg over 5 s (>50 mg)",
            severity="warning",
            blocking=False,
            metadata={"spread_g": spread},
        )
    return None


@register("capa.disk_projection")
@register("cone.disk_projection")
async def _disk_projection(ctx: ProfilePreflightContext) -> Problem | None:
    """Verify the bundle volume has at least 1.5x the projected size free.

    Conservative estimate: assume 1 MB / s + 100 MB headroom over a 30-minute run."""

    runs_root = Path(ctx.config.storage.bundle_root)
    runs_root = runs_root.resolve() if runs_root.is_absolute() else Path.cwd() / runs_root
    runs_root.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(runs_root).free
    projected_bytes = int(1_800.0 * 1_024 * 1_024)  # 30 min * 1 MB/s + headroom
    needed = int(projected_bytes * 1.5)
    if free_bytes < needed:
        return Problem(
            code="profile.disk_low",
            message=(
                f"only {free_bytes / 2**30:.1f} GiB free at {runs_root}; "
                f"need {needed / 2**30:.1f} GiB for 1.5x margin"
            ),
            severity="error",
            blocking=True,
            metadata={"free_bytes": free_bytes, "needed_bytes": needed},
        )
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _sample_one(
    ctx: ProfilePreflightContext,
    *,
    group_key: str,
    group_value: str,
    timeout_s: float = 2.0,
) -> ChannelSample | None:
    """Wait up to ``timeout_s`` for one ChannelSample on any channel tagged
    ``metadata[group_key]==group_value``. Returns ``None`` on timeout."""
    channel_names = [
        ch.name for ch in ctx.config.hardware.channels if ch.metadata.get(group_key) == group_value
    ]
    if not channel_names:
        return None
    sub = ctx.databus.subscribe(name=f"preflight-{group_value}")
    try:
        with anyio.move_on_after(timeout_s):
            async for emission in sub:
                if isinstance(emission, ChannelSample) and emission.channel in channel_names:
                    return emission
    finally:
        ctx.databus.unsubscribe(sub)
    return None


async def _sample_for(
    ctx: ProfilePreflightContext,
    *,
    group_key: str,
    group_value: str,
    seconds: float,
) -> list[ChannelSample]:
    """Collect every ChannelSample on the group's channels for ``seconds``."""
    channel_names = [
        ch.name for ch in ctx.config.hardware.channels if ch.metadata.get(group_key) == group_value
    ]
    out: list[ChannelSample] = []
    if not channel_names:
        return out
    sub = ctx.databus.subscribe(name=f"preflight-collect-{group_value}")
    try:
        with anyio.move_on_after(seconds):
            async for emission in sub:
                if isinstance(emission, ChannelSample) and emission.channel in channel_names:
                    out.append(emission)
    finally:
        ctx.databus.unsubscribe(sub)
    return out


def _resolve_required_groups(profile_id: str) -> tuple[ChannelRequirement, ...]:
    """Defer-import the profile module and return its required_channel_groups.

    Avoids a top-level import cycle (the profile modules already import from
    profiles.base; runtime.py is imported by the engine which imports the
    profile module too)."""
    if "capa_pyrolysis" in profile_id:
        from capa.experiment.profiles.capa_pyrolysis import REQUIRED_CHANNEL_GROUPS  # noqa: PLC0415

        return REQUIRED_CHANNEL_GROUPS
    if "cone_calorimeter" in profile_id:
        from capa.experiment.profiles.cone_calorimeter import (  # noqa: PLC0415
            REQUIRED_CHANNEL_GROUPS,
        )

        return REQUIRED_CHANNEL_GROUPS
    return ()


__all__ = [
    "CheckFn",
    "ProfilePreflightContext",
    "get",
    "register",
    "registered_ids",
    "run_profile_preflight",
]
