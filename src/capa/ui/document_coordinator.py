""":class:`DocumentCoordinator` — Setup ↔ Method sync.

Owned by :class:`MainWindow`. Resolves the slow-burning drift problem
in today's flow where the experiment YAML's ``method:`` ref is loaded
once into the Method tab but later edits in one place never propagate
to the other.

The coordinator ships the two-way sync:

* :meth:`on_setup_method_ref_changed` — fired by Setup's Files section
  when the operator changes mode (external / inline / none) or picks a
  new path. The coordinator loads the file when needed and pushes it
  into the Method tab.
* :meth:`on_method_tab_saved` — fired when MethodTab writes a file.
  The coordinator updates Setup's draft method_payload so a subsequent
  Setup-side Save composes the right inline blob (and dirty flags
  agree with what the operator just did).

Both directions guard against re-entry: when an inbound event would
push a state that already matches what we hold, the propagation
short-circuits. That prevents the obvious loop where Setup.methodRef →
MethodTab.load → MethodTab.methodChanged → Setup.refresh →
Setup.methodRef.

:meth:`build_applied_config` gives Apply & Connect a single
source-of-truth composer for the current draft + the current Method
tab state.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from PySide6.QtCore import QObject

from capa.core.errors import CapaError
from capa.experiment.method import Method

if TYPE_CHECKING:
    from capa.experiment.config import ExperimentConfig
    from capa.ui.tabs.method import MethodTab
    from capa.ui.tabs.setup import SetupTab

_logger = structlog.get_logger("capa.ui.document_coordinator")


class DocumentCoordinator(QObject):
    """Glue between the Setup tab's draft and the Method tab's loaded method.

    Construct once per :class:`MainWindow`. The coordinator wires its
    own signal connections in :meth:`__init__`; callers just hand it
    the two tab references.
    """

    def __init__(
        self,
        *,
        setup_tab: SetupTab,
        method_tab: MethodTab,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._setup_tab = setup_tab
        self._method_tab = method_tab
        # Re-entry guard. Set while we're applying an inbound change so
        # the resulting outbound signal from the receiving tab is
        # treated as a no-op echo, not a new edit.
        self._applying: bool = False

        # Wire signals.
        self._setup_tab.methodRefChanged.connect(self.on_setup_method_ref_changed)
        self._setup_tab.draftLoaded.connect(self._on_setup_draft_loaded)
        self._method_tab.methodChanged.connect(self._on_method_tab_changed)
        self._method_tab.methodSaved.connect(self.on_method_tab_saved)

        # Initial sync (in case the tabs were already populated before
        # the coordinator was wired in).
        self._on_setup_draft_loaded()

    # ----------------------------------------------------------------- public

    def on_setup_method_ref_changed(self, path: object) -> None:
        """The Setup Files section changed the method ref.

        ``path`` is whatever Setup emits — typically a ``Path`` for an
        external method or ``None`` for inline / none modes. The
        coordinator loads the path when present and feeds the resulting
        :class:`Method` into MethodTab. Inline / none modes either keep
        whatever's in the Method tab (inline — the buffer is the source
        of truth) or clear it (none).
        """
        if self._applying:
            return
        doc = self._setup_tab.draft.document
        mode = doc.method_mode
        if mode == "external":
            if isinstance(path, Path) and path.is_file():
                self._load_method_from_disk(path)
            elif doc.method_path is not None and doc.method_path.is_file():
                self._load_method_from_disk(doc.method_path)
            # else: path not set or doesn't exist yet — leave the
            # Method tab alone so the operator's typed path can still
            # be saved.
        elif mode == "inline":
            payload = doc.method_payload
            if isinstance(payload, dict) and payload:
                self._load_method_from_payload(payload, path=None)
        elif mode == "none":
            self._applying = True
            try:
                self._method_tab.clear()
            finally:
                self._applying = False

    def build_applied_config(self) -> ExperimentConfig:
        """Compose the Setup draft + current Method-tab buffer into one config.

        The Setup draft is the authoritative source
        for hardware, profile, and everything else; the Method tab is
        authoritative for the method *iff* it holds a buffer the
        ``_on_method_tab_changed`` sync hasn't already pushed back into
        the document. This guarantees Apply & Connect honours the operator's
        most recent intent even if they haven't saved the method yet.

        Raises :class:`~capa.core.errors.CapaError` (wrapping any
        Pydantic validation error) when the composed payload fails to
        validate — callers surface it as the Apply & Connect failure
        message.
        """
        from capa.experiment.config import ExperimentConfig  # noqa: PLC0415

        document = self._setup_tab.draft.document
        composed = document.composed_payload()

        # If the Method tab holds a buffer, prefer it. The
        # ``_on_method_tab_changed`` slot already mirrors saved-to-disk
        # methods back into ``document.method_payload`` so the two
        # agree; the case this branch matters for is the *unsaved*
        # buffer where the operator has edited steps but not pressed
        # Save in MethodTab.
        method = self._method_tab.method()
        if method is not None:
            composed["method"] = method.model_dump(mode="python")
        elif document.method_mode == "none":
            composed.pop("method", None)

        try:
            return ExperimentConfig.model_validate(composed)
        except Exception as exc:
            src = document.experiment_path or document.hardware_path
            label = str(src) if src else "<unsaved>"
            raise CapaError(f"{label}: {exc}") from exc

    def on_method_tab_saved(self, path: Path) -> None:
        """The Method tab wrote a file. Mirror the path + payload into
        the Setup draft so a Setup-side save stays consistent.

        Wired to :attr:`MethodTab.methodSaved`.
        """
        if self._applying:
            return
        doc = self._setup_tab.draft.document
        doc.method_path = path.resolve()
        doc.method_format = "toml"
        doc.method_mode = "external"
        try:
            with open(path, "rb") as fp:
                doc.method_payload = dict(tomllib.load(fp))
        except OSError as exc:
            _logger.warning("ui.coord.method_saved_read_failed", error=str(exc))
            return

    # ----------------------------------------------------------------- internal

    def _on_setup_draft_loaded(self) -> None:
        """A fresh draft arrived in Setup — push its method into MethodTab."""
        doc = self._setup_tab.draft.document
        if doc.method_mode == "external":
            if doc.method_path is not None and doc.method_path.is_file():
                self._load_method_from_disk(doc.method_path)
            elif isinstance(doc.method_payload, dict) and doc.method_payload:
                # External mode but the path isn't valid yet — feed
                # whatever payload we have so the Method tab doesn't
                # show stale state from a prior draft.
                self._load_method_from_payload(doc.method_payload, path=None)
            else:
                self._apply_clear()
        elif doc.method_mode == "inline":
            payload = doc.method_payload
            if isinstance(payload, dict) and payload:
                self._load_method_from_payload(payload, path=None)
            else:
                self._apply_clear()
        else:  # "none"
            self._apply_clear()

    def _on_method_tab_changed(self) -> None:
        """MethodTab's loaded method changed.

        Refresh Setup's view: the method_payload is updated to match
        the current Method, and (when the Method has a known source
        path) the Files section's path field is updated too.
        """
        if self._applying:
            return
        method = self._method_tab.method()
        doc = self._setup_tab.draft.document
        prior_payload = doc.method_payload
        prior_path = doc.method_path
        prior_mode = doc.method_mode
        prior_format = doc.method_format
        if method is None:
            # Tab cleared.
            if doc.method_mode != "none":
                doc.method_mode = "none"
                doc.method_path = None
                doc.method_format = None
                doc.method_payload = None
                self._refresh_setup_section("files")
                self._mark_setup_dirty("files")
            return
        new_payload = method.model_dump(mode="python")
        method_path = getattr(self._method_tab, "_method_path", None)
        if method_path is not None:
            doc.method_path = method_path.resolve()
            doc.method_format = "toml"
            doc.method_mode = "external"
        else:
            # Unsaved Method buffer — keep external mode + path if the
            # Setup draft was already pointed at a file, otherwise drop
            # to inline so the Save composer wires the method back.
            if doc.method_mode == "none":
                doc.method_mode = "inline"
        doc.method_payload = new_payload
        # Compare in canonical form: prior_payload may be a raw TOML dict
        # (from ConfigDocument.load) that omits defaults, while new_payload
        # is a Pydantic dump that includes them. Without normalisation, a
        # spurious re-emit of methodChanged on an unchanged method would
        # falsely mark Files dirty.
        prior_canonical: dict[str, Any] | None
        if isinstance(prior_payload, dict):
            try:
                prior_canonical = Method.model_validate(prior_payload).model_dump(mode="python")
            except (ValueError, CapaError):
                prior_canonical = prior_payload
        else:
            prior_canonical = prior_payload
        changed = (
            prior_canonical != new_payload
            or prior_path != doc.method_path
            or prior_mode != doc.method_mode
            or prior_format != doc.method_format
        )
        if changed:
            self._refresh_setup_section("files")
            self._mark_setup_dirty("files")

    def _load_method_from_disk(self, path: Path) -> None:
        try:
            with open(path, "rb") as fp:
                payload = dict(tomllib.load(fp))
            method = Method.model_validate(payload)
        except (OSError, ValueError, tomllib.TOMLDecodeError, CapaError) as exc:
            _logger.warning("ui.coord.method_load_failed", path=str(path), error=str(exc))
            return
        self._apply_load(method, path=path)

    def _load_method_from_payload(self, payload: dict[str, Any], *, path: Path | None) -> None:
        try:
            method = Method.model_validate(payload)
        except (ValueError, CapaError) as exc:
            _logger.warning("ui.coord.method_payload_invalid", error=str(exc))
            return
        self._apply_load(method, path=path)

    def _apply_load(self, method: Method, *, path: Path | None) -> None:
        self._applying = True
        try:
            self._method_tab.load_method(method, path=path)
        finally:
            self._applying = False

    def _apply_clear(self) -> None:
        self._applying = True
        try:
            self._method_tab.clear()
        finally:
            self._applying = False

    def _refresh_setup_section(self, section_id: str) -> None:
        section = self._setup_tab._sections.get(section_id)
        if section is not None:
            section.refresh()

    def _mark_setup_dirty(self, section_id: str) -> None:
        self._setup_tab.draft.mark_dirty(section_id)
        self._setup_tab._refresh_outline_markers()
        self._setup_tab._refresh_source_label()


__all__ = ["DocumentCoordinator"]
