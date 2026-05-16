"""Calibrations — first-class snapshotted objects, with documented uncertainty.

Every variant declares ``input_unit`` and ``output_unit`` (validated
against :data:`~capa.core.units.UNIT_REGISTRY`); construction fails when the
input/output dimensions are inconsistent with the variant's algebra. Every
variant carries an :class:`UncertaintySpec` (or an explicit ``None`` declaring
"unmeasured" — never silent), and every variant evaluates a ``raw -> value``
transform plus, when meaningful, an analytical or Monte-Carlo uncertainty
propagation.

A :class:`CalibrationSet` (collection of curves keyed by channel name) is
loaded at run-start and snapshotted into the bundle as ``calibration.json``.
"""

from __future__ import annotations

import bisect
from datetime import datetime
from itertools import pairwise
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from capa.core.errors import CalibrationError
from capa.core.units import UnitStr, units_compatible


class UncertaintySpec(BaseModel):
    """Documented uncertainty attached to a :class:`Calibration`.

    The plan calls out k=1 / k=2 explicitly and forbids silent zero-uncertainty
    claims — set the variant to ``None`` (i.e. don't supply an
    :class:`UncertaintySpec`) only when the calibration genuinely doesn't have
    one and the bundle is to be marked as such.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["absolute", "relative"]
    """``absolute``: ``value ± uncertainty`` in output units.
    ``relative``: ``value × (1 ± uncertainty)`` (dimensionless fraction)."""

    value: float = Field(ge=0)
    """Magnitude. For ``kind="absolute"`` this is in output units; for
    ``kind="relative"`` it is the dimensionless fraction (0.01 = 1%)."""

    coverage_factor: float = Field(default=1.0, gt=0)
    """k-factor (1 = standard, 2 = ~95% expanded). Recorded so an analyzer
    five years later quotes the right interval without guessing."""

    method: str | None = None
    """Free-text description of how the uncertainty was estimated (residuals
    from a fit, manufacturer spec, etc.)."""

    def absolute_for(self, value: float) -> float:
        """Return absolute uncertainty (output units) at ``value``.

        ``kind="relative"`` multiplies the fraction by ``|value|``; the result
        is still ``coverage_factor``-scaled.
        """
        magnitude = self.value if self.kind == "absolute" else self.value * abs(value)
        return magnitude * self.coverage_factor


class FitMetadata(BaseModel):
    """Pedigree of a calibration produced by a calibration *procedure*.

    a fitted Calibration records reference instrument, serial,
    date, residuals, and the source-procedure id + capa git SHA. This makes
    the curve's origin recoverable without trusting human-written notes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference_instrument: str
    reference_serial: str | None = None
    fitted_at: datetime
    rms_residual: float | None = None
    source_procedure_id: str | None = None
    capa_git_sha: str | None = None
    notes: str | None = None


class _CalibrationBase(BaseModel):
    """Common fields and dimensional check for every variant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_unit: UnitStr
    output_unit: UnitStr
    uncertainty: UncertaintySpec | None = None
    fit_metadata: FitMetadata | None = None

    def evaluate(self, raw: float) -> float:  # pragma: no cover - overridden
        raise NotImplementedError

    def evaluate_with_uncertainty(self, raw: float) -> tuple[float, float | None]:
        """Return ``(value, abs_uncertainty)`` in output units.

        The absolute uncertainty is computed by :meth:`UncertaintySpec.absolute_for`
        when an :class:`UncertaintySpec` is attached; otherwise ``None``. Variants
        that propagate a more nuanced uncertainty (e.g. Monte-Carlo for
        polynomials) override this method.
        """
        value = self.evaluate(raw)
        if self.uncertainty is None:
            return value, None
        return value, self.uncertainty.absolute_for(value)


class Identity(_CalibrationBase):
    """``raw -> raw`` (no transform). ``input_unit`` must match ``output_unit``."""

    kind: Literal["identity"] = "identity"

    def invert(self, value: float) -> float:
        """Identity inversion: ``raw == value``. Used by the setpoint
        write path so a derived-unit value round-trips through the
        identity calibration unchanged."""
        return value

    @model_validator(mode="after")
    def _check_units(self) -> Identity:
        if not units_compatible(self.input_unit, self.output_unit):
            raise CalibrationError(
                f"Identity calibration requires input/output dimensional match "
                f"(got {self.input_unit!r} -> {self.output_unit!r})"
            )
        return self

    def evaluate(self, raw: float) -> float:
        return raw


class LinearTwoPoint(_CalibrationBase):
    """Two-point linear fit: ``y = slope * raw + intercept``.

    Slope is computed from the two reference pairs at construction so that
    both the manifest (which serializes ``ref_low``/``ref_high``) and the
    runtime path agree.
    """

    kind: Literal["linear_two_point"] = "linear_two_point"
    ref_low_raw: float
    ref_low_value: float
    ref_high_raw: float
    ref_high_value: float

    @model_validator(mode="after")
    def _check(self) -> LinearTwoPoint:
        if self.ref_high_raw == self.ref_low_raw:
            raise CalibrationError("two-point calibration: raw inputs must differ")
        return self

    @property
    def slope(self) -> float:
        return (self.ref_high_value - self.ref_low_value) / (self.ref_high_raw - self.ref_low_raw)

    @property
    def intercept(self) -> float:
        return self.ref_low_value - self.slope * self.ref_low_raw

    def evaluate(self, raw: float) -> float:
        return self.slope * raw + self.intercept

    def invert(self, value: float) -> float:
        """Solve ``value = slope*raw + intercept`` for ``raw``. Used on
        the setpoint write path to convert a user-facing (``output_unit``)
        value back into the wire-unit (``input_unit``) the device
        expects."""
        return (value - self.intercept) / self.slope


class Polynomial(_CalibrationBase):
    """``y = c0 + c1*raw + c2*raw**2 + ...``"""

    kind: Literal["polynomial"] = "polynomial"
    coefficients: tuple[float, ...] = Field(min_length=1)

    def evaluate(self, raw: float) -> float:
        # Horner evaluation
        result = 0.0
        for coef in reversed(self.coefficients):
            result = result * raw + coef
        return result


class Lookup(_CalibrationBase):
    """Linear interpolation over a sorted ``(raw, value)`` table.

    Out-of-range raws clamp to the nearest endpoint; the variant declares this
    explicitly rather than silently extrapolating, since extrapolated
    calibrations are a recurring source of "the data looked plausible" bugs.
    """

    kind: Literal["lookup"] = "lookup"
    table: tuple[tuple[float, float], ...] = Field(min_length=2)

    @field_validator("table")
    @classmethod
    def _check_sorted(
        cls, value: tuple[tuple[float, float], ...]
    ) -> tuple[tuple[float, float], ...]:
        raws = [pair[0] for pair in value]
        if list(sorted(raws)) != raws:
            raise CalibrationError("lookup table must be sorted by raw input ascending")
        if len(set(raws)) != len(raws):
            raise CalibrationError("lookup table contains duplicate raw inputs")
        return value

    def evaluate(self, raw: float) -> float:
        raws = [pair[0] for pair in self.table]
        values = [pair[1] for pair in self.table]
        if raw <= raws[0]:
            return values[0]
        if raw >= raws[-1]:
            return values[-1]
        idx = bisect.bisect_left(raws, raw)
        # raws[idx-1] < raw < raws[idx]
        x0, x1 = raws[idx - 1], raws[idx]
        y0, y1 = values[idx - 1], values[idx]
        frac = (raw - x0) / (x1 - x0)
        return y0 + frac * (y1 - y0)


class PiecewiseSegment(BaseModel):
    """One segment of a :class:`Piecewise` calibration.

    ``raw_min`` and ``raw_max`` are inclusive on both ends. Adjacent segments
    must share their boundary raw value and produce the same output value at
    that boundary, otherwise construction fails — discontinuous piecewise
    fits silently produce step artifacts in plots.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    raw_min: float
    raw_max: float
    coefficients: tuple[float, ...] = Field(min_length=1)

    def evaluate(self, raw: float) -> float:
        result = 0.0
        for coef in reversed(self.coefficients):
            result = result * raw + coef
        return result


class Piecewise(_CalibrationBase):
    """Sequence of polynomial segments with continuity enforcement at join points."""

    kind: Literal["piecewise"] = "piecewise"
    segments: tuple[PiecewiseSegment, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_continuity(self) -> Piecewise:
        for prev, curr in pairwise(self.segments):
            if prev.raw_max != curr.raw_min:
                raise CalibrationError(
                    f"piecewise segments must share boundary ({prev.raw_max} != {curr.raw_min})"
                )
            left = prev.evaluate(prev.raw_max)
            right = curr.evaluate(curr.raw_min)
            if abs(left - right) > 1e-9 * max(1.0, abs(left)):
                raise CalibrationError(
                    f"piecewise segments are discontinuous at raw={prev.raw_max} "
                    f"({left} vs {right})"
                )
        return self

    def evaluate(self, raw: float) -> float:
        # First segment whose raw_min <= raw <= raw_max wins; clamp at edges.
        if raw <= self.segments[0].raw_min:
            return self.segments[0].evaluate(self.segments[0].raw_min)
        if raw >= self.segments[-1].raw_max:
            return self.segments[-1].evaluate(self.segments[-1].raw_max)
        for seg in self.segments:
            if seg.raw_min <= raw <= seg.raw_max:
                return seg.evaluate(raw)
        # unreachable: continuity check + bracketing covers every raw
        raise CalibrationError(f"piecewise: no segment covers raw={raw}")


class CustomCallable(_CalibrationBase):
    """Reference to an installed callable.

    a custom calibration must name an entry point, package version,
    distribution hash, callable id, serialized parameters, input/output
    dimensions, and test vectors. Anonymous lambdas / unversioned scripts are
    a config error. The active callable metadata is snapshotted into
    ``calibration.json``.

    Only validates the *schema* of the reference; resolving and invoking
    the callable requires the procedure plugin runtime.
    """

    kind: Literal["custom_callable"] = "custom_callable"
    entry_point: str
    """``"package.module:callable"`` form, resolvable via ``importlib.metadata``."""
    package: str
    version: str
    distribution_hash: str
    """SHA-256 of the installed wheel/sdist; matched against ``plugins.lock``."""
    callable_id: str
    """Stable id within the package (e.g. ``"thermocouple.k_type_v3"``)."""
    parameters: dict[str, float | int | str | bool] = Field(default_factory=dict)
    test_vectors: tuple[tuple[float, float], ...] = Field(min_length=1)
    """``(raw, expected_value)`` pairs the callable must reproduce. Used as a
    self-test at calibration-load time once the runtime is available."""

    @field_validator("entry_point")
    @classmethod
    def _check_entry_point(cls, value: str) -> str:
        if ":" not in value:
            raise CalibrationError(
                f"custom_callable entry_point must be 'package.module:callable', got {value!r}"
            )
        return value

    @field_validator("distribution_hash")
    @classmethod
    def _check_hash(cls, value: str) -> str:
        if ":" in value:
            algo, _ = value.split(":", 1)
            if algo not in {"sha256", "sha512"}:
                raise CalibrationError(
                    f"distribution_hash algorithm must be sha256 or sha512, got {algo!r}"
                )
        return value

    def evaluate(self, raw: float) -> float:
        raise CalibrationError("CustomCallable.evaluate() requires the plugin runtime.")


Calibration = Annotated[
    Identity | LinearTwoPoint | Polynomial | Lookup | Piecewise | CustomCallable,
    Field(discriminator="kind"),
]
"""Tagged union over every concrete calibration. Use this annotation in
:class:`~capa.channels.spec.ChannelSpec.calibration` so Pydantic dispatches on
the ``kind`` discriminator at deserialization time.
"""


class CalibrationSet(BaseModel):
    """Collection of calibration curves keyed by channel name.

    snapshotted into the bundle at run-start as ``calibration.json``,
    so re-deriving engineering values five years later does not depend on the
    cal table that happens to be active today.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    revision: str
    curves: dict[str, Calibration]

    def get(self, channel_name: str) -> Calibration:
        try:
            return self.curves[channel_name]
        except KeyError as exc:
            raise CalibrationError(
                f"channel {channel_name!r} has no entry in calibration set {self.name!r}"
            ) from exc


__all__ = [
    "Calibration",
    "CalibrationSet",
    "CustomCallable",
    "FitMetadata",
    "Identity",
    "LinearTwoPoint",
    "Lookup",
    "Piecewise",
    "PiecewiseSegment",
    "Polynomial",
    "UncertaintySpec",
]
