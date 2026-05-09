from __future__ import annotations

import pytest

from capa.channels.calibration import Identity, LinearTwoPoint
from capa.channels.registry import ChannelRegistry
from capa.channels.spec import (
    AlicatFrameField,
    ChannelKind,
    ChannelSpec,
    NIDAQReadingField,
    SartoriusReading,
    WatlowParameter,
)
from capa.core.errors import ConfigError


def _make_alicat_spec() -> ChannelSpec:
    return ChannelSpec(
        name="MFC_air.flow",
        kind=ChannelKind.MFC_FLOW,
        source=AlicatFrameField(device="air_mfc", field="Mass_Flow"),
        unit="slpm",
        derived_unit="slpm",
        calibration=Identity(input_unit="slpm", output_unit="slpm"),
    )


class TestChannelSpec:
    def test_minimal(self) -> None:
        spec = _make_alicat_spec()
        assert spec.name == "MFC_air.flow"
        assert spec.output_unit() == "slpm"

    def test_calibration_dimensional_mismatch_rejected(self) -> None:
        # ChannelSpec catches the case where the calibration's *internally
        # consistent* output dimension doesn't match the channel's
        # derived_unit. (Identity rejects its own bad pair earlier.)
        with pytest.raises(ConfigError):
            ChannelSpec(
                name="bad",
                kind=ChannelKind.ANALOG_IN,
                source=NIDAQReadingField(device="x", task="t", field="f"),
                unit="V",
                derived_unit="kg",  # mismatched with calibration output (kPa)
                calibration=LinearTwoPoint(
                    input_unit="V",
                    output_unit="kPa",
                    ref_low_raw=0,
                    ref_low_value=0,
                    ref_high_raw=1,
                    ref_high_value=1,
                ),
            )

    def test_calibration_input_mismatch_rejected(self) -> None:
        with pytest.raises(ConfigError):
            ChannelSpec(
                name="bad",
                kind=ChannelKind.ANALOG_IN,
                source=NIDAQReadingField(device="x", task="t", field="f"),
                unit="V",
                derived_unit="kPa",
                calibration=LinearTwoPoint(
                    input_unit="kg",  # mismatched with channel unit "V"
                    output_unit="kPa",
                    ref_low_raw=0,
                    ref_low_value=0,
                    ref_high_raw=1,
                    ref_high_value=1,
                ),
            )

    def test_keep_raw_default_false(self) -> None:
        assert _make_alicat_spec().keep_raw is False


class TestSourceBindings:
    def test_alicat(self) -> None:
        b = AlicatFrameField(device="air", field="Mass_Flow")
        assert b.source == "alicat_frame_field"
        assert b.device == "air"

    def test_watlow(self) -> None:
        b = WatlowParameter(device="heater", parameter="process_value", instance=1)
        assert b.source == "watlow_parameter"
        assert b.instance == 1

    def test_watlow_zero_instance_rejected(self) -> None:
        with pytest.raises(Exception):
            WatlowParameter(device="heater", parameter="pv", instance=0)

    def test_sartorius_default_field(self) -> None:
        b = SartoriusReading(device="balance")
        assert b.field == "value"

    def test_nidaq_reading_field(self) -> None:
        b = NIDAQReadingField(device="cdaq1", task="tc_task", field="TC_top_1")
        assert b.task == "tc_task"


class TestChannelRegistry:
    def test_register_and_resolve(self) -> None:
        reg = ChannelRegistry()
        reg.register(_make_alicat_spec())
        resolved = reg.resolve("MFC_air.flow")
        assert resolved.name == "MFC_air.flow"
        assert resolved.binding == _make_alicat_spec().source

    def test_duplicate_rejected(self) -> None:
        reg = ChannelRegistry.from_specs([_make_alicat_spec()])
        with pytest.raises(ConfigError):
            reg.register(_make_alicat_spec())

    def test_unknown_resolution_raises(self) -> None:
        reg = ChannelRegistry()
        with pytest.raises(ConfigError):
            reg.resolve("ghost")

    def test_freeze_blocks_mutation(self) -> None:
        reg = ChannelRegistry.from_specs([_make_alicat_spec()])
        reg.freeze()
        assert reg.is_frozen
        new_spec = ChannelSpec(
            name="other",
            kind=ChannelKind.ANALOG_IN,
            source=NIDAQReadingField(device="x", task="t", field="f"),
            unit="V",
            derived_unit="V",
            calibration=Identity(input_unit="V", output_unit="V"),
        )
        with pytest.raises(ConfigError):
            reg.register(new_spec)

    def test_iteration_and_len(self) -> None:
        reg = ChannelRegistry.from_specs([_make_alicat_spec()])
        assert len(reg) == 1
        names = [ch.name for ch in reg]
        assert names == ["MFC_air.flow"]
        assert "MFC_air.flow" in reg
        assert "ghost" not in reg
