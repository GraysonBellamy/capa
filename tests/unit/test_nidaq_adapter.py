"""Unit tests for :class:`capa.devices.nidaq.NIDAQAdapter` (P2).

Drives the real adapter against :class:`nidaqlib.backend.fake.FakeDaqBackend`
— the library's purpose-built test backend that satisfies the full
:class:`DaqSession` contract without touching ``nidaqmx``.
"""

from __future__ import annotations

from typing import Any

import pytest
from nidaqlib.backend.fake import FakeDaqBackend

from capa.channels.calibration import Identity
from capa.channels.spec import (
    ChannelKind,
    ChannelSpec,
    NIDAQBlockChannel,
    NIDAQReadingField,
)
from capa.core.errors import AdapterError
from capa.devices.adapter import Capability as CapaCapability
from capa.devices.nidaq import (
    ADAPTER_ID_BLOCK,
    ADAPTER_ID_POLLED,
    NIDAQAdapter,
    NIDAQAdapterParams,
    NIDAQTimingParams,
)
from capa.devices.records import (
    ChannelSample,
    DeviceSnapshot,
    SourceRecord,
)

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _voltage_channels() -> list[dict[str, Any]]:
    return [
        {
            "kind": "ai_voltage",
            "physical_channel": "Dev1/ai0",
            "name": "AI0",
            "unit": "V",
            "min_val": -10.0,
            "max_val": 10.0,
        },
        {
            "kind": "ai_voltage",
            "physical_channel": "Dev1/ai1",
            "name": "AI1",
            "unit": "V",
            "min_val": -10.0,
            "max_val": 10.0,
        },
    ]


def _capa_channels() -> list[ChannelSpec]:
    return [
        ChannelSpec(
            name="ai0_volts",
            kind=ChannelKind.ANALOG_IN,
            source=NIDAQReadingField(device="daq1", task="task1", field="AI0"),
            unit="V",
            derived_unit="V",
            calibration=Identity(input_unit="V", output_unit="V"),
        ),
        ChannelSpec(
            name="ai1_volts",
            kind=ChannelKind.ANALOG_IN,
            source=NIDAQReadingField(device="daq1", task="task1", field="AI1"),
            unit="V",
            derived_unit="V",
            calibration=Identity(input_unit="V", output_unit="V"),
        ),
    ]


def _make_adapter(
    *,
    name: str = "daq1",
    rate_hz: float = 50.0,
    snapshot_period_s: float = 1e6,
    timing: NIDAQTimingParams | None = None,
) -> tuple[NIDAQAdapter, FakeDaqBackend]:
    backend = FakeDaqBackend(read_block_default_shape=(2, 1))
    adapter = NIDAQAdapter(
        name=name,
        task_name="task1",
        channels=tuple(_voltage_channels()),
        rate_hz=rate_hz,
        snapshot_period_s=snapshot_period_s,
        timing=timing.model_dump() if timing is not None else None,
        backend=backend,
    )
    adapter.configure_channels(_capa_channels())
    return adapter, backend


def _split(
    emissions: list[Any],
) -> tuple[list[SourceRecord], list[ChannelSample], list[DeviceSnapshot]]:
    return (
        [e for e in emissions if isinstance(e, SourceRecord)],
        [e for e in emissions if isinstance(e, ChannelSample)],
        [e for e in emissions if isinstance(e, DeviceSnapshot)],
    )


async def _drain(adapter: NIDAQAdapter, *, max_records: int) -> list[Any]:
    emissions: list[Any] = []
    record_count = 0
    async for emission in adapter.stream():
        emissions.append(emission)
        if isinstance(emission, SourceRecord):
            record_count += 1
            if record_count >= max_records:
                await adapter.stop()
    return emissions


# ---------------------------------------------------------------------------
# Params
# ---------------------------------------------------------------------------


class TestParams:
    def test_polled_default(self) -> None:
        p = NIDAQAdapterParams(task_name="t", channels=tuple(_voltage_channels()))
        assert p.is_block_mode() is False
        assert p.adapter_id() == ADAPTER_ID_POLLED

    def test_block_mode_when_continuous(self) -> None:
        p = NIDAQAdapterParams(
            task_name="t",
            channels=tuple(_voltage_channels()),
            timing={"rate_hz": 1000.0, "mode": "continuous"},
        )
        assert p.is_block_mode() is True
        assert p.adapter_id() == ADAPTER_ID_BLOCK

    def test_polled_when_on_demand(self) -> None:
        p = NIDAQAdapterParams(
            task_name="t",
            channels=tuple(_voltage_channels()),
            timing={"rate_hz": 10.0, "mode": "on_demand"},
        )
        assert p.is_block_mode() is False

    def test_empty_channels_rejected(self) -> None:
        with pytest.raises(Exception):
            NIDAQAdapterParams(task_name="t", channels=())

    def test_build_task_spec_round_trips_channels(self) -> None:
        p = NIDAQAdapterParams(task_name="t", channels=tuple(_voltage_channels()))
        spec = p.build_task_spec()
        assert spec.name == "t"
        assert len(spec.channels) == 2
        assert spec.channels[0].physical_channel == "Dev1/ai0"


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_baseline_capabilities(self) -> None:
        a = NIDAQAdapter(
            name="daq1",
            task_name="t",
            channels=tuple(_voltage_channels()),
        )
        assert CapaCapability.READS_PROCESS_VAR in a.capabilities
        assert CapaCapability.SUPPORTS_DISCOVERY in a.capabilities
        assert CapaCapability.HARDWARE_CLOCKED not in a.capabilities

    def test_block_mode_capabilities(self) -> None:
        a = NIDAQAdapter(
            name="daq1",
            task_name="t",
            channels=tuple(_voltage_channels()),
            timing={"rate_hz": 1000.0, "mode": "continuous"},
        )
        assert CapaCapability.HARDWARE_CLOCKED in a.capabilities
        assert CapaCapability.EMITS_BLOCKS in a.capabilities

    def test_rejects_both_params_and_kwargs(self) -> None:
        params = NIDAQAdapterParams(task_name="t", channels=tuple(_voltage_channels()))
        with pytest.raises(TypeError):
            NIDAQAdapter(name="daq1", params=params, task_name="other")


# ---------------------------------------------------------------------------
# Lifecycle + streaming (polled mode against FakeDaqBackend)
# ---------------------------------------------------------------------------


class TestStreamPolled:
    async def test_open_then_stream(self) -> None:
        adapter, _backend = _make_adapter(rate_hz=100.0)
        await adapter.open()
        try:
            await adapter.start()
            emissions = await _drain(adapter, max_records=2)
        finally:
            await adapter.close()
        records, samples, snapshots = _split(emissions)
        assert len(records) >= 2
        assert all(r.shape == "wide_row" for r in records)
        assert all(r.adapter == ADAPTER_ID_POLLED for r in records)
        # Two channels declared; each polled tick yields two ChannelSamples.
        per_tick = len(samples) // len(records)
        assert per_tick == 2
        # Channel sample names round-trip correctly.
        assert {s.channel for s in samples} == {"ai0_volts", "ai1_volts"}
        # Initial DeviceSnapshot lands first.
        assert snapshots
        assert snapshots[0].adapter == ADAPTER_ID_POLLED
        # Native row preserves NI-style fields.
        for r in records:
            assert "AI0" in r.row or "AI1" in r.row


# ---------------------------------------------------------------------------
# Lifecycle + streaming (hardware-clocked block mode against FakeDaqBackend)
# ---------------------------------------------------------------------------


def _block_capa_channels() -> list[ChannelSpec]:
    return [
        ChannelSpec(
            name="ai0_volts",
            kind=ChannelKind.ANALOG_IN,
            source=NIDAQBlockChannel(device="daq1", task="task1", channel="AI0"),
            unit="V",
            derived_unit="V",
            calibration=Identity(input_unit="V", output_unit="V"),
        ),
        ChannelSpec(
            name="ai1_volts",
            kind=ChannelKind.ANALOG_IN,
            source=NIDAQBlockChannel(device="daq1", task="task1", channel="AI1"),
            unit="V",
            derived_unit="V",
            calibration=Identity(input_unit="V", output_unit="V"),
        ),
    ]


def _make_block_adapter(
    *,
    sample_rate_hz: float = 100.0,
    samples_per_channel: int = 10,
    n_blocks: int = 3,
    max_samples_per_block_unroll: int = 10_000,
) -> tuple[NIDAQAdapter, FakeDaqBackend]:
    """Build an adapter wired to a ``FakeDaqBackend`` with scripted blocks.

    Each scripted block has shape ``(2, samples_per_channel)`` — one row per
    declared analog-input channel. Channel 0 ramps with the absolute sample
    index; channel 1 is ``10×`` channel 0 so per-channel ordering is easy
    to assert against.
    """
    import numpy as np

    blocks = []
    for b in range(n_blocks):
        ch0 = np.arange(b * samples_per_channel, (b + 1) * samples_per_channel, dtype=np.float64)
        ch1 = ch0 * 10.0
        blocks.append(np.stack([ch0, ch1]))
    backend = FakeDaqBackend(
        blocks={"task1": blocks},
        # Once scripted blocks are exhausted the recorder may prefetch one
        # more before the test signals stop; synthesise same-shape blocks
        # rather than raising in that race.
        read_block_default_shape=(2, samples_per_channel),
    )
    timing = NIDAQTimingParams(
        rate_hz=sample_rate_hz,
        mode="continuous",
        samples_per_channel=samples_per_channel,
    )
    adapter = NIDAQAdapter(
        name="daq1",
        task_name="task1",
        channels=tuple(_voltage_channels()),
        timing=timing.model_dump(),
        snapshot_period_s=1e6,
        max_samples_per_block_unroll=max_samples_per_block_unroll,
        backend=backend,
    )
    adapter.configure_channels(_block_capa_channels())
    return adapter, backend


class TestStreamHardwareClocked:
    async def test_one_record_per_block(self) -> None:
        adapter, _ = _make_block_adapter(samples_per_channel=4, n_blocks=2)
        await adapter.open()
        try:
            await adapter.start()
            emissions = await _drain(adapter, max_records=2)
        finally:
            await adapter.close()
        records, _samples, _snaps = _split(emissions)
        assert len(records) >= 2
        for r in records:
            assert r.adapter == ADAPTER_ID_BLOCK
            assert r.shape == "wide_row"
            for key in (
                "block_index",
                "first_sample_index",
                "samples_per_channel",
                "sample_rate_hz",
                "channels",
                "task_started_at",
            ):
                assert key in r.row

    async def test_unroll_emits_n_times_c_samples_per_block(self) -> None:
        n = 5
        adapter, _ = _make_block_adapter(samples_per_channel=n, n_blocks=2)
        await adapter.open()
        try:
            await adapter.start()
            emissions = await _drain(adapter, max_records=2)
        finally:
            await adapter.close()
        records, samples, _ = _split(emissions)
        # Two declared NIDAQBlockChannel bindings (AI0, AI1); two blocks.
        # Expect at least n*2 samples per block (only counting the first 2 records).
        per_block_samples = [
            [s for s in samples if s.source_record_id == r.record_id] for r in records[:2]
        ]
        for group in per_block_samples:
            assert len(group) == n * 2

    async def test_timestamps_monotonic_with_exact_period(self) -> None:
        rate = 200.0
        adapter, _ = _make_block_adapter(sample_rate_hz=rate, samples_per_channel=8, n_blocks=2)
        await adapter.open()
        try:
            await adapter.start()
            emissions = await _drain(adapter, max_records=2)
        finally:
            await adapter.close()
        _, samples, _ = _split(emissions)
        # Group samples by channel, in emission order.
        ai0 = [s for s in samples if s.channel == "ai0_volts"]
        ai1 = [s for s in samples if s.channel == "ai1_volts"]
        assert len(ai0) >= 2 and len(ai1) >= 2
        expected_step_ns = int(1e9 / rate)
        for series in (ai0, ai1):
            diffs = [series[i + 1].t_mono_ns - series[i].t_mono_ns for i in range(len(series) - 1)]
            assert all(d == expected_step_ns for d in diffs), diffs

    async def test_source_record_id_back_pointers(self) -> None:
        adapter, _ = _make_block_adapter(samples_per_channel=3, n_blocks=2)
        await adapter.open()
        try:
            await adapter.start()
            emissions = await _drain(adapter, max_records=2)
        finally:
            await adapter.close()
        records, samples, _ = _split(emissions)
        record_ids = {r.record_id for r in records}
        assert record_ids
        assert all(s.source_record_id in record_ids for s in samples)

    async def test_channel_ordering_preserved(self) -> None:
        # Block b=0: ch0=[0,1,2,3], ch1=[0,10,20,30]
        adapter, _ = _make_block_adapter(samples_per_channel=4, n_blocks=1)
        await adapter.open()
        try:
            await adapter.start()
            emissions = await _drain(adapter, max_records=1)
        finally:
            await adapter.close()
        records, samples, _ = _split(emissions)
        first_record_id = records[0].record_id
        ai0_vals = [
            s.value
            for s in samples
            if s.channel == "ai0_volts" and s.source_record_id == first_record_id
        ]
        ai1_vals = [
            s.value
            for s in samples
            if s.channel == "ai1_volts" and s.source_record_id == first_record_id
        ]
        assert ai0_vals == [0.0, 1.0, 2.0, 3.0]
        assert ai1_vals == [0.0, 10.0, 20.0, 30.0]

    async def test_capability_flags(self) -> None:
        adapter, _ = _make_block_adapter()
        assert CapaCapability.HARDWARE_CLOCKED in adapter.capabilities
        assert CapaCapability.EMITS_BLOCKS in adapter.capabilities

    async def test_guardrail_rejects_oversize_block(self) -> None:
        # 20_000 samples per channel exceeds the default cap of 10_000.
        adapter, _ = _make_block_adapter(
            samples_per_channel=20_000, n_blocks=1, max_samples_per_block_unroll=10_000
        )
        with pytest.raises(AdapterError, match="max_samples_per_block_unroll"):
            await adapter.open()

    async def test_watchdog_period_uses_chunk_in_block_mode(self) -> None:
        rate = 100.0
        chunk = 25
        adapter, _ = _make_block_adapter(sample_rate_hz=rate, samples_per_channel=chunk, n_blocks=1)
        await adapter.open()
        try:
            state = adapter.watchdog_state()
            assert state.expected_period_ns == int(1e9 * chunk / rate)
        finally:
            await adapter.close()


# ---------------------------------------------------------------------------
# Authorization gate (P2: command surface rejects unsupported verbs *after*
# the auth gate)
# ---------------------------------------------------------------------------


class TestCommandsP2:
    async def test_unauthorized_command_refused(self) -> None:
        adapter, _ = _make_adapter()
        await adapter.open()
        try:
            from capa.devices.adapter import DeviceCommand

            cmd = DeviceCommand(
                kind="set_setpoint",
                target=None,
                payload={"value": 1.0},
                issued_by="alice",
                # no authorization_id, no confirmed_by
            )
            result = await adapter.command(cmd)
            assert result.accepted is False
        finally:
            await adapter.close()

    async def test_authorized_command_rejected_in_p2(self) -> None:
        """Authorized commands still raise: AO/DO writes land in P3."""
        adapter, _ = _make_adapter()
        await adapter.open()
        try:
            from capa.devices.adapter import DeviceCommand

            cmd = DeviceCommand(
                kind="set_setpoint",
                target=None,
                payload={"value": 1.0},
                issued_by="alice",
                authorization_id="run-x",
            )
            with pytest.raises(AdapterError, match="P2"):
                await adapter.command(cmd)
        finally:
            await adapter.close()


# ---------------------------------------------------------------------------
# Watchdog state
# ---------------------------------------------------------------------------


class TestWatchdog:
    async def test_silent_far_future(self) -> None:
        """A running adapter that hasn't emitted in many periods → ``is_silent``.

        ``_drain(max_records=1)`` calls ``adapter.stop()`` before returning, so
        the live ``watchdog_state()`` reports ``lifecycle_state="open"`` and
        the new grace logic suppresses silence. Reconstruct the state with
        ``lifecycle_state="running"`` to assert the *time math* in isolation
        — the lifecycle gating itself is covered by tests in
        ``test_adapter_helpers.TestWatchdogState``.
        """
        from capa.devices._helpers import WatchdogState

        adapter, _ = _make_adapter(rate_hz=50.0)
        await adapter.open()
        try:
            await adapter.start()
            await _drain(adapter, max_records=1)
            live = adapter.watchdog_state()
            assert live.last_t_mono_ns is not None
            running = WatchdogState(
                device=live.device,
                last_t_mono_ns=live.last_t_mono_ns,
                expected_period_ns=live.expected_period_ns,
                lifecycle_state="running",
            )
            far_future = (running.last_t_mono_ns or 0) + 10 * running.expected_period_ns
            assert running.is_silent(now_t_mono_ns=far_future)
        finally:
            await adapter.close()


class TestDeviceInfoProbe:
    """``device_info`` is populated by matching channel module against
    ``nidaqlib.system.discovery.list_devices``. Tests stub the discovery
    function so they don't touch ``nidaqmx``."""

    async def test_device_info_none_when_discovery_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # FakeDaqBackend doesn't go through the real ``list_devices``; with
        # nothing returned the probe must yield None and not raise.
        import nidaqlib.system.discovery as discovery

        monkeypatch.setattr(discovery, "list_devices", lambda: [])
        adapter, _ = _make_adapter()
        await adapter.open()
        try:
            assert adapter.device_info is None
        finally:
            await adapter.close()

    async def test_device_info_populated_from_module_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A channel ``Dev1/ai0`` matches a ``Dev1`` device row → identity wired."""
        import nidaqlib.system.discovery as discovery

        class _FakeDevice:
            def __init__(self, name: str, product_type: str, serial: int | str | None) -> None:
                self.name = name
                self.product_type = product_type
                self.serial_number = serial
                self.ai_physical_channels: tuple[str, ...] = ()
                self.ao_physical_channels: tuple[str, ...] = ()
                self.di_lines: tuple[str, ...] = ()
                self.do_lines: tuple[str, ...] = ()
                self.ci_physical_channels: tuple[str, ...] = ()
                self.co_physical_channels: tuple[str, ...] = ()

        # Single-board device (no chassis).
        monkeypatch.setattr(
            discovery,
            "list_devices",
            lambda: [_FakeDevice("Dev1", "PCIe-6320", 12345678)],
        )
        adapter, _ = _make_adapter()
        await adapter.open()
        try:
            info = adapter.device_info
            assert info is not None
            assert info.product_type == "PCIe-6320"
            assert info.serial_number == "12345678"  # int coerced to str
            assert info.physical_module == "Dev1"
            assert info.chassis is None
        finally:
            await adapter.close()

    async def test_device_info_resolves_cdaq_chassis(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``cDAQ1Mod1/ai0`` → module = ``cDAQ1Mod1``, chassis = ``cDAQ1`` when both exist."""
        import nidaqlib.system.discovery as discovery

        class _FakeDevice:
            def __init__(self, name: str, product_type: str, serial: int | None) -> None:
                self.name = name
                self.product_type = product_type
                self.serial_number = serial
                self.ai_physical_channels: tuple[str, ...] = ()
                self.ao_physical_channels: tuple[str, ...] = ()
                self.di_lines: tuple[str, ...] = ()
                self.do_lines: tuple[str, ...] = ()
                self.ci_physical_channels: tuple[str, ...] = ()
                self.co_physical_channels: tuple[str, ...] = ()

        monkeypatch.setattr(
            discovery,
            "list_devices",
            lambda: [
                _FakeDevice("cDAQ1", "cDAQ-9171", 31195776),
                _FakeDevice("cDAQ1Mod1", "NI 9214", 26994925),
            ],
        )

        backend = FakeDaqBackend(read_block_default_shape=(2, 1))
        adapter = NIDAQAdapter(
            name="cdaq1",
            task_name="task1",
            channels=(
                {
                    "kind": "ai_voltage",
                    "physical_channel": "cDAQ1Mod1/ai0",
                    "name": "AI0",
                    "min_val": -10.0,
                    "max_val": 10.0,
                },
            ),
            rate_hz=50.0,
            snapshot_period_s=1e6,
            backend=backend,
        )
        await adapter.open()
        try:
            info = adapter.device_info
            assert info is not None
            assert info.product_type == "NI 9214"
            assert info.serial_number == "26994925"
            assert info.physical_module == "cDAQ1Mod1"
            assert info.chassis == "cDAQ1"
        finally:
            await adapter.close()

    async def test_stream_until_stopped_max_records(self) -> None:
        """Helper stops on its own once ``max_records`` records have arrived,
        and ``close()`` afterwards must not deadlock — proving the inner
        record_polled async-with was wound down properly."""
        adapter, _ = _make_adapter(rate_hz=200.0)
        await adapter.open()
        try:
            await adapter.start()
            records: list[SourceRecord] = []
            async for emission in adapter.stream_until_stopped(max_records=3):
                if isinstance(emission, SourceRecord):
                    records.append(emission)
            assert len(records) >= 3
            # The real assertion: close() returns promptly. Pre-fix this would
            # block on the inner record_polled session lock if we'd ``break``
            # from the loop instead of using the helper.
        finally:
            await adapter.close()

    async def test_stream_until_stopped_external_stop(self) -> None:
        """Calling ``adapter.stop()`` from outside also unwinds cleanly."""
        adapter, _ = _make_adapter(rate_hz=200.0)
        await adapter.open()
        try:
            await adapter.start()
            received: list[Any] = []
            count = 0
            async for emission in adapter.stream_until_stopped():
                received.append(emission)
                count += 1
                if count == 5:
                    await adapter.stop()
            assert len(received) >= 5
        finally:
            await adapter.close()

    async def test_stream_until_stopped_rejects_non_positive_budget(self) -> None:
        adapter, _ = _make_adapter()
        await adapter.open()
        try:
            await adapter.start()
            with pytest.raises(ValueError, match="max_records"):
                async for _ in adapter.stream_until_stopped(max_records=0):
                    pass
            with pytest.raises(ValueError, match="max_emissions"):
                async for _ in adapter.stream_until_stopped(max_emissions=-1):
                    pass
        finally:
            await adapter.close()

    async def test_device_info_cleared_on_close(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import nidaqlib.system.discovery as discovery

        class _FakeDevice:
            name = "Dev1"
            product_type = "PCIe-6320"
            serial_number = 1
            ai_physical_channels: tuple[str, ...] = ()
            ao_physical_channels: tuple[str, ...] = ()
            di_lines: tuple[str, ...] = ()
            do_lines: tuple[str, ...] = ()
            ci_physical_channels: tuple[str, ...] = ()
            co_physical_channels: tuple[str, ...] = ()

        monkeypatch.setattr(discovery, "list_devices", lambda: [_FakeDevice()])
        adapter, _ = _make_adapter()
        await adapter.open()
        assert adapter.device_info is not None
        await adapter.close()
        assert adapter.device_info is None
