"""Initial-setpoint heuristics and tolerance defaults.

Kept separate from :mod:`.signals` because the lookup chain depends on
:class:`~capa.calibration.tune_artifact.HeatFluxTuneArtifact` — folding
these helpers into the pure-math module would pull a calibration
dependency into a file that's otherwise zero-import. Logically these
are still simple pure functions; the split is about dependency
hygiene, not algorithmic boundaries.
"""

from __future__ import annotations

import math

from capa.calibration.tune_artifact import HeatFluxTuneArtifact

from .config import InitialGuessSource


def sigma_t4_setpoint_c(target_kw_m2: float, ambient_c: float = 25.0) -> float:
    """σT⁴ fallback for a true cold start (no artifact, no operator guess).

    Solves ``F = k (T_h⁴ − T_∞⁴)`` for ``T_h`` with ``k`` fixed at an
    empirical anchor (50 kW/m² ≈ 650 °C heater-side, approximately
    right for the CAPA cone). The result is a *guess*, not a
    measurement — the procedure discovers the truth via feedback on
    iteration 1.
    """
    if target_kw_m2 <= 0:
        return ambient_c
    t_h_anchor_k = 923.15  # 650 °C
    t_inf_k = ambient_c + 273.15
    k = 50.0 / (t_h_anchor_k**4 - t_inf_k**4)
    # Explicit float math: ``x ** 0.25`` returns complex when ``x`` is
    # negative, so mypy infers Any for the result. We control the inputs
    # (all positive) so a math.pow call keeps the annotation honest.
    target_t_k: float = math.pow(target_kw_m2 / k + t_inf_k**4, 0.25)
    return target_t_k - 273.15


def choose_initial_setpoint(
    *,
    target_kw_m2: float,
    source: InitialGuessSource,
    operator_setpoint_c: float | None,
    prior_artifact: HeatFluxTuneArtifact | None,
    t_min_c: float,
    t_set_max_c: float,
    ambient_c: float = 25.0,
) -> tuple[float, str]:
    """Pick the first setpoint for ``target_kw_m2``.

    Priority chain:

    1. ``lookup`` — interpolated from the most recent on-disk artifact
       when one is available and brackets the target.
    2. ``operator`` — ``operator_initial_setpoint_c`` when set.
    3. ``sigma_t4`` — empirical σT⁴ fallback.

    Returns the chosen setpoint (clamped to ``[t_min_c, t_set_max_c]``)
    and a short reason string for logging.
    """
    if source == "lookup" and prior_artifact is not None:
        guess = prior_artifact.setpoint_for_target(target_kw_m2)
        if guess is not None:
            return max(t_min_c, min(t_set_max_c, guess)), "lookup"
    if operator_setpoint_c is not None:
        return (
            max(t_min_c, min(t_set_max_c, operator_setpoint_c)),
            "operator",
        )
    guess = sigma_t4_setpoint_c(target_kw_m2, ambient_c=ambient_c)
    return max(t_min_c, min(t_set_max_c, guess)), "sigma_t4"


def default_tolerance_kw_m2(target_kw_m2: float) -> float:
    """Default flux tolerance: ``max(0.1 kW/m², 0.005 * target)``."""
    return max(0.1, 0.005 * target_kw_m2)


def predicate_strictness(
    *,
    err_kw_m2: float,
    target_kw_m2: float,
    tolerance_kw_m2: float,
    relax_factor: float,
    far_fraction: float = 0.30,
) -> float:
    """Return the per-iteration predicate strictness multiplier ``k``.

    ``k`` lives in ``[1.0, relax_factor]`` and is applied as::

        slope_max_effective = slope_max_kw_per_min * k
        t_stable_effective  = t_stable_s / k

    so that being far from target loosens the slope-flat gate **and**
    shortens the confirmation dwell — wasting wall-clock waiting for a
    quiet field doesn't help when the next secant step is going to be a
    big move anyway. Linear ramp between ``near = 2·tolerance`` and
    ``far = far_fraction · target`` (with ``far`` clamped above ``near``
    for small targets). At iteration 1 the caller passes ``err_kw_m2 =
    None``-equivalent (e.g. ``inf``) so ``k = relax_factor`` — the most
    conservative assumption when there's no information about distance.

    ``relax_factor = 1.0`` disables the feature (predicate stays at
    config defaults throughout). The verify soak is unaffected by this —
    callers construct that predicate from ``cfg`` directly.
    """
    if relax_factor <= 1.0:
        return 1.0
    near = 2.0 * tolerance_kw_m2
    far = max(near + 1e-9, far_fraction * target_kw_m2)
    abs_err = abs(err_kw_m2)
    if not math.isfinite(abs_err) or abs_err >= far:
        return relax_factor
    if abs_err <= near:
        return 1.0
    frac = (abs_err - near) / (far - near)
    return 1.0 + (relax_factor - 1.0) * frac
