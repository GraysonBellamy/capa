"""Library-native :class:`SourceRecord` plus the normalized :class:`ChannelSample`.

Plan §5.6: the device libraries already made careful choices about their emitted
row/block shapes. Capa preserves those (``device_records/*.parquet`` in the
bundle) and *also* derives a normalized scientific channel stream
(``scalars.parquet``). The two objects below are what adapters emit per poll.

Both ``t_mono_s`` and ``t_mono_ns`` are populated on :class:`ChannelSample`; the
in-memory float is convenient, ``scalars.parquet`` stores the int64 ns column
as the canonical join key (lossless across hour-long runs). ``t_mono_s`` is
derived ``t_mono_ns / 1e9``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RecordShape = Literal["wide_row", "long_row", "block", "single_value_row"]
"""Shape tag that mirrors the library's natural row/block layout.

* ``wide_row``: alicatlib ``Sample`` (one ``DataFrame`` row per poll), nidaqlib
  polled ``DaqReading`` (one row per read with one column per channel).
* ``long_row``: watlowlib ``Sample`` (one row per ``(device, parameter,
  instance)``).
* ``single_value_row``: sartoriuslib ``Sample`` (one balance reading row).
* ``block``: nidaqlib hardware-clocked ``DaqBlock`` (rectangular
  ``(channels, samples_per_channel)``).
"""


class SourceRecord(BaseModel):
    """Library-native emitted row/block, preserved without reshaping.

    ``row`` is the flattened library-native dict — what
    :func:`alicatlib.sinks.sample_to_row`,
    :func:`watlowlib.sinks.sample_to_row`,
    :func:`sartoriuslib.sinks.sample_to_row`, and
    :func:`nidaqlib.sinks.reading_to_row` produce. For ``shape="block"``,
    ``row`` is empty and ``block_ref`` points at the rectangular sidecar
    (TDMS or in-bundle Parquet block file).

    Plan §5.6.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: str
    """Stable id within a run; referenced by every derived
    :class:`ChannelSample` via :attr:`ChannelSample.source_record_id`."""

    adapter: str
    """``"alicat"``, ``"watlow"``, ``"sartorius"``, ``"nidaq_polled"``,
    ``"nidaq_block"``."""

    device: str
    """Adapter-assigned device name (matches ``ChannelSpec.source.device``)."""

    shape: RecordShape

    t_mono_ns: int
    """Best record-level monotonic timestamp. For Alicat/Watlow/Sartorius this
    is the library's ``midpoint_at`` mapped onto the run's monotonic timebase;
    for NI polled it's the read midpoint; for NI block it's the read-start
    timestamp (per-sample times are reconstructed downstream)."""

    t_utc: datetime
    """Wall-clock timestamp corresponding to ``t_mono_ns``."""

    row: dict[str, float | int | str | bool | None] = Field(default_factory=dict)
    """Flattened library row. Empty when ``shape == "block"``."""

    block_ref: str | None = None
    """Path/handle for the rectangular block sidecar. ``None`` for non-block
    records."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """Free-form context (library version, protocol, status flags). Carries
    fields that don't fit into ``row`` cleanly (e.g. raw bytes, frozenset
    status codes)."""

    @model_validator(mode="after")
    def _check_shape_consistency(self) -> SourceRecord:
        if self.shape == "block":
            if self.row:
                raise ValueError("block records must have empty row")
            if self.block_ref is None:
                raise ValueError("block records must set block_ref")
        else:
            if self.block_ref is not None:
                raise ValueError(f"non-block record (shape={self.shape}) must not set block_ref")
        return self


SampleValue = float | int | bool


class ChannelSample(BaseModel):
    """Normalized scientific channel sample.

    Used by plots, alarms, procedures, sinks (``scalars.parquet``), and
    cross-device analysis. Adapters derive these from :class:`SourceRecord`\\ s
    via the :class:`~capa.channels.spec.SourceBinding` declared on each
    :class:`~capa.channels.spec.ChannelSpec`.

    Plan §5.6.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: str
    """The :attr:`ChannelSpec.name` this sample belongs to."""

    t_mono_ns: int
    """Canonical persisted timebase: int64 nanoseconds since
    :attr:`RunClock.started_mono_ns`."""

    t_mono_s: float
    """In-memory ergonomic timebase: ``t_mono_ns / 1e9``."""

    value: SampleValue
    """Calibrated, dimensioned value (in :attr:`ChannelSpec.derived_unit` or
    :attr:`ChannelSpec.unit` when no derived unit is declared)."""

    raw: float | int | bool | str | None = None
    """Pre-calibration value when :attr:`ChannelSpec.keep_raw` is set."""

    unit: str
    """The channel's output unit (canonicalized)."""

    uncertainty: float | None = None
    """Absolute uncertainty in ``unit``, populated when the channel's
    calibration declares an :class:`UncertaintySpec`."""

    status: str = "ok"
    """Health flag carried alongside the value. ``"ok"`` is the success path;
    adapters use ``"underrange"``, ``"overload"``, ``"sensor_fail"``,
    ``"comm_error"``, etc. as appropriate. The set is open-ended; sinks store
    it verbatim."""

    source_record_id: str | None = None
    """Back-pointer to :attr:`SourceRecord.record_id`; lets analyzers join a
    normalized sample back to the library-native row that produced it."""

    source_field: str | None = None
    """The library-native field this sample was derived from (Alicat
    ``Mass_Flow``, Watlow ``process_value``, etc.)."""

    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Device-level events and snapshots — emitted alongside samples for diagnostics.
# Stored in events.sqlite / status.sqlite respectively.
# ---------------------------------------------------------------------------


class DeviceEvent(BaseModel):
    """Discrete event emitted by an adapter (alarm, communication error,
    state change). Distinct from :class:`AlarmBand` evaluations, which are
    derived from channel samples by the safety layer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    adapter: str
    device: str
    t_mono_ns: int
    t_utc: datetime
    kind: str
    """Adapter-defined: ``"connect"``, ``"disconnect"``, ``"comm_error"``,
    ``"alarm_latch"``, ..."""
    message: str
    severity: Literal["info", "warning", "error"] = "info"
    metadata: dict[str, Any] = Field(default_factory=dict)


DeviceHealth = Literal["ok", "degraded", "down"]
"""Per-adapter health pill, surfaced in the UI status bar (plan §10.4) and
recorded into ``status.sqlite``.

``ok`` is the success path; ``degraded`` covers transient retries / late
samples (auto-reconnect counters > 0 within the snapshot window); ``down``
covers a lost connection / silent producer. Adapters compute the value from
their own state at ``snapshot()`` time.
"""


class DeviceSnapshot(BaseModel):
    """Periodic device-health snapshot (firmware version, connection state,
    bus diagnostics). Routed to ``status.sqlite``; never flows through the
    main fan-out queue."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    adapter: str
    device: str
    t_mono_ns: int
    t_utc: datetime
    healthy: bool
    """Coarse-grained boolean kept for back-compat with older snapshot shapes.
    Newer surfaces consume :attr:`health` instead."""
    health: DeviceHealth = "ok"
    """Tri-state health pill. Defaults to ``"ok"`` so existing snapshots
    constructed without the field round-trip cleanly through the schema."""
    fields: dict[str, float | int | str | bool | None] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# DeviceEmission — what an adapter's poll() yields.
# ---------------------------------------------------------------------------


DeviceEmission = SourceRecord | ChannelSample | DeviceEvent | DeviceSnapshot
"""Tagged union of everything an adapter emits per tick.

Most adapters emit one :class:`SourceRecord` followed by zero or more mapped
:class:`ChannelSample`\\ s. Status/error paths can emit :class:`DeviceEvent` or
:class:`DeviceSnapshot` without channel samples.
"""


__all__ = [
    "ChannelSample",
    "DeviceEmission",
    "DeviceEvent",
    "DeviceHealth",
    "DeviceSnapshot",
    "RecordShape",
    "SampleValue",
    "SourceRecord",
]
