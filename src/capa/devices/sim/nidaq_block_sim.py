"""Simulated NI-DAQ hardware-clocked block adapter.

rectangular ``DaqBlock`` records stay block-shaped — capa
deliberately does not scalarize kHz data through Python. The ``SourceRecord``
emitted here therefore carries ``shape="block"`` with a ``block_ref`` and an
empty ``row``; the block payload itself is held in adapter state for the
block sidecar / TDMS-passthrough plumbing to consume.

Per-channel ``ChannelSample`` derivation at low rate is out of scope for
the block adapter: the block adapter is for kHz data, and emitting per-sample
``ChannelSample``\\ s defeats the whole reason it stays block-shaped. A configurable
downsampler can emit one ``ChannelSample`` per second (mean / min / max) per
channel; the binding for those derived rows is
:class:`~capa.channels.spec.NIDAQBlockChannel`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

import anyio
import numpy as np
from nidaqlib import DaqBlock

from capa.channels.spec import ChannelSpec
from capa.core.clock import RunClock
from capa.core.errors import AdapterError
from capa.devices.adapter import (
    AdapterLifecycle,
    AdapterStartContext,
    Capability,
    CommandResult,
    DeviceCommand,
)
from capa.devices.records import (
    DeviceEmission,
    DeviceSnapshot,
    SourceRecord,
)
from capa.devices.sim._base import (
    channels_for_device,
    make_accepted_result,
    make_record_id,
    now_utc,
    reject_unless_authorized,
    synth_timing,
)
from capa.devices.sim._signals import SignalFn

if TYPE_CHECKING:
    from capa.devices.registry import (
        AdapterDescriptor,
    )

ADAPTER_ID: Final[str] = "nidaq_block"


@dataclass(slots=True)
class NIDAQBlockSim:
    """Simulated hardware-clocked NI-DAQ task adapter.

    ``signals`` is keyed by channel display name and evaluated at every block
    sample to produce the rectangular ``data`` array.
    """

    name: str
    task: str = "block_task"
    sample_rate_hz: float = 1000.0
    block_size: int = 1000
    """Samples per channel per block. ``sample_rate_hz / block_size`` is the
    block rate."""
    signals: dict[str, SignalFn] = field(default_factory=dict)
    units: dict[str, str | None] = field(default_factory=dict)
    capabilities: frozenset[Capability] = frozenset(
        {
            Capability.HARDWARE_CLOCKED,
            Capability.EMITS_BLOCKS,
            Capability.SUPPORTS_DISCOVERY,
        }
    )
    _lifecycle: AdapterLifecycle = field(default_factory=AdapterLifecycle)
    _channels: list[ChannelSpec] = field(default_factory=list)
    _clock: RunClock | None = None
    _seq: int = 0
    _block_index: int = 0
    _first_sample_index: int = 0
    _task_started_at_utc: datetime | None = None
    _blocks: list[DaqBlock] = field(default_factory=list)
    """In-memory log of emitted blocks. The block sidecar reads from this
    when finalizing the bundle; tests inspect it directly."""

    @property
    def block_period_s(self) -> float:
        return self.block_size / self.sample_rate_hz

    def configure_channels(self, specs: list[ChannelSpec]) -> None:
        self._channels = channels_for_device(
            specs, device=self.name, binding_source="nidaq_block_channel"
        )

    @property
    def expected_emission_rate_hz(self) -> float:
        # Per-sample ChannelSamples dominate; the per-block SourceRecord
        # adds a negligible blocks/s term.
        return self.sample_rate_hz * len(self._channels)

    @property
    def resource_id(self) -> str:
        return f"sim:{self.name}"

    async def open(self) -> None:
        self._lifecycle.open()

    async def close(self) -> None:
        self._lifecycle.close()

    async def start(self, ctx: AdapterStartContext) -> None:
        self._lifecycle.start()
        self._clock = ctx.clock
        self._block_index = 0
        self._first_sample_index = 0
        self._task_started_at_utc = self._clock.started_utc

    async def stop(self) -> None:
        self._lifecycle.stop()

    async def snapshot(self) -> DeviceSnapshot:
        clock = self._clock or RunClock.now()
        return DeviceSnapshot(
            adapter=ADAPTER_ID,
            device=self.name,
            t_mono_ns=clock.t_mono_ns(),
            t_utc=now_utc(),
            health="ok" if self._lifecycle.state == "running" else "down",
            fields={
                "task": self.task,
                "sample_rate_hz": self.sample_rate_hz,
                "block_size": self.block_size,
                "block_index": self._block_index,
                "channel_count": len(self.signals),
                "state": self._lifecycle.state,
            },
        )

    async def stream(self) -> AsyncIterator[DeviceEmission]:
        if self._clock is None:
            raise AdapterError("nidaq_block_sim.stream() requires start() first")
        while self._lifecycle.state == "running":
            for emission in self.tick_once():
                yield emission
            await anyio.sleep(self.block_period_s)

    def tick_once(self) -> list[DeviceEmission]:
        """Emit one rectangular block."""
        if self._clock is None or self._task_started_at_utc is None:
            raise AdapterError("nidaq_block_sim.tick_once() requires start() first")
        clock = self._clock
        task_started_at = self._task_started_at_utc
        if not self.signals:
            raise AdapterError(f"nidaq_block_sim {self.name!r}: at least one signal is required")
        _emit_mono_ns, _req, _rec, midpoint_at, _t_utc, _lat = synth_timing(clock)

        channels = tuple(self.signals.keys())
        n_channels = len(channels)

        # Per-sample times within the block:
        # absolute_index = first_sample_index + k
        # t_s = absolute_index / sample_rate_hz
        sample_indices = np.arange(self.block_size, dtype=np.int64)
        absolute = self._first_sample_index + sample_indices
        t_s = absolute.astype(np.float64) / self.sample_rate_hz

        data = np.empty((n_channels, self.block_size), dtype=np.float64)
        for row, channel in enumerate(channels):
            sig = self.signals[channel]
            for col, ts in enumerate(t_s):
                data[row, col] = float(sig(float(ts)))

        units = {ch: self.units.get(ch) for ch in channels}

        block_period_ns = int(1e9 / self.sample_rate_hz)
        # block.t_mono_ns is the absolute time.monotonic_ns() of sample 0.
        # Lock it to the task-start anchor so per-sample reconstruction
        # (block.t_mono_ns + k * block_period_ns) lands on a uniform grid
        # across blocks — matching what NI's hardware-clocked path produces.
        block_t_mono_ns = clock.started_mono_ns + self._first_sample_index * block_period_ns
        block_t_utc = task_started_at + timedelta(
            seconds=self._first_sample_index / self.sample_rate_hz
        )
        block = DaqBlock(
            device=self.name,
            task=self.task,
            channels=channels,
            data=data,
            block_index=self._block_index,
            first_sample_index=self._first_sample_index,
            samples_per_channel=self.block_size,
            block_period_ns=block_period_ns,
            task_started_at=task_started_at,
            t0=block_t_utc,
            t_mono_ns=block_t_mono_ns,
            t_utc=block_t_utc,
            t_midpoint_mono_ns=block_t_mono_ns + (self.block_size * block_period_ns) // 2,
            read_started_at=midpoint_at,
            read_finished_at=midpoint_at,
            elapsed_s=self.block_period_s,
            units=MappingProxyType(units),
        )
        self._blocks.append(block)

        self._seq += 1
        record_id = make_record_id(ADAPTER_ID, self.name, self._seq)
        # block_ref points at the in-memory log entry by index; rewritten to
        # a file path when block sidecars land.
        block_ref = f"memory:{self.name}:{self._block_index}"
        record = SourceRecord(
            record_id=record_id,
            adapter=ADAPTER_ID,
            device=self.name,
            shape="block",
            t_mono_ns=block_t_mono_ns - clock.started_mono_ns,
            t_utc=block_t_utc,
            row={},
            block_ref=block_ref,
            metadata={
                "task": self.task,
                "channels": list(channels),
                "block_index": self._block_index,
                "first_sample_index": self._first_sample_index,
                "samples_per_channel": self.block_size,
                "sample_rate_hz": self.sample_rate_hz,
            },
        )

        self._block_index += 1
        self._first_sample_index += self.block_size

        return [record]

    @property
    def emitted_blocks(self) -> list[DaqBlock]:
        """In-memory log of blocks emitted since :meth:`start`."""
        return list(self._blocks)

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        clock = self._clock or RunClock.now()
        rejection = reject_unless_authorized(
            cmd, adapter_id=ADAPTER_ID, device_name=self.name, clock=clock
        )
        if rejection is not None:
            return rejection
        return make_accepted_result(detail=f"sim ack {cmd.kind}", clock=clock)


__all__ = ["ADAPTER_ID", "DESCRIPTOR", "NIDAQBlockSim"]


def _build_descriptor() -> AdapterDescriptor:
    from capa.devices.registry import AdapterDescriptor  # noqa: PLC0415

    return AdapterDescriptor(
        id="capa.devices.sim.nidaq_block_sim",
        label="NI-DAQ hardware-clocked block (simulated)",
        family="sim",
        adapter_factory=NIDAQBlockSim,
        params_model=None,
        supported_binding_sources=("nidaq_block_channel",),
        default_params={},
        channel_templates=(),
    )


DESCRIPTOR = _build_descriptor()

from capa.devices.registry import register as _register  # noqa: E402

_register(DESCRIPTOR)
