""":class:`SectionWidget` — common protocol for Setup-editor sections.

Every section pane (Overview, Files, Experiment, Procedure,
Storage, Safety) is a ``QWidget`` that:

* exposes ``valuesChanged`` for the Setup tab's debounce machinery to
  subscribe to;
* exposes ``set_draft(draft)`` so the section reads the current payload
  off :class:`SetupDraft.document`;
* exposes ``refresh()`` so the Setup tab can re-render after a save or
  an external change.

Sections that don't accept edits (Overview) wire ``valuesChanged`` but
never emit it. Sections that do (Experiment, Files) emit on every form
change; the Setup tab applies the slice via
``draft.document.*_payload`` and re-validates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

if TYPE_CHECKING:
    from capa.ui.tabs.setup_state import SetupDraft


class SectionWidget(QWidget):
    """Base for an outline section's editor pane.

    Subclasses override :meth:`set_draft` to read payload slices and
    :meth:`refresh` to repaint after an external change. They emit
    :attr:`valuesChanged` whenever the operator edits a field; the
    Setup tab's slot reads :meth:`payload` and applies it back.
    """

    valuesChanged = Signal()  # noqa: N815 — Qt signal naming convention
    """Emitted on every operator-driven change in this section."""

    def set_draft(self, draft: SetupDraft) -> None:
        """Bind the section to ``draft``.

        Implementations typically stash a reference and call
        :meth:`refresh`. Called on initial bind and on any swap to a
        new draft (Open / New).
        """
        raise NotImplementedError

    def refresh(self) -> None:
        """Re-paint from the bound draft's current payload.

        Called after a save, an external load, or whenever the Setup
        tab knows the underlying document changed without going through
        this section's own widgets.
        """
        raise NotImplementedError

    def payload(self) -> dict[str, object] | None:
        """Return the current section's payload slice (or ``None``).

        ``None`` means "this section doesn't own editable payload" —
        Overview and (mostly) Files fall into that bucket; they
        manipulate :class:`ConfigDocument` attributes directly rather
        than producing a slice for the Setup tab to apply.
        """
        return None


__all__ = ["SectionWidget"]
