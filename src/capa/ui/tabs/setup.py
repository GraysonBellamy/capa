"""``SetupTab`` — editor shell.

The tab owns a :class:`SetupDraft`, surfaces an outline / main-editor /
Problems three-region layout, and exposes Save / Save As / Validate
against the underlying :class:`ConfigDocument`.
"""

from __future__ import annotations

import contextlib
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QLabel,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from capa.config import ConfigDocument, SaveError
from capa.config.problems import ConfigProblem
from capa.core.errors import CapaError
from capa.experiment.config import ExperimentConfig
from capa.ui.config_progress import ConfigLoadPhase, ConfigLoadProgress
from capa.ui.tabs.setup_outline import ALL_SECTIONS, SetupOutline
from capa.ui.tabs.setup_problems import SetupProblems
from capa.ui.tabs.setup_sections._base import SectionWidget
from capa.ui.tabs.setup_sections.experiment import ExperimentSection
from capa.ui.tabs.setup_sections.files import FilesSection
from capa.ui.tabs.setup_sections.overview import OverviewSection
from capa.ui.tabs.setup_sections.procedure import ProcedureSection
from capa.ui.tabs.setup_sections.safety import SafetySection
from capa.ui.tabs.setup_sections.storage import StorageSection
from capa.ui.tabs.setup_state import SetupDraft

if TYPE_CHECKING:
    from capa.ui.document_coordinator import DocumentCoordinator
    from capa.ui.state import RunController, RunUiState

_logger = structlog.get_logger("capa.ui.setup")


class _BannerState(StrEnum):
    """Mutually-exclusive Setup-tab banner states."""

    HIDDEN = "hidden"
    APPLIED_OK = "applied_ok"
    UNAPPLIED = "unapplied"
    APPLIED_FAILED = "applied_failed"
    CHECKING = "checking"
    APPLYING = "applying"
    FROZEN = "frozen"


# Per-state (text-template, css). ``{detail}`` is substituted from the
# Setup tab's ``_banner_detail`` slot before being displayed.
_BANNER_STYLES: dict[_BannerState, tuple[str, str]] = {
    _BannerState.FROZEN: (
        "Run is active — Apply to Rig is disabled until the run completes.",
        "background: #fff3cd; color: #856404; padding: 4px 8px; border: 1px solid #ffeeba;",
    ),
    _BannerState.APPLYING: (
        "Applying to rig — opening devices…",
        "background: #d1ecf1; color: #0c5460; padding: 4px 8px; border: 1px solid #bee5eb;",
    ),
    _BannerState.CHECKING: (
        "Checking hardware — read-only handshake in progress…",
        "background: #e2e3f3; color: #2c2e6b; padding: 4px 8px; border: 1px solid #c2c3e3;",
    ),
    _BannerState.APPLIED_FAILED: (
        "Apply failed: {detail}. No rig is currently applied — fix and Apply again.",
        "background: #f8d7da; color: #721c24; padding: 4px 8px; border: 1px solid #f5c6cb;",
    ),
    _BannerState.UNAPPLIED: (
        "Draft has unapplied changes — Apply to Rig to take effect.",
        "background: #fefbf3; color: #7c6f3a; padding: 4px 8px; border: 1px dashed #d9c878;",
    ),
    _BannerState.APPLIED_OK: (
        "Applied to rig: {detail}.",
        "background: #d4edda; color: #155724; padding: 4px 8px; border: 1px solid #c3e6cb;",
    ),
    _BannerState.HIDDEN: ("", ""),
}


# Top-level dict keys whose payload slice lives in ``hardware_payload``
# rather than ``experiment_payload``. The hardware-side editors emit
# their slice under one of these keys; everything else routes to the
# experiment side. Kept module-level so tests can introspect.
_HARDWARE_PAYLOAD_KEYS: frozenset[str] = frozenset({"devices", "channels", "cameras"})


class SetupTab(QWidget):
    """Editor shell for the experiment / hardware / method setup.

    See module docstring. The tab is constructed once per ``MainWindow``;
    every Open / New / Save As cycle reuses the same widget instance.
    """

    deviceActionRequested = Signal(str)  # noqa: N815 — Qt signal naming convention
    """Forwarded from the Devices section: operator chose "Open manual
    control" for a device row. The :class:`MainWindow` switches to the
    Manual tab and surfaces the named device."""

    saved = Signal()
    """Fires after a successful save. The main window watches this to
    refresh its source label and any recents list."""

    draftLoaded = Signal()  # noqa: N815 — Qt signal naming convention
    """Fires after :meth:`load_path` or :meth:`load_config` swaps the
    underlying :class:`SetupDraft`."""

    methodRefChanged = Signal(object)  # noqa: N815 — Qt signal naming convention
    """``Path | None`` — fires when Files-section method-ref changes
    settle. The :class:`DocumentCoordinator` consumes this to swap the
    Method tab's loaded method."""

    applyRequested = Signal(object, object)  # noqa: N815 — Qt signal naming convention
    """``(ExperimentConfig, Path | None)`` — fires when the operator
    clicks Apply to Rig on a valid draft. :class:`MainWindow` consumes
    this to drive ``RunController.set_active_config`` + the existing
    rebuild-docks side effects."""

    # ----------------------------------------------------------------------

    def __init__(
        self,
        *,
        controller: RunController | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller: RunController | None = controller
        self._document_coordinator: DocumentCoordinator | None = None
        self._draft: SetupDraft = SetupDraft.empty()
        # Cache of the last RunUiState seen via the controller's
        # ``state_changed`` signal. Tracked here (rather than read off
        # ``controller.state`` on demand) so unit-test stubs that emit
        # the signal without mirroring the state attribute drive the
        # banner correctly — and so the banner never lags behind the
        # signal in production either.
        self._controller_state: object | None = None
        # Apply-to-Rig state machine: True while an apply is mid-flight
        # (between ``applyRequested`` emit and the controller's
        # ``config_load_finished`` signal). Gates the Apply button so
        # the operator can't double-click during the open phase.
        self._apply_in_flight: bool = False
        # Last apply outcome, used by the banner state machine to keep
        # transient success / failure messages visible until either a
        # new edit happens or another apply attempt fires.
        self._apply_outcome: tuple[_BannerState, str] | None = None
        # Check-Hardware in-flight flag — gates the Check button and
        # drives the CHECKING banner. The actual coroutine is scheduled
        # against the qasync loop in :meth:`_on_check_hardware`.
        self._check_in_flight: bool = False

        # 200 ms debounce — every operator edit
        # restarts the timer; when it fires we re-run the validation
        # pipeline against the current draft and refresh Problems +
        # outline markers.
        self._validate_timer = QTimer(self)
        self._validate_timer.setSingleShot(True)
        self._validate_timer.setInterval(200)
        self._validate_timer.timeout.connect(self._run_validate)

        # Auto-fade timer for the "applied_ok" banner — green pill
        # stays up for 4 seconds, then drops to whatever lower-priority
        # state is true (typically ``HIDDEN``).
        self._banner_fade_timer = QTimer(self)
        self._banner_fade_timer.setSingleShot(True)
        self._banner_fade_timer.setInterval(4000)
        self._banner_fade_timer.timeout.connect(self._on_apply_ok_fade)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        # Toolbar.
        self._toolbar = QToolBar("Setup", self)
        self._toolbar.setMovable(False)
        self._action_new = self._toolbar.addAction("New", self._on_new)
        self._action_open = self._toolbar.addAction("Open", self._on_open)
        self._action_save = self._toolbar.addAction("Save", self._on_save)
        self._action_save_as = self._toolbar.addAction("Save As", self._on_save_as)
        self._action_validate = self._toolbar.addAction("Validate", self._on_validate)
        self._toolbar.addSeparator()
        # Discover / Check Hardware / Apply to Rig — all gated by
        # frozen-while-armed. Check Hardware drives Layer 5 against the
        # current draft; Apply hands the validated config to the run
        # controller.
        self._action_discover = self._toolbar.addAction("Discover", self._on_discover)
        self._action_discover.setEnabled(False)
        self._action_discover.setToolTip(
            "Scan for connected devices and offer to add them to the draft."
        )
        self._action_check = self._toolbar.addAction("Check Hardware", self._on_check_hardware)
        self._action_check.setEnabled(False)
        self._action_check.setToolTip("Read-only handshake against each device in the draft.")
        self._action_apply = self._toolbar.addAction("Apply to Rig", self._on_apply_to_rig)
        self._action_apply.setEnabled(False)
        self._action_apply.setToolTip("Validate the draft and apply it to the run controller.")

        spacer = QWidget(self._toolbar)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._toolbar.addWidget(spacer)
        self._source_label = QLabel("untitled", self._toolbar)
        self._source_label.setContentsMargins(0, 0, 8, 0)
        self._toolbar.addWidget(self._source_label)

        outer.addWidget(self._toolbar)

        # Multi-state banner — frozen-while-armed, applying,
        # applied_ok, applied_failed, unapplied. Priority handled by
        # ``_compute_banner_state``; only one state visible at a time.
        self._banner_state: _BannerState = _BannerState.HIDDEN
        self._banner_detail: str = ""
        self._banner = QLabel("", self)
        self._banner.setWordWrap(True)
        self._banner.setVisible(False)
        outer.addWidget(self._banner)

        # Outline + main editor splitter.
        body = QSplitter(Qt.Orientation.Horizontal, self)
        self._outline = SetupOutline(body)
        body.addWidget(self._outline)

        self._stack = QStackedWidget(body)
        body.addWidget(self._stack)
        body.setStretchFactor(0, 0)
        body.setStretchFactor(1, 1)
        body.setSizes([190, 800])
        outer.addWidget(body, stretch=1)

        # Problems panel.
        self._problems = SetupProblems(self)
        outer.addWidget(self._problems)

        # Build the section widgets and register them with the stack.
        # Every entry in the outline tree gets a pane — children too,
        # because selecting "Devices" in the outline must swap to the
        # Devices editor rather than the Hardware glance view.
        #
        # Each section is wrapped in a QScrollArea so tall panes (e.g.
        # CAPA Profile) get a vertical scrollbar instead of either
        # spilling off-screen or being squashed when the window grows.
        # ``_sections`` still maps to the underlying SectionWidget so
        # payload/refresh wiring is unchanged; ``_section_panes`` holds
        # the scroll-area wrappers that the QStackedWidget owns.
        self._sections: dict[str, SectionWidget] = {}
        self._section_panes: dict[str, QScrollArea] = {}
        for entry in ALL_SECTIONS:
            widget = self._build_section(entry.section_id)
            self._sections[entry.section_id] = widget
            pane = self._wrap_in_scroll_area(widget)
            self._section_panes[entry.section_id] = pane
            self._stack.addWidget(pane)
            widget.valuesChanged.connect(lambda sid=entry.section_id: self._on_section_edited(sid))

        # Files section gets the extra methodRefChanged signal hookup.
        files_section = self._sections.get("files")
        if isinstance(files_section, FilesSection):
            files_section.methodRefChanged.connect(self._on_method_ref_changed)

        # Hardware glance view's "Edit…" buttons jump to child sections.
        from capa.ui.tabs.setup_sections.hardware import HardwareGlanceSection  # noqa: PLC0415

        hardware_section = self._sections.get("hardware")
        if isinstance(hardware_section, HardwareGlanceSection):
            hardware_section.editSectionRequested.connect(self._outline.select)

        # Wire outline ↔ stack.
        self._outline.sectionSelected.connect(self._on_section_selected)
        self._problems.problemActivated.connect(self._on_problem_activated)

        # Bind the initial empty draft.
        self._refresh_all_sections()
        self._refresh_source_label()

        # Subscribe to controller state for the frozen banner. Tests
        # inject stub controllers that only carry the signals they
        # actually drive — fail soft if a signal is missing.
        if self._controller is not None:
            with contextlib.suppress(AttributeError):
                self._controller.state_changed.connect(self._on_controller_state)
            with contextlib.suppress(AttributeError):
                self._controller.config_load_finished.connect(self._on_config_load_finished)
            with contextlib.suppress(Exception):
                self._on_controller_state(self._controller.state)
        self._refresh_banner()
        self._refresh_apply_enabled()

    # ------------------------------------------------------------------ API

    @property
    def draft(self) -> SetupDraft:
        return self._draft

    def set_document_coordinator(self, coordinator: DocumentCoordinator) -> None:
        """Inject the :class:`DocumentCoordinator` for Apply-to-Rig.

        Apply needs to compose Setup's draft with the Method tab's
        buffer (the operator may have edited the method without saving
        it). The coordinator owns that composition. The setter exists
        because the coordinator references both tabs and is constructed
        *after* :class:`SetupTab` — the constructor order can't accept
        it as an ``__init__`` argument without restructuring.
        """
        self._document_coordinator = coordinator
        self._refresh_apply_enabled()

    def load_path(self, path: Path) -> None:
        """Open an experiment YAML/TOML and replace the current draft.

        Errors surface as a modal. The previously-loaded draft is left
        in place when the load fails so the operator doesn't lose mid-
        edit state to a typo.
        """
        try:
            draft = SetupDraft.from_path(path)
        except CapaError as exc:
            QMessageBox.critical(self, "Open failed", f"{path}\n\n{exc}")
            _logger.warning("ui.setup.load_failed", path=str(path), error=str(exc))
            return
        self._draft = draft
        self._draft.unapplied = False
        self._apply_in_flight = False
        self._apply_outcome = None
        self._refresh_all_sections()
        self._refresh_source_label()
        self._refresh_outline_markers()
        self._problems.set_problems(self._draft.problems)
        self._refresh_banner()
        self._refresh_apply_enabled()
        self.draftLoaded.emit()
        _logger.info("ui.setup.loaded", path=str(path))

    def load_config(self, config: ExperimentConfig, *, path: Path | None = None) -> None:
        """Seed the tab from an already-validated :class:`ExperimentConfig`.

        Called by ``MainWindow._apply_loaded_config``. When a path is
        supplied, prefer :meth:`load_path` semantics (full
        :class:`ConfigDocument.load` round-trip) so the source-layout
        info matches the on-disk shape. Otherwise build a synthetic
        ``ConfigDocument`` from the model dump — used by tests that
        construct configs programmatically.
        """
        if path is not None and path.is_file():
            self.load_path(path)
            return
        # Synthetic path: model-dump the config and stuff it into a
        # ConfigDocument so the section widgets see consistent payloads.
        dump = config.model_dump(mode="python")
        hardware = dump.pop("hardware", {}) or {}
        method = dump.pop("method", None)
        document = ConfigDocument(
            experiment_payload=dump,
            hardware_payload=dict(hardware),
            method_payload=dict(method) if isinstance(method, dict) else None,
            hardware_mode="inline",
            method_mode="inline" if isinstance(method, dict) else "none",
        )
        self._draft = SetupDraft(document=document)
        self._draft.validate()
        self._draft.unapplied = False
        self._apply_in_flight = False
        self._apply_outcome = None
        self._refresh_all_sections()
        self._refresh_source_label()
        self._refresh_outline_markers()
        self._problems.set_problems(self._draft.problems)
        self._refresh_banner()
        self._refresh_apply_enabled()
        self.draftLoaded.emit()

    def clear(self) -> None:
        """Drop the current draft and re-seed with an empty document."""
        self._draft = SetupDraft.empty()
        self._apply_in_flight = False
        self._apply_outcome = None
        self._refresh_all_sections()
        self._refresh_source_label()
        self._refresh_outline_markers()
        self._problems.set_problems([])
        self._refresh_banner()
        self._refresh_apply_enabled()
        self.draftLoaded.emit()

    # --------------------------------------------------------------- toolbar

    def _on_new(self) -> None:
        """Open the New Setup wizard."""
        from capa.ui.tabs.setup_wizard import SetupWizard  # noqa: PLC0415

        document = SetupWizard.run(self)
        if document is None:
            return
        from capa.ui.tabs.setup_state import SetupDraft  # noqa: PLC0415

        self._draft = SetupDraft(document=document)
        self._draft.validate()
        self._refresh_all_sections()
        self._problems.set_problems(self._draft.problems)
        self._refresh_outline_markers()
        self._refresh_source_label()
        self._refresh_banner()
        self._refresh_apply_enabled()
        self.draftLoaded.emit()

    def _on_open(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Open setup",
            str(self._initial_dir()),
            "Configs (*.yaml *.yml *.toml);;All files (*)",
        )
        if not path_str:
            return
        self.load_path(Path(path_str))

    def _on_save(self) -> None:
        if self._draft.document.experiment_path is None:
            self._on_save_as()
            return
        problems = self._draft.validate()
        if any(p.severity == "error" for p in problems):
            self._problems.set_problems(problems)
            self._refresh_outline_markers()
            QMessageBox.warning(
                self,
                "Save refused",
                "Fix the errors in the Problems panel before saving.",
            )
            return
        try:
            self._draft.document.save()
        except SaveError as exc:
            self._show_save_error(exc)
            return
        self._draft.clear_dirty()
        self._refresh_source_label()
        self._refresh_outline_markers()
        self.saved.emit()
        _logger.info(
            "ui.setup.saved",
            path=str(self._draft.document.experiment_path),
        )

    def _on_save_as(self) -> None:
        from capa.ui.tabs.setup_save_as_dialog import (  # noqa: PLC0415  # local import to keep ui-tabs leaf
            SaveAsDialog,
        )

        problems = self._draft.validate()
        if any(p.severity == "error" for p in problems):
            self._problems.set_problems(problems)
            self._refresh_outline_markers()
            QMessageBox.warning(
                self,
                "Save refused",
                "Fix the errors in the Problems panel before saving.",
            )
            return
        dialog = SaveAsDialog(document=self._draft.document, parent=self)
        if not dialog.exec():
            return
        layout = dialog.chosen_layout()
        if layout is None:
            return
        try:
            self._draft.document.save_as(layout)
        except SaveError as exc:
            self._show_save_error(exc)
            return
        self._draft.clear_dirty()
        self._refresh_source_label()
        self._refresh_outline_markers()
        # Refresh sections — Save As may have flipped inline/external.
        self._refresh_all_sections()
        self.saved.emit()
        _logger.info(
            "ui.setup.saved_as",
            path=str(self._draft.document.experiment_path),
        )

    def _on_validate(self) -> None:
        problems = self._draft.validate()
        self._problems.set_problems(problems)
        self._refresh_outline_markers()
        # Pulse Overview if it's the visible section so the validation
        # snapshot updates immediately.
        self._refresh_section("overview")
        errs = sum(1 for p in problems if p.severity == "error")
        warns = sum(1 for p in problems if p.severity == "warning")
        if not problems:
            QMessageBox.information(self, "Validate", "No problems found.")
        else:
            QMessageBox.information(
                self,
                "Validate",
                f"{errs} error(s), {warns} warning(s). See Problems panel.",
            )

    def _on_discover(self) -> None:
        """Open the discovery dialog.

        Built once per click; the dialog is non-destructive — operator
        explicitly clicks [Add] on each row to insert into the draft.
        Refused during an active run (Discover opens serial buses).
        """
        if self._controller is not None and (
            getattr(self._controller, "is_active", False) or self._is_controller_busy()
        ):
            QMessageBox.information(
                self,
                "Discover refused",
                "A run is active. Discover is disabled until the run completes.",
            )
            return
        from capa.ui.tabs.setup_discovery import DiscoveryDialog  # noqa: PLC0415

        existing = self._existing_device_names() | self._existing_camera_names()
        # Hand the controller's lifecycle registry to the dialog so its
        # scan tasks are visible to the ShutdownCoordinator — without
        # this, closing the main window while a discovery dialog is
        # open (or even shortly after) leaves orphan tasks holding
        # serial-port / IOCP handles and wedges loop.close() until
        # Ctrl-C.
        lifecycle = (
            getattr(self._controller, "lifecycle", None) if self._controller is not None else None
        )
        dialog = DiscoveryDialog(existing_names=existing, lifecycle=lifecycle, parent=self)
        dialog.entryAdded.connect(self._on_discovered_entry_added)
        dialog.show()

    def _existing_device_names(self) -> set[str]:
        devices = self._draft.document.hardware_payload.get("devices", [])
        names: set[str] = set()
        if isinstance(devices, list):
            for dev in devices:
                if isinstance(dev, dict) and "name" in dev:
                    names.add(str(dev["name"]))
        return names

    def _existing_camera_names(self) -> set[str]:
        cams = self._draft.document.hardware_payload.get("cameras", [])
        names: set[str] = set()
        if isinstance(cams, list):
            for cam in cams:
                if isinstance(cam, dict) and "name" in cam:
                    names.add(str(cam["name"]))
        return names

    def _on_discovered_device_added(self, payload: object) -> None:
        """Backwards-compatible handler — routes a device row only."""
        self._on_discovered_entry_added("devices", payload)

    def _on_discovered_entry_added(self, section: object, payload: object) -> None:
        """Insert a discovery-row payload into the right draft section."""
        if not isinstance(payload, dict):
            return
        target_section = str(section) if isinstance(section, str) else "devices"
        if target_section not in ("devices", "cameras"):
            return
        bucket = self._draft.document.hardware_payload.setdefault(target_section, [])
        if not isinstance(bucket, list):
            bucket = []
            self._draft.document.hardware_payload[target_section] = bucket
        bucket.append(dict(payload))
        self._draft.mark_dirty(target_section)
        # Whichever section the entry landed in gets a refresh; the
        # Overview reflects counts regardless.
        self._refresh_section(target_section)
        self._refresh_section("overview")
        self._refresh_outline_markers()
        self._refresh_source_label()
        self._refresh_banner()
        self._refresh_apply_enabled()
        self._validate_timer.start()

    def _on_plot_calibration_requested(self, channel_name: object) -> None:
        """Open the calibration plot popup for the named channel.

        The channels section emits
        ``plotCalibrationRequested(name)``; we look up the channel
        payload in the draft and hand it to
        :class:`CalibrationPlotDialog`. The popup is non-modal so
        operators can keep editing while it's open.
        """
        if not isinstance(channel_name, str):
            return
        channel = self._find_channel_by_name(channel_name)
        if channel is None:
            return
        from capa.ui.tabs.setup_calibration_plot import CalibrationPlotDialog  # noqa: PLC0415

        dialog = CalibrationPlotDialog.show_for_channel(channel=channel, parent=self)
        if dialog is None:
            QMessageBox.information(
                self,
                "Plot calibration",
                f"Channel {channel_name!r} has no calibration to plot yet.",
            )

    def _on_apply_calibration_requested(self, channel_name: object) -> None:
        """Open the "Apply calibration to other channels" dialog.

        Replaces the recurring "clone six
        thermocouple curves" tedium with a single multi-select dialog.
        """
        if not isinstance(channel_name, str):
            return
        source = self._find_channel_by_name(channel_name)
        if source is None:
            return
        cal = source.get("calibration") if isinstance(source, dict) else None
        if not isinstance(cal, dict):
            QMessageBox.information(
                self,
                "Apply calibration",
                f"Channel {channel_name!r} has no calibration to clone.",
            )
            return
        from capa.ui.tabs.setup_apply_to_channels_dialog import (  # noqa: PLC0415
            ApplyCalibrationDialog,
        )

        all_channels = self._all_channel_payloads()
        targets = ApplyCalibrationDialog.choose(
            source_name=channel_name,
            source_calibration=cal,
            channels=all_channels,
            parent=self,
        )
        if not targets:
            return
        for ch in all_channels:
            if ch.get("name") in targets:
                ch["calibration"] = dict(cal)
        self._draft.mark_dirty("channels")
        self._refresh_section("channels")
        self._refresh_outline_markers()
        self._refresh_source_label()
        self._refresh_banner()
        self._refresh_apply_enabled()
        self._validate_timer.start()

    def _on_calibration_section_mutated(self) -> None:
        """The Calibration section just mutated the channels list
        (apply-set / clear-all). Mark dirty + refresh + re-validate."""
        self._draft.mark_dirty("channels")
        self._refresh_section("channels")
        self._refresh_section("calibration")
        self._refresh_outline_markers()
        self._refresh_source_label()
        self._refresh_banner()
        self._refresh_apply_enabled()
        self._validate_timer.start()

    def _find_channel_by_name(self, name: str) -> dict[str, Any] | None:
        for ch in self._all_channel_payloads():
            if ch.get("name") == name:
                return ch
        return None

    def _all_channel_payloads(self) -> list[dict[str, Any]]:
        channels = self._draft.document.hardware_payload.get("channels", [])
        if not isinstance(channels, list):
            return []
        return [ch for ch in channels if isinstance(ch, dict)]

    def _on_check_hardware(self) -> None:
        """Run Layer 5 (live handshake) against the current draft.

        Disabled while a run is active
        (Discover / Check open serial buses the run owns) and while
        the draft has errors (live checks can't run against an invalid
        config). The check itself runs on the qasync loop so the Qt
        thread stays responsive; the CHECKING banner reflects in-flight
        state.
        """
        if self._check_in_flight:
            return
        if self._controller is not None and (
            getattr(self._controller, "is_active", False) or self._is_controller_busy()
        ):
            QMessageBox.information(
                self,
                "Check Hardware refused",
                "A run is active. Check Hardware is disabled until the run completes.",
            )
            return
        problems = self._draft.validate()
        self._problems.set_problems(problems)
        self._refresh_outline_markers()
        if self._draft.has_errors:
            QMessageBox.warning(
                self,
                "Check Hardware refused",
                "Fix the errors in the Problems panel before checking hardware.",
            )
            return

        import asyncio  # noqa: PLC0415

        from capa.config import validate_live_async  # noqa: PLC0415

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            # No qasync loop available — happens in unit tests that
            # don't spin one up. Surface a polite message rather than
            # silently doing nothing; the test path exercises the
            # in-flight banner directly through ``begin_check`` /
            # ``finish_check`` helpers below.
            QMessageBox.information(
                self,
                "Check Hardware unavailable",
                "Live checks require an active event loop. Run from the GUI.",
            )
            return

        self._begin_check()
        document = self._draft.document

        async def _runner() -> None:
            try:
                live_problems = await validate_live_async(document)
            except Exception as exc:
                _logger.warning("ui.setup.check_hardware_failed", error=str(exc))
                live_problems = []
            self._finish_check(live_problems)

        loop.create_task(_runner())

    def _begin_check(self) -> None:
        """Flip into the CHECKING state. Public-ish for tests."""
        self._check_in_flight = True
        # During an in-flight check the apply button is greyed so the
        # operator can't fire two long-running adapter sequences at
        # once against the same bus.
        self._refresh_apply_enabled()
        self._refresh_banner()

    def _finish_check(self, live_problems: list[ConfigProblem]) -> None:
        """Merge live findings + clear the CHECKING banner."""
        self._check_in_flight = False
        # Re-run layers 1–4 fresh so the merged list is consistent —
        # the live coroutine may have taken seconds and the operator
        # may have edited fields in the meantime; we don't want stale
        # live findings to outlive the schema state that produced them.
        merged = self._draft.validate()
        merged.extend(live_problems)
        self._draft.problems = merged
        self._problems.set_problems(merged)
        self._refresh_outline_markers()
        self._refresh_apply_enabled()
        self._refresh_banner()

    def _on_apply_to_rig(self) -> None:
        """Apply-to-Rig flow.

        1. Re-run layers 1–4. Refuse on errors.
        2. Refuse if a run is currently active (frozen-while-armed).
        3. Compose the draft + Method-tab buffer via
           :class:`DocumentCoordinator.build_applied_config`.
        4. Emit ``applyRequested(cfg, path)`` for :class:`MainWindow`,
           which calls ``RunController.set_active_config`` and rebuilds
           the run-side docks. Completion is signal-driven —
           :meth:`_on_config_load_finished` flips banner + button state.
        """
        if self._controller is None:
            QMessageBox.information(
                self,
                "Apply unavailable",
                "Setup tab is running without a run controller.",
            )
            return
        if getattr(self._controller, "is_active", False) or self._is_controller_busy():
            QMessageBox.information(
                self,
                "Apply refused",
                "A run is active. Apply to Rig is disabled until the run completes.",
            )
            return

        problems = self._draft.validate()
        self._problems.set_problems(problems)
        self._refresh_outline_markers()
        if self._draft.has_errors:
            QMessageBox.warning(
                self,
                "Apply refused",
                "Fix the errors in the Problems panel before applying.",
            )
            return

        # Compose the apply-time config. Prefer the coordinator (picks
        # up an unsaved Method-tab buffer); fall back to the draft's
        # own ``build_config`` if the coordinator isn't wired (tests).
        try:
            if self._document_coordinator is not None:
                cfg = self._document_coordinator.build_applied_config()
            else:
                cfg = self._draft.document.build_config()
        except CapaError as exc:
            QMessageBox.critical(self, "Apply failed", str(exc))
            _logger.warning("ui.setup.apply_compose_failed", error=str(exc))
            return

        self._apply_in_flight = True
        self._apply_outcome = None
        self._refresh_apply_enabled()
        self._refresh_banner()
        _logger.info(
            "ui.setup.apply_requested",
            path=str(self._draft.document.experiment_path)
            if self._draft.document.experiment_path
            else None,
        )
        self.applyRequested.emit(cfg, self._draft.document.experiment_path)

    # ---------------------------------------------------------- slots: outline

    def _on_section_selected(self, section_id: str) -> None:
        pane = self._section_panes.get(section_id)
        if pane is not None:
            self._stack.setCurrentWidget(pane)

    def _on_section_edited(self, section_id: str) -> None:
        """A section reports a value edit.

        Routes the section's payload slice into the document, marks the
        section dirty, kicks the 200 ms validate debounce, and refreshes
        outline markers + source label so dirty state surfaces
        immediately. The Overview pane refreshes synchronously so its
        live summary follows the operator's typing.
        """
        section = self._sections.get(section_id)
        if section is not None:
            slice_payload = section.payload()
            if slice_payload is not None:
                self._apply_payload(section_id, slice_payload)
        self._draft.mark_dirty(section_id)
        # An edit invalidates any sticky apply-outcome banner; the
        # operator is moving on and the green/red pill becomes stale.
        if self._apply_outcome is not None:
            self._apply_outcome = None
        self._refresh_outline_markers()
        self._refresh_source_label()
        self._refresh_banner()
        self._refresh_apply_enabled()
        # Live Overview update — read-only but driven by the same payload.
        self._refresh_section("overview")
        # Kick the debounce.
        self._validate_timer.start()

    def _apply_payload(self, section_id: str, slice_payload: dict[str, object]) -> None:
        """Merge a section's payload slice into the right document dict.

        Routing is keyed by the slice's top-level dict key rather than
        by the section id, so a single section can write to multiple
        destinations (the CAPA Profile section returns both ``channels``
        — hardware payload — and ``domain_profile`` — experiment payload —
        in one emit).
        """
        for key, value in slice_payload.items():
            if key in _HARDWARE_PAYLOAD_KEYS:
                self._draft.document.hardware_payload[key] = value
            else:
                self._draft.document.experiment_payload[key] = value

    def _run_validate(self) -> None:
        """Debounce-fired validation pass.

        Pure-Python: layers 1–4, no I/O (Layer 5 only runs on explicit
        Check Hardware). Updates ``draft.problems`` in place and refreshes
        the dependent surfaces.
        """
        self._draft.validate()
        self._problems.set_problems(self._draft.problems)
        self._refresh_outline_markers()
        self._refresh_section("overview")
        self._refresh_banner()
        self._refresh_apply_enabled()

    def _on_method_ref_changed(self) -> None:
        path = self._draft.document.method_path
        self.methodRefChanged.emit(path)

    def _on_problem_activated(self, problem: object) -> None:
        if isinstance(problem, ConfigProblem):
            self._outline.select(problem.section)

    # -------------------------------------------------- slots: controller state

    def _on_controller_state(self, state: RunUiState | object) -> None:
        self._controller_state = state
        self._refresh_banner()
        self._refresh_apply_enabled()

    def _on_config_load_finished(self, progress: object) -> None:
        """Listen for the controller's terminal config-load phase.

        Called for every config load — including File→Open and Apply. We
        only adjust the apply-related state when ``_apply_in_flight`` is
        set, so File→Open's existing UX (modal progress dialog) keeps
        owning its own success/failure surface.
        """
        if not self._apply_in_flight:
            return
        if not isinstance(progress, ConfigLoadProgress):
            return
        phase = progress.phase
        if phase is ConfigLoadPhase.READY:
            self._apply_in_flight = False
            self._draft.unapplied = False
            # Summarise: count of device rows that reached READY.
            ready = sum(1 for row in progress.devices if row.status.value == "ready")
            detail = (
                f"{ready} device{'s' if ready != 1 else ''} ready"
                if progress.devices
                else "no devices"
            )
            self._apply_outcome = (_BannerState.APPLIED_OK, detail)
            self._banner_fade_timer.start()
            _logger.info("ui.setup.apply_succeeded", devices=ready)
        elif phase is ConfigLoadPhase.FAILED:
            self._apply_in_flight = False
            self._draft.unapplied = True
            self._apply_outcome = (
                _BannerState.APPLIED_FAILED,
                progress.message or "device initialization failed",
            )
            _logger.warning("ui.setup.apply_failed", message=progress.message)
        else:
            # IDLE / interim phases — keep waiting.
            return
        self._refresh_banner()
        self._refresh_source_label()
        self._refresh_apply_enabled()

    def _on_apply_ok_fade(self) -> None:
        if self._apply_outcome is not None and self._apply_outcome[0] is _BannerState.APPLIED_OK:
            self._apply_outcome = None
            self._refresh_banner()

    # --------------------------------------------------- banner state machine

    def _compute_banner_state(self) -> tuple[_BannerState, str]:
        """Resolve the highest-priority banner the operator should see."""
        if self._is_controller_busy():
            return (_BannerState.FROZEN, "")
        if self._apply_in_flight:
            return (_BannerState.APPLYING, "")
        if self._check_in_flight:
            return (_BannerState.CHECKING, "")
        if self._apply_outcome is not None:
            return self._apply_outcome
        if self._draft.unapplied and not self._draft.has_errors:
            return (_BannerState.UNAPPLIED, "")
        return (_BannerState.HIDDEN, "")

    def _is_controller_busy(self) -> bool:
        """``True`` if the run controller is in any non-IDLE phase.

        Prefers the cached ``_controller_state`` (updated by the
        ``state_changed`` signal) over the attribute on the controller
        so test stubs that emit the signal without updating their own
        attribute still drive the banner correctly.
        """
        if self._controller is None:
            return False
        from capa.ui.state import RunUiState  # noqa: PLC0415

        state = self._controller_state
        if state is None:
            state = getattr(self._controller, "state", None)
        return state in (
            RunUiState.PREPARING,
            RunUiState.RUNNING,
            RunUiState.DRAINING,
            RunUiState.FINALIZING,
        )

    def _refresh_banner(self) -> None:
        state, detail = self._compute_banner_state()
        self._banner_state = state
        self._banner_detail = detail
        if state is _BannerState.HIDDEN:
            self._banner.setVisible(False)
            return
        template, css = _BANNER_STYLES[state]
        text = template.format(detail=detail) if "{detail}" in template else template
        self._banner.setText(text)
        self._banner.setStyleSheet(css)
        self._banner.setVisible(True)

    def _refresh_apply_enabled(self) -> None:
        """Toggle the Apply / Discover / Check buttons.

        Apply: only available when there's an unapplied valid draft and
        no run is in progress.

        Discover / Check Hardware: gated by frozen-while-armed and by
        check-in-flight (one bus operation at a time).
        """
        controller_is_active = False
        if self._controller is not None:
            controller_is_active = (
                getattr(self._controller, "is_active", False) or self._is_controller_busy()
            )
        bus_locked = controller_is_active or self._check_in_flight or self._apply_in_flight
        apply_enabled = (
            self._controller is not None
            and not bus_locked
            and not self._draft.has_errors
            and self._draft.unapplied
        )
        self._action_apply.setEnabled(apply_enabled)
        self._action_discover.setEnabled(not bus_locked)
        # Check Hardware also requires a non-empty, error-free draft —
        # there's nothing to handshake otherwise. Allow it without an
        # ``unapplied`` flag because the operator may want to check
        # the *applied* config too. Layer-5 (``live.*``) errors are
        # excluded here: a failed handshake is exactly the thing the
        # operator wants to retry after fixing a cable or power-cycling
        # a controller, so it must not disable its own re-run button.
        schema_has_errors = any(
            p.severity == "error" and not p.code.startswith("live.") for p in self._draft.problems
        )
        self._action_check.setEnabled(not bus_locked and not schema_has_errors)

    # ------------------------------------------------------------------ helpers

    def _wrap_in_scroll_area(self, widget: SectionWidget) -> QScrollArea:
        # setWidgetResizable=True lets the inner section grow horizontally
        # with the splitter (otherwise the section would keep its
        # sizeHint width and the scroll area would show a horizontal bar
        # at full screen). Vertical scrollbar appears only when the
        # section's content height exceeds the viewport.
        scroll = QScrollArea(self._stack)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(widget)
        return scroll

    def _build_section(self, section_id: str) -> SectionWidget:
        if section_id == "overview":
            return OverviewSection(self)
        if section_id == "files":
            return FilesSection(self)
        if section_id == "experiment":
            return ExperimentSection(self)
        if section_id == "procedure":
            return ProcedureSection(self)
        if section_id == "storage":
            return StorageSection(self)
        if section_id == "safety":
            return SafetySection(self)
        if section_id == "devices":
            from capa.ui.tabs.setup_sections.devices import DevicesSection  # noqa: PLC0415

            devices = DevicesSection(self)
            devices.deviceActionRequested.connect(self.deviceActionRequested)
            return devices
        if section_id == "channels":
            from capa.ui.tabs.setup_sections.channels import ChannelsSection  # noqa: PLC0415

            channels = ChannelsSection(self)
            channels.plotCalibrationRequested.connect(self._on_plot_calibration_requested)
            channels.applyCalibrationRequested.connect(self._on_apply_calibration_requested)
            return channels
        if section_id == "cameras":
            from capa.ui.tabs.setup_sections.cameras import CamerasSection  # noqa: PLC0415

            return CamerasSection(self)
        if section_id == "hardware":
            from capa.ui.tabs.setup_sections.hardware import HardwareGlanceSection  # noqa: PLC0415

            return HardwareGlanceSection(self)
        if section_id == "capa_profile":
            from capa.ui.tabs.setup_sections.capa_profile import CapaProfileSection  # noqa: PLC0415

            return CapaProfileSection(self)
        if section_id == "calibration":
            from capa.ui.tabs.setup_sections.calibration import CalibrationSection  # noqa: PLC0415

            calibration = CalibrationSection(self)
            calibration.channelsMutated.connect(self._on_calibration_section_mutated)
            return calibration
        # Anything unrecognised falls back to a placeholder so the
        # outline stays navigable even if a section module fails to load.
        return _PlaceholderSection(section_id, self)

    def _refresh_all_sections(self) -> None:
        for section in self._sections.values():
            section.set_draft(self._draft)

    def _refresh_section(self, section_id: str) -> None:
        widget = self._sections.get(section_id)
        if widget is not None:
            widget.refresh()

    def _refresh_outline_markers(self) -> None:
        self._outline.set_markers(
            dirty_sections=set(self._draft.dirty_sections),
            problems=self._draft.problems,
        )

    def _refresh_source_label(self) -> None:
        doc = self._draft.document
        if doc.experiment_path is not None:
            text = str(doc.experiment_path.name)
        elif doc.hardware_path is not None:
            text = f"(hardware only) {doc.hardware_path.name}"
        else:
            text = "untitled"
        if self._draft.is_dirty:
            text = f"{text} ●"
        if self._draft.unapplied:
            text = f"{text}  ↑ Unapplied"
        self._source_label.setText(text)

    def _initial_dir(self) -> Path:
        doc = self._draft.document
        if doc.experiment_path is not None:
            return doc.experiment_path.parent
        if doc.hardware_path is not None:
            return doc.hardware_path.parent
        return Path.cwd()

    def _show_save_error(self, exc: SaveError) -> None:
        rolled = ", ".join(str(p) for p in exc.rolled_back_paths)
        body = str(exc)
        if rolled:
            body = f"{body}\n\nRolled back: {rolled}"
        QMessageBox.warning(self, "Save failed", body)
        _logger.warning(
            "ui.setup.save_failed",
            failed=str(exc.failed_path) if exc.failed_path else None,
            rolled_back=[str(p) for p in exc.rolled_back_paths],
        )


class _PlaceholderSection(SectionWidget):
    """Stand-in for an outline entry whose live editor hasn't landed yet.

    Used by Slice D1 for Experiment / Procedure / Storage / Safety so
    operators can confirm navigation works; Slice D2 replaces these in
    place.
    """

    def __init__(self, section_id: str, parent: Any = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        title = QLabel(section_id.capitalize(), self)
        title.setStyleSheet("font-size: 14pt; font-weight: 600;")
        outer.addWidget(title)
        note = QLabel(
            "Editor pane lands in Slice D2 — section navigation is wired,"
            " editing is not yet available here.",
            self,
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #555;")
        outer.addWidget(note)
        outer.addStretch(1)

    def set_draft(self, draft: SetupDraft) -> None:
        # Placeholder has no payload; nothing to do.
        pass

    def refresh(self) -> None:
        pass


__all__ = ["SetupTab"]
