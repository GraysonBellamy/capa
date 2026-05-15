"""Outline widget — section navigator with status markers.

The outline is a thin :class:`QTreeWidget` that lists the Setup editor's
sections and decorates each with the right glyph for its state:

| Marker | Meaning |
|---|---|
| (none) | clean, validated |
| ``●`` | dirty (edits not saved) |
| ``⚠`` | warnings only |
| ``✗`` | at least one error |

Markers can stack (``●⚠``). The Hardware parent holds three children
(Devices / Channels / Cameras) because operators jump between them
constantly, with a CAPA Profile sibling carrying the curated profile
editor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem, QWidget

if TYPE_CHECKING:
    from capa.config.problems import ConfigProblem


@dataclass(frozen=True, slots=True)
class OutlineEntry:
    """One row in the outline tree.

    ``section_id`` is the stable string the Setup tab keys section
    widgets and dirty tracking off. ``label`` is what the operator sees.
    ``children`` is the sub-tree; empty for leaf rows. ``rolls_up``
    lists the child ids whose problems/dirty markers aggregate up onto
    this entry — for Hardware that's its three children, so the parent
    shows a single ``✗`` when any sub-table has an error.
    """

    section_id: str
    label: str
    children: tuple[OutlineEntry, ...] = ()
    rolls_up: tuple[str, ...] = ()


# Section tree. ``section_id`` strings match :data:`capa.config.problems.Section`
# (so problem-navigation lands on the right entry); ``hardware`` is a
# UI-only grouping that has no matching ``Section`` literal.
SECTIONS: tuple[OutlineEntry, ...] = (
    OutlineEntry("overview", "Overview"),
    OutlineEntry("files", "Files"),
    OutlineEntry("experiment", "Experiment"),
    OutlineEntry("procedure", "Procedure"),
    OutlineEntry("capa_profile", "CAPA Profile"),
    OutlineEntry(
        "hardware",
        "Hardware",
        children=(
            OutlineEntry("devices", "Devices"),
            OutlineEntry("channels", "Channels"),
            OutlineEntry("cameras", "Cameras"),
        ),
        rolls_up=("devices", "channels", "cameras"),
    ),
    OutlineEntry("storage", "Storage"),
    OutlineEntry("safety", "Safety"),
    OutlineEntry("calibration", "Calibration"),
)


LEAF_SECTIONS: tuple[tuple[str, str], ...] = tuple(
    (entry.section_id, entry.label) for entry in SECTIONS if not entry.children
)
"""All sections without children, as ``(section_id, label)`` pairs."""


def _flatten(tree: tuple[OutlineEntry, ...]) -> tuple[OutlineEntry, ...]:
    """Walk the section tree depth-first, returning every entry."""
    out: list[OutlineEntry] = []
    for entry in tree:
        out.append(entry)
        out.extend(_flatten(entry.children))
    return tuple(out)


ALL_SECTIONS: tuple[OutlineEntry, ...] = _flatten(SECTIONS)
"""Flattened section list — every leaf and parent, depth-first."""


class SetupOutline(QTreeWidget):
    """Section navigator.

    Emits :attr:`sectionSelected` whenever the user picks a section; the
    Setup tab uses that to swap the central :class:`QStackedWidget`. The
    :meth:`set_markers` call refreshes glyphs from a dict produced by
    the Setup tab (which knows the draft's dirty set + problems list).
    """

    sectionSelected = Signal(str)  # noqa: N815 — Qt signal naming convention
    """Argument is the section id (``"overview"``, ``"files"``, …)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(True)
        # ``hardware`` is the only parent today; decorate so the disclosure
        # triangle is visible. The flat sections sit at the top level
        # alongside it without indentation.
        self.setRootIsDecorated(True)
        self.setColumnCount(1)
        self.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.setIndentation(12)
        self.setFixedWidth(190)

        self._items: dict[str, QTreeWidgetItem] = {}
        for entry in SECTIONS:
            self._build_entry(entry, parent=None)
        # Expand every parent so children are visible by default — the
        # operator should see the full structure on first paint.
        for entry in SECTIONS:
            if entry.children:
                item = self._items.get(entry.section_id)
                if item is not None:
                    item.setExpanded(True)

        self.itemSelectionChanged.connect(self._on_selection_changed)

        # Default selection — Overview is the natural landing.
        if "overview" in self._items:
            self.setCurrentItem(self._items["overview"])

    # -- API ----------------------------------------------------------------

    def select(self, section_id: str) -> None:
        """Programmatically select ``section_id`` if present.

        Falls back silently when the id is unknown — the apply flow may
        emit problem records that point at sections the outline doesn't
        surface (e.g. ``files``)."""
        item = self._items.get(section_id)
        if item is not None:
            self.setCurrentItem(item)

    def set_markers(
        self,
        *,
        dirty_sections: set[str],
        problems: list[ConfigProblem],
    ) -> None:
        """Refresh status glyphs from the draft's bookkeeping."""
        # Aggregate severity per section in a single pass.
        sev_for: dict[str, str] = {}
        for problem in problems:
            section = problem.section
            current = sev_for.get(section)
            if problem.severity == "error":
                sev_for[section] = "error"
            elif problem.severity == "warning" and current != "error":
                sev_for[section] = "warning"

        # Roll children's severity / dirty up into their parents.
        for entry in ALL_SECTIONS:
            if not entry.rolls_up:
                continue
            agg_sev: str | None = None
            for child_id in entry.rolls_up:
                child_sev = sev_for.get(child_id)
                if child_sev == "error":
                    agg_sev = "error"
                    break
                if child_sev == "warning" and agg_sev != "error":
                    agg_sev = "warning"
            # Parent never overrides its own (no problems land directly
            # on ``hardware`` today, but keep the guard).
            if agg_sev is not None and sev_for.get(entry.section_id) != "error":
                sev_for[entry.section_id] = agg_sev

        # Compose dirty roll-up the same way so a child's ``●`` propagates
        # to the Hardware parent even though the parent has no section
        # widget of its own.
        rolled_dirty = set(dirty_sections)
        for entry in ALL_SECTIONS:
            for child_id in entry.rolls_up:
                if child_id in dirty_sections or _dirty_matches(child_id, dirty_sections):
                    rolled_dirty.add(entry.section_id)
                    break

        for entry in ALL_SECTIONS:
            item = self._items.get(entry.section_id)
            if item is None:
                continue
            markers: list[str] = []
            if entry.section_id in rolled_dirty or _dirty_matches(entry.section_id, dirty_sections):
                markers.append("●")
            sev = sev_for.get(entry.section_id)
            if sev == "error":
                markers.append("✗")
            elif sev == "warning":
                markers.append("⚠")
            prefix = (" ".join(markers) + " ") if markers else ""
            item.setText(0, f"{prefix}{entry.label}")

    # -- internals ----------------------------------------------------------

    def _build_entry(self, entry: OutlineEntry, *, parent: QTreeWidgetItem | None) -> None:
        item = QTreeWidgetItem([entry.label])
        item.setData(0, Qt.ItemDataRole.UserRole, entry.section_id)
        if parent is None:
            self.addTopLevelItem(item)
        else:
            parent.addChild(item)
        self._items[entry.section_id] = item
        for child in entry.children:
            self._build_entry(child, parent=item)

    def _on_selection_changed(self) -> None:
        items = self.selectedItems()
        if not items:
            return
        section_id = items[0].data(0, Qt.ItemDataRole.UserRole)
        if isinstance(section_id, str):
            self.sectionSelected.emit(section_id)


def _dirty_matches(section_id: str, dirty: set[str]) -> bool:
    """Match dotted dirty keys (``"hardware.devices"``) to outline ids.

    Today's hardware sections use flat ids (``"devices"``, ``"channels"``,
    ``"cameras"``) but external callers could still emit dotted keys, so
    the helper stays.
    """
    prefix = f"{section_id}."
    return any(key.startswith(prefix) for key in dirty)


__all__ = [
    "ALL_SECTIONS",
    "LEAF_SECTIONS",
    "SECTIONS",
    "OutlineEntry",
    "SetupOutline",
]
