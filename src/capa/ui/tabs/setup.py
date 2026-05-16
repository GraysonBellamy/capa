"""``SetupTab`` — editor shell.

The tab owns a :class:`SetupDraft`, surfaces an outline / main-editor /
Problems three-region layout, and exposes Save / Save As / Validate
against the underlying :class:`ConfigDocument`.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from capa.config import ConfigDocument, SaveError
from capa.config.problems import ConfigProblem
from capa.core.errors import CapaError
from capa.experiment.config import ExperimentConfig
from capa.ui.config_progress import ConfigLoadProgress, ConfigLoadState
from capa.ui.recents import load_recents
from capa.ui.tabs.setup_connection_strip import (
    ConnectionInputs,
    ConnectionStrip,
)
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
    clicks Apply & Connect on a valid draft. :class:`MainWindow` consumes
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
        # Apply & Connect state machine: True while an apply is mid-flight
        # (between ``applyRequested`` emit and the controller's
        # ``config_load_finished`` signal). Gates the Apply button so
        # the operator can't double-click during hardware preparation.
        self._apply_in_flight: bool = False
        # Most recent failure detail (full error text from a failed
        # apply). Cleared on the next apply attempt or any draft edit;
        # surfaced to the operator via the connection strip's
        # Details… dialog so the failure is never silently hidden.
        self._last_apply_failed: bool = False
        self._last_apply_succeeded: bool = False
        self._last_failure_detail: str = ""
        # Most recent connected detail ("12 devices · 8 channels") shown
        # in the green CONNECTED state. Re-derived on every config-load.
        self._connected_detail: str = ""
        # Check-Hardware in-flight flag — gates the Check button and
        # drives the CHECKING strip. The actual coroutine is scheduled
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

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        # Toolbar — three primary actions plus an overflow. Open carries
        # New / Open / Recent in a submenu so the seven-button row that
        # used to live here collapses to a single line.
        self._toolbar = QToolBar("Setup", self)
        self._toolbar.setMovable(False)

        # Open ▾ toolbutton with submenu.
        self._open_btn = QToolButton(self._toolbar)
        self._open_btn.setText("Open")
        self._open_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._open_menu = QMenu(self._open_btn)
        self._action_new = self._open_menu.addAction("New from template…", self._on_new)
        self._open_menu.addAction("Open file…", self._on_open_file)
        self._recent_submenu = self._open_menu.addMenu("Recent")
        self._recent_submenu.aboutToShow.connect(self._populate_recent_submenu)
        self._open_btn.setMenu(self._open_menu)
        self._toolbar.addWidget(self._open_btn)

        # Save stays as a single-action button.
        self._action_save = self._toolbar.addAction("Save", self._on_save)

        # Spacer to push the overflow to the right edge. Source label
        # sits just before it so the operator can see which config the
        # tab is editing without taking up toolbar width that primary
        # actions need.
        spacer = QWidget(self._toolbar)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._toolbar.addWidget(spacer)
        self._source_label = QLabel("untitled", self._toolbar)
        self._source_label.setContentsMargins(0, 0, 8, 0)
        self._toolbar.addWidget(self._source_label)

        # ⋮ overflow — actions that exist but aren't primary. Save As,
        # Validate, Scan, Verify, Revert. Apply & Connect lives in the
        # connection strip so it isn't duplicated here.
        self._overflow_btn = QToolButton(self._toolbar)
        self._overflow_btn.setText("⋮")
        self._overflow_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._overflow_btn.setToolTip("More actions")
        self._overflow_menu = QMenu(self._overflow_btn)
        self._action_save_as = self._overflow_menu.addAction("Save As…", self._on_save_as)
        self._action_validate = self._overflow_menu.addAction("Validate", self._on_validate)
        self._overflow_menu.addSeparator()
        self._action_discover = self._overflow_menu.addAction("Scan for devices", self._on_discover)
        self._action_discover.setEnabled(False)
        self._action_discover.setToolTip(
            "Scan each device family's bus / network / USB tree and offer to "
            "add reachable devices to the draft."
        )
        self._action_check = self._overflow_menu.addAction(
            "Verify connection", self._on_check_hardware
        )
        self._action_check.setEnabled(False)
        self._action_check.setToolTip(
            "Read-only handshake against each device in the draft — confirms the "
            "rig can be reached without opening long-lived connections."
        )
        self._overflow_menu.addSeparator()
        self._action_revert = self._overflow_menu.addAction("Revert draft", self._on_revert_draft)
        self._overflow_btn.setMenu(self._overflow_menu)
        self._toolbar.addWidget(self._overflow_btn)

        # Apply & Connect retained as an internal action so existing
        # gating + tooltip code paths (and the tests that drive
        # ``_action_apply``) keep working. The visible Apply button lives
        # on the connection strip; this action is the keyboard / API
        # entry point.
        self._action_apply = QAction("Apply && Connect", self)
        self._action_apply.triggered.connect(self._on_apply_to_rig)
        self._action_apply.setEnabled(False)
        self._action_apply.setToolTip(
            "Validate the draft, open hardware connections, and start background acquisition."
        )

        outer.addWidget(self._toolbar)

        # Persistent connection strip — always-on answer to "is the rig
        # live?". State priority is resolved inside
        # :class:`ConnectionStrip`; this tab only feeds it inputs.
        self._connection_strip = ConnectionStrip(self)
        self._connection_strip.applyRequested.connect(self._on_apply_to_rig)
        self._connection_strip.revertRequested.connect(self._on_revert_draft)
        self._connection_strip.detailsRequested.connect(self._on_show_failure_details)
        outer.addWidget(self._connection_strip)

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
            # Hardware-ready state gates Check Hardware. When the pool is
            # open and the draft hasn't been edited since apply, a fresh
            # handshake would conflict with the pool's open port — so we
            # disable the button rather than letting the operator
            # discover the conflict via a row of failures.
            with contextlib.suppress(AttributeError):
                self._controller.hardware_ready_changed.connect(self._on_hardware_ready_changed)
            with contextlib.suppress(Exception):
                self._on_controller_state(self._controller.state)
        self._refresh_connection_strip()
        self._refresh_apply_enabled()

    # ------------------------------------------------------------------ API

    @property
    def draft(self) -> SetupDraft:
        return self._draft

    def set_document_coordinator(self, coordinator: DocumentCoordinator) -> None:
        """Inject the :class:`DocumentCoordinator` for Apply & Connect.

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
        self._clear_apply_outcome()
        self._refresh_all_sections()
        self._refresh_source_label()
        self._refresh_outline_markers()
        self._problems.set_problems(self._draft.problems)
        self._refresh_connection_strip()
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
        self._clear_apply_outcome()
        self._refresh_all_sections()
        self._refresh_source_label()
        self._refresh_outline_markers()
        self._problems.set_problems(self._draft.problems)
        self._refresh_connection_strip()
        self._refresh_apply_enabled()
        self.draftLoaded.emit()

    def clear(self) -> None:
        """Drop the current draft and re-seed with an empty document."""
        self._draft = SetupDraft.empty()
        self._apply_in_flight = False
        self._clear_apply_outcome()
        self._refresh_all_sections()
        self._refresh_source_label()
        self._refresh_outline_markers()
        self._problems.set_problems([])
        self._refresh_connection_strip()
        self._refresh_apply_enabled()
        self.draftLoaded.emit()

    # --------------------------------------------------------------- toolbar

    def _on_new(self) -> None:
        """Open the New Setup wizard.

        A wizard-produced draft is by definition not the same as the
        currently-applied config (or there is no applied config), so we
        mark ``unapplied`` so the Apply & Connect button lights up as the
        next logical step. Without this, opening the wizard and clicking
        Apply immediately would be impossible — the operator would have
        to make a stray edit just to unlock the button.
        """
        if self._is_controller_busy():
            QMessageBox.information(
                self,
                "New refused",
                "A run is active. Creating a new setup is disabled until the run completes.",
            )
            return
        from capa.ui.tabs.setup_wizard import SetupWizard  # noqa: PLC0415

        document = SetupWizard.run(self)
        if document is None:
            return
        from capa.ui.tabs.setup_state import SetupDraft  # noqa: PLC0415

        self._draft = SetupDraft(document=document)
        self._draft.validate()
        self._draft.unapplied = True
        self._apply_in_flight = False
        self._clear_apply_outcome()
        self._refresh_all_sections()
        self._problems.set_problems(self._draft.problems)
        self._refresh_outline_markers()
        self._refresh_source_label()
        self._refresh_connection_strip()
        self._refresh_apply_enabled()
        self.draftLoaded.emit()

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
        self._refresh_connection_strip()
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
        self._refresh_connection_strip()
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
        self._refresh_connection_strip()
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
        self._refresh_connection_strip()

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
        self._refresh_connection_strip()

    def _on_apply_to_rig(self) -> None:
        """Apply & Connect flow.

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
                "A run is active. Apply & Connect is disabled until the run completes.",
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
        self._clear_apply_outcome()
        self._refresh_apply_enabled()
        self._refresh_connection_strip()
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
        # An edit invalidates any sticky apply-outcome state; the
        # operator is moving on and the strip's red failure detail or
        # green connected detail becomes stale.
        self._clear_apply_outcome()
        self._refresh_outline_markers()
        self._refresh_source_label()
        self._refresh_connection_strip()
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
        self._refresh_connection_strip()
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
        self._refresh_connection_strip()
        self._refresh_apply_enabled()

    def _on_hardware_ready_changed(self, _ready: bool) -> None:
        """Pool readiness flipped — re-evaluate Check Hardware gating
        and the connection strip (which reads ``controller.hardware_ready``
        to decide CONNECTED vs UNAPPLIED)."""
        self._refresh_connection_strip()
        self._refresh_apply_enabled()

    def _on_config_load_finished(self, progress: object) -> None:
        """Listen for the controller's terminal config-load state.

        Called for every config load — including File→Open and Apply. We
        only adjust the apply-related state when ``_apply_in_flight`` is
        set, so File→Open's existing UX (modal progress dialog) keeps
        owning its own success/failure surface.
        """
        if not self._apply_in_flight:
            return
        if not isinstance(progress, ConfigLoadProgress):
            return
        state = progress.state
        if state is ConfigLoadState.READY:
            self._apply_in_flight = False
            self._draft.unapplied = False
            # Summarise: count of device rows that reached READY.
            ready = sum(1 for row in progress.devices if row.status.value == "ready")
            detail = (
                f"{ready} device{'s' if ready != 1 else ''} ready"
                if progress.devices
                else "no devices"
            )
            self._last_apply_failed = False
            self._last_apply_succeeded = True
            self._last_failure_detail = ""
            self._connected_detail = detail
            _logger.info("ui.setup.apply_succeeded", devices=ready)
        elif state is ConfigLoadState.FAILED:
            self._apply_in_flight = False
            self._draft.unapplied = True
            self._last_apply_failed = True
            self._last_apply_succeeded = False
            self._last_failure_detail = progress.message or "device initialization failed"
            self._connected_detail = ""
            _logger.warning("ui.setup.apply_failed", message=progress.message)
        else:
            # IDLE / interim states: keep waiting.
            return
        self._refresh_connection_strip()
        self._refresh_source_label()
        self._refresh_apply_enabled()

    # --------------------------------------------------- connection strip helpers

    def _is_controller_busy(self) -> bool:
        """``True`` if the run controller is in any non-IDLE state.

        Prefers the cached ``_controller_state`` (updated by the
        ``state_changed`` signal) over the attribute on the controller
        so test stubs that emit the signal without updating their own
        attribute still drive the strip correctly.
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

    def _refresh_connection_strip(self) -> None:
        """Compose the latest :class:`ConnectionInputs` and push to the strip."""
        has_config = self._draft.document.experiment_path is not None or bool(
            self._draft.document.experiment_payload
        )
        hardware_ready = bool(
            self._controller is not None and getattr(self._controller, "hardware_ready", False)
        )
        inputs = ConnectionInputs(
            has_config=has_config,
            hardware_ready=hardware_ready,
            draft_unapplied=self._draft.unapplied,
            draft_dirty_count=self._draft.dirty_section_count,
            draft_has_errors=self._draft.has_errors,
            apply_in_flight=self._apply_in_flight,
            check_in_flight=self._check_in_flight,
            controller_busy=self._is_controller_busy(),
            last_apply_failed=self._last_apply_failed,
            last_apply_succeeded=self._last_apply_succeeded,
            failure_detail=self._last_failure_detail,
            connected_detail=self._connected_detail,
        )
        self._connection_strip.update_state(inputs)

    def _clear_apply_outcome(self) -> None:
        """Reset failure / connection-detail bookkeeping.

        Called on every draft-load, draft-edit, or new apply attempt so
        the connection strip stops showing stale "Last apply failed…" or
        "Connected — N devices" text once those snapshots are no longer
        accurate.
        """
        self._last_apply_failed = False
        self._last_apply_succeeded = False
        self._last_failure_detail = ""
        self._connected_detail = ""

    def _on_open_file(self) -> None:
        """Open a config from disk via the toolbar's Open ▾ menu.

        The :class:`MainWindow`'s File → Open still works the same way;
        this is the inline shortcut so the operator doesn't have to
        leave the Setup tab to load a different config. Routes through
        :meth:`load_path` so the same load pipeline (validation,
        recents persistence, draft-state reset) runs.
        """
        if self._is_controller_busy():
            QMessageBox.information(
                self,
                "Open refused",
                "A run is active. Open is disabled until the run completes.",
            )
            return
        start_dir = ""
        if self._draft.document.experiment_path is not None:
            start_dir = str(self._draft.document.experiment_path.parent)
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Open experiment config",
            start_dir,
            "Configs (*.yaml *.yml *.toml);;All files (*)",
        )
        if not path_str:
            return
        self.load_path(Path(path_str))

    def _populate_recent_submenu(self) -> None:
        """Rebuild the Open → Recent submenu just before it opens.

        Lazy population keeps the menu in sync with whatever other capa
        sessions have written to ``~/.capa/recents.json`` without
        forcing the tab to subscribe to filesystem changes.
        """
        self._recent_submenu.clear()
        entries = load_recents()
        if not entries:
            placeholder = self._recent_submenu.addAction("(no recent configs)")
            placeholder.setEnabled(False)
            return
        for entry in entries:
            action = self._recent_submenu.addAction(str(entry.path))
            action.triggered.connect(lambda _checked=False, p=entry.path: self.load_path(p))

    def _on_revert_draft(self) -> None:
        """Connection-strip Revert button: drop unsaved edits.

        Re-loads the underlying document so any in-progress section
        edits are discarded. If the draft has no on-disk source, falls
        back to clearing the dirty/unapplied bits.
        """
        path = self._draft.document.experiment_path
        if path is not None and path.is_file():
            self.load_path(path)
            return
        self._draft.clear_dirty()
        self._draft.unapplied = False
        self._clear_apply_outcome()
        self._refresh_all_sections()
        self._refresh_source_label()
        self._refresh_outline_markers()
        self._refresh_connection_strip()
        self._refresh_apply_enabled()

    def _on_show_failure_details(self) -> None:
        """Connection-strip Details… button: open a modal with the full error.

        The strip's single-line label can only carry so much; the
        detail dialog renders the entire message in a copyable text
        area so the operator can paste it into a bug report.
        """
        dialog = QDialog(self)
        dialog.setWindowTitle("Apply failed")
        dialog.resize(560, 360)
        layout = QVBoxLayout(dialog)
        header = QLabel(
            "The last Apply & Connect failed. Fix the issue and try again.",
            dialog,
        )
        header.setWordWrap(True)
        layout.addWidget(header)
        body = QPlainTextEdit(dialog)
        body.setReadOnly(True)
        body.setPlainText(self._last_failure_detail or "(no detail captured)")
        layout.addWidget(body)
        button_row = QHBoxLayout()
        copy_btn = QPushButton("Copy", dialog)
        copy_btn.clicked.connect(lambda: QGuiApplication.clipboard().setText(body.toPlainText()))
        button_row.addWidget(copy_btn)
        button_row.addStretch(1)
        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=dialog)
        close_box.rejected.connect(dialog.reject)
        close_box.accepted.connect(dialog.accept)
        button_row.addWidget(close_box)
        layout.addLayout(button_row)
        dialog.exec()

    def _refresh_apply_enabled(self) -> None:
        """Toggle the New / Apply / Discover / Check buttons.

        Apply: only available when there's an unapplied valid draft and
        no run is in progress.

        New: refused during an armed run — the operator's mental model
        is that the rig's setup is fixed for the duration of the run, so
        dropping a fresh draft on top of it would be confusing.

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
        self._action_new.setEnabled(not controller_is_active)
        self._action_apply.setEnabled(apply_enabled)
        self._action_discover.setEnabled(not bus_locked)
        # Check Hardware: gated by frozen-while-armed, schema errors,
        # AND by "the pool already owns the ports we'd want to handshake
        # against". A fresh handshake opens its own connection to the
        # device — if the pool is already open on the same port, the
        # second open fails and we'd report every connected device as
        # broken. Stays disabled for the entire duration the pool is
        # open: even after the operator edits the draft, the pool still
        # holds the original ports until the next apply, so any
        # handshake against an unchanged port would still collide. The
        # operator's path to re-verify is "apply the new config" or
        # disconnect — not "edit and re-check".
        #
        # Layer-5 (``live.*``) errors are excluded from the schema-error
        # check: a failed handshake is exactly the thing the operator
        # wants to retry after fixing a cable or power-cycling a
        # controller, so it must not disable its own re-run button.
        schema_has_errors = any(
            p.severity == "error" and not p.code.startswith("live.") for p in self._draft.problems
        )
        hardware_ready = bool(
            self._controller is not None and getattr(self._controller, "hardware_ready", False)
        )
        check_enabled = not bus_locked and not schema_has_errors and not hardware_ready
        self._action_check.setEnabled(check_enabled)
        if hardware_ready:
            self._action_check.setToolTip(
                "Hardware is connected — apply the new config or disconnect to re-verify."
            )
        else:
            self._action_check.setToolTip("Read-only handshake against each device in the draft.")

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

    Operators can still confirm navigation works even before a full
    editor exists for a section.
    """

    def __init__(self, section_id: str, parent: Any = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        title = QLabel(section_id.capitalize(), self)
        title.setStyleSheet("font-size: 14pt; font-weight: 600;")
        outer.addWidget(title)
        note = QLabel(
            "Editor pane is not available yet; section navigation is wired,"
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
