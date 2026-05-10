"""Tests for :mod:`capa.devices.nidaq_channels`.

Covers:

* Strict UPPER_SNAKE name validation (``"K"`` works, ``"k"`` doesn't).
* Backwards compatibility with raw integer ``.value`` inputs.
* The new nidaqlib v0.2.0 ADC-timing / auto-zero / terminal-config knobs.
* Discriminated-union dispatch — typed kinds resolve to typed models, unknown
  kinds fall back to the pass-through model.
* End-to-end round trip through ``NIDAQAdapterParams.build_task_spec()`` so
  the materialised :class:`nidaqlib.TaskSpec` carries the right enum members.
"""

from __future__ import annotations

import pytest
from nidaqmx.constants import (
    ADCTimingMode,
    AutoZeroType,
    CJCSource,
    TemperatureUnits,
    TerminalConfiguration,
    ThermocoupleType,
)
from pydantic import TypeAdapter, ValidationError

from capa.devices.nidaq import NIDAQAdapterParams
from capa.devices.nidaq_channels import (
    NIDAQChannelConfig,
    NIDAQRawChannelConfig,
    NIDAQThermocoupleConfig,
    NIDAQVoltageConfig,
)

_CHANNEL_ADAPTER: TypeAdapter[NIDAQChannelConfig] = TypeAdapter(NIDAQChannelConfig)


# ---------------------------------------------------------------------------
# Discriminated-union dispatch
# ---------------------------------------------------------------------------


class TestDiscriminator:
    def test_thermocouple_kind_dispatches_to_typed_model(self) -> None:
        cfg = _CHANNEL_ADAPTER.validate_python(
            {
                "kind": "thermocouple",
                "physical_channel": "Dev1/ai0",
                "thermocouple_type": "K",
                "min_val": 0.0,
                "max_val": 1000.0,
            }
        )
        assert isinstance(cfg, NIDAQThermocoupleConfig)
        assert cfg.thermocouple_type == "K"

    def test_voltage_kind_dispatches_to_typed_model(self) -> None:
        cfg = _CHANNEL_ADAPTER.validate_python(
            {"kind": "ai_voltage", "physical_channel": "Dev1/ai2"}
        )
        assert isinstance(cfg, NIDAQVoltageConfig)

    def test_unknown_kind_falls_back_to_raw(self) -> None:
        # ``do`` is a valid nidaqlib kind that capa doesn't (yet) have a typed
        # model for, so the discriminated union routes it to the raw fallback.
        cfg = _CHANNEL_ADAPTER.validate_python(
            {"kind": "do", "physical_channel": "Dev1/port0/line0"}
        )
        assert isinstance(cfg, NIDAQRawChannelConfig)
        assert cfg.kind == "do"


# ---------------------------------------------------------------------------
# Thermocouple — name vs int, defaults, new v0.2.0 fields
# ---------------------------------------------------------------------------


class TestThermocoupleNames:
    def test_canonical_name_accepted(self) -> None:
        cfg = NIDAQThermocoupleConfig(
            kind="thermocouple",
            physical_channel="Dev1/ai0",
            thermocouple_type="K",
            min_val=0.0,
            max_val=1000.0,
            cjc_source="BUILT_IN",
            units="DEG_C",
        )
        d = cfg.to_nidaqlib_dict()
        assert d["thermocouple_type"] == ThermocoupleType.K.value
        assert d["cjc_source"] == CJCSource.BUILT_IN.value
        assert d["units"] == TemperatureUnits.DEG_C.value

    def test_int_value_accepted_for_backwards_compat(self) -> None:
        cfg = NIDAQThermocoupleConfig(
            kind="thermocouple",
            physical_channel="Dev1/ai0",
            thermocouple_type=ThermocoupleType.K.value,  # 10073
            min_val=0.0,
            max_val=1000.0,
            cjc_source=CJCSource.BUILT_IN.value,  # 10200
            units=TemperatureUnits.DEG_C.value,  # 10143
        )
        # Validator canonicalises to the name regardless of input form.
        assert cfg.thermocouple_type == "K"
        assert cfg.cjc_source == "BUILT_IN"
        assert cfg.units == "DEG_C"

    def test_lowercase_name_rejected_strict(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            NIDAQThermocoupleConfig(
                kind="thermocouple",
                physical_channel="Dev1/ai0",
                thermocouple_type="k",
                min_val=0.0,
                max_val=1000.0,
            )
        # The error message lists the canonical names so users can copy from it.
        assert "K" in str(excinfo.value)

    def test_unknown_name_rejected(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            NIDAQThermocoupleConfig(
                kind="thermocouple",
                physical_channel="Dev1/ai0",
                thermocouple_type="Z",
                min_val=0.0,
                max_val=1000.0,
            )
        assert "ThermocoupleType" in str(excinfo.value)

    def test_unknown_int_value_rejected(self) -> None:
        with pytest.raises(ValidationError):
            NIDAQThermocoupleConfig(
                kind="thermocouple",
                physical_channel="Dev1/ai0",
                thermocouple_type=99999,
                min_val=0.0,
                max_val=1000.0,
            )

    def test_bool_rejected(self) -> None:
        with pytest.raises(ValidationError):
            NIDAQThermocoupleConfig(
                kind="thermocouple",
                physical_channel="Dev1/ai0",
                thermocouple_type=True,
                min_val=0.0,
                max_val=1000.0,
            )

    def test_units_default_is_celsius(self) -> None:
        cfg = NIDAQThermocoupleConfig(
            kind="thermocouple",
            physical_channel="Dev1/ai0",
            thermocouple_type="K",
            min_val=0.0,
            max_val=1000.0,
        )
        assert cfg.units == "DEG_C"

    def test_units_kelvin_does_not_collide_with_thermocouple_type_k(self) -> None:
        # ``TemperatureUnits.K`` is Kelvin; ``ThermocoupleType.K`` is the alloy.
        # Each field validates against its own enum class, so this works.
        cfg = NIDAQThermocoupleConfig(
            kind="thermocouple",
            physical_channel="Dev1/ai0",
            thermocouple_type="K",
            min_val=0.0,
            max_val=1500.0,
            units="K",
        )
        d = cfg.to_nidaqlib_dict()
        assert d["thermocouple_type"] == ThermocoupleType.K.value  # alloy
        assert d["units"] == TemperatureUnits.K.value  # Kelvin

    def test_cjc_source_optional(self) -> None:
        cfg = NIDAQThermocoupleConfig(
            kind="thermocouple",
            physical_channel="Dev1/ai0",
            thermocouple_type="K",
            min_val=0.0,
            max_val=1000.0,
        )
        d = cfg.to_nidaqlib_dict()
        assert d["cjc_source"] is None
        assert d["cjc_val"] is None


# ---------------------------------------------------------------------------
# nidaqlib v0.2.0 — ADC timing + auto-zero on AnalogInputBase
# ---------------------------------------------------------------------------


class TestADCTimingAndAutoZero:
    def test_adc_timing_mode_high_resolution(self) -> None:
        cfg = NIDAQThermocoupleConfig(
            kind="thermocouple",
            physical_channel="Dev1/ai0",
            thermocouple_type="K",
            min_val=0.0,
            max_val=1000.0,
            adc_timing_mode="HIGH_RESOLUTION",
        )
        assert cfg.to_nidaqlib_dict()["adc_timing_mode"] == ADCTimingMode.HIGH_RESOLUTION.value

    def test_auto_zero_modes_round_trip(self) -> None:
        for name, member in [
            ("NONE", AutoZeroType.NONE),
            ("ONCE", AutoZeroType.ONCE),
            ("EVERY_SAMPLE", AutoZeroType.EVERY_SAMPLE),
        ]:
            cfg = NIDAQThermocoupleConfig(
                kind="thermocouple",
                physical_channel="Dev1/ai0",
                thermocouple_type="K",
                min_val=0.0,
                max_val=1000.0,
                auto_zero_mode=name,
            )
            assert cfg.to_nidaqlib_dict()["auto_zero_mode"] == member.value

    def test_unset_adc_fields_serialise_to_none(self) -> None:
        cfg = NIDAQThermocoupleConfig(
            kind="thermocouple",
            physical_channel="Dev1/ai0",
            thermocouple_type="K",
            min_val=0.0,
            max_val=1000.0,
        )
        d = cfg.to_nidaqlib_dict()
        assert d["adc_timing_mode"] is None
        assert d["adc_custom_timing_mode"] is None
        assert d["auto_zero_mode"] is None

    def test_custom_timing_pairs_with_custom_code(self) -> None:
        # ``adc_timing_mode = "CUSTOM"`` requires a paired ``adc_custom_timing_mode``;
        # that constraint is enforced by nidaqlib's ChannelSpec __post_init__,
        # not by the capa-side model. Verify the dict survives translation
        # so build_task_spec() reaches the nidaqlib check.
        cfg = NIDAQThermocoupleConfig(
            kind="thermocouple",
            physical_channel="Dev1/ai0",
            thermocouple_type="K",
            min_val=0.0,
            max_val=1000.0,
            adc_timing_mode="CUSTOM",
            adc_custom_timing_mode=42,
        )
        d = cfg.to_nidaqlib_dict()
        assert d["adc_timing_mode"] == ADCTimingMode.CUSTOM.value
        assert d["adc_custom_timing_mode"] == 42

    def test_custom_timing_without_code_caught_by_nidaqlib(self) -> None:
        # capa-side accepts the partial config (it's a valid Pydantic shape);
        # nidaqlib's NIDaqValidationError should fire when we try to
        # materialise the TaskSpec.
        params = NIDAQAdapterParams(
            task_name="t",
            channels=(
                {
                    "kind": "thermocouple",
                    "physical_channel": "Dev1/ai0",
                    "thermocouple_type": "K",
                    "min_val": 0.0,
                    "max_val": 1000.0,
                    "adc_timing_mode": "CUSTOM",
                    # adc_custom_timing_mode deliberately omitted
                },
            ),
        )
        with pytest.raises(Exception, match="adc_custom_timing_mode"):
            params.build_task_spec()


# ---------------------------------------------------------------------------
# Voltage channel — terminal_config and custom_scale_name
# ---------------------------------------------------------------------------


class TestVoltage:
    def test_terminal_config_named(self) -> None:
        cfg = NIDAQVoltageConfig(
            kind="ai_voltage",
            physical_channel="Dev1/ai0",
            terminal_config="DIFF",
        )
        assert cfg.to_nidaqlib_dict()["terminal_config"] == TerminalConfiguration.DIFF.value

    def test_terminal_config_int_backcompat(self) -> None:
        cfg = NIDAQVoltageConfig(
            kind="ai_voltage",
            physical_channel="Dev1/ai0",
            terminal_config=TerminalConfiguration.RSE.value,
        )
        assert cfg.terminal_config == "RSE"

    def test_default_min_max(self) -> None:
        cfg = NIDAQVoltageConfig(kind="ai_voltage", physical_channel="Dev1/ai0")
        d = cfg.to_nidaqlib_dict()
        assert d["min_val"] == -10.0
        assert d["max_val"] == 10.0

    def test_custom_scale_name(self) -> None:
        cfg = NIDAQVoltageConfig(
            kind="ai_voltage",
            physical_channel="Dev1/ai0",
            custom_scale_name="my_scale",
        )
        assert cfg.to_nidaqlib_dict()["custom_scale_name"] == "my_scale"


# ---------------------------------------------------------------------------
# Typo / shape protection
# ---------------------------------------------------------------------------


class TestExtraForbid:
    def test_typed_model_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            NIDAQThermocoupleConfig(
                kind="thermocouple",
                physical_channel="Dev1/ai0",
                thermocouple_type="K",
                min_val=0.0,
                max_val=1000.0,
                cjcsource="BUILT_IN",  # type: ignore[call-arg]  # typo
            )

    def test_raw_model_allows_unknown_field(self) -> None:
        cfg = NIDAQRawChannelConfig(
            kind="counter_input",
            physical_channel="Dev1/ctr0",
            edge="rising",
        )
        assert cfg.to_nidaqlib_dict()["edge"] == "rising"


# ---------------------------------------------------------------------------
# End-to-end through NIDAQAdapterParams.build_task_spec
# ---------------------------------------------------------------------------


class TestBuildTaskSpec:
    def test_named_thermocouple_round_trips_to_nidaqlib_spec(self) -> None:
        params = NIDAQAdapterParams(
            task_name="default_task",
            channels=(
                {
                    "kind": "thermocouple",
                    "physical_channel": "cDAQ1Mod1/ai0",
                    "name": "TC_top_1",
                    "thermocouple_type": "K",
                    "min_val": 0.0,
                    "max_val": 1000.0,
                    "cjc_source": "BUILT_IN",
                    "units": "DEG_C",
                    "adc_timing_mode": "HIGH_RESOLUTION",
                    "auto_zero_mode": "ONCE",
                },
            ),
        )
        spec = params.build_task_spec()
        assert len(spec.channels) == 1
        ch = spec.channels[0]
        # Library-side enum members survived the round trip.
        assert ch.thermocouple_type is ThermocoupleType.K  # type: ignore[attr-defined]
        assert ch.cjc_source is CJCSource.BUILT_IN  # type: ignore[attr-defined]
        assert ch.units is TemperatureUnits.DEG_C  # type: ignore[attr-defined]
        assert ch.adc_timing_mode is ADCTimingMode.HIGH_RESOLUTION  # type: ignore[attr-defined]
        assert ch.auto_zero_mode is AutoZeroType.ONCE  # type: ignore[attr-defined]

    def test_int_form_round_trips_identically(self) -> None:
        named = NIDAQAdapterParams(
            task_name="t",
            channels=(
                {
                    "kind": "thermocouple",
                    "physical_channel": "Dev1/ai0",
                    "thermocouple_type": "K",
                    "min_val": 0.0,
                    "max_val": 1000.0,
                    "cjc_source": "BUILT_IN",
                },
            ),
        )
        ints = NIDAQAdapterParams(
            task_name="t",
            channels=(
                {
                    "kind": "thermocouple",
                    "physical_channel": "Dev1/ai0",
                    "thermocouple_type": ThermocoupleType.K.value,
                    "min_val": 0.0,
                    "max_val": 1000.0,
                    "cjc_source": CJCSource.BUILT_IN.value,
                },
            ),
        )
        assert named.build_task_spec().channels[0] == ints.build_task_spec().channels[0]

    def test_raw_channel_kind_passes_through(self) -> None:
        params = NIDAQAdapterParams(
            task_name="t",
            channels=(
                {
                    "kind": "ai_voltage",
                    "physical_channel": "Dev1/ai0",
                    "min_val": -1.0,
                    "max_val": 1.0,
                },
                {
                    "kind": "do",
                    "physical_channel": "Dev1/port0/line0",
                },
            ),
        )
        spec = params.build_task_spec()
        kinds = [type(ch).__name__ for ch in spec.channels]
        assert kinds == ["AnalogInputVoltage", "DigitalOutput"]
