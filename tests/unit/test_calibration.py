from __future__ import annotations

import pytest

from capa.channels.calibration import (
    CustomCallable,
    Identity,
    LinearTwoPoint,
    Lookup,
    Piecewise,
    PiecewiseSegment,
    Polynomial,
    UncertaintySpec,
)
from capa.core.errors import CalibrationError


class TestUncertainty:
    def test_absolute(self) -> None:
        u = UncertaintySpec(kind="absolute", value=0.5, coverage_factor=2)
        assert u.absolute_for(100.0) == pytest.approx(1.0)

    def test_relative(self) -> None:
        u = UncertaintySpec(kind="relative", value=0.01, coverage_factor=2)
        assert u.absolute_for(100.0) == pytest.approx(2.0)

    def test_negative_value_rejected(self) -> None:
        with pytest.raises(Exception):
            UncertaintySpec(kind="absolute", value=-1.0)


class TestIdentity:
    def test_passthrough(self) -> None:
        cal = Identity(input_unit="V", output_unit="V")
        assert cal.evaluate(1.5) == 1.5
        assert cal.evaluate_with_uncertainty(1.5) == (1.5, None)

    def test_dimensional_match(self) -> None:
        # degC and K share dimensionality.
        Identity(input_unit="degC", output_unit="K")

    def test_dimensional_mismatch_rejected(self) -> None:
        with pytest.raises(CalibrationError):
            Identity(input_unit="V", output_unit="kg")


class TestLinearTwoPoint:
    def test_evaluate(self) -> None:
        cal = LinearTwoPoint(
            input_unit="V",
            output_unit="kPa",
            ref_low_raw=0.0,
            ref_low_value=0.0,
            ref_high_raw=5.0,
            ref_high_value=100.0,
        )
        assert cal.evaluate(2.5) == pytest.approx(50.0)
        assert cal.slope == pytest.approx(20.0)
        assert cal.intercept == pytest.approx(0.0)

    def test_with_uncertainty(self) -> None:
        cal = LinearTwoPoint(
            input_unit="V",
            output_unit="kPa",
            ref_low_raw=0.0,
            ref_low_value=0.0,
            ref_high_raw=5.0,
            ref_high_value=100.0,
            uncertainty=UncertaintySpec(kind="absolute", value=0.5, coverage_factor=2),
        )
        value, unc = cal.evaluate_with_uncertainty(2.5)
        assert value == pytest.approx(50.0)
        assert unc == pytest.approx(1.0)

    def test_collinear_rejected(self) -> None:
        with pytest.raises(CalibrationError):
            LinearTwoPoint(
                input_unit="V",
                output_unit="kPa",
                ref_low_raw=1.0,
                ref_low_value=0.0,
                ref_high_raw=1.0,
                ref_high_value=100.0,
            )


class TestPolynomial:
    def test_constant(self) -> None:
        cal = Polynomial(input_unit="V", output_unit="K", coefficients=(273.15,))
        assert cal.evaluate(123.0) == 273.15

    def test_linear(self) -> None:
        # y = 273.15 + 100*x
        cal = Polynomial(input_unit="V", output_unit="K", coefficients=(273.15, 100.0))
        assert cal.evaluate(0.0) == 273.15
        assert cal.evaluate(1.0) == 373.15

    def test_quadratic(self) -> None:
        # y = 1 + 2x + 3x^2
        cal = Polynomial(input_unit="V", output_unit="V", coefficients=(1.0, 2.0, 3.0))
        assert cal.evaluate(2.0) == pytest.approx(1 + 4 + 12)


class TestLookup:
    def test_endpoints(self) -> None:
        cal = Lookup(
            input_unit="ohm",
            output_unit="degC",
            table=((100.0, 0.0), (138.51, 100.0)),
        )
        assert cal.evaluate(100.0) == 0.0
        assert cal.evaluate(138.51) == 100.0

    def test_interpolation(self) -> None:
        cal = Lookup(
            input_unit="ohm",
            output_unit="degC",
            table=((100.0, 0.0), (138.51, 100.0)),
        )
        midpoint = (100.0 + 138.51) / 2.0
        assert cal.evaluate(midpoint) == pytest.approx(50.0)

    def test_clamps_below_range(self) -> None:
        cal = Lookup(
            input_unit="ohm",
            output_unit="degC",
            table=((100.0, 0.0), (138.51, 100.0)),
        )
        assert cal.evaluate(50.0) == 0.0  # clamped
        assert cal.evaluate(200.0) == 100.0  # clamped

    def test_unsorted_rejected(self) -> None:
        with pytest.raises(CalibrationError):
            Lookup(
                input_unit="ohm",
                output_unit="degC",
                table=((138.51, 100.0), (100.0, 0.0)),
            )

    def test_duplicate_raw_rejected(self) -> None:
        with pytest.raises(CalibrationError):
            Lookup(
                input_unit="ohm",
                output_unit="degC",
                table=((100.0, 0.0), (100.0, 50.0), (200.0, 100.0)),
            )


class TestPiecewise:
    def test_continuous(self) -> None:
        # seg1: [0,1] y = 100x
        # seg2: [1,2] y = 100 + 0*(x-1) = constant 100? No — coefficients=(50,50)
        # means y = 50 + 50x evaluated at raw_min=1 = 100, at raw_max=2 = 150.
        # First segment at x=1 evaluates as 100, so they meet.
        cal = Piecewise(
            input_unit="V",
            output_unit="K",
            segments=(
                PiecewiseSegment(raw_min=0, raw_max=1, coefficients=(0.0, 100.0)),
                PiecewiseSegment(raw_min=1, raw_max=2, coefficients=(50.0, 50.0)),
            ),
        )
        assert cal.evaluate(0.5) == pytest.approx(50.0)
        assert cal.evaluate(1.0) == pytest.approx(100.0)
        assert cal.evaluate(1.5) == pytest.approx(125.0)

    def test_discontinuous_rejected(self) -> None:
        with pytest.raises(CalibrationError):
            Piecewise(
                input_unit="V",
                output_unit="K",
                segments=(
                    PiecewiseSegment(raw_min=0, raw_max=1, coefficients=(0.0, 100.0)),
                    PiecewiseSegment(raw_min=1, raw_max=2, coefficients=(0.0, 50.0)),
                ),
            )

    def test_boundary_mismatch_rejected(self) -> None:
        with pytest.raises(CalibrationError):
            Piecewise(
                input_unit="V",
                output_unit="K",
                segments=(
                    PiecewiseSegment(raw_min=0, raw_max=1, coefficients=(0.0, 100.0)),
                    PiecewiseSegment(raw_min=1.5, raw_max=2, coefficients=(0.0, 100.0)),
                ),
            )


class TestCustomCallable:
    HASH = "sha256:" + "a" * 64

    def _make_kwargs(self) -> dict:
        return dict(
            input_unit="V",
            output_unit="K",
            entry_point="lab_pkg:thermo_v3",
            package="lab-pkg",
            version="1.0.0",
            distribution_hash=self.HASH,
            callable_id="thermo.k_type_v3",
            test_vectors=((1.0, 273.15),),
        )

    def test_valid_construction(self) -> None:
        cc = CustomCallable(**self._make_kwargs())
        assert cc.callable_id == "thermo.k_type_v3"

    def test_unversioned_entry_point_rejected(self) -> None:
        kwargs = self._make_kwargs()
        kwargs["entry_point"] = "no_colon_here"
        with pytest.raises(CalibrationError):
            CustomCallable(**kwargs)

    def test_bad_hash_algo_rejected(self) -> None:
        kwargs = self._make_kwargs()
        kwargs["distribution_hash"] = "md5:abc"
        with pytest.raises(CalibrationError):
            CustomCallable(**kwargs)

    def test_evaluate_requires_runtime(self) -> None:
        cc = CustomCallable(**self._make_kwargs())
        with pytest.raises(CalibrationError):
            cc.evaluate(1.0)

    def test_no_test_vectors_rejected(self) -> None:
        kwargs = self._make_kwargs()
        kwargs["test_vectors"] = ()
        with pytest.raises(Exception):
            CustomCallable(**kwargs)
