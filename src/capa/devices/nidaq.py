"""Real :class:`NIDAQAdapter` — wraps a :class:`nidaqlib.tasks.session.DaqSession` (P2).

Plan §16 P2 entry: "real ``NIDAQAdapter``. Capability flags. Device watchdogs
and health surfacing. Discovery (``capa devices discover``).
``capa validate --strict``."

One adapter == one NI task. The ``params`` block describes the task
declaratively: ``task_name``, a list of ``channels`` (dicts that
:meth:`nidaqlib.channels.ChannelSpec.from_dict` can revive), optional
``timing`` (sample-clock + acquisition mode for hardware-clocked tasks),
optional ``tdms`` driver-side logging, and ``rate_hz`` (the recorder's
polling cadence in software-timed mode).

Two emission shapes (plan §5.6):

* **Polled (software-timed).** ``timing is None`` or ``timing.mode ==
  on_demand``. The adapter drives :func:`nidaqlib.streaming.record_polled`
  and turns each :class:`DaqReading` into one wide-row :class:`SourceRecord`
  plus one :class:`ChannelSample` per matching :class:`NIDAQReadingField`
  binding. This is the common cone-rig case (3–60 Hz scalar TC + AI).
* **Hardware-clocked block.** ``timing.mode in {finite, continuous}``.
  The adapter drives :func:`nidaqlib.streaming.block.record` at NI's
  onboard sample clock and emits, per :class:`DaqBlock`, one
  ``shape="wide_row"`` :class:`SourceRecord` of block metadata plus one
  :class:`ChannelSample` per ``(channel, sample)`` for every
  :class:`NIDAQBlockChannel` binding. Per-sample timestamps come from
  ``task_started_at + (first_sample_index + k) / sample_rate_hz``. The
  unroll is gated by :attr:`NIDAQAdapterParams.max_samples_per_block_unroll`
  so kHz acquisition cannot accidentally land on this path; the
  rectangular-block sidecar / TDMS escape (plan §8.7) is P3 territory.

For tests, an opt-in ``backend`` kwarg is forwarded to
:func:`nidaqlib.tasks.open_device` so a
:class:`~nidaqlib.backend.fake.FakeDaqBackend` drives the adapter without
touching real hardware. Alternatively, an ``session_factory`` kwarg supplies
a fully pre-built :class:`DaqSession` for tests that need finer control.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any, Final, Literal

from nidaqlib.channels.base import ChannelSpec as NidaqChannelSpec
from nidaqlib.errors import NIDaqError
from nidaqlib.streaming import OverflowPolicy, record_polled
from nidaqlib.streaming.block import ErrorPolicy as NidaqErrorPolicy
from nidaqlib.tasks import open_device as nidaq_open_device
from nidaqlib.tasks.models import DaqBlock, DaqReading
from nidaqlib.tasks.session import DaqSession
from nidaqlib.tasks.spec import AcquisitionMode, TaskSpec, Timing
from pydantic import BaseModel, ConfigDict, Field, field_validator

from capa.channels.spec import ChannelSpec, NIDAQBlockChannel, NIDAQReadingField
from capa.core.clock import RunClock
from capa.core.errors import AdapterError
from capa.devices._helpers import (
    LastSampleTracker,
    WatchdogState,
    build_channel_sample,
    channels_for_device,
    make_not_open_result,
    make_record_id,
    reject_unless_authorized,
)
from capa.devices.adapter import (
    AdapterLifecycle,
    Capability,
    CommandResult,
    DeviceCommand,
)
from capa.devices.records import (
    DeviceEmission,
    DeviceHealth,
    DeviceSnapshot,
    SourceRecord,
)

ADAPTER_ID_POLLED: Final[str] = "nidaq_polled"
ADAPTER_ID_BLOCK: Final[str] = "nidaq_block"


# ---------------------------------------------------------------------------
# Adapter params (Pydantic) — what shows up under ``[devices.params]`` in TOML.
# ---------------------------------------------------------------------------


class NIDAQTimingParams(BaseModel):
    """Subset of :class:`nidaqlib.tasks.spec.Timing` exposed in capa configs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rate_hz: float = Field(gt=0)
    mode: Literal["finite", "continuous", "on_demand"] = "continuous"
    samples_per_channel: int | None = Field(default=None, gt=0)
    source: str | None = None
    active_edge: Literal["rising", "falling"] = "rising"

    def to_library(self) -> Timing:
        from nidaqlib.tasks.spec import Edge  # noqa: PLC0415

        return Timing(
            rate_hz=self.rate_hz,
            mode=AcquisitionMode(self.mode),
            samples_per_channel=self.samples_per_channel,
            source=self.source,
            active_edge=Edge(self.active_edge),
        )


class NIDAQAdapterParams(BaseModel):
    """Per-device adapter configuration for an NI-DAQ task.

    Plan §5.4: adapter-specific knobs live under ``DeviceConfig.params`` and are
    parsed by the adapter at construction time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_name: str
    """Logical task name. Used as :attr:`TaskSpec.name`, the ``task`` field
    on emitted :class:`DaqReading`\\ s, and as the join key in
    :class:`NIDAQReadingField.task` / :class:`NIDAQBlockChannel.task`."""

    channels: tuple[dict[str, Any], ...]
    """Channel dicts in :class:`nidaqlib.channels.ChannelSpec` format. Each
    must include a ``kind`` discriminator (``"ai_voltage"``, ``"thermocouple"``,
    ``"digital_output"``, …) and ``physical_channel`` (``"Dev1/ai0"``)."""

    timing: NIDAQTimingParams | None = None
    """Hardware-clocked timing config. ``None`` means software-timed
    polling at :attr:`rate_hz`."""

    rate_hz: float = Field(gt=0, le=1000.0, default=10.0)
    """Polling cadence for software-timed mode. Ignored for hardware-clocked
    block mode (the block recorder runs at ``timing.rate_hz`` natively).
    Capped at 1 kHz: anything higher belongs in block mode."""

    snapshot_period_s: float = Field(gt=0, default=30.0)
    """Cadence of :class:`DeviceSnapshot` emissions during a run."""

    auto_reconnect: bool = False
    """Software-timed mode only. When ``True``, transient
    :class:`NIDaqError`\\ s do not terminate the stream — the recorder
    surfaces them via the per-tick ``error`` field and the adapter
    increments its degradation counter. Default ``False`` because most
    NI errors (driver fault, hardware unplug) are not transient and a
    failed run should fail visibly."""

    overflow: Literal["block", "drop_newest"] = "block"

    max_samples_per_block_unroll: int = Field(gt=0, default=10_000)
    """Refuse to unroll a block larger than this many samples per channel.

    Block mode emits one :class:`ChannelSample` per (channel, sample),
    fanned out through ``scalars.parquet``. This is fine at low rates
    (≤ a few hundred Hz × a few channels) but explodes at kHz. The
    guardrail trips at :meth:`NIDAQAdapter.open` if the resolved chunk
    size exceeds it — pointing the operator at the rectangular-block
    sidecar / TDMS path (P3) instead. Default ``10 000`` covers
    500 Hz × 20 ch × 1-second blocks; aggressive enough to stop a typo."""

    @field_validator("channels")
    @classmethod
    def _check_channels_nonempty(
        cls, value: tuple[dict[str, Any], ...]
    ) -> tuple[dict[str, Any], ...]:
        if not value:
            raise ValueError("NI adapter requires at least one channel")
        return value

    def is_block_mode(self) -> bool:
        """Hardware-clocked iff ``timing.mode != "on_demand"``."""
        return self.timing is not None and self.timing.mode != "on_demand"

    def adapter_id(self) -> str:
        """The string used as ``SourceRecord.adapter`` for this configuration."""
        return ADAPTER_ID_BLOCK if self.is_block_mode() else ADAPTER_ID_POLLED

    def overflow_policy(self) -> OverflowPolicy:
        return OverflowPolicy.BLOCK if self.overflow == "block" else OverflowPolicy.DROP_NEWEST

    def build_task_spec(self) -> TaskSpec:
        """Materialise the :class:`TaskSpec` from the declarative dicts."""
        channels = tuple(NidaqChannelSpec.from_dict(c) for c in self.channels)
        return TaskSpec(
            name=self.task_name,
            channels=channels,
            timing=self.timing.to_library() if self.timing is not None else None,
        )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


SessionFactory = Callable[[], Awaitable[DaqSession]]
"""Test seam: a factory returning a *started* :class:`DaqSession`. The adapter
calls it instead of :func:`nidaqlib.tasks.open_device`. Used by tests that
need to attach a :class:`FakeDaqBackend` directly."""


class NIDAQAdapter:
    """Real NI-DAQ adapter (one task per instance).

    Two construction shapes mirror the other real adapters:

    * ``NIDAQAdapter(name=..., **params_kwargs)`` — engine path.
    * ``NIDAQAdapter(name=..., params=NIDAQAdapterParams(...))`` — programmatic.

    Both shapes accept an optional ``session_factory`` kwarg as a test seam.
    The ``backend`` kwarg, when set, is passed to
    :func:`nidaqlib.tasks.open_device` (lets tests swap in a FakeDaqBackend
    without writing a full factory).
    """

    __slots__ = (
        "_backend",
        "_channels",
        "_clock",
        "_last_sample",
        "_last_snapshot_t_mono_ns",
        "_lifecycle",
        "_recoverable_error_count",
        "_seq",
        "_session",
        "_session_factory",
        "_stop_requested",
        "_task_spec",
        "capabilities",
        "name",
        "params",
    )

    name: str
    params: NIDAQAdapterParams
    capabilities: frozenset[Capability]

    def __init__(
        self,
        *,
        name: str,
        params: NIDAQAdapterParams | None = None,
        session_factory: SessionFactory | None = None,
        backend: Any = None,
        **params_kwargs: Any,
    ) -> None:
        if params is not None and params_kwargs:
            raise TypeError("NIDAQAdapter accepts either `params=` or per-field kwargs, not both")
        if params is None:
            params = NIDAQAdapterParams.model_validate(params_kwargs)
        self.name = name
        self.params = params
        flags: set[Capability] = {
            Capability.READS_PROCESS_VAR,
            Capability.SUPPORTS_DISCOVERY,
        }
        if params.is_block_mode():
            flags.add(Capability.HARDWARE_CLOCKED)
            flags.add(Capability.EMITS_BLOCKS)
        if params.auto_reconnect:
            flags.add(Capability.SUPPORTS_AUTO_RECONNECT)
        self.capabilities = frozenset(flags)
        self._session_factory: SessionFactory | None = session_factory
        self._backend = backend
        self._session: DaqSession | None = None
        self._task_spec: TaskSpec | None = None
        self._channels: list[ChannelSpec] = []
        self._clock: RunClock | None = None
        self._lifecycle = AdapterLifecycle()
        self._seq = 0
        self._last_snapshot_t_mono_ns = -(2**62)
        self._last_sample = LastSampleTracker()
        self._recoverable_error_count = 0
        self._stop_requested = False

    # ------------------------------------------------------------------ wiring

    def configure_channels(self, specs: list[ChannelSpec]) -> None:
        """Bind to the matching channel sources for the configured mode.

        Polled mode binds to :class:`NIDAQReadingField` (one
        :class:`ChannelSample` per binding per tick). Block mode binds to
        :class:`NIDAQBlockChannel` (one :class:`ChannelSample` per
        binding per sample, unrolled from each :class:`DaqBlock`).
        Bindings on the wrong source kind are filtered out.
        """
        binding_source = (
            "nidaq_block_channel" if self.params.is_block_mode() else "nidaq_reading_field"
        )
        self._channels = channels_for_device(specs, device=self.name, binding_source=binding_source)

    @property
    def task_spec(self) -> TaskSpec | None:
        """The materialised :class:`TaskSpec`. ``None`` until :meth:`open`."""
        return self._task_spec

    # ------------------------------------------------------------------ lifecycle

    async def open(self) -> None:
        """Open the underlying NI session.

        Constructs the :class:`TaskSpec`, instantiates a :class:`DaqSession`
        via :func:`nidaqlib.tasks.open_device` (or the injected factory),
        and configures it. Idempotent on already-open adapters.

        Note: ``autostart`` is left ``True`` — NI's own ``start()`` is
        cheap and the adapter's :meth:`start` then only flips its own
        lifecycle flag. Hardware-clocked tasks that need NI's start
        deferred (e.g. trigger arming via a callback bridge) should
        bypass this adapter and use the recorder directly.
        """
        if self._lifecycle.state in ("open", "running"):
            return
        try:
            self._task_spec = self.params.build_task_spec()
            if self.params.is_block_mode():
                chunk = self._resolve_chunk_size()
                if chunk > self.params.max_samples_per_block_unroll:
                    raise AdapterError(
                        f"nidaq {self.name!r}: block chunk size {chunk} exceeds "
                        f"max_samples_per_block_unroll="
                        f"{self.params.max_samples_per_block_unroll}; reduce "
                        f"timing.samples_per_channel or use the kHz block sidecar (P3)",
                        device=self.name,
                    )
            self._session = await self._build_session(self._task_spec)
        except NIDaqError as exc:
            await self._safe_close_session()
            raise AdapterError(f"nidaq {self.name!r} open failed: {exc}", device=self.name) from exc
        self._lifecycle.open()

    async def close(self) -> None:
        """Release the NI task. Idempotent."""
        if self._lifecycle.state == "closed":
            return
        await self._safe_close_session()
        self._session = None
        self._task_spec = None
        self._lifecycle.close()

    async def start(self, clock: RunClock | None = None) -> None:
        """Capture the :class:`RunClock` anchor and arm the streaming loop.

        :func:`nidaqlib.tasks.open_device` already started the NI task as
        part of :meth:`open` (``autostart=True``). This method only flips
        the capa-side lifecycle so :meth:`stream` knows it can emit.
        """
        self._lifecycle.start()
        self._clock = clock or RunClock.now()
        self._stop_requested = False
        self._last_sample.reset()
        self._recoverable_error_count = 0
        self._last_snapshot_t_mono_ns = -(2**62)

    async def stop(self) -> None:
        """Request the streaming loop to exit cleanly. Idempotent."""
        if self._lifecycle.state != "running":
            return
        self._stop_requested = True
        self._lifecycle.stop()

    async def snapshot(self) -> DeviceSnapshot:
        """Build a :class:`DeviceSnapshot` from cached identity + live health."""
        clock = self._clock or RunClock.now()
        return DeviceSnapshot(
            adapter=self.params.adapter_id(),
            device=self.name,
            t_mono_ns=clock.t_mono_ns(),
            t_utc=datetime.now(UTC),
            healthy=self._lifecycle.state in ("open", "running"),
            health=self._compute_health(clock=clock),
            fields=self._snapshot_fields(),
        )

    # ------------------------------------------------------------------ stream

    async def stream(self) -> AsyncIterator[DeviceEmission]:
        """Yield :class:`DeviceEmission`\\ s while sampling is active.

        Polled mode (``timing is None`` or ``timing.mode == "on_demand"``)
        uses :func:`nidaqlib.streaming.record_polled` and emits one
        :class:`SourceRecord` (wide row, via :func:`nidaqlib.sinks.reading_to_row`)
        plus one :class:`ChannelSample` per matching :class:`NIDAQReadingField`
        per tick.

        Block mode (``timing.mode in {"finite", "continuous"}``) drives
        :func:`nidaqlib.streaming.block.record` at NI's onboard sample
        clock, emits one ``shape="wide_row"`` :class:`SourceRecord` of
        block metadata per emitted :class:`DaqBlock`, and unrolls each
        block into ``samples_per_channel × bound_channels``
        :class:`ChannelSample`\\ s timestamped from
        ``task_started_at + (first_sample_index + k) / sample_rate_hz``.
        """
        if self._session is None:
            raise AdapterError(
                f"nidaq {self.name!r} stream() requires open() first",
                device=self.name,
            )
        if self._clock is None:
            raise AdapterError(
                f"nidaq {self.name!r} stream() requires start() first",
                device=self.name,
            )

        snap = await self.snapshot()
        self._last_snapshot_t_mono_ns = snap.t_mono_ns
        yield snap

        if self.params.is_block_mode():
            async for emission in self._stream_block_mode():
                yield emission
        else:
            async for emission in self._stream_polled_mode():
                yield emission

    async def _stream_polled_mode(self) -> AsyncIterator[DeviceEmission]:
        """Software-timed polled acquisition. One ``DaqReading`` per tick."""
        assert self._session is not None
        error_policy = (
            NidaqErrorPolicy.RETURN if self.params.auto_reconnect else NidaqErrorPolicy.RAISE
        )

        try:
            async with record_polled(
                self._session,
                rate_hz=self.params.rate_hz,
                error_policy=error_policy,
                overflow=self.params.overflow_policy(),
                buffer_size=64,
            ) as (rx, _summary):
                async for payload in rx:
                    if self._stop_requested:
                        break
                    # ``record_polled`` against a single ``DaqSession`` yields
                    # bare :class:`DaqReading` items (manager mode would
                    # yield a Mapping[name, DeviceResult[DaqReading]] — out of
                    # scope here since one adapter == one task).
                    if not isinstance(payload, DaqReading):
                        # Defensive: shouldn't happen with a session source.
                        continue
                    if payload.error is not None:
                        self._recoverable_error_count += 1
                        # Native row preserves the error fields — emit it
                        # so the device-records sink keeps the diagnostic.
                        yield self._record_for_reading(payload)
                        continue
                    record = self._record_for_reading(payload)
                    yield record
                    self._last_sample.mark(record.t_mono_ns)
                    for cs in self._channel_samples_for(payload, record.record_id):
                        yield cs
                    if self._snapshot_due():
                        snap = await self.snapshot()
                        self._last_snapshot_t_mono_ns = snap.t_mono_ns
                        yield snap
        except* NIDaqError as eg:
            first = next(iter(eg.exceptions))
            raise AdapterError(
                f"nidaq {self.name!r} stream failed: {first}", device=self.name
            ) from first

    async def _stream_block_mode(self) -> AsyncIterator[DeviceEmission]:
        """Hardware-clocked block acquisition. One ``DaqBlock`` per chunk."""
        from nidaqlib.streaming.block import (  # noqa: PLC0415
            OverflowPolicy as BlockOverflowPolicy,
        )
        from nidaqlib.streaming.block import (  # noqa: PLC0415
            record as record_blocks,
        )

        assert self._session is not None
        chunk = self._resolve_chunk_size()
        error_policy = (
            NidaqErrorPolicy.RETURN if self.params.auto_reconnect else NidaqErrorPolicy.RAISE
        )
        # The block recorder defaults to DROP_OLDEST (nidaqlib §13.3); for
        # capa's bundle path we want durable capture, so honour the same
        # ``overflow`` knob exposed for polled mode.
        overflow = (
            BlockOverflowPolicy.BLOCK
            if self.params.overflow == "block"
            else BlockOverflowPolicy.DROP_NEWEST
        )

        try:
            async with record_blocks(
                self._session,
                chunk_size=chunk,
                error_policy=error_policy,
                overflow=overflow,
                buffer_size=16,
            ) as (rx, _summary):
                async for block in rx:
                    if self._stop_requested:
                        break
                    if block.error is not None:
                        self._recoverable_error_count += 1
                        if not self.params.auto_reconnect:
                            raise AdapterError(
                                f"nidaq {self.name!r} block error: {block.error}",
                                device=self.name,
                            )
                        continue
                    record = self._record_for_block(block)
                    yield record
                    last_t_mono_ns: int | None = None
                    for cs in self._channel_samples_for_block(block, record.record_id):
                        last_t_mono_ns = cs.t_mono_ns
                        yield cs
                    if last_t_mono_ns is not None:
                        self._last_sample.mark(last_t_mono_ns)
                    if self._snapshot_due():
                        snap = await self.snapshot()
                        self._last_snapshot_t_mono_ns = snap.t_mono_ns
                        yield snap
        except* NIDaqError as eg:
            first = next(iter(eg.exceptions))
            raise AdapterError(
                f"nidaq {self.name!r} block stream failed: {first}", device=self.name
            ) from first

    # ------------------------------------------------------------------ commands

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        """NI tasks have no scalar setpoint commands in P2.

        Analog-output / digital-output writes will land in P3 alongside the
        method editor. For now the command surface enforces the auth gate
        and rejects unknown verbs.
        """
        clock = self._clock or RunClock.now()
        rejection = reject_unless_authorized(
            cmd, adapter_id=self.params.adapter_id(), device_name=self.name, clock=clock
        )
        if rejection is not None:
            return rejection
        if self._session is None:
            return make_not_open_result(
                adapter_id=self.params.adapter_id(), device_name=self.name, clock=clock
            )
        raise AdapterError(
            f"nidaq {self.name!r}: command kind {cmd.kind!r} not supported in P2 "
            f"(AO/DO writes land in P3)",
            device=self.name,
        )

    # ------------------------------------------------------------------ helpers

    async def _build_session(self, spec: TaskSpec) -> DaqSession:
        if self._session_factory is not None:
            return await self._session_factory()
        return await nidaq_open_device(spec, backend=self._backend)

    async def _safe_close_session(self) -> None:
        if self._session is None:
            return
        try:
            await self._session.close()
        except NIDaqError:
            return

    def _record_for_reading(self, reading: DaqReading) -> SourceRecord:
        """Convert a :class:`DaqReading` into a wide-row :class:`SourceRecord`.

        Uses :func:`nidaqlib.sinks.reading_to_row` so the row schema matches
        what an offline ``nidaqlib`` recorder would produce — important for
        ``device_records/nidaq_polled.parquet`` parity with sim bundles.
        """
        from nidaqlib.sinks.base import reading_to_row  # noqa: PLC0415

        assert self._clock is not None
        row = reading_to_row(reading)
        t_mono_ns = reading.monotonic_ns - self._clock.started_mono_ns
        self._seq += 1
        adapter_id = self.params.adapter_id()
        return SourceRecord(
            record_id=make_record_id(adapter_id, self.name, self._seq),
            adapter=adapter_id,
            device=self.name,
            shape="wide_row",
            t_mono_ns=t_mono_ns,
            t_utc=reading.midpoint_at,
            row=row,
            metadata={"task": reading.task or self.params.task_name},
        )

    def _resolve_chunk_size(self) -> int:
        """Samples per channel each emitted :class:`DaqBlock` carries.

        Defaults to ``timing.samples_per_channel`` when pinned; otherwise
        falls back to ``int(timing.rate_hz / 10)`` (one block per 100 ms,
        a UI-responsive default). Always ≥ 1.
        """
        timing = self.params.timing
        if timing is None:
            raise AdapterError(f"nidaq {self.name!r}: block mode requires timing", device=self.name)
        if timing.samples_per_channel is not None:
            return max(1, timing.samples_per_channel)
        return max(1, int(timing.rate_hz / 10))

    def _expected_period_ns(self) -> int:
        """Inter-emission cadence in ns for the configured mode.

        Polled mode emits once per ``1 / rate_hz`` seconds; block mode
        emits once per ``chunk / sample_rate_hz`` seconds.
        """
        if self.params.is_block_mode():
            timing = self.params.timing
            assert timing is not None
            chunk = self._resolve_chunk_size()
            return int(1e9 * chunk / timing.rate_hz)
        return int(1e9 / self.params.rate_hz)

    def _record_for_block(self, block: DaqBlock) -> SourceRecord:
        """Per-block metadata :class:`SourceRecord`.

        ``shape="wide_row"`` is intentional: this record is *metadata about*
        a block (block index, first sample index, sample rate, channel
        order, NI's task_started_at), not the block itself. The unrolled
        :class:`ChannelSample`\\ s in ``scalars.parquet`` are the source of
        truth for measurement values; no rectangular sidecar is written in
        this acquisition mode. Files key off ``adapter`` so this lands in
        ``device_records/nidaq_block.parquet``.
        """
        assert self._clock is not None
        self._seq += 1
        t_mono_ns = block.monotonic_ns - self._clock.started_mono_ns
        row: dict[str, float | int | str | bool | None] = {
            "block_index": block.block_index,
            "first_sample_index": block.first_sample_index,
            "samples_per_channel": block.samples_per_channel,
            "sample_rate_hz": block.sample_rate_hz,
            "channels": ",".join(block.channels),
            "task_started_at": block.task_started_at.isoformat(),
            "read_started_at": block.read_started_at.isoformat(),
            "read_finished_at": block.read_finished_at.isoformat(),
            "elapsed_s": block.elapsed_s,
        }
        return SourceRecord(
            record_id=make_record_id(ADAPTER_ID_BLOCK, self.name, self._seq),
            adapter=ADAPTER_ID_BLOCK,
            device=self.name,
            shape="wide_row",
            t_mono_ns=t_mono_ns,
            t_utc=block.read_started_at,
            row=row,
            metadata={"task": block.task or self.params.task_name},
        )

    def _channel_samples_for_block(self, block: DaqBlock, record_id: str) -> Iterator[Any]:
        """Unroll one rectangular block into per-(channel, sample) :class:`ChannelSample`\\ s.

        Per the :class:`DaqBlock` contract::

            t_utc[k] = block.task_started_at + (block.first_sample_index + k) / block.sample_rate_hz

        Converted to capa's run-relative monotonic ns by anchoring to
        :attr:`RunClock.started_utc` — the run anchor captures both UTC
        and monotonic at the same instant, so UTC drift over a single
        run stays sub-millisecond on a sane host. Plan §6.
        """
        assert self._clock is not None
        rate = block.sample_rate_hz
        if rate is None or rate <= 0:
            return  # Cannot reconstruct timestamps without a rate.
        run_started_utc = self._clock.started_utc
        n_samples = block.samples_per_channel

        bindings: dict[str, ChannelSpec] = {}
        task_name = block.task or self.params.task_name
        for spec in self._channels:
            binding = spec.source
            assert isinstance(binding, NIDAQBlockChannel)
            if binding.task != task_name:
                continue
            bindings[binding.channel] = spec

        # Anchor in ns up-front so per-sample arithmetic stays in integer space
        # at the last step; otherwise the float multiplication
        # ``t_relative_s * 1e9`` rounds 1 ULP either way of an integer ns
        # boundary and the resulting ``int(...)`` truncation can wobble ±1.
        anchor_offset_s = (block.task_started_at - run_started_utc).total_seconds()
        for channel_idx, channel_name in enumerate(block.channels):
            bound_spec = bindings.get(channel_name)
            if bound_spec is None:
                continue
            for k in range(n_samples):
                absolute_index = block.first_sample_index + k
                t_relative_s = anchor_offset_s + absolute_index / rate
                t_mono_ns = round(t_relative_s * 1e9)
                raw_value = float(block.data[channel_idx, k])
                yield build_channel_sample(
                    spec=bound_spec,
                    raw_value=raw_value,
                    t_mono_ns=t_mono_ns,
                    source_record_id=record_id,
                    source_field=channel_name,
                )

    def _channel_samples_for(self, reading: DaqReading, record_id: str) -> list[DeviceEmission]:
        """Map ``reading`` against the configured :class:`NIDAQReadingField` bindings."""
        assert self._clock is not None
        t_mono_ns = reading.monotonic_ns - self._clock.started_mono_ns
        emissions: list[DeviceEmission] = []
        values: Mapping[str, float | int | bool] = reading.values
        task_name = reading.task or self.params.task_name
        for spec in self._channels:
            binding = spec.source
            assert isinstance(binding, NIDAQReadingField)
            if binding.task != task_name:
                continue
            raw_value = values.get(binding.field)
            if raw_value is None:
                continue
            emissions.append(
                build_channel_sample(
                    spec=spec,
                    raw_value=float(raw_value),
                    t_mono_ns=t_mono_ns,
                    source_record_id=record_id,
                    source_field=binding.field,
                )
            )
        return emissions

    def watchdog_state(self) -> WatchdogState:
        """Watchdog view for the engine's silent-device task (plan §13.2)."""
        return WatchdogState(
            device=self.name,
            last_t_mono_ns=self._last_sample.last_t_mono_ns,
            expected_period_ns=self._expected_period_ns(),
        )

    def _snapshot_due(self) -> bool:
        if self._clock is None:
            return False
        elapsed_ns = self._clock.t_mono_ns() - self._last_snapshot_t_mono_ns
        return elapsed_ns >= int(self.params.snapshot_period_s * 1e9)

    def _compute_health(self, *, clock: RunClock) -> DeviceHealth:
        """Derive the :class:`DeviceHealth` pill from adapter state."""
        if self._lifecycle.state == "closed":
            return "down"
        if self._lifecycle.state == "open":
            return "ok"
        if self._recoverable_error_count > 0:
            return "degraded"
        age_ns = self._last_sample.age_ns(now_t_mono_ns=clock.t_mono_ns())
        if age_ns is not None:
            stale_threshold_ns = 3 * self._expected_period_ns()
            if age_ns > stale_threshold_ns:
                return "degraded"
        return "ok"

    def _snapshot_fields(self) -> dict[str, float | int | str | bool | None]:
        out: dict[str, float | int | str | bool | None] = {
            "task": self.params.task_name,
            "rate_hz": self.params.rate_hz,
            "channel_count_declared": len(self.params.channels),
            "channel_count_bound": len(self._channels),
            "state": self._lifecycle.state,
            "recoverable_errors": self._recoverable_error_count,
            "block_mode": self.params.is_block_mode(),
        }
        if self._task_spec is not None:
            out["physical_channels"] = ",".join(
                ch.physical_channel for ch in self._task_spec.channels
            )
        return out


# ---------------------------------------------------------------------------
# CLI handshake hook (``capa validate --strict``)
# ---------------------------------------------------------------------------


async def handshake(params: dict[str, Any]) -> str:
    """Read-only enumerate-and-verify against the connected NI hardware.

    Confirms that every declared physical channel resolves on the local
    NI system. Does NOT open the task — opening allocates NI resources
    and (for AO tasks) could actuate hardware. Plan §14: "non-disruptive
    read-only handshake."
    """
    parsed = NIDAQAdapterParams.model_validate(params)
    try:
        from nidaqlib.system.discovery import list_devices  # noqa: PLC0415
    except ImportError as exc:
        raise AdapterError(f"nidaq handshake: nidaqmx not installed: {exc}") from exc

    try:
        devices = list_devices()
    except NIDaqError as exc:
        raise AdapterError(f"nidaq handshake: {exc}") from exc

    # Build a flat set of all known physical-channel names across the system.
    known: set[str] = set()
    for d in devices:
        known.update(d.ai_physical_channels)
        known.update(d.ao_physical_channels)
        known.update(d.di_lines)
        known.update(d.do_lines)
        known.update(d.ci_physical_channels)
        known.update(d.co_physical_channels)

    missing: list[str] = []
    for ch_dict in parsed.channels:
        physical = str(ch_dict.get("physical_channel", ""))
        if not physical:
            missing.append("(unnamed)")
            continue
        if physical not in known:
            missing.append(physical)

    if missing:
        raise AdapterError(
            f"nidaq handshake: physical channel(s) not present on local "
            f"NI system: {sorted(missing)!r}"
        )

    device_summary = (
        ", ".join(f"{d.name}={d.product_type or '?'}" for d in devices) or "(no devices)"
    )
    return (
        f"nidaq task={parsed.task_name} channels={len(parsed.channels)} "
        f"system_devices=[{device_summary}]"
    )


# ---------------------------------------------------------------------------
# Discovery hook (``capa devices discover``)
# ---------------------------------------------------------------------------


async def discover() -> list[dict[str, Any]]:
    """Enumerate NI devices visible on the local system.

    Wraps :func:`nidaqlib.system.discovery.list_devices`. One row per
    physical NI device, with channel inventories listed inline. Returns
    an empty list when ``nidaqmx`` is not installed, the NI runtime is
    missing, or no NI hardware is present — the function is safe to call
    on a workstation with none of those.
    """
    try:
        from nidaqlib.system.discovery import list_devices  # noqa: PLC0415
    except ImportError:
        return []
    try:
        devices = list_devices()
    except (NIDaqError, Exception):
        # nidaqmx itself raises ``DaqNotFoundError`` (not subclassed from
        # ``NIDaqError``) when the NI runtime is missing. Catch anything —
        # ``discover`` is meant to be safe to call from an idle CLI.
        return []

    out: list[dict[str, Any]] = []
    for d in devices:
        out.append(
            {
                "adapter": "nidaq",
                "device": d.name,
                "product_type": d.product_type,
                "serial": d.serial_number,
                "ai_channels": list(d.ai_physical_channels),
                "ao_channels": list(d.ao_physical_channels),
                "di_lines": list(d.di_lines),
                "do_lines": list(d.do_lines),
                "ci_channels": list(d.ci_physical_channels),
                "co_channels": list(d.co_physical_channels),
            }
        )
    return out


__all__ = [
    "ADAPTER_ID_BLOCK",
    "ADAPTER_ID_POLLED",
    "NIDAQAdapter",
    "NIDAQAdapterParams",
    "NIDAQTimingParams",
    "discover",
    "handshake",
]
