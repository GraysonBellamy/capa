"""Simulated NI-DAQ hardware-clocked block adapter.

Plan §5.6 / §8.7: rectangular ``DaqBlock`` records stay block-shaped — capa
deliberately does not scalarize kHz data through Python. The ``SourceRecord``
emitted here therefore carries ``shape="block"`` with a ``block_ref`` and an
empty ``row``; the block payload itself is held in adapter state for P0b's
block sidecar / TDMS-passthrough plumbing to consume. P0a tests verify the
shape and the metadata.

Per-channel ``ChannelSample`` derivation at low rate is **out of scope for
P0a**: the block adapter is for kHz data, and emitting per-sample
``ChannelSample``\\ s defeats the whole reason it stays block-shaped. P3
introduces a configurable downsampler that emits one ``ChannelSample`` per
second (mean / min / max) per channel; the binding for those derived rows is
:class:`~capa.channels.spec.NIDAQBlockChannel`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

import anyio
import numpy as np
from nidaqlib.tasks.models import DaqBlock

from capa.channels.spec import ChannelSpec
from capa.core.clock import RunClock
from capa.core.errors import AdapterError
from capa.devices.adapter import (
    AdapterLifecycle,
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
    make_record_id,
    now_utc,
    synth_timing,
)
from capa.devices.sim._signals import SignalFn

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
    _task_started_at_utc: object | None = None
    _blocks: list[DaqBlock] = field(default_factory=list)
    """In-memory log of emitted blocks. P0b's block sidecar reads from this
    when finalizing the bundle; P0a tests inspect it directly."""

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

    async def open(self) -> None:
        self._lifecycle.open()

    async def close(self) -> None:
        self._lifecycle.close()

    async def start(self, clock: RunClock | None = None) -> None:
        self._lifecycle.start()
        self._clock = clock or RunClock.now()
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
            healthy=self._lifecycle.state == "running",
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
        if self._clock is None:
            raise AdapterError("nidaq_block_sim.tick_once() requires start() first")
        clock = self._clock
        if not self.signals:
            raise AdapterError(f"nidaq_block_sim {self.name!r}: at least one signal is required")
        t_mono_ns, _req, _rec, midpoint_at, _t_utc, _lat = synth_timing(clock)

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

        block = DaqBlock(
            device=self.name,
            task=self.task,
            channels=channels,
            data=data,
            block_index=self._block_index,
            first_sample_index=self._first_sample_index,
            samples_per_channel=self.block_size,
            sample_rate_hz=self.sample_rate_hz,
            dt_s=1.0 / self.sample_rate_hz,
            task_started_at=self._task_started_at_utc,  # type: ignore[arg-type]
            t0=midpoint_at,
            monotonic_ns=t_mono_ns,
            read_started_at=midpoint_at,
            read_finished_at=midpoint_at,
            elapsed_s=self.block_period_s,
            units=MappingProxyType(units),
        )
        self._blocks.append(block)

        self._seq += 1
        record_id = make_record_id(ADAPTER_ID, self.name, self._seq)
        # block_ref points at the in-memory log entry by index. P0b will
        # rewrite this to a file path when block sidecars land.
        block_ref = f"memory:{self.name}:{self._block_index}"
        record = SourceRecord(
            record_id=record_id,
            adapter=ADAPTER_ID,
            device=self.name,
            shape="block",
            t_mono_ns=t_mono_ns,
            t_utc=midpoint_at,
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
        if cmd.authorization_id is None and cmd.confirmed_by is None:
            return CommandResult(
                accepted=False,
                detail="nidaq_block_sim refuses unauthorized commands",
                t_mono_ns=(self._clock or RunClock.now()).t_mono_ns(),
                t_utc=now_utc(),
            )
        clock = self._clock or RunClock.now()
        return CommandResult(
            accepted=True,
            detail=f"sim ack {cmd.kind}",
            t_mono_ns=clock.t_mono_ns(),
            t_utc=now_utc(),
        )


__all__ = ["ADAPTER_ID", "NIDAQBlockSim"]
