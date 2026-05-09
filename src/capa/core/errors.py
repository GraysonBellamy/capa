"""Error hierarchy.

Every capa-side error inherits from :class:`CapaError`. Adapter-layer errors
re-raise device-library exceptions into :class:`AdapterError` with channel/device
context attached, so UI toasts and ``events.sqlite`` rows have a single typed
surface to read from.
"""

from __future__ import annotations


class CapaError(Exception):
    """Base for every capa-raised exception."""


class ConfigError(CapaError):
    """Raised when an :class:`~capa.experiment.config.ExperimentConfig` or any
    of its nested models fails validation beyond what Pydantic catches.

    Pure-Pydantic ``ValidationError``\\ s are surfaced as themselves; this
    type covers cross-model constraints (dimensional mismatches, missing
    channel references, etc.).
    """


class CalibrationError(CapaError):
    """Raised when a :class:`~capa.channels.calibration.Calibration` cannot be
    constructed or evaluated — e.g. dimensional mismatch with the bound
    :class:`~capa.channels.spec.ChannelSpec`, ill-formed coefficients, or an
    unversioned :class:`~capa.channels.calibration.CustomCallable`.
    """


class AdapterError(CapaError):
    """Adapter-layer error.

    Concrete adapters subclass this and add device/channel context. The base
    class carries the underlying library exception in ``__cause__`` whenever
    it wraps one, so a tracebacks reads "AdapterError -> NIDaqError -> OSError"
    without losing fidelity.
    """

    def __init__(self, message: str, *, device: str | None = None) -> None:
        super().__init__(message)
        self.device = device


class PluginTrustError(CapaError):
    """Raised when a procedure plugin fails the ``plugins.lock`` trust check
    (missing lock entry, version mismatch, distribution-hash drift).
    """


class BackpressureAbortError(CapaError):
    """Raised when a queue under :class:`~capa.core.backpressure.BackpressurePolicy.ABORT_RUN`
    has stayed full past its configured timeout.
    """


__all__ = [
    "AdapterError",
    "BackpressureAbortError",
    "CalibrationError",
    "CapaError",
    "ConfigError",
    "PluginTrustError",
]
