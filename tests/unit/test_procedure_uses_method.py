""":func:`procedure_uses_method` — defaults + builtins.

Tests the small helper that the UI consults to decide whether the
Method tab is meaningful for the currently-selected procedure. The
contract is two lines: builtin procedures opt in/out explicitly, and
classes that don't declare the attribute default to ``True`` (matching
the pre-existing always-visible behaviour so older plugins keep working).
"""

from __future__ import annotations

from capa.experiment.procedures.base import procedure_uses_method
from capa.experiment.procedures.builtin.free_run import FreeRun
from capa.experiment.procedures.builtin.heat_flux_tune.controller import HeatFluxTune
from capa.experiment.procedures.builtin.recipe_runner import RecipeRunner


def test_recipe_runner_uses_method() -> None:
    assert procedure_uses_method(RecipeRunner) is True


def test_free_run_does_not_use_method() -> None:
    assert procedure_uses_method(FreeRun) is False


def test_heat_flux_tune_does_not_use_method() -> None:
    assert procedure_uses_method(HeatFluxTune) is False


def test_default_for_class_without_attribute_is_true() -> None:
    class _LegacyProcedure:
        """Plugin written before ``uses_method`` was added to the Protocol."""

    assert procedure_uses_method(_LegacyProcedure) is True


def test_explicit_false_is_honored() -> None:
    class _SelfDriving:
        uses_method = False

    assert procedure_uses_method(_SelfDriving) is False
