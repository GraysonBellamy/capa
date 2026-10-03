"""CAPA Profile section — the profile metadata editor.

Four metadata panes (Specimen / Heater program / Atmosphere / Analyzer
& SOP) sit above the required-mapping panel that ties each CAPA group
to a hardware channel. Every pane is built from the profile's own
models (:class:`CapaSpecimen`, :class:`HeaterProgram`,
:class:`Atmosphere`, and the remaining top-level fields of
:class:`CapaPyrolysisMetadata`), so what the form writes is exactly
what the profile validates.

The Specimen pane is the one place the operator describes the
specimen. Every edit also rewrites the experiment's ``sample`` block
from it (:func:`capa.config.capa_profile.sample_from_specimen`); the
Operator & sample section shows that block read-only while the profile
is active.

The mapping panel is the operator-visible payoff for the hardware
side: every required group reports a green ✓ / red ✗ chip, and
selecting a channel writes ``metadata["capa_group"]`` on it.

Experiments without a domain profile get an "Add CAPA profile" button
that seeds the specimen from the existing ``sample`` block; experiments
on a different profile are left alone.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, create_model
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from capa.calibration.tune_artifact import (
    HeatFluxTuneArtifact,
    TuneArtifactError,
    load_artifact,
    load_latest,
)
from capa.channels.spec import ChannelKind
from capa.config.capa_profile import (
    CAPA_OPTIONAL_GROUPS,
    CAPA_PROFILE_ID,
    CAPA_REQUIRED_GROUPS,
    SPECIMEN_SAMPLE_FIELDS,
    current_capa_mappings,
    is_capa_profile,
    sample_from_specimen,
)
from capa.experiment.procedures.builtin.heat_flux_tune.config import (
    PROCEDURE_ID as HEAT_FLUX_TUNE_PROCEDURE_ID,
)
from capa.experiment.profiles.capa_pyrolysis import (
    Atmosphere,
    CapaPyrolysisMetadata,
    CapaSpecimen,
    HeaterProgram,
)
from capa.runtime.emissions import ProcedureTick
from capa.ui.forms import build_form
from capa.ui.tabs.setup_sections._base import SectionWidget

if TYPE_CHECKING:
    from capa.ui.forms.from_model import ModelForm
    from capa.ui.state import RunController
    from capa.ui.tabs.setup_state import SetupDraft


# ---------------------------------------------------------------------------
# Metadata panes.
# ---------------------------------------------------------------------------


_PANES: tuple[tuple[str, str, type[BaseModel]], ...] = (
    ("specimen", "Specimen", CapaSpecimen),
    ("program", "Heater program", HeaterProgram),
    ("atmosphere", "Atmosphere", Atmosphere),
)
"""``(metadata key, pane title, model)`` for each dedicated pane."""

_RecordView: type[BaseModel] = create_model(
    "_RecordView",
    **{
        name: (info.annotation, info)
        for name, info in CapaPyrolysisMetadata.model_fields.items()
        if name not in {key for key, _title, _model in _PANES}
    },  # type: ignore[call-overload]
)
"""The top-level metadata fields without a dedicated pane (the
downstream analyzer and SOP revision). Derived from the model so a new
top-level field gets an editor without touching this module."""


_DEFAULT_FLUX_DIR = "configs/calibrations/flux"
"""Where :func:`save_artifact` writes daily tune artifacts. Mirrored
in :class:`~capa.experiment.procedures.builtin.heat_flux_tune.HeatFluxTuneConfig.persist_dir`.
The Setup tab's autofill button reads from the same path so an artifact
written by the procedure is discoverable immediately."""


def _without_none(value: Any) -> Any:
    """Drop ``None`` entries from nested dicts.

    Every optional field in the CAPA models defaults to ``None``, so an
    absent key and ``None`` mean the same thing; leaving them out keeps
    the saved YAML as terse as a hand-written one (and TOML-safe).
    """
    if isinstance(value, Mapping):
        return {k: _without_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list | tuple):
        return [_without_none(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Required-mapping panel.
# ---------------------------------------------------------------------------


class _MappingRow:
    """One row of the required-mapping panel.

    Each row owns a combobox of acceptable channels + a status chip
    label. The section iterates rows on refresh, repopulating both.
    """

    __slots__ = ("chip", "combo", "group", "label", "required")

    def __init__(
        self,
        *,
        group: str,
        required: bool,
        combo: QComboBox,
        chip: QLabel,
        label: QLabel,
    ) -> None:
        self.group = group
        self.required = required
        self.combo = combo
        self.chip = chip
        self.label = label


# ---------------------------------------------------------------------------
# Section widget.
# ---------------------------------------------------------------------------


def _bordered(title: str, hint: str | None = None) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setFrameShape(QFrame.Shape.StyledPanel)
    frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    box = QVBoxLayout(frame)
    box.setContentsMargins(8, 8, 8, 8)
    box.setSpacing(6)
    header = QLabel(title, frame)
    header.setStyleSheet("font-weight: 600;")
    box.addWidget(header)
    if hint:
        note = QLabel(hint, frame)
        note.setWordWrap(True)
        note.setStyleSheet("color: #666;")
        box.addWidget(note)
    return frame, box


class CapaProfileSection(SectionWidget):
    """Curated CAPA pyrolysis profile editor."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._draft: SetupDraft | None = None
        self._suppress = False
        self._active = False
        self._mapping_rows: list[_MappingRow] = []
        self._pane_forms: dict[str, ModelForm] = {}
        # Hold-mode post-tune apply prompt state. ``_hold_prompt`` holds
        # the live non-modal QMessageBox so Qt doesn't garbage-collect
        # the dialog out from under us; ``_hold_prompt_fired`` latches
        # for the current run so a procedure that keeps emitting
        # ``phase="holding"`` ticks (or the UI re-receiving the same
        # tick on resubscribe) doesn't re-pop the dialog.
        self._controller: RunController | None = None
        self._hold_prompt: QMessageBox | None = None
        self._hold_prompt_fired = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        title = QLabel("CAPA Profile", self)
        title.setStyleSheet("font-size: 14pt; font-weight: 600;")
        outer.addWidget(title)

        # Shown instead of the editor when the experiment has no CAPA
        # profile.
        self._inactive = QWidget(self)
        inactive_box = QVBoxLayout(self._inactive)
        inactive_box.setContentsMargins(0, 0, 0, 0)
        self._inactive_label = QLabel(self._inactive)
        self._inactive_label.setWordWrap(True)
        self._inactive_label.setStyleSheet("color: #555;")
        inactive_box.addWidget(self._inactive_label)
        self._add_profile_btn = QPushButton("Add CAPA profile", self._inactive)
        self._add_profile_btn.setToolTip(
            "Attach the CAPA pyrolysis profile to this experiment. The specimen "
            "starts from the current sample id, material, mass, thickness and notes."
        )
        self._add_profile_btn.clicked.connect(self._on_add_profile_clicked)
        inactive_box.addWidget(self._add_profile_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        outer.addWidget(self._inactive)

        self._editor = QWidget(self)
        editor_box = QVBoxLayout(self._editor)
        editor_box.setContentsMargins(0, 0, 0, 0)
        editor_box.setSpacing(10)
        outer.addWidget(self._editor)

        for key, pane_title, model_cls in _PANES:
            hint = (
                "The experiment's sample block (run id, catalog entry) is filled from here."
                if key == "specimen"
                else None
            )
            frame, box = _bordered(pane_title, hint)
            form = build_form(model_cls, parent=frame)
            form.valuesChanged.connect(self._on_metadata_changed)
            box.addWidget(form)
            self._pane_forms[key] = form
            if key == "program":
                box.addLayout(self._build_tune_row(frame))
            editor_box.addWidget(frame)

        record_frame, record_box = _bordered("Analyzer & SOP")
        self._record_form = build_form(_RecordView, parent=record_frame)
        self._record_form.valuesChanged.connect(self._on_metadata_changed)
        record_box.addWidget(self._record_form)
        editor_box.addWidget(record_frame)

        # Required channel mappings.
        mapping_frame, mapping_box = _bordered("Required channel mappings")
        self._mapping_form = QFormLayout()
        self._mapping_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        mapping_box.addLayout(self._mapping_form)
        editor_box.addWidget(mapping_frame)

        outer.addStretch(1)

        self._build_mapping_rows()
        self._show_active(False)

    def _build_tune_row(self, parent: QWidget) -> QHBoxLayout:
        """Tune-artifact toolbar under the heater-program form.

        The artifact maps heater setpoint ↔ measured flux; clicking
        "Apply latest" reads the operator's target_heat_flux_kw_m2 and
        writes back the interpolated heater_setpoint_c +
        flux_calibration_ref. The artifact lookup is intentionally
        on-demand (button), not automatic — the operator owns the
        decision to overwrite the current setpoint.
        """
        tune_row = QHBoxLayout()
        tune_row.setContentsMargins(0, 0, 0, 0)
        tune_row.setSpacing(6)
        self._tune_status_label = QLabel("(no tune artifact loaded)", parent)
        self._tune_status_label.setStyleSheet("color: #666; font-style: italic;")
        tune_row.addWidget(self._tune_status_label, stretch=1)
        apply_latest_btn = QPushButton("Apply latest tune", parent)
        apply_latest_btn.setToolTip(
            "Look up the most recent on-disk HeatFluxTuneArtifact under "
            f"{_DEFAULT_FLUX_DIR!s}, interpolate to the current "
            "target_heat_flux_kw_m2, and write the result into "
            "heater_setpoint_c + flux_calibration_ref."
        )
        apply_latest_btn.clicked.connect(self._on_apply_latest_tune_clicked)
        tune_row.addWidget(apply_latest_btn)
        browse_btn = QPushButton("Browse…", parent)
        browse_btn.setToolTip("Pick a specific tune artifact .toml from disk.")
        browse_btn.clicked.connect(self._on_browse_tune_clicked)
        tune_row.addWidget(browse_btn)
        clear_btn = QPushButton("Clear ref", parent)
        clear_btn.setToolTip("Clear flux_calibration_ref (heater_setpoint_c is left as-is).")
        clear_btn.clicked.connect(self._on_clear_tune_ref_clicked)
        tune_row.addWidget(clear_btn)
        return tune_row

    @property
    def _heater_form(self) -> ModelForm:
        return self._pane_forms["program"]

    # -- SectionWidget API --------------------------------------------------

    def set_draft(self, draft: SetupDraft) -> None:
        """Replace the in-progress draft."""
        self._draft = draft
        self.refresh()

    def refresh(self) -> None:
        """Recompute the form from the current draft."""
        if self._draft is None:
            return
        exp = self._draft.document.experiment_payload
        if not is_capa_profile(exp):
            profile = exp.get("domain_profile")
            if isinstance(profile, Mapping):
                self._inactive_label.setText(
                    f"This experiment uses the {profile.get('id')!r} domain profile. "
                    "This section edits the CAPA pyrolysis profile only."
                )
                self._add_profile_btn.setVisible(False)
            else:
                self._inactive_label.setText(
                    "This experiment has no domain profile, so there is no specimen, "
                    "heater-program or atmosphere record to edit."
                )
                self._add_profile_btn.setVisible(True)
            self._show_active(False)
            return

        metadata = exp["domain_profile"].get("metadata") or {}
        if not isinstance(metadata, Mapping):
            metadata = {}
        self._suppress = True
        try:
            for key, form in self._pane_forms.items():
                block = metadata.get(key)
                form.set_values(dict(block) if isinstance(block, Mapping) else {}, replace=True)
            self._record_form.set_values(dict(metadata), replace=True)
            self._refresh_mapping_rows()
        finally:
            self._suppress = False
        self._show_active(True)

    def payload(self) -> dict[str, object] | None:
        """Emit a multi-key payload — channels go to hardware, profile and
        sample to experiment. The Setup tab's router splits on the key."""
        if self._draft is None or not self._active:
            return None
        exp = self._draft.document.experiment_payload
        domain_profile = self._compose_domain_profile()
        specimen = domain_profile["metadata"]["specimen"]
        current_sample = exp.get("sample")
        return {
            "domain_profile": domain_profile,
            "sample": sample_from_specimen(
                specimen,
                current_sample if isinstance(current_sample, Mapping) else None,
            ),
            "channels": self._compose_channels_with_mappings(),
        }

    # -- slots: profile on/off ----------------------------------------------

    def _show_active(self, active: bool) -> None:
        self._active = active
        self._editor.setVisible(active)
        self._inactive.setVisible(not active)

    def _on_add_profile_clicked(self) -> None:
        """Attach the CAPA profile, seeding the specimen from ``sample``.

        The rest of the metadata starts at the model defaults (required
        numbers unset), so the Problems panel lists what still needs
        filling in.
        """
        if self._draft is None:
            return
        sample = self._draft.document.experiment_payload.get("sample")
        seed: dict[str, Any] = {}
        if isinstance(sample, Mapping):
            for specimen_key, sample_key in SPECIMEN_SAMPLE_FIELDS:
                value = sample.get(sample_key)
                if value is not None and value != "":
                    seed[specimen_key] = value
        self._suppress = True
        try:
            for key, form in self._pane_forms.items():
                form.set_values(seed if key == "specimen" else {}, replace=True)
            self._record_form.set_values({}, replace=True)
            self._refresh_mapping_rows()
        finally:
            self._suppress = False
        self._show_active(True)
        self.valuesChanged.emit()

    # -- slots: tune-artifact autofill --------------------------------------

    def _on_apply_latest_tune_clicked(self) -> None:
        """Load ``configs/calibrations/flux/latest.toml`` and apply it.

        Failure modes:

        * No directory / no ``latest.toml`` pointer → friendly toast with
          a "run a tune first" hint.
        * Pointer references a missing artifact → toast with the bad id.
        * Artifact loaded but doesn't bracket the current target →
          toast naming the artifact's bracket range so the operator
          knows whether to re-tune or pick a different target.
        """
        flux_dir = self._resolve_flux_dir()
        try:
            artifact = load_latest(flux_dir)
        except TuneArtifactError as exc:
            QMessageBox.warning(
                self,
                "Tune artifact unreadable",
                f"Could not load the latest tune artifact at {flux_dir}:\n\n{exc}",
            )
            return
        if artifact is None:
            QMessageBox.information(
                self,
                "No tune artifact found",
                f"No HeatFluxTuneArtifact found under {flux_dir}. Run a heat-flux tune first.",
            )
            return
        self._apply_artifact(artifact)

    def _on_browse_tune_clicked(self) -> None:
        """Open a file dialog and apply the chosen artifact."""
        flux_dir = self._resolve_flux_dir()
        start_dir = str(flux_dir) if flux_dir.is_dir() else ""
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Pick a tune artifact",
            start_dir,
            "Tune artifact (*.toml)",
        )
        if not path_str:
            return
        try:
            artifact = load_artifact(Path(path_str))
        except TuneArtifactError as exc:
            QMessageBox.warning(
                self,
                "Tune artifact unreadable",
                f"Could not load {path_str}:\n\n{exc}",
            )
            return
        self._apply_artifact(artifact)

    def _on_clear_tune_ref_clicked(self) -> None:
        """Clear ``flux_calibration_ref`` only; leave ``heater_setpoint_c``
        alone so an operator who is about to re-enter the setpoint by
        hand doesn't lose context."""
        if self._heater_form.values().get("flux_calibration_ref") is None:
            return
        self._suppress = True
        try:
            self._heater_form.set_values({"flux_calibration_ref": None})
        finally:
            self._suppress = False
        self._tune_status_label.setText("(flux_calibration_ref cleared)")
        self._tune_status_label.setStyleSheet("color: #666; font-style: italic;")
        self.valuesChanged.emit()

    def _apply_artifact(self, artifact: HeatFluxTuneArtifact) -> None:
        """Interpolate ``artifact`` against the form's current target.

        Writes ``heater_setpoint_c`` + ``flux_calibration_ref`` back into
        the heater form on success. On out-of-bracket targets, leaves
        the form untouched and updates the inline status label with the
        artifact's bracket so the operator knows what to fix.
        """
        target = self._heater_form.values().get("target_heat_flux_kw_m2")
        if not isinstance(target, int | float) or target <= 0:
            QMessageBox.information(
                self,
                "No target declared",
                "Set ``target_heat_flux_kw_m2`` first; the tune artifact is "
                "applied by interpolating against the declared target.",
            )
            return
        setpoint = artifact.setpoint_for_target(target)
        accepted = [p for p in artifact.points if p.accepted]
        if setpoint is None:
            if accepted:
                lo = min(p.target_flux_kw_m2 for p in accepted)
                hi = max(p.target_flux_kw_m2 for p in accepted)
                bracket = f"{lo:g}–{hi:g}"
            else:
                bracket = "(no accepted points)"
            self._tune_status_label.setText(
                f"⚠ artifact {artifact.id!r} does not bracket {target:g} kW/m² "
                f"(covered range: {bracket})"
            )
            self._tune_status_label.setStyleSheet("color: #b33;")
            QMessageBox.warning(
                self,
                "Target out of bracket",
                f"The tune artifact {artifact.id!r} covers targets {bracket} kW/m². "
                f"It cannot extrapolate to {target:g} kW/m² — run a new tune that "
                f"includes this target.",
            )
            return
        self._suppress = True
        try:
            self._heater_form.set_values(
                {"heater_setpoint_c": float(setpoint), "flux_calibration_ref": artifact.id}
            )
        finally:
            self._suppress = False
        self._tune_status_label.setText(
            f"✓ applied {artifact.id} (target {target:g} → setpoint {setpoint:.1f} °C)"
        )
        self._tune_status_label.setStyleSheet("color: #2a7;")
        self.valuesChanged.emit()

    # -- slots: post-tune apply prompt --------------------------------------

    def set_run_controller(self, controller: RunController) -> None:
        """Attach a run controller for the post-tune apply prompt.

        Subscribes to :attr:`RunController.procedure_tick_received` so a
        successful Heat-Flux Tune that emits ``phase="holding"`` can
        offer to write its converged target/setpoint pair into this
        section's heater-program form. Idempotent: re-attaching the
        same controller is a no-op.

        The connection uses ``contextlib.suppress(AttributeError)`` to
        survive stub controllers in tests that don't define the signal
        — the section degrades to the old "no hold prompt" behavior
        rather than refusing to render.
        """
        if self._controller is controller:
            return
        self._controller = controller
        with contextlib.suppress(AttributeError):
            controller.procedure_tick_received.connect(self._on_procedure_tick)

    def _on_procedure_tick(self, tick: object) -> None:
        """Listen for ``phase="holding"`` and offer to apply the held SP.

        Non-holding ticks reset the prompt-fired latch so the next run
        in the same session gets a fresh dialog when its hold tick
        lands. The dock's tick path uses the same signal; this
        subscriber is independent.
        """
        if not isinstance(tick, ProcedureTick):
            return
        if tick.procedure_id != HEAT_FLUX_TUNE_PROCEDURE_ID:
            return
        payload = dict(tick.payload)
        if payload.get("phase") != "holding":
            self._hold_prompt_fired = False
            return
        if self._hold_prompt_fired:
            return
        try:
            target_f = float(payload.get("target_kw_m2"))  # type: ignore[arg-type]
            sp_f = float(payload.get("commanded_setpoint_c"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        self._hold_prompt_fired = True
        self._prompt_apply_hold(target_f, sp_f)

    def _prompt_apply_hold(self, target_kw_m2: float, setpoint_c: float) -> None:
        """Raise the non-modal "apply held tune?" dialog.

        Skipped when the experiment has no CAPA profile — there is no
        heater-program form to write to. Stored as ``self._hold_prompt``
        until dismissed so Qt doesn't garbage-collect the dialog while
        the operator is reading it.
        """
        if not self._active:
            return
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setWindowTitle("Apply held tune?")
        msg.setText(
            f"Apply this tune's {target_kw_m2:g} kW/m² → "
            f"{setpoint_c:.1f} °C to the active method's "
            f"heater_setpoint_c?"
        )
        msg.setInformativeText(
            "The tune left the heater holding at this setpoint. Apply "
            "writes the converged value into the heater-program form; "
            "Dismiss leaves the form untouched (you can apply later "
            "via 'Apply latest tune')."
        )
        msg.setStandardButtons(
            QMessageBox.StandardButton.Apply | QMessageBox.StandardButton.Discard
        )
        msg.setDefaultButton(QMessageBox.StandardButton.Apply)
        msg.setWindowModality(Qt.WindowModality.NonModal)
        msg.buttonClicked.connect(
            lambda btn, m=msg, t=target_kw_m2, s=setpoint_c: self._on_hold_prompt_button(
                btn, m, t, s
            )
        )
        self._hold_prompt = msg
        msg.show()

    def _on_hold_prompt_button(
        self,
        button: QAbstractButton,
        msg: QMessageBox,
        target_kw_m2: float,
        setpoint_c: float,
    ) -> None:
        if msg.standardButton(button) == QMessageBox.StandardButton.Apply:
            self._apply_held_values(target_kw_m2, setpoint_c)
        msg.deleteLater()
        if self._hold_prompt is msg:
            self._hold_prompt = None

    def _apply_held_values(self, target_kw_m2: float, setpoint_c: float) -> None:
        """Write the held ``(target, setpoint)`` pair into the heater form.

        Mirrors :meth:`_apply_artifact` but skips the artifact-lookup
        path — the held tune just produced exactly the values we want,
        so we can write them through directly rather than re-loading
        the on-disk artifact and interpolating. ``flux_calibration_ref``
        is intentionally left untouched: the artifact-based "Apply
        latest tune" button is still the right way to set that.
        """
        self._suppress = True
        try:
            self._heater_form.set_values(
                {
                    "target_heat_flux_kw_m2": float(target_kw_m2),
                    "heater_setpoint_c": float(setpoint_c),
                }
            )
        finally:
            self._suppress = False
        self._tune_status_label.setText(
            f"✓ applied held tune (target {target_kw_m2:g} kW/m² → setpoint {setpoint_c:.1f} °C)"
        )
        self._tune_status_label.setStyleSheet("color: #2a7;")
        self.valuesChanged.emit()

    def _resolve_flux_dir(self) -> Path:
        """Project root + ``configs/calibrations/flux``.

        Resolved relative to the current working directory at section
        construction time — mirrors how the procedure writes its
        artifact. A future enhancement could pick this up from a
        project-level config key, but the current single-rig
        deployment doesn't need that flexibility.
        """
        base = Path.cwd() / _DEFAULT_FLUX_DIR
        return base.resolve()

    # -- slots --------------------------------------------------------------

    def _on_metadata_changed(self) -> None:
        if self._suppress:
            return
        self.valuesChanged.emit()

    def _on_mapping_changed(self, group: str) -> None:
        if self._suppress:
            return
        # When a mapping picks a new channel, clear the old assignment
        # for the same group on every other channel (single-channel
        # mapping semantics — for multi-channel TC arrays the operator
        # edits the channel metadata directly in the Channels section).
        if self._draft is None:
            return
        # Repaint chips immediately so the operator sees the effect.
        self._refresh_chip_for(group)
        self.valuesChanged.emit()

    # -- internals: mapping rows --------------------------------------------

    def _build_mapping_rows(self) -> None:
        # Required groups first (rendered in deterministic order so the
        # operator's eye triangulates between rebuilds), then optional.
        for group in CAPA_REQUIRED_GROUPS:
            self._add_mapping_row(group, required=True)
        for group in CAPA_OPTIONAL_GROUPS:
            self._add_mapping_row(group, required=False)

    def _add_mapping_row(self, group: str, *, required: bool) -> None:
        label = QLabel(self._mapping_label(group, required))
        combo = QComboBox()
        combo.setEditable(False)
        combo.currentIndexChanged.connect(lambda _idx=0, g=group: self._on_mapping_changed(g))
        chip = QLabel("—")
        chip.setMinimumWidth(20)
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(combo, stretch=1)
        row_layout.addWidget(chip)
        self._mapping_form.addRow(label, row)
        self._mapping_rows.append(
            _MappingRow(group=group, required=required, combo=combo, chip=chip, label=label)
        )

    def _mapping_label(self, group: str, required: bool) -> str:
        spec = CAPA_REQUIRED_GROUPS.get(group) if required else CAPA_OPTIONAL_GROUPS.get(group)
        kinds = "/".join(spec or ())
        tag = "required" if required else "optional"
        return f"{group} ({tag}, {kinds}):"

    def _refresh_mapping_rows(self) -> None:
        channels = self._current_channels()
        mappings = current_capa_mappings(channels)
        for row in self._mapping_rows:
            row.combo.blockSignals(True)
            try:
                row.combo.clear()
                # Always include a "(none)" sentinel so the operator can
                # clear an optional mapping.
                row.combo.addItem("(none)", "")
                allowed_kinds = (
                    CAPA_REQUIRED_GROUPS.get(row.group)
                    if row.required
                    else CAPA_OPTIONAL_GROUPS.get(row.group)
                )
                for channel in channels:
                    if not _channel_kind_matches(channel, allowed_kinds):
                        continue
                    name = channel.get("name", "")
                    if isinstance(name, str):
                        row.combo.addItem(name, name)
                # Reflect the currently-mapped channel (first match wins
                # for the single-channel UX).
                current_names = mappings.get(row.group) or []
                current = current_names[0] if current_names else ""
                idx = row.combo.findData(current)
                row.combo.setCurrentIndex(idx if idx >= 0 else 0)
            finally:
                row.combo.blockSignals(False)
            self._refresh_chip_for(row.group)

    def _refresh_chip_for(self, group: str) -> None:
        for row in self._mapping_rows:
            if row.group != group:
                continue
            selected = row.combo.currentData()
            if selected:
                row.chip.setText("✓")
                row.chip.setStyleSheet("color: #2a7;")
            elif row.required:
                row.chip.setText("✗")
                row.chip.setStyleSheet("color: #b33;")
            else:
                row.chip.setText("–")
                row.chip.setStyleSheet("color: #888;")
            break

    # -- internals: payload composition ------------------------------------

    def _current_channels(self) -> list[dict[str, Any]]:
        if self._draft is None:
            return []
        hw = self._draft.document.hardware_payload
        channels = hw.get("channels") if isinstance(hw, dict) else None
        if isinstance(channels, list):
            return [dict(c) for c in channels if isinstance(c, dict)]
        return []

    def _compose_domain_profile(self) -> dict[str, Any]:
        """Build ``domain_profile`` from the panes.

        Keeps the block's other keys (``standard_refs``) and any
        metadata keys the panes don't own (the ``_``-prefixed preflight
        knobs) so a save round-trips them.
        """
        exp = self._draft.document.experiment_payload if self._draft is not None else {}
        existing = exp.get("domain_profile")
        out: dict[str, Any] = (
            {k: v for k, v in existing.items() if k != "metadata"}
            if isinstance(existing, Mapping)
            else {}
        )
        out["id"] = CAPA_PROFILE_ID
        current = existing.get("metadata") if isinstance(existing, Mapping) else None
        metadata: dict[str, Any] = dict(current) if isinstance(current, Mapping) else {}
        for key, form in self._pane_forms.items():
            metadata[key] = _without_none(form.values())
        for key, value in self._record_form.values().items():
            if value is None:
                metadata.pop(key, None)
            else:
                metadata[key] = _without_none(value)
        out["metadata"] = metadata
        return out

    def _compose_channels_with_mappings(self) -> list[dict[str, Any]]:
        """Project the current channel list with any mapping-row changes.

        Each row's selected channel becomes the sole owner of that
        ``capa_group`` value — other channels carrying it are cleared.
        ``capa_group``s for groups the section doesn't manage (e.g. a
        custom plugin-defined group) are preserved as-is.
        """
        channels = self._current_channels()
        managed_groups = set(CAPA_REQUIRED_GROUPS) | set(CAPA_OPTIONAL_GROUPS)
        # Mapping from group -> selected channel name (empty string =
        # cleared) read off the combo rows.
        selected_for: dict[str, str] = {}
        for row in self._mapping_rows:
            data = row.combo.currentData()
            selected_for[row.group] = data if isinstance(data, str) else ""

        for channel in channels:
            metadata = dict(channel.get("metadata") or {})
            group = metadata.get("capa_group")
            if isinstance(group, str) and group in managed_groups:
                # Was the operator assignment changed?
                expected_owner = selected_for.get(group, "")
                if channel.get("name") != expected_owner:
                    # This channel is no longer the canonical owner; drop
                    # the group. (Multi-channel TC arrays survive only
                    # via direct edits in the Channels section.)
                    metadata.pop("capa_group", None)
            # If this channel is the newly-selected owner for some
            # managed group, set the metadata accordingly.
            for group_name, owner in selected_for.items():
                if owner and channel.get("name") == owner:
                    metadata["capa_group"] = group_name
            if metadata:
                channel["metadata"] = metadata
            else:
                channel.pop("metadata", None)
        return channels


def _channel_kind_matches(channel: dict[str, Any], allowed_kinds: tuple[str, ...] | None) -> bool:
    if not allowed_kinds:
        return True
    kind = channel.get("kind", "")
    if hasattr(kind, "value"):
        kind = kind.value
    if not isinstance(kind, str):
        return False
    # Accept "tc" / "thermocouple" interchangeably (both StrEnum values
    # map to the same physical channel; the validator does the same).
    normalised = "tc" if kind == ChannelKind.THERMOCOUPLE.value else kind
    return normalised in allowed_kinds or kind in allowed_kinds


__all__ = ["CapaProfileSection"]
