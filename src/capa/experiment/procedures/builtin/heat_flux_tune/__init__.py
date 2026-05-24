"""HeatFluxTune procedure package.

Symbols live in their topic modules; import them directly:

* :mod:`.config` — :class:`HeatFluxTuneConfig`, :class:`HeatFluxTuneError`,
  :data:`PROCEDURE_ID`, :data:`PROCEDURE_NAME`, :data:`PROCEDURE_VERSION`,
  :data:`InitialGuessSource`.
* :mod:`.signals` — :class:`RollingWindow`, :class:`SteadyStatePredicate`,
  :class:`RunawayDetector`, :func:`hampel_mask`, :func:`linear_slope_per_min`,
  :func:`secant_step`.
* :mod:`.setpoint` — :func:`choose_initial_setpoint`,
  :func:`default_tolerance_kw_m2`, :func:`predicate_strictness`,
  :func:`sigma_t4_setpoint_c`.
* :mod:`.controller` — :class:`HeatFluxTune` (the procedure entry point).

:class:`HeatFluxTune` is re-exported at the package root so the
``pyproject.toml`` entry point ``…heat_flux_tune:HeatFluxTune`` resolves.
This import is deferred (only runs when something actually imports the
package), which is what keeps it out of the ``plugins_runtime`` init
cycle.
"""

from capa.experiment.procedures.builtin.heat_flux_tune.controller import HeatFluxTune

__all__ = ["HeatFluxTune"]
