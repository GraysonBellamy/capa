"""Per-section widgets for the Setup editor.

Each section is a :class:`SectionWidget` that owns the editor surface
for one outline entry (Overview, Files, Experiment, Procedure, Storage,
Safety, Hardware, CAPA Profile, Calibration). The Setup tab composes
them in a ``QStackedWidget`` driven by the outline.
"""

from __future__ import annotations

from capa.ui.tabs.setup_sections._base import SectionWidget

__all__ = ["SectionWidget"]
