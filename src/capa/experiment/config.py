""":class:`ExperimentConfig` and the nested ``HardwareProfile``, ``StoragePolicy``,
``SafetyPolicy``, ``SampleInfo`` models.

Plan §5.4. The full run recipe — everything needed to launch a run. Pydantic-
validated, YAML/TOML on disk, snapshotted into the run bundle.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ruamel.yaml import YAML

from capa.channels.spec import ChannelSpec
from capa.core.errors import ConfigError
from capa.devices.camera.base import CameraSpec
from capa.experiment.method import Method

# ---------------------------------------------------------------------------
# Hardware profile — devices and channels.
# ---------------------------------------------------------------------------


class DeviceConfig(BaseModel):
    """Per-device adapter configuration.

    Adapter-specific knobs live under ``params``. The adapter-side Pydantic
    model parses them at adapter construction; the outer ExperimentConfig
    only requires ``adapter`` and ``name`` to be coherent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    """Adapter-assigned device name. Used as the ``device`` key in
    :class:`~capa.channels.spec.SourceBinding` variants."""
    adapter: str
    """Module path or registered id (``"capa.devices.sim.alicat_sim"``,
    ``"capa.devices.alicat"``)."""
    params: dict[str, Any] = Field(default_factory=dict)
    """Adapter-specific parameters: serial port, baud rate, polling rate, etc."""


class HardwareProfile(BaseModel):
    """Devices + channels for a single rig.

    Plan §4: lives under ``configs/hardware/`` as one TOML per rig setup.
    Cross-checked against :class:`ExperimentConfig.method` to make sure every
    setpoint target resolves to a real channel.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    devices: tuple[DeviceConfig, ...] = Field(default_factory=tuple)
    channels: tuple[ChannelSpec, ...] = Field(default_factory=tuple)
    cameras: tuple[CameraSpec, ...] = Field(default_factory=tuple)
    """Per-rig camera entries (plan §12). Cameras are peers of devices, not
    a subtype: they own their own output container, emit frame receipts +
    health snapshots rather than ``ChannelSample``\\ s, and are wired into
    the engine task group through a parallel construction pass."""

    @model_validator(mode="after")
    def _check_unique(self) -> HardwareProfile:
        device_names = [d.name for d in self.devices]
        if len(device_names) != len(set(device_names)):
            raise ConfigError("hardware profile: duplicate device names")
        channel_names = [c.name for c in self.channels]
        if len(channel_names) != len(set(channel_names)):
            raise ConfigError("hardware profile: duplicate channel names")
        camera_names = [c.name for c in self.cameras]
        if len(camera_names) != len(set(camera_names)):
            raise ConfigError("hardware profile: duplicate camera names")
        # cameras share the device-name namespace because both surface as
        # "physical things on the rig" in operator-facing logs and manifests.
        if set(camera_names) & set(device_names):
            raise ConfigError(
                "hardware profile: camera and device names overlap "
                f"({sorted(set(camera_names) & set(device_names))})"
            )
        # every channel binding must reference a known device (except DerivedBinding)
        device_set = set(device_names)
        for ch in self.channels:
            binding = ch.source
            device = getattr(binding, "device", None)
            if device is None:
                continue  # derived channels have no device
            if device not in device_set:
                raise ConfigError(
                    f"channel {ch.name!r} binds to device {device!r} which is "
                    f"not declared in hardware.devices"
                )
        return self

    def channel_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.channels)

    def camera_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.cameras)


# ---------------------------------------------------------------------------
# Procedure / domain-profile / calibration references.
# ---------------------------------------------------------------------------


class ProcedureRef(BaseModel):
    """Reference to a registered procedure plugin.

    Plan §11. The plugin id is matched at startup against ``plugins.lock``;
    the version constraint follows PEP 440.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    """e.g. ``"capa.builtin.recipe_runner"``."""
    version: str | None = None
    """Optional version pin (PEP 440 specifier). ``None`` means "any installed
    version"; production runs typically pin."""
    config: dict[str, Any] = Field(default_factory=dict)
    """Plugin-specific config blob. Validated by the plugin's
    :attr:`Procedure.config_model` at load time (P3)."""


class DomainProfileRef(BaseModel):
    """Reference to a domain profile (cone calorimeter, future ASTM variants)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    """e.g. ``"capa.profiles.cone_calorimeter"``."""
    standard_refs: tuple[str, ...] = Field(default_factory=tuple)
    """Standard editions used (``"ASTM E1354-25"``, ``"ISO 5660-1:2015"``).
    Recorded into ``manifest.json``."""
    metadata: dict[str, Any] = Field(default_factory=dict)
    """Profile-specific metadata (cone-profile specimen fields go here)."""


class CalibrationSetRef(BaseModel):
    """Pointer to a CalibrationSet on disk.

    A snapshot of the resolved set is written into the bundle as
    ``calibration.json`` (plan §5.5) so the bundle is self-sufficient even
    if the on-disk source moves.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    revision: str | None = None


# ---------------------------------------------------------------------------
# Storage / safety / sample / operator.
# ---------------------------------------------------------------------------


class StoragePolicy(BaseModel):
    """Storage knobs.

    Plan §8.5–§8.7: in-flight flush cadence, final Parquet codec, optional
    TDMS pass-through, optional RO-Crate generation. P0a only stores the
    schema; P0b's bundle writer reads it.

    In-flight artifacts are Arrow IPC streams (``*.in-flight.arrows``); see
    ``arrow-ipc-streaming-plan.md``. IPC has no compression-level knob, so
    ``inflight_compression`` is just the codec name (e.g. ``"zstd"``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    bundle_root: str = "runs"
    inflight_flush_seconds: float = Field(default=1.0, gt=0)
    parquet_final_row_group_rows: int = Field(default=262_144, gt=0)
    inflight_compression: str = "zstd"
    parquet_final_compression: str = "zstd:6"
    enable_tdms_passthrough: bool = False
    enable_rocrate: bool = False


class SafetyRuleConfig(BaseModel):
    """Declarative safety-rule entry inside :class:`SafetyPolicy`.

    Plan §9: rule evaluator lands in P0c+; P0a stores the schema only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: str
    """Rule type discriminator: ``"max_temperature"``, ``"max_ramp_rate"``,
    ``"missing_data_timeout"``, ``"disk_space_low"``, ``"writer_lag"``,
    ``"camera_recording_failure"``, ..."""
    params: dict[str, Any] = Field(default_factory=dict)
    action: str = "warn"
    """One of ``"warn"`` / ``"pause_method"`` / ``"abort_run"`` /
    ``"safe_shutdown"`` (plan §9)."""


class SafetyPolicy(BaseModel):
    """Set of safety rules + abort-mode configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rules: tuple[SafetyRuleConfig, ...] = Field(default_factory=tuple)
    default_abort: str = "safe_shutdown"
    """What the UI's red button does by default. ``"safe_shutdown"`` runs
    the cooldown phase; ``"abort_run"`` is immediate cancel."""


class SampleInfo(BaseModel):
    """Specimen metadata captured at run-start.

    Plan §5.4. The cone-calorimeter profile (P0a's domain profile) layers
    additional required fields on top of these via its own metadata model.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    material: str | None = None
    thickness_mm: float | None = Field(default=None, gt=0)
    mass_g: float | None = Field(default=None, gt=0)
    notes: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class OperatorRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    display_name: str | None = None


# ---------------------------------------------------------------------------
# ExperimentConfig — the top-level run recipe.
# ---------------------------------------------------------------------------


class ExperimentConfig(BaseModel):
    """The full run recipe.

    Plan §5.4. YAML/TOML on disk; Pydantic-validated; snapshotted into the
    bundle as ``config.toml`` at run-arm.

    The :class:`Method` import is local to avoid pulling the method module
    into every place that imports the config schema.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    hardware: HardwareProfile
    method: Any | None = None
    """Optional :class:`~capa.experiment.method.Method`. Free runs have no
    method. Typed as ``Any`` here to keep the import surface small; validated
    in :meth:`load`."""
    method_source_path: Path | None = None
    """Original method file path when ``method:`` was a string ref in the
    experiment YAML. ``None`` when the method was inlined or absent. The UI
    uses this so editing an auto-loaded method writes back to its source file."""
    procedure: ProcedureRef
    domain_profile: DomainProfileRef | None = None
    calibration_set: CalibrationSetRef
    storage: StoragePolicy = Field(default_factory=StoragePolicy)
    safety: SafetyPolicy = Field(default_factory=SafetyPolicy)
    operator: OperatorRef
    sample: SampleInfo
    tags: tuple[str, ...] = Field(default_factory=tuple)
    custom: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_method(self) -> ExperimentConfig:
        if self.method is None:
            return self
        if isinstance(self.method, Method):
            method = self.method
        elif isinstance(self.method, dict):
            # Pydantic will sometimes hand us the dict if Any is the annotation;
            # parse and re-bind.
            method = Method.model_validate(self.method)
            object.__setattr__(self, "method", method)
        else:
            raise ConfigError(
                f"ExperimentConfig.method must be a Method or dict, got {type(self.method).__name__}"
            )
        # Cross-check: every Step.target.name must resolve to a known channel.
        channel_names = set(self.hardware.channel_names())
        for idx, step in enumerate(method.steps):
            target = getattr(step, "target", None)
            if target is None:
                continue
            if target.name not in channel_names:
                raise ConfigError(
                    f"method step {idx} ({step.kind}): target {target.name!r} "
                    f"is not in hardware.channels"
                )
        return self

    def channel_names(self) -> tuple[str, ...]:
        return self.hardware.channel_names()

    @classmethod
    def load(cls, path: str | Path) -> ExperimentConfig:
        """Load and validate an experiment file (YAML or TOML).

        File-ref resolution: when ``hardware:`` is a string, treat it as a
        path to a hardware-profile TOML and load it. Relative paths resolve
        against the experiment file's directory. ``method:`` follows the
        same rule.
        """
        source = Path(path).resolve()
        data = _load_structured_file(source)
        if not isinstance(data, dict):
            raise ConfigError(f"{source}: top-level must be a mapping")
        data = _resolve_external_refs(data, source.parent)
        try:
            return cls.model_validate(data)
        except Exception as exc:
            raise ConfigError(f"{source}: {exc}") from exc


def _load_structured_file(path: Path) -> Any:
    """Load YAML or TOML from ``path`` based on the suffix.

    Suffix dispatch keeps the loader simple — capa stores experiments as
    either ``.yaml``/``.yml`` (operator-friendly) or ``.toml`` (programmatic).
    """
    if not path.is_file():
        raise ConfigError(f"file not found: {path}")
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        yaml = YAML(typ="safe")
        with open(path, encoding="utf-8") as fp:
            return yaml.load(fp)
    if suffix == ".toml":
        with open(path, "rb") as fp:
            return tomllib.load(fp)
    raise ConfigError(f"unsupported config suffix {suffix!r}: {path}")


def _resolve_external_refs(data: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """Replace string values for ``hardware:`` / ``method:`` / etc. with the
    contents of the referenced file.

    Relative paths resolve against ``base_dir`` (the experiment file's
    directory). Absolute paths are loaded as-is.
    """
    out = dict(data)
    for key in ("hardware", "method"):
        ref = out.get(key)
        if isinstance(ref, str):
            ref_path = Path(ref)
            if not ref_path.is_absolute():
                ref_path = base_dir / ref_path
            ref_path = ref_path.resolve()
            out[key] = _load_structured_file(ref_path)
            if key == "method":
                out["method_source_path"] = ref_path
    return out


__all__ = [
    "CalibrationSetRef",
    "DeviceConfig",
    "DomainProfileRef",
    "ExperimentConfig",
    "HardwareProfile",
    "OperatorRef",
    "ProcedureRef",
    "SafetyPolicy",
    "SafetyRuleConfig",
    "SampleInfo",
    "StoragePolicy",
]
