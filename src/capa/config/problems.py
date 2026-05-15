""":class:`ConfigProblem` — the navigable validation-error record.

A ``ConfigProblem`` is the single shape the Setup editor's Problems panel
consumes. Every layer of the validation pipeline emits ``ConfigProblem``s
with consistent ``(section, path)`` addressing so the panel can navigate
from a row click to the offending field.

Path tuples mirror Pydantic's ``ValidationError.errors()[i]["loc"]``
shape — Layer 1 maps them 1:1 — so any field a Pydantic validator
rejects is automatically navigable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["error", "warning", "info"]

Section = Literal[
    "experiment",
    "procedure",
    "capa_profile",
    "devices",
    "channels",
    "cameras",
    "storage",
    "safety",
    "files",
]


class ConfigProblem(BaseModel):
    """One validation finding addressable by ``(section, path)``.

    ``severity`` controls colour and whether Apply-to-Rig stays disabled
    (any ``"error"`` blocks). ``code`` is a stable identifier
    (``"channel.missing_source_device"``) so the UI can offer code-keyed
    quick fixes; ``message`` is the operator-facing prose.

    ``path`` is the tuple address from the section's root model — e.g.
    ``("devices", 2, "params", "port")`` points at the third device's
    ``params.port`` field. ``source_file`` indicates which file would
    carry the fix (hardware TOML vs experiment YAML) so the Save dialog
    can show the right path next to the problem.
    """

    model_config = ConfigDict(frozen=True)

    severity: Severity
    code: str
    message: str
    section: Section
    path: tuple[str | int, ...] = Field(default_factory=tuple)
    source_file: Path | None = None
    fix_label: str | None = None
    """Short imperative label for a one-click fix (``"Choose a device"``).
    Optional — only emitted by checks that have a canonical fix."""


__all__ = ["ConfigProblem", "Section", "Severity"]
