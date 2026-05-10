"""Typed Pydantic channel models for the NI-DAQ adapter.

The :class:`~capa.devices.nidaq.NIDAQAdapter` consumes ``[[devices.params.channels]]``
TOML blocks. Historically those blocks carried raw ``int`` values for NI enum
fields::

    thermocouple_type = 10073    # ThermocoupleType.K
    cjc_source = 10200           # CJCSource.BUILT_IN
    units = 10143                # TemperatureUnits.DEG_C

That works but is unreadable in configs and equipment manifests. The models in
this module accept the canonical NI enum **name** instead::

    thermocouple_type = "K"
    cjc_source = "BUILT_IN"
    units = "DEG_C"
    adc_timing_mode = "HIGH_RESOLUTION"   # NI 9214 default; nidaqlib v0.2.0 knob
    auto_zero_mode = "ONCE"

Names match :mod:`nidaqmx.constants` exactly (UPPER_SNAKE, case-sensitive). For
backwards compatibility with existing run-bundle ``equipment.toml`` snapshots,
the validators also accept the raw integer ``.value`` and canonicalise to the
name. Anything else fails fast at config-load time with a list of valid names.

Channel kinds without a typed model (``digital_input``, ``digital_output``,
``ao_voltage``, the counter family) flow through :class:`NIDAQRawChannelConfig`
unchanged — the dict is forwarded to :func:`nidaqlib.channels.ChannelSpec.from_dict`
without translation. Add typed models incrementally as configs start using new
kinds.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal

from nidaqmx.constants import (
    ADCTimingMode,
    AutoZeroType,
    CJCSource,
    TemperatureUnits,
    TerminalConfiguration,
    ThermocoupleType,
)
from pydantic import BaseModel, BeforeValidator, ConfigDict, Discriminator, Field, Tag


def _enum_name_validator(enum_cls: Any) -> Callable[[object], str]:
    """Return a Pydantic ``BeforeValidator`` callable for an NI enum.

    Accepts either the canonical UPPER_SNAKE member name (case-sensitive) or
    the integer ``.value``. Returns the canonical name. Anything else raises
    ``ValueError`` listing the valid names.

    The strict UPPER_SNAKE match is intentional: ``"k"`` vs ``"K"`` would mean
    different things on different fields (``TemperatureUnits.K`` is Kelvin,
    ``ThermocoupleType.K`` is the K-type alloy), and a forgiving validator would
    let typos like ``"BUITL_IN"`` parse as something the user didn't intend.
    """
    valid_names = tuple(m.name for m in enum_cls)

    def coerce(value: object) -> str:
        if isinstance(value, str):
            if value not in valid_names:
                raise ValueError(
                    f"unknown {enum_cls.__name__} {value!r}; valid names: {valid_names}"
                )
            return value
        if isinstance(value, bool):
            raise ValueError(f"{enum_cls.__name__} cannot be a bool; use one of {valid_names}")
        if isinstance(value, int):
            try:
                # nidaqmx enums resolve to Any (no py.typed); the cast asserts
                # the runtime invariant that ``Enum.name`` is always a str.
                return str(enum_cls(value).name)
            except ValueError as exc:
                raise ValueError(
                    f"unknown {enum_cls.__name__} value {value!r}; valid names: {valid_names}"
                ) from exc
        raise ValueError(
            f"{enum_cls.__name__} must be a string name or int value; got {type(value).__name__}"
        )

    return coerce


ThermocoupleTypeName = Annotated[str, BeforeValidator(_enum_name_validator(ThermocoupleType))]
"""``"J" | "K" | "N" | "R" | "S" | "T" | "B" | "E" | "A" | "C"``."""

CJCSourceName = Annotated[str, BeforeValidator(_enum_name_validator(CJCSource))]
"""``"BUILT_IN" | "CONSTANT_USER_VALUE" | "SCANNABLE_CHANNEL"``."""

TemperatureUnitsName = Annotated[str, BeforeValidator(_enum_name_validator(TemperatureUnits))]
"""``"DEG_C" | "DEG_F" | "K" | "DEG_R" | "FROM_CUSTOM_SCALE"``."""

ADCTimingModeName = Annotated[str, BeforeValidator(_enum_name_validator(ADCTimingMode))]
"""``"AUTOMATIC" | "HIGH_RESOLUTION" | "HIGH_SPEED" | "BEST_50_HZ_REJECTION" |
"BEST_60_HZ_REJECTION" | "CUSTOM"``. Trades conversion rate against resolution
on delta-sigma modules; ``BEST_50/60_HZ_REJECTION`` suppresses mains hum;
``CUSTOM`` requires a paired :attr:`adc_custom_timing_mode`."""

AutoZeroTypeName = Annotated[str, BeforeValidator(_enum_name_validator(AutoZeroType))]
"""``"NONE" | "ONCE" | "EVERY_SAMPLE"``. ``ONCE`` is the most common useful
setting (auto-zero at acquisition start); ``EVERY_SAMPLE`` autozeros each
conversion at the cost of throughput."""

TerminalConfigName = Annotated[str, BeforeValidator(_enum_name_validator(TerminalConfiguration))]
"""``"RSE" | "NRSE" | "DIFF" | "PSEUDO_DIFF" | "DEFAULT"``."""


# ---------------------------------------------------------------------------
# Typed channel models
# ---------------------------------------------------------------------------


class _ChannelBase(BaseModel):
    """Fields shared by every typed channel kind."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    physical_channel: str = Field(min_length=1)
    """NI physical-channel id, e.g. ``"cDAQ1Mod1/ai0"``."""
    name: str | None = None
    """Friendly channel name; defaults to ``physical_channel``."""
    unit: str | None = None
    """Engineering unit string for sinks; not interpreted by the NI backend."""
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)


class _AnalogInputBase(_ChannelBase):
    """ADC-timing knobs shared by every analog-input kind (added in nidaqlib v0.2.0)."""

    adc_timing_mode: ADCTimingModeName | None = None
    adc_custom_timing_mode: int | None = None
    """Device-specific code; required iff ``adc_timing_mode == "CUSTOM"``."""
    auto_zero_mode: AutoZeroTypeName | None = None


class NIDAQThermocoupleConfig(_AnalogInputBase):
    """Thermocouple analog-input channel (NI 9213/9214 modules).

    Maps to :class:`nidaqlib.ThermocoupleInput`.
    """

    kind: Literal["thermocouple"]
    thermocouple_type: ThermocoupleTypeName
    """Required. Common: ``"K"`` (general purpose), ``"J"`` (legacy iron/constantan),
    ``"T"`` (cryogenic). See :data:`ThermocoupleTypeName`."""
    min_val: float
    """Lower limit of expected temperature, in :attr:`units`."""
    max_val: float
    """Upper limit of expected temperature, in :attr:`units`."""
    cjc_source: CJCSourceName | None = None
    """Cold-junction compensation source. ``None`` lets NI pick the device default
    (typically ``BUILT_IN`` for cDAQ TC modules)."""
    cjc_val: float | None = None
    """Cold-junction reference temperature in :attr:`units`. Only meaningful when
    ``cjc_source == "CONSTANT_USER_VALUE"``."""
    units: TemperatureUnitsName = "DEG_C"
    """Output temperature units. Defaults to ``"DEG_C"``."""

    def to_nidaqlib_dict(self) -> dict[str, Any]:
        """Serialise to the dict shape :func:`nidaqlib.ChannelSpec.from_dict` expects."""
        return {
            "kind": self.kind,
            "physical_channel": self.physical_channel,
            "name": self.name,
            "unit": self.unit,
            "metadata": dict(self.metadata),
            "min_val": self.min_val,
            "max_val": self.max_val,
            "thermocouple_type": ThermocoupleType[self.thermocouple_type].value,
            "cjc_source": (
                CJCSource[self.cjc_source].value if self.cjc_source is not None else None
            ),
            "cjc_val": self.cjc_val,
            "units": TemperatureUnits[self.units].value,
            "adc_timing_mode": (
                ADCTimingMode[self.adc_timing_mode].value
                if self.adc_timing_mode is not None
                else None
            ),
            "adc_custom_timing_mode": self.adc_custom_timing_mode,
            "auto_zero_mode": (
                AutoZeroType[self.auto_zero_mode].value if self.auto_zero_mode is not None else None
            ),
        }


class NIDAQVoltageConfig(_AnalogInputBase):
    """Voltage analog-input channel.

    Maps to :class:`nidaqlib.AnalogInputVoltage`.
    """

    kind: Literal["ai_voltage"]
    min_val: float = -10.0
    max_val: float = 10.0
    terminal_config: TerminalConfigName | None = None
    """``"RSE"`` / ``"NRSE"`` / ``"DIFF"`` / ``"PSEUDO_DIFF"`` / ``"DEFAULT"``.
    ``None`` lets NI pick the device default."""
    custom_scale_name: str | None = None
    """Pre-configured custom scale registered in NI MAX. When set, ``min_val``
    / ``max_val`` are scaled engineering units rather than volts."""

    def to_nidaqlib_dict(self) -> dict[str, Any]:
        """Serialise to the dict shape :func:`nidaqlib.ChannelSpec.from_dict` expects."""
        return {
            "kind": self.kind,
            "physical_channel": self.physical_channel,
            "name": self.name,
            "unit": self.unit,
            "metadata": dict(self.metadata),
            "min_val": self.min_val,
            "max_val": self.max_val,
            "terminal_config": (
                TerminalConfiguration[self.terminal_config].value
                if self.terminal_config is not None
                else None
            ),
            "custom_scale_name": self.custom_scale_name,
            "adc_timing_mode": (
                ADCTimingMode[self.adc_timing_mode].value
                if self.adc_timing_mode is not None
                else None
            ),
            "adc_custom_timing_mode": self.adc_custom_timing_mode,
            "auto_zero_mode": (
                AutoZeroType[self.auto_zero_mode].value if self.auto_zero_mode is not None else None
            ),
        }


class NIDAQRawChannelConfig(BaseModel):
    """Pass-through model for channel kinds we haven't typed yet.

    Forwards every field to :func:`nidaqlib.channels.ChannelSpec.from_dict` as-is.
    Use this for ``digital_input`` / ``digital_output`` / ``ao_voltage`` /
    counter kinds until a typed model is added — typo safety is weaker here
    (``extra="allow"``) but the channel still flows through the adapter.
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    kind: str
    physical_channel: str = Field(min_length=1)

    def to_nidaqlib_dict(self) -> dict[str, Any]:
        """Forward the model dump unchanged."""
        return self.model_dump()


# ---------------------------------------------------------------------------
# Discriminated union — Pydantic dispatches by the ``kind`` field
# ---------------------------------------------------------------------------


_TYPED_KINDS = {"thermocouple", "ai_voltage"}


def _kind_discriminator(value: object) -> str:
    """Map a raw dict / model instance to its discriminator tag.

    Typed kinds dispatch to their own model; everything else lands on
    :class:`NIDAQRawChannelConfig`. A non-string ``kind`` is also routed to the
    raw model so the standard "kind must be a string" error is raised by the
    raw model's validator rather than this function (clearer error path).
    """
    kind = value.get("kind") if isinstance(value, dict) else getattr(value, "kind", None)
    if isinstance(kind, str) and kind in _TYPED_KINDS:
        return kind
    return "raw"


NIDAQChannelConfig = Annotated[
    Annotated[NIDAQThermocoupleConfig, Tag("thermocouple")]
    | Annotated[NIDAQVoltageConfig, Tag("ai_voltage")]
    | Annotated[NIDAQRawChannelConfig, Tag("raw")],
    Discriminator(_kind_discriminator),
]
"""Discriminated union over the typed channel kinds, with a raw fallback."""


__all__ = [
    "ADCTimingModeName",
    "AutoZeroTypeName",
    "CJCSourceName",
    "NIDAQChannelConfig",
    "NIDAQRawChannelConfig",
    "NIDAQThermocoupleConfig",
    "NIDAQVoltageConfig",
    "TemperatureUnitsName",
    "TerminalConfigName",
    "ThermocoupleTypeName",
]
