"""ChannelSpec, ChannelKind, SourceBinding (tagged union), AlarmBand.

Plan §5.1. ``ChannelSpec`` is the universal binding unit — UI binds to channels,
sinks key off channels, calibrations attach to channels, plotting groups by
channels. Devices come and go; channels are the stable contract.

``SourceBinding`` is deliberately more specific than "device + channel". Each
variant points at the exact emitted field or parameter inside the underlying
library record (alicatlib ``Sample``, watlowlib ``Sample``, sartoriuslib
``Sample``, nidaqlib ``DaqReading`` / ``DaqBlock``).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from capa.channels.calibration import Calibration, Identity
from capa.core.errors import ConfigError
from capa.core.units import UnitStr, units_compatible


class ChannelKind(StrEnum):
    """High-level role of a channel.

    Plan §5.1. Used by the UI to pick widgets/axes and by safety to gate
    setpoint-only checks.
    """

    ANALOG_IN = "analog_in"
    """Voltage / current / arbitrary scalar from a DAQ AI channel."""
    THERMOCOUPLE = "tc"
    """Temperature from a thermocouple input."""
    ANALOG_OUT = "ao"
    """Commanded analog voltage."""
    DIGITAL_OUT = "do"
    """Commanded digital line."""
    COUNTER = "counter"
    """Edge / pulse counter."""
    PROCESS_VAR = "process_var"
    """A controller's measured process variable (Watlow PV, etc.)."""
    SETPOINT = "setpoint"
    """A controller's commanded setpoint (Watlow SP, MFC flow setpoint)."""
    MASS = "mass"
    """Balance reading."""
    MFC_FLOW = "mfc_flow"
    """Mass flow controller flow value."""
    VIDEO_VISIBLE = "video_visible"
    """Visible-camera frame stream (no scalar value column)."""
    VIDEO_IR = "video_ir"
    """IR-camera frame stream."""
    DERIVED = "derived"
    """Computed from other channels (declared in :mod:`capa.channels.derived`)."""


# ---------------------------------------------------------------------------
# SourceBinding — one variant per library row shape.
#
# Plan §5.1 / §5.6: alicatlib emits wide DataFrame rows; watlowlib emits long
# rows per (device, parameter, instance); sartoriuslib emits one balance row;
# nidaqlib polled emits wide rows; nidaqlib hardware-clocked emits rectangular
# blocks. Each variant below is the *capa-side selector* into one of those
# emitted records.
# ---------------------------------------------------------------------------


class _BindingBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device: str


class AlicatFrameField(_BindingBase):
    """Selector into an :class:`alicatlib.streaming.Sample`.

    The library's :meth:`alicatlib.devices.data_frame.DataFrame.as_dict` exposes
    fields under the library's canonical underscored names — ``Mass_Flow``,
    ``Abs_Press``, ``Mass_Flow_Setpt`` — *not* the wire-format names with
    spaces. Use the underscored form here.
    """

    source: Literal["alicat_frame_field"] = "alicat_frame_field"
    field: str


class WatlowParameter(_BindingBase):
    """Selector into a :class:`watlowlib.streaming.Sample`.

    ``parameter`` is the canonical name from ``watlowlib.registry.parameters``
    (``"process_value"``, ``"setpoint"``, etc.); ``instance`` is the 1-indexed
    loop number. Watlow rows are long-format, so most channels map one-to-one.
    """

    source: Literal["watlow_parameter"] = "watlow_parameter"
    parameter: str
    instance: int = Field(ge=1, default=1)


class SartoriusReading(_BindingBase):
    """Selector into a :class:`sartoriuslib.streaming.Sample`.

    The reading row carries ``value``, ``unit``, ``stable``/``overload``/``underload``
    flags, ``raw`` bytes, ``protocol``, and error fields. ``field`` defaults to
    ``"value"``; pick a flag (e.g. ``"stable"``) when you want it as a channel.
    """

    source: Literal["sartorius_reading"] = "sartorius_reading"
    field: str = "value"


class NIDAQReadingField(_BindingBase):
    """Selector into a polled :class:`nidaqlib.tasks.models.DaqReading`.

    Polled DAQ readings are wide rows keyed by channel display name (the
    ``ChannelSpec.display_name`` from the underlying ``TaskSpec``). ``task`` is
    the ``TaskSpec.name``.
    """

    source: Literal["nidaq_reading_field"] = "nidaq_reading_field"
    task: str
    field: str


class NIDAQBlockChannel(_BindingBase):
    """Selector into a hardware-clocked :class:`nidaqlib.tasks.models.DaqBlock`.

    Hardware-clocked blocks stay rectangular until either the adapter derives
    channel samples at capa's normal 3–60 Hz class or hands the byte path to
    TDMS for kHz-rate capture (plan §8.7).
    """

    source: Literal["nidaq_block_channel"] = "nidaq_block_channel"
    task: str
    channel: str


class DerivedBinding(BaseModel):
    """Channel that is computed from other channels rather than read.

    Concrete derivations live in :mod:`capa.channels.derived` (P0a stub). The
    binding records the dependency list so the registry can topologically sort
    derived channels and surface circular-dep errors at config-load.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    source: Literal["derived"] = "derived"
    expression: str
    """Identifier of a registered derivation (e.g. ``"oxygen_depletion"``)."""
    inputs: tuple[str, ...] = Field(min_length=1)
    """Channel names this derivation reads from."""


SourceBinding = Annotated[
    AlicatFrameField
    | WatlowParameter
    | SartoriusReading
    | NIDAQReadingField
    | NIDAQBlockChannel
    | DerivedBinding,
    Field(discriminator="source"),
]
"""Tagged union over every binding variant. The ``source`` discriminator picks
the right model on deserialization.
"""


# ---------------------------------------------------------------------------
# AlarmBand — declarative alarm rule attached to a ChannelSpec. P0a ships
# the schema; the evaluator lands with SafetyMonitor in P0c+.
# ---------------------------------------------------------------------------


class AlarmAction(StrEnum):
    WARN = "warn"
    PAUSE_METHOD = "pause_method"
    ABORT_RUN = "abort_run"
    SAFE_SHUTDOWN = "safe_shutdown"


class AlarmBand(BaseModel):
    """One declarative alarm threshold.

    Plan §9: each rule declares its action; the same fault should always have
    the same response.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    """Stable id used as the rule key in :class:`SafetyMonitor.state`."""
    op: Literal[">", ">=", "<", "<=", "=="]
    threshold: float
    action: AlarmAction = AlarmAction.WARN
    debounce_s: float = Field(default=0.0, ge=0)
    """Window during which the band must continuously hold true before
    firing. Catches single-sample spikes."""
    message: str | None = None


# ---------------------------------------------------------------------------
# ChannelSpec — the universal binding unit.
# ---------------------------------------------------------------------------


def _default_identity() -> Identity:
    """Module-level default factory for the :class:`Identity` placeholder.

    Pydantic re-evaluates ``default_factory`` on each construction; we want a
    placeholder Identity that the post-init validator then re-wires to the
    actual channel units, but Identity needs *some* input/output unit at
    construction time, so we use a sentinel that the validator catches.

    In practice ChannelSpec consumers always supply an explicit calibration
    (see config fixtures); this default exists only so unit tests can
    construct a minimal spec without spelling out an Identity.
    """
    return Identity(input_unit="dimensionless", output_unit="dimensionless")


def make_identity(unit: str) -> Identity:
    """Convenience constructor for an :class:`Identity` calibration matching ``unit``."""
    return Identity(input_unit=unit, output_unit=unit)


class ChannelSpec(BaseModel):
    """One channel's full configuration.

    Plan §5.1. ``unit`` is the *raw* (pre-calibration) unit on the wire;
    ``derived_unit`` is what the calibration produces. When ``calibration``
    is :class:`~capa.channels.calibration.Identity`, the two must be
    dimensionally compatible (and typically equal). When non-identity, the
    calibration must consume ``unit`` and emit ``derived_unit``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    """Stable run-local identifier. UI/sinks/plots key off this."""

    kind: ChannelKind

    source: SourceBinding

    unit: UnitStr
    """Pre-calibration unit. e.g. ``"V"``, ``"K"``, ``"kPa"``."""

    derived_unit: UnitStr | None = None
    """Post-calibration unit. ``None`` means "same as ``unit``" — i.e. an
    identity calibration."""

    keep_raw: bool = False
    """If ``True``, the pre-calibration value is also written to the
    channel-sample sink (in addition to the calibrated value). Useful when
    a researcher wants the raw counts alongside the engineering value."""

    sample_rate_hz: float | None = Field(default=None, gt=0)
    """``None`` for event-driven channels (cameras) or channels whose cadence
    is set by their adapter rather than declared on the spec."""

    calibration: Calibration = Field(default_factory=_default_identity)

    plot_group: str | None = None
    """e.g. ``"temperatures"``, ``"flows"``, ``"mass"`` — used by the Plots
    pane to lay out related channels together."""

    alarms: tuple[AlarmBand, ...] = ()

    sinks: tuple[str, ...] = ("scalars",)
    """Which named sinks receive this channel. Matches keys in the storage
    layer wired up in P0b."""

    decimate_to_hz: float = Field(default=60.0, gt=0)
    """Plot-only decimation. Underlying disk capture is at native rate.
    Default is intentionally above the fastest producer (50 Hz Sartorius
    balance) so the ring buffer keeps every sample — the plot pane's
    peak-mode downsampler then preserves transients regardless of the
    actual repaint cadence (currently 10 Hz). Set lower in config only
    if a channel produces faster than the buffer can usefully store."""

    metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_dimensional_consistency(self) -> ChannelSpec:
        target_output = self.derived_unit or self.unit
        # Calibration variants carry their own input/output_unit fields. They
        # must agree with the channel's declared raw/derived unit.
        if not units_compatible(self.calibration.input_unit, self.unit):
            raise ConfigError(
                f"channel {self.name!r}: calibration input_unit "
                f"{self.calibration.input_unit!r} is not dimensionally compatible "
                f"with channel unit {self.unit!r}"
            )
        if not units_compatible(self.calibration.output_unit, target_output):
            raise ConfigError(
                f"channel {self.name!r}: calibration output_unit "
                f"{self.calibration.output_unit!r} is not dimensionally compatible "
                f"with channel derived_unit {target_output!r}"
            )
        return self

    def output_unit(self) -> str:
        """Return ``derived_unit`` when set, otherwise ``unit``."""
        return self.derived_unit if self.derived_unit is not None else self.unit


__all__ = [
    "AlarmAction",
    "AlarmBand",
    "AlicatFrameField",
    "ChannelKind",
    "ChannelSpec",
    "DerivedBinding",
    "NIDAQBlockChannel",
    "NIDAQReadingField",
    "SartoriusReading",
    "SourceBinding",
    "WatlowParameter",
    "make_identity",
]
