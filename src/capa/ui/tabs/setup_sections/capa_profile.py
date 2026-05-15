"""CAPA Profile section — curated profile editor.

Three small metadata panes (Specimen / Heater Program / Atmosphere)
sit above the required-mapping panel that ties each CAPA
group to a hardware channel. The mapping panel is the operator-visible
payoff: every required group reports a green ✓ / red ✗ chip, and
selecting a channel writes ``metadata["capa_group"]`` on it without
touching the Channels section.

The metadata panes edit ``experiment_payload["domain_profile"]["metadata"]``
through view models defined locally — :class:`DomainProfileRef.metadata`
is a free-form ``dict[str, Any]`` so we curate the editor without
changing the runtime model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from capa.channels.spec import ChannelKind
from capa.config.capa_profile import (
    CAPA_OPTIONAL_GROUPS,
    CAPA_REQUIRED_GROUPS,
    current_capa_mappings,
)
from capa.ui.forms import build_form
from capa.ui.tabs.setup_sections._base import SectionWidget

if TYPE_CHECKING:
    from capa.ui.forms.from_model import ModelForm
    from capa.ui.tabs.setup_state import SetupDraft


# ---------------------------------------------------------------------------
# View models for the metadata panes.
# ---------------------------------------------------------------------------


class _SpecimenView(BaseModel):
    """Specimen metadata pane."""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    material: str = ""
    form: str = "disk"
    mass_g: float = Field(default=0.0, ge=0)
    thickness_mm: float = Field(default=0.0, ge=0)
    specimen_holder: str = ""


class _HeaterProgramView(BaseModel):
    """Heater program metadata pane."""

    model_config = ConfigDict(extra="ignore")

    target_heat_flux_kw_m2: float = Field(default=50.0, ge=0)
    heater_setpoint_c: float = Field(default=600.0, ge=0)
    flux_calibration_ref: str = ""
    ramp_rate_c_per_min: float | None = None


class _AtmosphereView(BaseModel):
    """Atmosphere metadata pane."""

    model_config = ConfigDict(extra="ignore")

    mode: str = "inert"
    """``"inert"`` / ``"oxidative"`` / ``"reducing"`` / ``"blend"``."""
    purge_species: str = "N2"
    purge_purity: str = "99.999%"
    purge_flow_lpm: float = Field(default=10.0, ge=0)
    purge_duration_s: float = Field(default=120.0, ge=0)


# Sub-dict keys under ``domain_profile.metadata`` so the three view-model
# blocks don't stomp on each other. Each block reads/writes its own key.
_METADATA_KEYS = ("specimen", "heater_program", "atmosphere")


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


def _bordered(title: str) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setFrameShape(QFrame.Shape.StyledPanel)
    frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    box = QVBoxLayout(frame)
    box.setContentsMargins(8, 8, 8, 8)
    box.setSpacing(6)
    header = QLabel(title, frame)
    header.setStyleSheet("font-weight: 600;")
    box.addWidget(header)
    return frame, box


class CapaProfileSection(SectionWidget):
    """Curated CAPA pyrolysis profile editor."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._draft: SetupDraft | None = None
        self._suppress = False
        self._mapping_rows: list[_MappingRow] = []
        self._specimen_form: ModelForm | None = None
        self._heater_form: ModelForm | None = None
        self._atmosphere_form: ModelForm | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        title = QLabel("CAPA Profile", self)
        title.setStyleSheet("font-size: 14pt; font-weight: 600;")
        outer.addWidget(title)

        # Specimen block.
        specimen_frame, specimen_box = _bordered("Specimen")
        self._specimen_form = build_form(_SpecimenView, parent=specimen_frame)
        self._specimen_form.valuesChanged.connect(lambda: self._on_metadata_changed("specimen"))
        specimen_box.addWidget(self._specimen_form)
        outer.addWidget(specimen_frame)

        # Heater Program block.
        heater_frame, heater_box = _bordered("Heater Program")
        self._heater_form = build_form(_HeaterProgramView, parent=heater_frame)
        self._heater_form.valuesChanged.connect(lambda: self._on_metadata_changed("heater_program"))
        heater_box.addWidget(self._heater_form)
        outer.addWidget(heater_frame)

        # Atmosphere block.
        atm_frame, atm_box = _bordered("Atmosphere")
        self._atmosphere_form = build_form(_AtmosphereView, parent=atm_frame)
        self._atmosphere_form.valuesChanged.connect(lambda: self._on_metadata_changed("atmosphere"))
        atm_box.addWidget(self._atmosphere_form)
        outer.addWidget(atm_frame)

        # Required channel mappings.
        mapping_frame, mapping_box = _bordered("Required channel mappings")
        self._mapping_form = QFormLayout()
        self._mapping_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        mapping_box.addLayout(self._mapping_form)
        outer.addWidget(mapping_frame)

        outer.addStretch(1)

        self._build_mapping_rows()

    # -- SectionWidget API --------------------------------------------------

    def set_draft(self, draft: SetupDraft) -> None:
        self._draft = draft
        self.refresh()

    def refresh(self) -> None:
        if self._draft is None:
            return
        exp = self._draft.document.experiment_payload
        domain_profile = exp.get("domain_profile") or {}
        if not isinstance(domain_profile, dict):
            domain_profile = {}
        metadata = domain_profile.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}

        self._suppress = True
        try:
            if self._specimen_form is not None:
                self._specimen_form.set_values(metadata.get("specimen") or {})
            if self._heater_form is not None:
                self._heater_form.set_values(metadata.get("heater_program") or {})
            if self._atmosphere_form is not None:
                self._atmosphere_form.set_values(metadata.get("atmosphere") or {})
            self._refresh_mapping_rows()
        finally:
            self._suppress = False

    def payload(self) -> dict[str, object]:
        """Emit a multi-key payload — channels go to hardware, profile to
        experiment. The Setup tab's router splits on the key."""
        out: dict[str, object] = {}
        out["domain_profile"] = self._compose_domain_profile()
        # Channels carry the capa_group mapping; only emit if we have a
        # bound draft (otherwise nothing to mutate).
        if self._draft is not None:
            out["channels"] = self._compose_channels_with_mappings()
        return out

    # -- slots --------------------------------------------------------------

    def _on_metadata_changed(self, _block: str) -> None:
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
        # Preserve the existing domain_profile dict's other keys
        # (id, standard_refs) so save round-trips.
        if self._draft is None:
            return {}
        exp = self._draft.document.experiment_payload
        existing = exp.get("domain_profile")
        if isinstance(existing, dict):
            out = {k: v for k, v in existing.items() if k != "metadata"}
        else:
            out = {"id": "capa.profiles.capa_pyrolysis"}
        metadata = (
            dict((existing or {}).get("metadata") or {}) if isinstance(existing, dict) else {}
        )
        if self._specimen_form is not None:
            metadata["specimen"] = self._specimen_form.values()
        if self._heater_form is not None:
            metadata["heater_program"] = self._heater_form.values()
        if self._atmosphere_form is not None:
            metadata["atmosphere"] = self._atmosphere_form.values()
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
