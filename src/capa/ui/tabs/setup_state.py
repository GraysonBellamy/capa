""":class:`SetupDraft` — the Setup tab's working state.

Owns the :class:`ConfigDocument`, dirty-section tracking, and the
problems list. Distinct from the visible Setup tab in
:mod:`capa.ui.tabs.setup` — keeping this module UI-free means the
draft layer can be tested headlessly.

The draft is the editable layer: payloads are mutable dicts, validation
runs on demand (debounced from the UI, eager in tests).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from capa.config.document import ConfigDocument
from capa.config.problems import ConfigProblem
from capa.config.validate import validate

if TYPE_CHECKING:
    from capa.experiment.config import ExperimentConfig


@dataclass
class SetupDraft:
    """The Setup tab's working state.

    Attributes:
        document: the source-tracking + payload-holding layer.
        dirty_sections: which sections the operator has edited since
            the last save (``"experiment"``, ``"hardware.devices"``,
            ``"hardware.channels"`` etc.).
        unapplied: ``True`` when the draft differs from the
            :class:`RunController`'s currently-applied config.
        last_valid_config: the most recent successful
            :meth:`ConfigDocument.build_config` result; ``None`` while
            the draft has schema errors.
        problems: the latest validation findings.
    """

    document: ConfigDocument
    dirty_sections: set[str] = field(default_factory=set)
    unapplied: bool = False
    last_valid_config: ExperimentConfig | None = None
    problems: list[ConfigProblem] = field(default_factory=list)

    # -- editing operations -------------------------------------------------

    def mark_dirty(self, section: str) -> None:
        """Note that ``section`` has unsaved edits.

        Section keys use dotted names so the outline-status code can
        resolve nested entries (``"hardware.devices"`` vs
        ``"hardware.channels"``) without parsing.
        """
        self.dirty_sections.add(section)
        self.unapplied = True

    def clear_dirty(self) -> None:
        """Reset dirty tracking after a successful save."""
        self.dirty_sections.clear()

    @property
    def is_dirty(self) -> bool:
        """``True`` if the draft has unsaved changes relative to the loaded config."""
        return bool(self.dirty_sections)

    @property
    def dirty_section_count(self) -> int:
        """Number of sections with unsaved edits.

        The connection strip surfaces this as "draft has N unsaved
        edit(s)". Counts sections rather than fields because the draft
        layer only tracks edits at section granularity — a field-level
        count would require plumbing every section widget's per-field
        diff back into the draft, which buys very little for the
        operator.
        """
        return len(self.dirty_sections)

    @property
    def has_errors(self) -> bool:
        """``True`` if validation produced at least one error-severity problem."""
        return any(p.severity == "error" for p in self.problems)

    # -- validation ---------------------------------------------------------

    def validate(self, *, with_live_checks: bool = False) -> list[ConfigProblem]:
        """Re-run the validation pipeline; update ``problems`` in place.

        Returns the same list for callers that want to chain on the
        latest result without re-reading the attribute.
        """
        self.problems = validate(self.document, with_live_checks=with_live_checks)
        if not self.has_errors:
            try:
                self.last_valid_config = self.document.build_config()
            except Exception:
                self.last_valid_config = None
        return self.problems

    # -- factories ----------------------------------------------------------

    @classmethod
    def from_path(cls, path: Path | str) -> SetupDraft:
        """Load a draft from an experiment file."""
        document = ConfigDocument.load(path)
        draft = cls(document=document)
        draft.validate()
        return draft

    @classmethod
    def empty(cls) -> SetupDraft:
        """A blank draft for the New Setup wizard's "Blank" starting point."""
        return cls(document=ConfigDocument())


__all__ = ["SetupDraft"]
