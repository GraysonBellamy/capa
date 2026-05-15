"""Slice F3 — Apply to Rig flow (plan §5.14).

Covers:

* Click Apply with a valid draft → emits ``applyRequested`` with the
  composed :class:`ExperimentConfig` + path; banner flips to APPLYING.
* On controller's ``config_load_finished`` READY → banner flips to
  APPLIED_OK, draft is marked applied, Apply button greys.
* On ``config_load_finished`` FAILED → banner flips to APPLIED_FAILED,
  draft stays unapplied.
* Apply during an active run → refused with a modal, no emit.
* Apply with errors in the draft → refused, no emit.
* The frozen banner overrides any APPLYING / UNAPPLIED state.
* Editing the draft after a successful apply clears the green pill.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from PySide6.QtCore import QObject, Signal

from capa.config import ConfigDocument
from capa.experiment.config import ExperimentConfig
from capa.runtime.progress import DeviceInitProgress, DeviceInitStatus
from capa.ui.config_progress import ConfigLoadPhase, ConfigLoadProgress
from capa.ui.document_coordinator import DocumentCoordinator
from capa.ui.state import RunUiState
from capa.ui.tabs.method import MethodTab
from capa.ui.tabs.setup import SetupTab, _BannerState

REPO_ROOT = Path(__file__).resolve().parents[4]
SIM_CAPA_EXP = REPO_ROOT / "configs" / "experiments" / "sim_capa_pyrolysis.yaml"


class _ControllerStub(QObject):
    """Minimal stand-in for ``RunController`` for Apply tests.

    Exposes ``state_changed`` and ``config_load_finished`` signals plus
    an ``is_active`` flag the SetupTab's Apply gate checks. Tests drive
    the signals directly to simulate the full open / failed sequence.
    """

    state_changed = Signal(object)
    config_load_finished = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.state = RunUiState.IDLE
        self.is_active = False


def _make_triple(
    qtbot: Any,
) -> tuple[SetupTab, MethodTab, DocumentCoordinator, _ControllerStub]:
    controller = _ControllerStub()
    setup = SetupTab(controller=controller)  # type: ignore[arg-type]
    method = MethodTab()
    qtbot.addWidget(setup)
    qtbot.addWidget(method)
    coord = DocumentCoordinator(setup_tab=setup, method_tab=method)
    setup.set_document_coordinator(coord)
    return setup, method, coord, controller


def _make_progress(
    phase: ConfigLoadPhase, *, devices: int = 2, message: str = ""
) -> ConfigLoadProgress:
    rows = tuple(
        DeviceInitProgress(
            name=f"d{i}",
            adapter="sim",
            resource_id=f"res{i}",
            status=DeviceInitStatus.READY,
            detail="ok",
        )
        for i in range(devices)
    )
    return ConfigLoadProgress(phase=phase, message=message, devices=rows)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_apply_emits_request_and_shows_applying_banner(qtbot: Any) -> None:
    setup, _method, _coord, _controller = _make_triple(qtbot)
    setup.load_path(SIM_CAPA_EXP)
    # The fixture loads with unapplied=False — mark it unapplied so
    # the Apply gate is satisfied (operator just edited something).
    setup._draft.unapplied = True
    setup._refresh_apply_enabled()
    assert setup._action_apply.isEnabled()

    captured: list[tuple[object, object]] = []
    setup.applyRequested.connect(lambda cfg, path: captured.append((cfg, path)))
    setup._on_apply_to_rig()

    assert len(captured) == 1
    cfg, path = captured[0]
    assert isinstance(cfg, ExperimentConfig)
    assert path == SIM_CAPA_EXP.resolve()
    assert setup._apply_in_flight is True
    assert setup._banner_state is _BannerState.APPLYING
    # The button is disabled while the apply is mid-flight.
    assert not setup._action_apply.isEnabled()


def test_apply_succeeded_flips_banner_and_clears_unapplied(qtbot: Any) -> None:
    setup, _method, _coord, controller = _make_triple(qtbot)
    setup.load_path(SIM_CAPA_EXP)
    setup._draft.unapplied = True
    setup._refresh_apply_enabled()
    setup._on_apply_to_rig()
    assert setup._apply_in_flight is True

    controller.config_load_finished.emit(_make_progress(ConfigLoadPhase.READY))
    assert setup._apply_in_flight is False
    assert setup._draft.unapplied is False
    assert setup._banner_state is _BannerState.APPLIED_OK
    # Apply button is grey now — nothing to apply.
    assert not setup._action_apply.isEnabled()


def test_apply_failed_flips_banner_and_preserves_unapplied(qtbot: Any) -> None:
    setup, _method, _coord, controller = _make_triple(qtbot)
    setup.load_path(SIM_CAPA_EXP)
    setup._draft.unapplied = True
    setup._refresh_apply_enabled()
    setup._on_apply_to_rig()

    controller.config_load_finished.emit(
        _make_progress(
            ConfigLoadPhase.FAILED,
            message="Device 'heater' failed to open: port not present",
        )
    )
    assert setup._apply_in_flight is False
    assert setup._draft.unapplied is True
    assert setup._banner_state is _BannerState.APPLIED_FAILED
    # Apply remains available so the operator can retry once the
    # underlying problem is resolved.
    assert setup._action_apply.isEnabled()


# ---------------------------------------------------------------------------
# Refusal paths
# ---------------------------------------------------------------------------


def test_apply_refused_during_active_run(qtbot: Any) -> None:
    setup, _method, _coord, controller = _make_triple(qtbot)
    setup.load_path(SIM_CAPA_EXP)
    setup._draft.unapplied = True
    setup._refresh_apply_enabled()

    controller.is_active = True
    controller.state = RunUiState.RUNNING
    controller.state_changed.emit(RunUiState.RUNNING)

    captured: list[Any] = []
    setup.applyRequested.connect(lambda cfg, path: captured.append((cfg, path)))
    with patch("capa.ui.tabs.setup.QMessageBox.information") as info:
        setup._on_apply_to_rig()
    assert captured == []
    assert info.call_count == 1
    assert setup._apply_in_flight is False
    assert setup._banner_state is _BannerState.FROZEN


def test_apply_refused_when_draft_has_errors(qtbot: Any) -> None:
    setup, _method, _coord, _controller = _make_triple(qtbot)
    setup.load_path(SIM_CAPA_EXP)
    # Break the schema — drop the required hardware ``name``.
    setup._draft.document.hardware_payload.pop("name", None)
    setup._draft.validate()
    setup._draft.unapplied = True
    setup._refresh_apply_enabled()
    assert setup._draft.has_errors

    captured: list[Any] = []
    setup.applyRequested.connect(lambda cfg, path: captured.append((cfg, path)))
    with patch("capa.ui.tabs.setup.QMessageBox.warning") as warn:
        setup._on_apply_to_rig()
    assert captured == []
    assert warn.call_count == 1


# ---------------------------------------------------------------------------
# Edit clears the success/failure pill
# ---------------------------------------------------------------------------


def test_apply_ok_banner_clears_on_next_edit(qtbot: Any) -> None:
    setup, _method, _coord, controller = _make_triple(qtbot)
    setup.load_path(SIM_CAPA_EXP)
    setup._draft.unapplied = True
    setup._on_apply_to_rig()
    controller.config_load_finished.emit(_make_progress(ConfigLoadPhase.READY))
    assert setup._banner_state is _BannerState.APPLIED_OK

    # Simulate an edit. The Setup tab clears the green pill.
    setup._on_section_edited("storage")
    assert setup._apply_outcome is None
    # The new edit also marked the section dirty → unapplied true → banner UNAPPLIED.
    assert setup._draft.unapplied is True
    assert setup._banner_state is _BannerState.UNAPPLIED


# ---------------------------------------------------------------------------
# Banner priority
# ---------------------------------------------------------------------------


def test_frozen_banner_trumps_applying(qtbot: Any) -> None:
    """If a run starts during an apply, the frozen banner wins."""
    setup, _method, _coord, controller = _make_triple(qtbot)
    setup.load_path(SIM_CAPA_EXP)
    setup._draft.unapplied = True
    setup._on_apply_to_rig()
    assert setup._banner_state is _BannerState.APPLYING
    controller.state_changed.emit(RunUiState.RUNNING)
    assert setup._banner_state is _BannerState.FROZEN


def test_apply_enabled_only_when_unapplied_and_no_errors(qtbot: Any) -> None:
    setup, _method, _coord, _controller = _make_triple(qtbot)
    setup.load_path(SIM_CAPA_EXP)
    # Loaded but not edited: unapplied is False (load resets it).
    assert not setup._draft.unapplied
    assert not setup._action_apply.isEnabled()
    # Mark unapplied: Apply becomes available.
    setup._draft.unapplied = True
    setup._refresh_apply_enabled()
    assert setup._action_apply.isEnabled()
    # Introduce an error: Apply greys out again.
    setup._draft.document.hardware_payload.pop("name", None)
    setup._draft.validate()
    setup._refresh_apply_enabled()
    assert not setup._action_apply.isEnabled()


# ---------------------------------------------------------------------------
# New-wizard gates (unified-open-pipeline refactor)
# ---------------------------------------------------------------------------


def test_new_wizard_marks_draft_unapplied_and_enables_apply(qtbot: Any, monkeypatch: Any) -> None:
    """The New wizard produces an apply-ready draft.

    Before this fix, _on_new left ``unapplied=False`` (the default on a
    fresh SetupDraft), so the Apply button stayed greyed out until the
    operator made a stray edit. The wizard's output is by definition
    not the same as the currently-applied config, so Apply should light
    up the moment the wizard returns.
    """
    setup, _method, _coord, _controller = _make_triple(qtbot)
    fixture_doc = ConfigDocument.load(SIM_CAPA_EXP)
    monkeypatch.setattr(
        "capa.ui.tabs.setup_wizard.SetupWizard.run",
        classmethod(lambda cls, parent: fixture_doc),
    )

    setup._on_new()

    assert setup._draft.unapplied is True
    assert setup._action_apply.isEnabled()
    assert setup._banner_state is _BannerState.UNAPPLIED


def test_new_action_disabled_during_active_run(qtbot: Any) -> None:
    """New is frozen while a run is armed.

    Consistent with Apply / Discover / Check Hardware: changing the
    setup mid-run is refused. Operators see a polite modal and the
    toolbar action greys out.
    """
    setup, _method, _coord, controller = _make_triple(qtbot)
    setup.load_path(SIM_CAPA_EXP)
    controller.is_active = True
    controller.state = RunUiState.RUNNING
    controller.state_changed.emit(RunUiState.RUNNING)

    assert setup._action_new.isEnabled() is False

    captured: list[Any] = []
    setup.draftLoaded.connect(lambda: captured.append(None))
    with patch("capa.ui.tabs.setup.QMessageBox.information") as info:
        setup._on_new()
    assert captured == []
    assert info.call_count == 1


# ---------------------------------------------------------------------------
# Coordinator fallback
# ---------------------------------------------------------------------------


def test_apply_falls_back_when_coordinator_not_set(qtbot: Any) -> None:
    """SetupTab still works in tests / harnesses without the coordinator."""
    controller = _ControllerStub()
    setup = SetupTab(controller=controller)  # type: ignore[arg-type]
    qtbot.addWidget(setup)
    setup.load_path(SIM_CAPA_EXP)
    setup._draft.unapplied = True
    setup._refresh_apply_enabled()

    captured: list[Any] = []
    setup.applyRequested.connect(lambda cfg, path: captured.append((cfg, path)))
    setup._on_apply_to_rig()
    assert len(captured) == 1
    cfg, _ = captured[0]
    assert isinstance(cfg, ExperimentConfig)
