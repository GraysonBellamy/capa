from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from capa.devices.adapter import (
    AdapterLifecycle,
    Capability,
    CommandResult,
    DeviceCommand,
)
from capa.devices.records import (
    ChannelSample,
    DeviceEvent,
    DeviceSnapshot,
    SourceRecord,
)

NOW = datetime(2026, 5, 7, 15, 30, 0, tzinfo=UTC)


class TestSourceRecord:
    def test_wide_row(self) -> None:
        rec = SourceRecord(
            record_id="r0",
            adapter="alicat",
            device="air",
            shape="wide_row",
            t_mono_ns=12345,
            t_utc=NOW,
            row={"Mass_Flow": 50.0, "status": ""},
        )
        assert rec.shape == "wide_row"
        assert rec.row["Mass_Flow"] == 50.0
        assert rec.block_ref is None

    def test_block_with_row_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceRecord(
                record_id="r1",
                adapter="ni",
                device="cdaq1",
                shape="block",
                t_mono_ns=0,
                t_utc=NOW,
                row={"x": 1.0},
                block_ref="memory:cdaq1:0",
            )

    def test_block_without_ref_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceRecord(
                record_id="r2",
                adapter="ni",
                device="cdaq1",
                shape="block",
                t_mono_ns=0,
                t_utc=NOW,
            )

    def test_non_block_with_ref_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SourceRecord(
                record_id="r3",
                adapter="alicat",
                device="air",
                shape="wide_row",
                t_mono_ns=0,
                t_utc=NOW,
                row={"x": 1.0},
                block_ref="bad",
            )


class TestChannelSample:
    def test_minimal(self) -> None:
        cs = ChannelSample(
            channel="MFC_air.flow",
            t_mono_ns=12345,
            t_mono_s=12345 / 1e9,
            value=50.0,
            unit="slpm",
        )
        assert cs.value == 50.0
        assert cs.status == "ok"
        assert cs.uncertainty is None
        assert cs.raw is None

    def test_full(self) -> None:
        cs = ChannelSample(
            channel="O2",
            t_mono_ns=42,
            t_mono_s=42 / 1e9,
            value=20.95,
            raw=4.987,
            unit="percent",
            uncertainty=0.05,
            status="ok",
            source_record_id="ni:cdaq1:7",
            source_field="AI_O2",
        )
        assert cs.raw == 4.987
        assert cs.uncertainty == 0.05


class TestDeviceEvents:
    def test_event_minimal(self) -> None:
        ev = DeviceEvent(
            adapter="watlow",
            device="heater",
            t_mono_ns=0,
            t_utc=NOW,
            kind="connect",
            message="connected",
        )
        assert ev.severity == "info"

    def test_snapshot(self) -> None:
        snap = DeviceSnapshot(
            adapter="watlow",
            device="heater",
            t_mono_ns=0,
            t_utc=NOW,
            healthy=True,
            fields={"firmware": "PM6", "alarms": 0},
        )
        assert snap.healthy is True


class TestAdapterContract:
    def test_capability_flag_combine(self) -> None:
        caps = Capability.HAS_SETPOINT | Capability.HAS_RAMP
        assert Capability.HAS_RAMP in caps
        assert Capability.HAS_TARE not in caps

    def test_command_requires_authorization(self) -> None:
        # The DeviceCommand model does not enforce this — adapters do.
        # But construction with neither id should still parse, since plugins
        # may pass them in via other channels.
        cmd = DeviceCommand(
            kind="set_setpoint",
            target="setpoint:1",
            payload={"value": 400.0},
            issued_by="abr",
        )
        assert cmd.authorization_id is None

    def test_command_result(self) -> None:
        r = CommandResult(accepted=True, t_mono_ns=0, t_utc=NOW)
        assert r.accepted is True

    def test_lifecycle_state_machine(self) -> None:
        life = AdapterLifecycle()
        assert str(life.state) == "closed"
        life.open()
        assert str(life.state) == "open"
        life.start()
        assert str(life.state) == "running"
        life.stop()
        assert str(life.state) == "open"
        life.close()
        assert str(life.state) == "closed"

    def test_lifecycle_start_without_open_raises(self) -> None:
        life = AdapterLifecycle()
        with pytest.raises(RuntimeError):
            life.start()

    def test_lifecycle_open_idempotent(self) -> None:
        life = AdapterLifecycle()
        life.open()
        life.open()
        assert life.state == "open"
