"""Unit tests for :mod:`capa.runtime.build` resource validation."""

from __future__ import annotations

from dataclasses import dataclass, field

from capa.runtime.build import _check_daqmx_channel_uniqueness, _check_webcam_uniqueness


@dataclass
class _Stub:
    name: str
    resource_id: str
    physical_channels: tuple[str, ...] = field(default_factory=tuple)


class TestDaqmxChannelUniqueness:
    def test_disjoint_channels_pass(self) -> None:
        a = _Stub(
            name="a",
            resource_id="daqmx:chassis:cDAQ1",
            physical_channels=("cDAQ1Mod1/ai0", "cDAQ1Mod1/ai1"),
        )
        b = _Stub(
            name="b",
            resource_id="daqmx:chassis:cDAQ1",
            physical_channels=("cDAQ1Mod1/ai2",),
        )

        assert _check_daqmx_channel_uniqueness([a, b]) == []

    def test_same_channel_twice_problem(self) -> None:
        a = _Stub(
            name="a",
            resource_id="daqmx:chassis:cDAQ1",
            physical_channels=("cDAQ1Mod1/ai0",),
        )
        b = _Stub(
            name="b",
            resource_id="daqmx:chassis:cDAQ1",
            physical_channels=("cDAQ1Mod1/ai0",),
        )

        problems = _check_daqmx_channel_uniqueness([a, b])

        assert len(problems) == 1
        assert "cDAQ1Mod1/ai0" in problems[0].message
        assert problems[0].severity == "error"
        assert problems[0].code == "devices.daqmx_channel_conflict"

    def test_conflict_carries_both_names(self) -> None:
        a = _Stub(name="a", resource_id="daqmx:chassis:cDAQ1", physical_channels=("ch1",))
        b = _Stub(name="b", resource_id="daqmx:chassis:cDAQ1", physical_channels=("ch1",))

        problems = _check_daqmx_channel_uniqueness([a, b])

        assert len(problems) == 1
        assert set(problems[0].path[1:3]) == {"a", "b"}
        assert problems[0].path[3] == "ch1"

    def test_non_daqmx_adapter_skipped(self) -> None:
        a = _Stub(
            name="serial_thing",
            resource_id="serial:COM6",
            physical_channels=("looks-like-a-channel",),
        )
        b = _Stub(
            name="daqmx_thing",
            resource_id="daqmx:chassis:cDAQ1",
            physical_channels=("looks-like-a-channel",),
        )

        assert _check_daqmx_channel_uniqueness([a, b]) == []

    def test_missing_physical_channels_attribute(self) -> None:
        @dataclass
        class _NoChannels:
            name: str
            resource_id: str

        a = _NoChannels(name="sim_daq", resource_id="daqmx:chassis:cDAQ1")

        assert _check_daqmx_channel_uniqueness([a]) == []


class TestWebcamUniqueness:
    def test_disjoint_webcams_pass(self) -> None:
        a = _Stub(name="cam0", resource_id="webcam:0")
        b = _Stub(name="cam1", resource_id="webcam:1")

        assert _check_webcam_uniqueness([a, b]) == []

    def test_same_webcam_twice_problem(self) -> None:
        a = _Stub(name="cam_a", resource_id="webcam:0")
        b = _Stub(name="cam_b", resource_id="webcam:0")

        problems = _check_webcam_uniqueness([a, b])

        assert len(problems) == 1
        assert "webcam:0" in problems[0].message
        assert problems[0].severity == "error"
        assert problems[0].code == "cameras.webcam_handle_conflict"

    def test_conflict_carries_both_names(self) -> None:
        a = _Stub(name="left", resource_id="webcam:0")
        b = _Stub(name="right", resource_id="webcam:0")

        problems = _check_webcam_uniqueness([a, b])

        assert len(problems) == 1
        assert set(problems[0].path[1:3]) == {"left", "right"}


class TestSchemeIsolation:
    def test_full_config_no_conflicts(self) -> None:
        adapters = [
            _Stub(name="heater", resource_id="serial:COM6"),
            _Stub(name="purge_mfc", resource_id="serial:COM7"),
            _Stub(name="balance", resource_id="serial:COM4"),
            _Stub(
                name="cdaq1",
                resource_id="daqmx:chassis:cDAQ1",
                physical_channels=("cDAQ1Mod1/ai0", "cDAQ1Mod1/ai1"),
            ),
            _Stub(name="visible_cam0", resource_id="webcam:0"),
            _Stub(name="ir_cam0", resource_id="webcam:1"),
        ]

        assert _check_daqmx_channel_uniqueness(adapters) == []
        assert _check_webcam_uniqueness(adapters) == []
