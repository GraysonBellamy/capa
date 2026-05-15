"""Config IO and validation surface.

``ConfigDocument`` is the source-tracking layer — it knows what file the
draft came from, what format it was, and whether hardware/method were
inline or external. Distinct from
:class:`~capa.experiment.config.ExperimentConfig`, which is the
validated, frozen runtime object.

The validation pipeline (``validate``) and the ``ConfigProblem`` shape
layer on top.
"""

from __future__ import annotations

from capa.config.document import ConfigDocument, SaveError, SourceLayout
from capa.config.problems import ConfigProblem, Section, Severity
from capa.config.validate import validate, validate_live_async

__all__ = [
    "ConfigDocument",
    "ConfigProblem",
    "SaveError",
    "Section",
    "Severity",
    "SourceLayout",
    "validate",
    "validate_live_async",
]
