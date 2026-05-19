"""Operational tune-result artifacts.

This package holds artifact models that record the outcome of a
calibration / tune *procedure* — distinct from the channel-level
:class:`~capa.channels.calibration.Calibration` family, which models
raw-acquisition to engineering-unit transforms attached to a
:class:`~capa.channels.spec.ChannelSpec`.

The first member is :class:`~capa.calibration.tune_artifact.HeatFluxTuneArtifact`,
written by ``capa.builtin.heat_flux_tune``. A heater-setpoint to
delivered-flux mapping is not a channel calibration (there is no
``ChannelSpec`` whose raw unit is "commanded °C" and whose derived unit
is "kW/m²"), so it lives in its own model with its own storage path.
"""

from capa.calibration.tune_artifact import (
    HeatFluxTuneArtifact,
    HeatFluxTunePoint,
    TuneArtifactError,
    load_artifact,
    load_latest,
    save_artifact,
    to_toml,
)

__all__ = [
    "HeatFluxTuneArtifact",
    "HeatFluxTunePoint",
    "TuneArtifactError",
    "load_artifact",
    "load_latest",
    "save_artifact",
    "to_toml",
]
