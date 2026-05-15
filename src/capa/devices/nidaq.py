"""Real :class:`NIDAQAdapter` — wraps a :class:`nidaqlib.tasks.session.DaqSession`.

Plan §16: "real ``NIDAQAdapter``. Capability flags. Device health
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
  rectangular-block sidecar / TDMS escape (plan §8.7) is future work.

For tests, an opt-in ``backend`` kwarg is forwarded to
:func:`nidaqlib.tasks.open_device` so a
:class:`~nidaqlib.backend.fake.FakeDaqBackend` drives the adapter without
touching real hardware. Alternatively, an ``session_factory`` kwarg supplies
a fully pre-built :class:`DaqSession` for tests that need finer control.
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal, cast

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
    WatchdogState,
    build_channel_sample,
    channels_for_device,
    daqmx_resource_id_from_channels,
    make_not_open_result,
    make_record_id,
    reject_unless_authorized,
)
from capa.devices.adapter import (
    AdapterStartContext,
    Capability,
    CommandResult,
    DeviceCommand,
)
from capa.devices.nidaq_channels import NIDAQChannelConfig
from capa.devices.records import (
    DeviceEmission,
    DeviceHealth,
    DeviceSnapshot,
    SourceRecord,
)
from capa.devices.runtime_state import AdapterRuntimeState

if TYPE_CHECKING:
    from capa.devices.registry import AdapterDescriptor

ADAPTER_ID_POLLED: Final[str] = "nidaq_polled"
ADAPTER_ID_BLOCK: Final[str] = "nidaq_block"

_MODULE_SUFFIX_RE: Final[re.Pattern[str]] = re.compile(r"Mod\d+$")
"""Strips ``ModN`` from a cDAQ module name to derive the chassis name
(``cDAQ1Mod1`` → ``cDAQ1``). Only used inside :meth:`NIDAQAdapter._probe_device_info`."""


@dataclass(frozen=True, slots=True)
class NIDAQDeviceInfo:
    """Identity record for an NI device backing a :class:`NIDAQAdapter`.

    Populated lazily during :meth:`NIDAQAdapter.open` by enumerating the local
    NI system via :func:`nidaqlib.system.discovery.list_devices` and matching
    the first declared ``physical_channel`` against the returned device names.
    Field names align with the manifest writer's identity fields so the
    ``manifest.json.devices[*].identity`` block surfaces them automatically.
    """

    product_type: str | None
    """NI product family of the module owning the channels (e.g. ``"NI 9214"``)."""

    serial_number: str | None
    """Module serial number as a string (NI returns ints; coerced for TOML)."""

    physical_module: str | None
    """The cDAQ module name resolved from the channel prefix (``"cDAQ1Mod1"``)."""

    chassis: str | None
    """The owning chassis device name (``"cDAQ1"``) when discovered, else ``None``.
    Single-board cards (PCIe, USB-DAQ) report ``None`` here."""


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

    channels: tuple[NIDAQChannelConfig, ...]
    """Per-channel typed config. Each entry's ``kind`` discriminator
    (``"thermocouple"`` / ``"ai_voltage"`` / pass-through for others) selects
    the validating model from
    :mod:`capa.devices.nidaq_channels`. NI enum-typed fields
    (``thermocouple_type``, ``cjc_source``, ``units``, ``adc_timing_mode``,
    ``auto_zero_mode``, ``terminal_config``) take the canonical NI name
    only (``"K"``, ``"BUILT_IN"``, ``"DEG_C"``)."""

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
    sidecar / TDMS path instead. Default ``10 000`` covers
    500 Hz × 20 ch × 1-second blocks; aggressive enough to stop a typo."""

    @field_validator("channels")
    @classmethod
    def _check_channels_nonempty(
        cls, value: tuple[NIDAQChannelConfig, ...]
    ) -> tuple[NIDAQChannelConfig, ...]:
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
        """Materialise the :class:`TaskSpec` from the typed channel configs."""
        channels = tuple(NidaqChannelSpec.from_dict(c.to_nidaqlib_dict()) for c in self.channels)
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
        "_device_info",
        "_session",
        "_session_factory",
        "_state",
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
        self._state = AdapterRuntimeState()
        self._device_info: NIDAQDeviceInfo | None = None

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
    def expected_emission_rate_hz(self) -> float:
        bound = len(self._channels)
        if self.params.is_block_mode():
            assert self.params.timing is not None
            # One SourceRecord per block plus one ChannelSample per bound
            # channel per sample. The per-block SourceRecord term is dwarfed
            # by the per-sample ChannelSamples at any meaningful block
            # cadence, so the bound-channel term carries the estimate.
            return self.params.timing.rate_hz * bound
        # Polled: one SourceRecord + one ChannelSample per bound channel.
        return self.params.rate_hz * (1 + bound)

    @property
    def resource_id(self) -> str:
        return daqmx_resource_id_from_channels(ch.physical_channel for ch in self.params.channels)

    @property
    def task_spec(self) -> TaskSpec | None:
        """The materialised :class:`TaskSpec`. ``None`` until :meth:`open`."""
        return self._task_spec

    @property
    def device_info(self) -> NIDAQDeviceInfo | None:
        """Identity record probed during :meth:`open`. ``None`` when ``open``
        hasn't run, ``nidaqmx`` isn't installed, or the channel module didn't
        match any system device (typical for tests using ``FakeDaqBackend``).

        The engine's ``_collect_equipment_blocks`` reads this attribute via
        duck-typed ``getattr`` to populate ``manifest.json.devices[*].identity``.
        """
        return self._device_info

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
        if self._state.lifecycle.state in ("open", "running"):
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
                        f"timing.samples_per_channel or use the kHz block sidecar",
                        device=self.name,
                    )
            self._session = await self._build_session(self._task_spec)
        except NIDaqError as exc:
            await self._safe_close_session()
            raise AdapterError(f"nidaq {self.name!r} open failed: {exc}", device=self.name) from exc
        # Best-effort identity probe — failures here must not break ``open()``.
        # Tests using ``FakeDaqBackend`` don't have ``nidaqmx`` system devices,
        # so ``_probe_device_info`` returns ``None`` and the manifest identity
        # block stays empty (same as before this change).
        self._device_info = self._probe_device_info()
        self._state.lifecycle.open()

    async def close(self) -> None:
        """Release the NI task. Idempotent."""
        if self._state.lifecycle.state == "closed":
            return
        await self._safe_close_session()
        self._session = None
        self._task_spec = None
        self._device_info = None
        self._state.lifecycle.close()

    async def start(self, ctx: AdapterStartContext) -> None:
        """Capture the :class:`RunClock` anchor and arm the streaming loop.

        :func:`nidaqlib.tasks.open_device` already started the NI task as
        part of :meth:`open` (``autostart=True``). This method only flips
        the capa-side lifecycle so :meth:`stream` knows it can emit.
        """
        self._state.on_start(ctx.clock)

    async def stop(self) -> None:
        """Request the streaming loop to exit cleanly. Idempotent."""
        self._state.request_stop()

    async def snapshot(self) -> DeviceSnapshot:
        """Build a :class:`DeviceSnapshot` from cached identity + live health."""
        clock = self._state.clock or RunClock.now()
        return DeviceSnapshot(
            adapter=self.params.adapter_id(),
            device=self.name,
            t_mono_ns=clock.t_mono_ns(),
            t_utc=datetime.now(UTC),
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
        if self._state.clock is None:
            raise AdapterError(
                f"nidaq {self.name!r} stream() requires start() first",
                device=self.name,
            )

        snap = await self.snapshot()
        self._state.last_snapshot_t_mono_ns = snap.t_mono_ns
        yield snap

        if self.params.is_block_mode():
            async for emission in self._stream_block_mode():
                yield emission
        else:
            async for emission in self._stream_polled_mode():
                yield emission

    async def stream_until_stopped(
        self,
        *,
        max_records: int | None = None,
        max_emissions: int | None = None,
    ) -> AsyncIterator[DeviceEmission]:
        """Yield emissions like :meth:`stream`, but drive shutdown cooperatively.

        Stops when *either* :meth:`stop` is called externally, or the optional
        ``max_records`` / ``max_emissions`` budget is met. Always closes the
        inner :func:`nidaqlib.streaming.record_polled` async-context-manager
        before returning so :meth:`close` can acquire the
        :class:`DaqSession` lock without deadlocking.

        Why this exists: ``async for emission in adapter.stream(): ...; break``
        leaves the underlying ``async with record_polled(...)`` paused and
        holding the session lock until the outer generator is garbage
        collected — which doesn't happen synchronously, so a subsequent
        :meth:`close` deadlocks. Test code previously had to know to set
        ``_stop_requested`` and explicitly ``await stream.aclose()``;
        production callers that just want "run until I tell you to stop"
        can use this helper instead.
        """
        if max_records is not None and max_records <= 0:
            raise ValueError(f"max_records must be > 0; got {max_records}")
        if max_emissions is not None and max_emissions <= 0:
            raise ValueError(f"max_emissions must be > 0; got {max_emissions}")
        record_count = 0
        emission_count = 0
        # ``stream()`` is annotated ``AsyncIterator`` to match the adapter
        # protocol, but it's an ``AsyncGenerator`` at runtime — needed here so
        # the ``finally``-block ``aclose()`` lets the underlying ``record_polled``
        # context manager release the session lock cleanly (see method docstring).
        stream = cast("AsyncGenerator[DeviceEmission]", self.stream())
        try:
            async for emission in stream:
                yield emission
                emission_count += 1
                if isinstance(emission, SourceRecord):
                    record_count += 1
                if self._state.stop_requested:
                    continue  # let the inner stream wind down naturally
                if (max_records is not None and record_count >= max_records) or (
                    max_emissions is not None and emission_count >= max_emissions
                ):
                    await self.stop()
        finally:
            await stream.aclose()

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
                    if self._state.stop_requested:
                        break
                    # ``record_polled`` against a single ``DaqSession`` yields
                    # bare :class:`DaqReading` items (manager mode would
                    # yield a Mapping[name, DeviceResult[DaqReading]] — out of
                    # scope here since one adapter == one task).
                    if not isinstance(payload, DaqReading):
                        # Defensive: shouldn't happen with a session source.
                        continue
                    if payload.error is not None:
                        self._state.recoverable_error_count += 1
                        # Native row preserves the error fields — emit it
                        # so the device-records sink keeps the diagnostic.
                        yield self._record_for_reading(payload)
                        continue
                    record = self._record_for_reading(payload)
                    yield record
                    self._state.last_sample.mark(record.t_mono_ns)
                    for cs in self._channel_samples_for(payload, record.record_id):
                        yield cs
                    if self._state.snapshot_due(period_s=self.params.snapshot_period_s):
                        snap = await self.snapshot()
                        self._state.last_snapshot_t_mono_ns = snap.t_mono_ns
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
                    if self._state.stop_requested:
                        break
                    if block.error is not None:
                        self._state.recoverable_error_count += 1
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
                        self._state.last_sample.mark(last_t_mono_ns)
                    if self._state.snapshot_due(period_s=self.params.snapshot_period_s):
                        snap = await self.snapshot()
                        self._state.last_snapshot_t_mono_ns = snap.t_mono_ns
                        yield snap
        except* NIDaqError as eg:
            first = next(iter(eg.exceptions))
            raise AdapterError(
                f"nidaq {self.name!r} block stream failed: {first}", device=self.name
            ) from first

    # ------------------------------------------------------------------ commands

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        """NI tasks have no scalar setpoint commands.

        The command surface enforces the auth gate and rejects unknown verbs.
        """
        clock = self._state.clock or RunClock.now()
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
            f"nidaq {self.name!r}: command kind {cmd.kind!r} not supported",
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

    def _probe_device_info(self) -> NIDAQDeviceInfo | None:
        """Best-effort identity lookup by matching the first declared
        ``physical_channel`` against ``nidaqlib.system.discovery.list_devices``.

        Returns ``None`` (and never raises) when ``nidaqmx`` isn't installed,
        the runtime is missing, no NI hardware is present, or the channel
        prefix can't be resolved — manifest identity stays empty in those
        cases, which matches pre-fix behaviour. Tests using ``FakeDaqBackend``
        legitimately land here and should not be perturbed.
        """
        try:
            from nidaqlib.system.discovery import list_devices  # noqa: PLC0415
        except ImportError:
            return None
        try:
            devices = list_devices()
        except Exception:
            # Catch broad — nidaqmx raises a non-``NIDaqError`` ``DaqNotFoundError``
            # when the runtime is missing; the discovery hook does the same.
            return None
        if not devices:
            return None

        first_channel = self.params.channels[0].physical_channel if self.params.channels else ""
        if not first_channel or "/" not in first_channel:
            return None
        module_name = first_channel.split("/", 1)[0]

        by_name = {d.name: d for d in devices}
        module = by_name.get(module_name)
        if module is None:
            return None

        # Chassis name is the module name with the trailing ``ModN`` stripped
        # (cDAQ convention). Single-board cards have no such suffix and
        # therefore no chassis.
        candidate_chassis = _MODULE_SUFFIX_RE.sub("", module_name)
        chassis_name: str | None = (
            candidate_chassis
            if candidate_chassis != module_name and candidate_chassis in by_name
            else None
        )

        serial_raw = getattr(module, "serial_number", None)
        return NIDAQDeviceInfo(
            product_type=getattr(module, "product_type", None),
            serial_number=str(serial_raw) if serial_raw not in (None, "", 0) else None,
            physical_module=module_name,
            chassis=chassis_name,
        )

    def _record_for_reading(self, reading: DaqReading) -> SourceRecord:
        """Convert a :class:`DaqReading` into a wide-row :class:`SourceRecord`.

        Uses :func:`nidaqlib.sinks.reading_to_row` so the row schema matches
        what an offline ``nidaqlib`` recorder would produce — important for
        ``device_records/nidaq_polled.parquet`` parity with sim bundles.
        """
        from nidaqlib.sinks.base import reading_to_row  # noqa: PLC0415

        clock = self._state.clock
        assert clock is not None
        row = reading_to_row(reading)
        t_mono_ns = reading.monotonic_ns - clock.started_mono_ns
        self._state.seq += 1
        adapter_id = self.params.adapter_id()
        return SourceRecord(
            record_id=make_record_id(adapter_id, self.name, self._state.seq),
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
        clock = self._state.clock
        assert clock is not None
        self._state.seq += 1
        t_mono_ns = block.monotonic_ns - clock.started_mono_ns
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
            record_id=make_record_id(ADAPTER_ID_BLOCK, self.name, self._state.seq),
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
        clock = self._state.clock
        assert clock is not None
        rate = block.sample_rate_hz
        if rate is None or rate <= 0:
            return  # Cannot reconstruct timestamps without a rate.
        run_started_utc = clock.started_utc
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
        clock = self._state.clock
        assert clock is not None
        t_mono_ns = reading.monotonic_ns - clock.started_mono_ns
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
        """Return a compact silence-state view for tests and future policy work.

        NI-DAQ's expected period is mode-dependent — polled uses ``1/rate_hz``,
        block uses ``chunk/sample_rate_hz`` — so this can't go through the
        ``rate_hz``-shaped :meth:`AdapterRuntimeState.watchdog` helper.
        """
        return WatchdogState(
            device=self.name,
            last_t_mono_ns=self._state.last_sample.last_t_mono_ns,
            expected_period_ns=self._expected_period_ns(),
            lifecycle_state=self._state.lifecycle.state,
        )

    def _compute_health(self, *, clock: RunClock) -> DeviceHealth:
        """Derive the :class:`DeviceHealth` pill from adapter state.

        Like :meth:`watchdog_state`, this inlines the mode-aware
        ``_expected_period_ns()`` rather than going through
        :meth:`AdapterRuntimeState.compute_health` (which assumes
        ``1/rate_hz``).
        """
        if self._state.lifecycle.state == "closed":
            return "down"
        if self._state.lifecycle.state == "open":
            return "ok"
        if self._state.recoverable_error_count > 0:
            return "degraded"
        age_ns = self._state.last_sample.age_ns(now_t_mono_ns=clock.t_mono_ns())
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
            "state": self._state.lifecycle.state,
            "recoverable_errors": self._state.recoverable_error_count,
            "block_mode": self.params.is_block_mode(),
        }
        if self._task_spec is not None:
            out["physical_channels"] = ",".join(
                ch.physical_channel for ch in self._task_spec.channels
            )
        info = self._device_info
        if info is not None:
            if info.product_type:
                out["product_type"] = info.product_type
            if info.serial_number:
                out["serial_number"] = info.serial_number
            if info.physical_module:
                out["physical_module"] = info.physical_module
            if info.chassis:
                out["chassis"] = info.chassis
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
    for ch in parsed.channels:
        physical = ch.physical_channel
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
    "DESCRIPTOR",
    "NIDAQAdapter",
    "NIDAQAdapterParams",
    "NIDAQDeviceInfo",
    "NIDAQTimingParams",
    "discover",
    "handshake",
]


def _build_descriptor() -> AdapterDescriptor:
    from capa.devices._templates import NIDAQ_THERMOCOUPLE  # noqa: PLC0415
    from capa.devices.adapter import Capability  # noqa: PLC0415
    from capa.devices.registry import AdapterDescriptor  # noqa: PLC0415

    return AdapterDescriptor(
        id="capa.devices.nidaq",
        label="NI-DAQmx chassis",
        family="nidaq",
        adapter_factory=NIDAQAdapter,
        params_model=NIDAQAdapterParams,
        supported_binding_sources=("nidaq_reading_field", "nidaq_block_channel"),
        default_params={"rate_hz": 10.0},
        channel_templates=(NIDAQ_THERMOCOUPLE,),
        discoverable=True,
        handshake_available=True,
        capabilities=frozenset(
            {
                Capability.READS_PROCESS_VAR,
                Capability.SUPPORTS_DISCOVERY,
                Capability.HARDWARE_CLOCKED,
                Capability.EMITS_BLOCKS,
            }
        ),
    )


DESCRIPTOR = _build_descriptor()

from capa.devices.registry import register as _register  # noqa: E402

_register(DESCRIPTOR)
