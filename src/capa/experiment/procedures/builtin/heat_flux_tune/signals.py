"""Pure-math primitives backing the heat-flux tuner.

Hampel rejection, the bounded-time rolling window, the three-condition
steady-state predicate, the damped secant step, and the wrong-direction
runaway detector all live here. Every function/class is unit-testable
without async, without a databus, and without any capa-specific import —
keeping this module dependency-free is what lets the controller stay
testable in isolation and what makes the signal primitives reusable in
other procedures should the need arise.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass, field


def hampel_mask(values: list[float], *, k: float = 3.0) -> list[bool]:
    """Return a per-sample keep/discard mask via the Hampel identifier.

    Any sample more than ``k`` median-absolute-deviations from the
    window median is marked for discard (``False`` in the returned
    mask). A window with zero MAD (every sample identical) returns an
    all-``True`` mask. Non-finite samples are always rejected.

    Returning a mask (not a filtered value list) lets callers apply the
    same rejection decision to paired sequences — e.g. timestamps and
    values — without value-matching gymnastics.
    """
    n = len(values)
    if n < 3:
        return [math.isfinite(v) for v in values]
    finite_vals = [v for v in values if math.isfinite(v)]
    if len(finite_vals) < 3:
        return [math.isfinite(v) for v in values]
    med = statistics.median(finite_vals)
    deviations = [abs(v - med) for v in finite_vals]
    mad = statistics.median(deviations)
    if mad == 0.0:
        # Degenerate case: ≥ half the samples equal the median. A
        # value not equal to the median (a single 999 spike against a
        # constant baseline) is then a clear outlier. A genuinely
        # all-identical window has every ``v == med`` so this still
        # keeps everything.
        return [math.isfinite(v) and v == med for v in values]
    threshold = k * 1.4826 * mad  # 1.4826 = consistency factor for Gaussian
    return [math.isfinite(v) and abs(v - med) <= threshold for v in values]


def linear_slope_per_min(samples: list[tuple[float, float]]) -> float:
    """Least-squares slope of ``samples`` in units-per-minute.

    ``samples`` is ``[(t_seconds, value), ...]`` ordered by time. Fewer
    than two points returns ``0.0``. Two identical timestamps that
    would produce a divide-by-zero similarly return ``0.0`` — the
    caller is feeding a rolling window where a degenerate slice
    shouldn't abort the procedure.
    """
    n = len(samples)
    if n < 2:
        return 0.0
    sum_t = 0.0
    sum_v = 0.0
    sum_tt = 0.0
    sum_tv = 0.0
    for t, v in samples:
        sum_t += t
        sum_v += v
        sum_tt += t * t
        sum_tv += t * v
    denom = n * sum_tt - sum_t * sum_t
    if denom == 0.0:
        return 0.0
    slope_per_s = (n * sum_tv - sum_t * sum_v) / denom
    return slope_per_s * 60.0


@dataclass(slots=True)
class RollingWindow:
    """Bounded-time rolling window of ``(t_seconds, value)`` samples.

    Eviction is purely time-based — push the newest, drop everything
    older than ``window_s``. Mean/std/slope all run over the
    Hampel-filtered subset of the current window; the filtered samples
    are dropped from those statistics but stay in the window itself
    (so a transient spike doesn't shorten the effective dwell when the
    next non-spike sample lands).
    """

    window_s: float
    hampel_k: float = 3.0
    _samples: deque[tuple[float, float]] = field(default_factory=deque)

    def push(self, t_s: float, value: float) -> None:
        if not math.isfinite(value):
            return
        self._samples.append((t_s, value))
        cutoff = t_s - self.window_s
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def count(self) -> int:
        return len(self._samples)

    def clear(self) -> None:
        """Drop all samples. Used at iteration boundaries so the rolling
        statistics reflect only post-setpoint-change data — without this
        the std and slope are dominated by stale samples from before the
        last secant step for up to ``window_s`` seconds.
        """
        self._samples.clear()

    def span_s(self) -> float:
        if len(self._samples) < 2:
            return 0.0
        return self._samples[-1][0] - self._samples[0][0]

    def is_warm(self, target_window_s: float) -> bool:
        """Return True once enough wall time has elapsed since the first push.

        ``span_s()`` is bounded above by ``window_s`` minus one sample
        period — the oldest retained sample sits one period *inside*
        the eviction cutoff — so a naive ``span >= window_s`` check can
        never fire at non-trivial sample rates. Use the in-window
        average period as the slop term: at steady state
        ``span + period`` straddles ``window_s`` from below.
        """
        n = len(self._samples)
        if n < 2:
            return False
        span = self._samples[-1][0] - self._samples[0][0]
        avg_period = span / (n - 1) if n > 1 else 0.0
        return span + avg_period >= target_window_s

    def _keep_pairs(self) -> list[tuple[float, float]]:
        pairs = list(self._samples)
        if not pairs:
            return []
        values = [v for _, v in pairs]
        mask = hampel_mask(values, k=self.hampel_k)
        return [pair for pair, keep in zip(pairs, mask, strict=True) if keep]

    def mean(self) -> float:
        pairs = self._keep_pairs()
        if not pairs:
            return 0.0
        return statistics.fmean(v for _, v in pairs)

    def std(self) -> float:
        pairs = self._keep_pairs()
        if len(pairs) < 2:
            return 0.0
        return statistics.pstdev(v for _, v in pairs)

    def slope_per_min(self) -> float:
        return linear_slope_per_min(self._keep_pairs())


# ---------------------------------------------------------------------------
# Steady-state predicate
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SteadyStatePredicate:
    """Three-condition predicate with hold-time confirmation.

    Conditions:

    * ``|mean(heater.pv) - heater.setpoint| <= delta_t_band_c``
    * rolling std-dev of flux ``<= sigma_flux_max``
    * ``|d(flux)/d(min)| <= slope_max_kw_per_min``

    All three must hold continuously for ``t_stable_s`` before
    :meth:`fired` returns ``True``. As soon as any one fails, the hold
    timer resets.

    The PV gate compares the **windowed mean** of the heater PV to the
    commanded setpoint (not the latest instantaneous sample). The
    Watlow PID tracks its setpoint very tightly on average but its
    instantaneous readings carry thermal/wire noise on the order of a
    few hundred mK — at high setpoints (≥ ~600 °C) those blips
    repeatedly reset an instantaneous-sample timer even though the
    heater is, by every other measure, perfectly steady. The mean
    smooths out the blips; a *genuine* tracking offset still moves the
    mean and trips the gate.
    """

    delta_t_band_c: float
    sigma_flux_max: float
    slope_max_kw_per_min: float
    t_stable_s: float
    _hold_start_s: float | None = None
    _last_reason: str = "no-samples-yet"

    def evaluate(
        self,
        *,
        now_s: float,
        pv_mean_c: float | None,
        setpoint_c: float | None,
        flux_std_kw_m2: float,
        flux_slope_kw_per_min: float,
        window_full: bool,
    ) -> None:
        if not window_full:
            self._hold_start_s = None
            self._last_reason = "window-not-full"
            return
        if pv_mean_c is None or setpoint_c is None:
            self._hold_start_s = None
            self._last_reason = "missing-pv-or-setpoint"
            return
        if abs(pv_mean_c - setpoint_c) > self.delta_t_band_c:
            self._hold_start_s = None
            self._last_reason = "pv-out-of-band"
            return
        if flux_std_kw_m2 > self.sigma_flux_max:
            self._hold_start_s = None
            self._last_reason = "flux-noisy"
            return
        if abs(flux_slope_kw_per_min) > self.slope_max_kw_per_min:
            self._hold_start_s = None
            self._last_reason = "flux-drifting"
            return
        if self._hold_start_s is None:
            self._hold_start_s = now_s
        self._last_reason = "holding"

    def fired(self, now_s: float) -> bool:
        return self._hold_start_s is not None and (now_s - self._hold_start_s) >= self.t_stable_s

    def dwell_s(self, now_s: float) -> float:
        if self._hold_start_s is None:
            return 0.0
        return now_s - self._hold_start_s

    @property
    def last_reason(self) -> str:
        return self._last_reason

    def reset(self) -> None:
        self._hold_start_s = None
        self._last_reason = "reset"


# ---------------------------------------------------------------------------
# Correction step and runaway detector
# ---------------------------------------------------------------------------


def secant_step(
    *,
    err_kw_m2: float,
    df_dt_kw_m2_per_c: float,
    damping: float,
    delta_t_step_max_c: float,
) -> float:
    """Compute one damped, clamped ΔT correction.

    Returns ``0.0`` when ``df_dt`` is non-positive or numerically tiny
    — applying a step with a zero/wrong-sign Jacobian would do nothing
    useful or move the heater the wrong way. The runaway detector
    handles wrong-sign explicitly via the next-iteration check; this
    function just refuses to amplify a bad slope.
    """
    if not math.isfinite(df_dt_kw_m2_per_c) or df_dt_kw_m2_per_c <= 1e-6:
        return 0.0
    raw = err_kw_m2 / df_dt_kw_m2_per_c
    damped = damping * raw
    return max(-delta_t_step_max_c, min(delta_t_step_max_c, damped))


@dataclass(slots=True)
class RunawayDetector:
    """Tripwire for "the heater is being commanded the wrong way".

    Counts consecutive iterations where ``sign(err)`` and
    ``sign(delta_t_last)`` disagree. Trips after ``trip_threshold``
    such iterations.

    ``record(err=0.0, delta=0.0)`` resets — converged iterations are
    not runaway candidates and neither are step-zero iterations
    (already-clamped, no information).
    """

    trip_threshold: int
    _count: int = 0

    def record(self, *, err_kw_m2: float, delta_t_c: float) -> None:
        if err_kw_m2 == 0.0 or delta_t_c == 0.0:
            self._count = 0
            return
        same_sign = (err_kw_m2 > 0) == (delta_t_c > 0)
        if same_sign:
            self._count = 0
        else:
            self._count += 1

    @property
    def count(self) -> int:
        return self._count

    def tripped(self) -> bool:
        return self._count >= self.trip_threshold
