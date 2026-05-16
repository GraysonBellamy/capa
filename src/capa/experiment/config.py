""":class:`ExperimentConfig` and the nested ``HardwareProfile``, ``StoragePolicy``,
``SafetyPolicy``, ``SampleInfo`` models.

The full run recipe — everything needed to launch a run. Pydantic-
validated, YAML/TOML on disk, snapshotted into the run bundle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from capa.channels.spec import ChannelSpec
from capa.core.errors import ConfigError
from capa.devices.adapter import FailurePolicy
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
    resource_id: str | None = None
    """Optional override for the adapter's declared hardware contention
    domain. When ``None`` (the default) the runtime reads
    :attr:`~capa.devices.adapter.DeviceAdapter.resource_id` from the
    constructed adapter; setting an explicit value lets two adapters
    share a worker (e.g. a multi-drop RS-485 bus where the auto-derived
    ids would otherwise differ) or split a worker for test fixtures."""
    on_failure: FailurePolicy = FailurePolicy.ABORT
    """Failure policy when this device's worker stream fails.

    Recorded on resolved runtime metadata. Enforcement is not wired yet,
    so the field is advisory until the conductor grows per-device failure
    policy handling."""


class HardwareProfile(BaseModel):
    """Devices + channels for a single rig.

    lives under ``configs/hardware/`` as one TOML per rig setup.
    Cross-checked against :class:`ExperimentConfig.method` to make sure every
    setpoint target resolves to a real channel.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    devices: tuple[DeviceConfig, ...] = Field(default_factory=tuple)
    channels: tuple[ChannelSpec, ...] = Field(default_factory=tuple)
    cameras: tuple[CameraSpec, ...] = Field(default_factory=tuple)
    """Per-rig camera entries (). Cameras are peers of devices, not
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

    The plugin id is matched at startup against ``plugins.lock``;
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
    :attr:`Procedure.config_model` at load time."""


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
    ``calibration.json`` () so the bundle is self-sufficient even
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

    –§8.7: in-flight flush cadence, final Parquet codec, optional
    TDMS pass-through, optional RO-Crate generation. Stores the
    schema; the bundle writer reads it.

    In-flight artifacts are Arrow IPC streams (``*.in-flight.arrows``); see
    ``arrow-ipc-streaming-plan.md``. IPC has no compression-level knob, so
    ``inflight_compression`` is just the codec name (e.g. ``"zstd"``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    bundle_root: str = "runs"
    # Everything below this line is tuning the operator rarely touches —
    # collapsed into the section's "Advanced" disclosure so the Storage
    # editor opens with just the bundle-root field visible.
    inflight_flush_seconds: float = Field(
        default=1.0,
        gt=0,
        json_schema_extra={
            "capa_group": "advanced",
            "capa_group_subtitle": "IPC / Parquet / TDMS tuning",
        },
    )
    parquet_final_row_group_rows: int = Field(
        default=262_144,
        gt=0,
        json_schema_extra={"capa_group": "advanced"},
    )
    inflight_compression: str = Field(
        default="zstd",
        json_schema_extra={"capa_group": "advanced"},
    )
    parquet_final_compression: str = Field(
        default="zstd:6",
        json_schema_extra={"capa_group": "advanced"},
    )
    enable_tdms_passthrough: bool = Field(
        default=False,
        json_schema_extra={"capa_group": "advanced"},
    )
    enable_rocrate: bool = Field(
        default=False,
        json_schema_extra={"capa_group": "advanced"},
    )
    producer_queue_abort_after_s: float = Field(
        default=5.0,
        gt=0,
        json_schema_extra={"capa_group": "advanced"},
    )
    """How long the producer→fan-out queue may stay at capacity before the
    run aborts. The producer queue's policy is :class:`ABORT_RUN`, so a
    sustained writer-thread or fan-out stall surfaces as a crashed run
    (bundle still sealed) rather than an indefinite acquisition freeze.
    Tune upward only if the rig genuinely tolerates longer durable-sink
    pauses — the default 5 s is the operator-runbook trade-off."""


class SafetyRuleConfig(BaseModel):
    """Declarative safety-rule entry inside :class:`SafetyPolicy`.

    declarative safety rule entry.
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
    ``"safe_shutdown"`` ()."""


class SafetyPolicy(BaseModel):
    """Set of safety rules + abort-mode configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rules: tuple[SafetyRuleConfig, ...] = Field(default_factory=tuple)
    default_abort: str = "safe_shutdown"
    """What the UI's red button does by default. ``"safe_shutdown"`` runs
    the cooldown step; ``"abort_run"`` is immediate cancel."""


class SampleInfo(BaseModel):
    """Specimen metadata captured at run-start.

    The cone-calorimeter domain profile layers
    additional required fields on top of these via its own metadata model.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    material: str | None = None
    thickness_mm: float | None = Field(default=None, gt=0)
    mass_g: float | None = Field(default=None, gt=0)
    notes: str | None = None
    extra: dict[str, Any] = Field(
        default_factory=dict,
        json_schema_extra={
            "capa_group": "metadata",
            "capa_group_subtitle": "Free-form extras",
        },
    )


class OperatorRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    display_name: str | None = None


# ---------------------------------------------------------------------------
# Runtime tunables.
# ---------------------------------------------------------------------------


class RuntimeConfig(BaseModel):
    """Operator-tunable runtime knobs.

    Only fields an operator may reasonably want to adjust per experiment
    live here. Internal timing constants (adapter grace timers, saturation
    deadline, bridge capacity factor) remain code-level — promote one
    here when a real experiment demands it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    shutdown_grace_s: float = Field(default=5.0, gt=0)
    """Per-worker grace before the conductor forces a hard-stop during
    disarm. Matches :data:`~capa.runtime.conductor.DEFAULT_SHUTDOWN_GRACE_S`."""
    loop_lag_warn_ms: float = Field(default=50.0, gt=0)
    """Threshold at which the per-loop heartbeat starts logging warnings.
    Surfaced in the status-bar latency badge."""
    ui_bridge_capacity: int = Field(default=4096, gt=0)
    """Capacity of the Conductor → UI :class:`ThreadBridge`. ``DROP_OLDEST``
    policy so the conductor loop never blocks on a slow UI subscriber."""


# ---------------------------------------------------------------------------
# ExperimentConfig — the top-level run recipe.
# ---------------------------------------------------------------------------


class ExperimentConfig(BaseModel):
    """The full run recipe.

    YAML/TOML on disk; Pydantic-validated; snapshotted into the
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
    method_source_path: Path | None = Field(default=None, exclude=True)
    """Original method file path when ``method:`` was a string ref in the
    experiment YAML. ``None`` when the method was inlined or absent. The UI
    uses this so editing an auto-loaded method writes back to its source file.

    Excluded from serialisation: this is in-memory IO bookkeeping, not a
    config field. :class:`~capa.config.document.ConfigDocument` is the
    authoritative source-tracking layer; this attribute survives as a
    convenience for callers that still go through :meth:`load`."""
    hardware_source_path: Path | None = Field(default=None, exclude=True)
    """Original hardware file path when ``hardware:`` was a string ref in
    the experiment YAML. ``None`` when the hardware block was inlined.
    Mirrors :attr:`method_source_path`. Excluded from serialisation."""
    procedure: ProcedureRef
    domain_profile: DomainProfileRef | None = None
    calibration_set: CalibrationSetRef
    storage: StoragePolicy = Field(default_factory=StoragePolicy)
    safety: SafetyPolicy = Field(default_factory=SafetyPolicy)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
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

        Delegates to :class:`~capa.config.document.ConfigDocument`, which
        owns the source-tracking layer (paths, formats, inline/external
        modes). This classmethod stays as the headless / CLI entry point;
        anything that needs the raw payloads or save-back ability should
        use :class:`ConfigDocument` directly.

        File-ref resolution: when ``hardware:`` is a string, treat it as a
        path to a hardware-profile TOML and load it. Relative paths resolve
        against the experiment file's directory. ``method:`` follows the
        same rule.
        """
        # Local import: capa.config.document imports from this module, so
        # the dep flows one direction at runtime.
        from capa.config.document import ConfigDocument  # noqa: PLC0415

        return cast(ExperimentConfig, ConfigDocument.load(path).build_config())


__all__ = [
    "CalibrationSetRef",
    "DeviceConfig",
    "DomainProfileRef",
    "ExperimentConfig",
    "FailurePolicy",
    "HardwareProfile",
    "OperatorRef",
    "ProcedureRef",
    "RuntimeConfig",
    "SafetyPolicy",
    "SafetyRuleConfig",
    "SampleInfo",
    "StoragePolicy",
]
