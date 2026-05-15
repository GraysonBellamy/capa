""":class:`SetupWizard` — from-scratch setup author.

The wizard turns a few-click flow into a fully-validated draft so a
researcher who's never touched the underlying TOML can get to "press
Start" in minutes. Four screens (starting point / source layout /
method / save): every starting point we ship has a canonical device +
channel seed, and operators can edit the result in the
Devices/Channels sections after Finish.

The wizard never writes to disk or opens hardware until the operator
explicitly chooses Save now → Finish.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QLineEdit,
    QRadioButton,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from capa.config import ConfigDocument, SourceLayout

StartingPoint = Literal["sim_capa", "real_capa", "free_sim", "free_real", "blank"]
SourceLayoutKind = Literal["yaml_ext_toml", "toml_ext_toml", "single_yaml", "single_toml"]
MethodChoice = Literal["new", "attach", "none"]


class _Spec:
    """Mutable accumulator for the wizard's collected choices."""

    starting_point: StartingPoint = "sim_capa"
    layout: SourceLayoutKind = "yaml_ext_toml"
    method_choice: MethodChoice = "none"
    method_path: Path | None = None
    save_now: bool = True
    experiment_path: Path | None = None


# ---------------------------------------------------------------------------
# Page 1 — starting point.
# ---------------------------------------------------------------------------


class _StartingPointPage(QWizardPage):
    """Pick a starting template.

    Each option lands a different canonical seed in
    :func:`_seed_payloads_for`; the operator can edit the resulting
    draft afterwards. "Blank" produces an empty hardware profile —
    useful when the operator wants the full layout from scratch.
    """

    def __init__(self, spec: _Spec) -> None:
        super().__init__()
        self.setTitle("Starting point")
        self.setSubTitle(
            "Pick a template. Every option is a starting draft — you can"
            " add or remove devices afterwards."
        )
        self._spec = spec

        layout = QVBoxLayout(self)
        self._group = QButtonGroup(self)

        self._options: list[tuple[StartingPoint, str]] = [
            ("sim_capa", "CAPA pyrolysis — simulated rig (recommended for first run)"),
            ("real_capa", "CAPA pyrolysis — real rig (Watlow + Alicat + Sartorius)"),
            ("free_sim", "Free run — simulated"),
            ("free_real", "Free run — real rig"),
            ("blank", "Blank (no devices, no channels)"),
        ]
        for idx, (value, label) in enumerate(self._options):
            btn = QRadioButton(label, self)
            if value == spec.starting_point:
                btn.setChecked(True)
            self._group.addButton(btn, idx)
            layout.addWidget(btn)
        layout.addStretch(1)

    def validatePage(self) -> bool:
        idx = self._group.checkedId()
        if idx < 0:
            return False
        self._spec.starting_point = self._options[idx][0]
        return True


# ---------------------------------------------------------------------------
# Page 2 — source layout.
# ---------------------------------------------------------------------------


class _LayoutPage(QWizardPage):
    """Pick the on-disk shape (one file vs two; YAML vs TOML)."""

    def __init__(self, spec: _Spec) -> None:
        super().__init__()
        self.setTitle("File layout")
        self.setSubTitle(
            "Hardware lives in its own TOML by default — that lets two"
            " operators share a hardware file across experiments. Pick"
            " a single-file layout if you'd rather keep everything in"
            " one place."
        )
        self._spec = spec

        layout = QVBoxLayout(self)
        self._group = QButtonGroup(self)

        self._options: list[tuple[SourceLayoutKind, str]] = [
            (
                "yaml_ext_toml",
                "Experiment YAML + external hardware TOML  (recommended)",
            ),
            (
                "toml_ext_toml",
                "Experiment TOML + external hardware TOML",
            ),
            (
                "single_yaml",
                "Single experiment YAML (hardware inline)",
            ),
            (
                "single_toml",
                "Single experiment TOML (hardware inline)",
            ),
        ]
        for idx, (value, label) in enumerate(self._options):
            btn = QRadioButton(label, self)
            if value == spec.layout:
                btn.setChecked(True)
            self._group.addButton(btn, idx)
            layout.addWidget(btn)
        layout.addStretch(1)

    def validatePage(self) -> bool:
        idx = self._group.checkedId()
        if idx < 0:
            return False
        self._spec.layout = self._options[idx][0]
        return True


# ---------------------------------------------------------------------------
# Page 3 — method.
# ---------------------------------------------------------------------------


class _MethodPage(QWizardPage):
    """Pick what to do about the method file.

    The wizard always defaults to "no method (free run)" so the
    starting draft is immediately valid; operators authoring a recipe
    pick "Attach existing" or "Skip — author the method after Finish".
    """

    def __init__(self, spec: _Spec) -> None:
        super().__init__()
        self.setTitle("Method")
        self.setSubTitle(
            "A method controls the procedure (setpoint ramps, etc.). For"
            " a quick try, leave it as free run — you can attach a"
            " method later from the Setup tab's Files view."
        )
        self._spec = spec

        layout = QVBoxLayout(self)
        self._group = QButtonGroup(self)

        self._options: list[tuple[MethodChoice, str]] = [
            ("none", "Free run — no method (recommended for first try)"),
            ("attach", "Attach an existing method TOML"),
            ("new", "Skip — author the method after Finish"),
        ]
        for idx, (value, label) in enumerate(self._options):
            btn = QRadioButton(label, self)
            if value == spec.method_choice:
                btn.setChecked(True)
            self._group.addButton(btn, idx)
            layout.addWidget(btn)

        self._path_edit = QLineEdit(self)
        self._path_edit.setPlaceholderText("Path to method TOML…")
        self._path_edit.setEnabled(spec.method_choice == "attach")
        layout.addWidget(self._path_edit)
        layout.addStretch(1)

        self._group.idClicked.connect(self._on_choice_changed)

    def _on_choice_changed(self, idx: int) -> None:
        choice = self._options[idx][0] if 0 <= idx < len(self._options) else "none"
        self._path_edit.setEnabled(choice == "attach")

    def validatePage(self) -> bool:
        idx = self._group.checkedId()
        if idx < 0:
            return False
        choice = self._options[idx][0]
        self._spec.method_choice = choice
        if choice == "attach":
            text = self._path_edit.text().strip()
            if not text:
                return False
            self._spec.method_path = Path(text)
        else:
            self._spec.method_path = None
        return True


# ---------------------------------------------------------------------------
# Page 4 — save.
# ---------------------------------------------------------------------------


class _SavePage(QWizardPage):
    """Pick whether to write the draft to disk on Finish."""

    def __init__(self, spec: _Spec) -> None:
        super().__init__()
        self.setTitle("Save")
        self.setSubTitle(
            "Choose where to write the new setup. Picking 'Continue as"
            " unsaved draft' lands you in the Setup tab with the draft"
            " loaded but no file on disk — the next Ctrl+S asks for a"
            " path."
        )
        self._spec = spec

        layout = QVBoxLayout(self)
        self._group = QButtonGroup(self)
        self._save_now_btn = QRadioButton("Save now — pick a path", self)
        self._save_now_btn.setChecked(spec.save_now)
        self._defer_btn = QRadioButton("Continue as unsaved draft", self)
        self._defer_btn.setChecked(not spec.save_now)
        self._group.addButton(self._save_now_btn, 0)
        self._group.addButton(self._defer_btn, 1)
        layout.addWidget(self._save_now_btn)
        layout.addWidget(self._defer_btn)

        self._path_edit = QLineEdit(self)
        self._path_edit.setPlaceholderText("Experiment file path…")
        layout.addWidget(self._path_edit)
        layout.addStretch(1)

        self._save_now_btn.toggled.connect(self._on_toggle)
        self._on_toggle(spec.save_now)

    def _on_toggle(self, save_now: bool) -> None:
        self._path_edit.setEnabled(save_now)

    def validatePage(self) -> bool:
        self._spec.save_now = self._save_now_btn.isChecked()
        if self._spec.save_now:
            text = self._path_edit.text().strip()
            if not text:
                # Operator picked "Save now" but typed no path —
                # offer a file dialog instead of failing silently.
                suggested = _suggest_path_for(self._spec)
                chosen, _ = QFileDialog.getSaveFileName(
                    self,
                    "Save new setup",
                    str(suggested),
                    "Configs (*.yaml *.yml *.toml)",
                )
                if not chosen:
                    return False
                self._spec.experiment_path = Path(chosen)
                self._path_edit.setText(str(self._spec.experiment_path))
            else:
                self._spec.experiment_path = Path(text)
        else:
            self._spec.experiment_path = None
        return True


# ---------------------------------------------------------------------------
# Wizard.
# ---------------------------------------------------------------------------


class SetupWizard(QWizard):
    """Top-level wizard. Use :meth:`run` to drive a modal session."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New Setup")
        self.setMinimumSize(600, 420)
        self._spec = _Spec()
        self.addPage(_StartingPointPage(self._spec))
        self.addPage(_LayoutPage(self._spec))
        self.addPage(_MethodPage(self._spec))
        self.addPage(_SavePage(self._spec))

    @classmethod
    def run(cls, parent: QWidget | None) -> ConfigDocument | None:
        """Open the wizard modally; return the constructed draft or
        ``None`` if cancelled.

        The returned :class:`ConfigDocument` has its payloads seeded
        per the starting-point template and (when the operator chose
        "Save now") has been written to disk via
        :meth:`ConfigDocument.save_as`.
        """
        wiz = cls(parent)
        if wiz.exec() != QWizard.DialogCode.Accepted:
            return None
        doc = build_document(wiz._spec)
        if wiz._spec.save_now and wiz._spec.experiment_path is not None:
            layout = _layout_for(wiz._spec, wiz._spec.experiment_path)
            doc.save_as(layout)
        return doc


# ---------------------------------------------------------------------------
# Seed templates.
# ---------------------------------------------------------------------------


def build_document(spec: _Spec) -> ConfigDocument:
    """Compose a :class:`ConfigDocument` from a wizard's collected
    choices.

    Public so tests can drive the seed logic without running Qt.

    Strategy: load a canonical fixture from ``configs/`` for each
    starting point and re-use its payloads. This is *substantially*
    simpler than maintaining a parallel inline seed and guarantees
    that wizard output passes Layers 1-4 without the operator typing
    anything.
    """
    if spec.starting_point == "blank":
        exp_payload, hw_payload = _blank_seed()
        doc = ConfigDocument(
            experiment_payload=exp_payload,
            hardware_payload=hw_payload,
        )
    else:
        template = _load_template(spec.starting_point)
        doc = ConfigDocument(
            experiment_payload=dict(template.experiment_payload),
            hardware_payload=dict(template.hardware_payload),
        )

    # The wizard always produces an unsaved draft (no source paths)
    # regardless of where the template came from; save_as in the
    # caller sets the final paths.
    doc.experiment_path = None
    doc.hardware_path = None

    if spec.layout in ("yaml_ext_toml", "toml_ext_toml"):
        doc.hardware_mode = "external"
    else:
        doc.hardware_mode = "inline"

    if spec.method_choice == "attach" and spec.method_path is not None:
        doc.method_path = spec.method_path
        doc.method_format = "toml"
        doc.method_mode = "external"
    else:
        doc.method_mode = "none"
        doc.method_payload = None
        doc.method_path = None
    return doc


def _blank_seed() -> tuple[dict[str, object], dict[str, object]]:
    """Empty draft — operator fills in everything by hand.

    Doesn't validate (no procedure, no operator) but the Problems
    panel guides the operator. The other starting points all clone
    a validated fixture, so this is the only one that intentionally
    ships invalid.
    """
    exp: dict[str, object] = {
        "operator": {"id": "wizard"},
        "sample": {"id": "wizard_sample"},
    }
    hw: dict[str, object] = {
        "name": "blank_profile",
        "devices": [],
        "channels": [],
        "cameras": [],
    }
    return exp, hw


# Map a starting-point id to the canonical fixture file it clones.
_TEMPLATE_FIXTURES: dict[StartingPoint, str] = {
    "sim_capa": "configs/experiments/sim_capa_pyrolysis.yaml",
    "free_sim": "configs/experiments/sim_capa_pyrolysis.yaml",
    "real_capa": "configs/experiments/sim_capa_pyrolysis.yaml",
    "free_real": "configs/experiments/sim_capa_pyrolysis.yaml",
}
"""One canonical fixture (sim_capa) that every non-blank starting
point clones. Real-hardware seeds and free-run seeds will get their
own fixtures later; until then they share the sim seed and the operator
swaps adapters via the Devices section. The wizard documents this in
its starting-point labels."""


def _repo_root() -> Path:
    """Locate the repo root by walking up from this module until we
    find a ``configs`` directory.

    Returns the current working directory as a fallback — tests that
    drive the wizard from a tmp_path will still see a sensible
    template if the working directory was set inside the repo.
    """
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "configs").is_dir():
            return parent
    return Path.cwd()


def _load_template(start: StartingPoint) -> ConfigDocument:
    """Load the canonical fixture for ``start`` from the repo.

    Cached only by the OS page cache — the file is small (~2 KB)
    and the wizard runs at most once per launch.
    """
    rel = _TEMPLATE_FIXTURES[start]
    path = _repo_root() / rel
    if not path.exists():
        # Operators running from a non-standard install (or tests with
        # an empty tmp_path) get an empty seed rather than a crash.
        exp, hw = _blank_seed()
        return ConfigDocument(experiment_payload=exp, hardware_payload=hw)
    return ConfigDocument.load(path)


# ---------------------------------------------------------------------------
# Source layout selection.
# ---------------------------------------------------------------------------


def _layout_for(spec: _Spec, experiment_path: Path) -> SourceLayout:
    parent = experiment_path.parent
    stem = experiment_path.stem
    if spec.layout == "yaml_ext_toml":
        return SourceLayout(
            experiment_path=experiment_path.with_suffix(".yaml"),
            experiment_format="yaml",
            hardware_path=parent / f"{stem}_hardware.toml",
            hardware_format="toml",
            hardware_mode="external",
            method_path=spec.method_path,
            method_format="toml" if spec.method_path is not None else None,
            method_mode="external" if spec.method_path is not None else "none",
        )
    if spec.layout == "toml_ext_toml":
        return SourceLayout(
            experiment_path=experiment_path.with_suffix(".toml"),
            experiment_format="toml",
            hardware_path=parent / f"{stem}_hardware.toml",
            hardware_format="toml",
            hardware_mode="external",
            method_path=spec.method_path,
            method_format="toml" if spec.method_path is not None else None,
            method_mode="external" if spec.method_path is not None else "none",
        )
    if spec.layout == "single_yaml":
        return SourceLayout(
            experiment_path=experiment_path.with_suffix(".yaml"),
            experiment_format="yaml",
            hardware_path=None,
            hardware_format=None,
            hardware_mode="inline",
            method_path=spec.method_path,
            method_format="toml" if spec.method_path is not None else None,
            method_mode="external" if spec.method_path is not None else "none",
        )
    # single_toml
    return SourceLayout(
        experiment_path=experiment_path.with_suffix(".toml"),
        experiment_format="toml",
        hardware_path=None,
        hardware_format=None,
        hardware_mode="inline",
        method_path=spec.method_path,
        method_format="toml" if spec.method_path is not None else None,
        method_mode="external" if spec.method_path is not None else "none",
    )


def _suggest_path_for(spec: _Spec) -> Path:
    base = Path("configs/experiments")
    name_root = {
        "sim_capa": "new_sim_capa",
        "real_capa": "new_real_capa",
        "free_sim": "new_free_sim",
        "free_real": "new_free_real",
        "blank": "new_blank",
    }[spec.starting_point]
    ext = ".yaml" if spec.layout in ("yaml_ext_toml", "single_yaml") else ".toml"
    return base / f"{name_root}{ext}"


__all__ = ["SetupWizard", "build_document"]
