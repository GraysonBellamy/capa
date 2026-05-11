"""P0-2 acceptance: rate-aware producer queue + ABORT_RUN deadline.

Covers the engine's queue-sizing helper in isolation (no need to spin up a
full task group) and the underlying queue semantics that the helper relies
on. The full engine-shape acceptance — a parked fan-out producing a
``crashed`` run with the bundle sealed — is left to the integration tests
that already exercise the crash-but-sealed path; this file pins the
arithmetic and the policy switch.
"""

from __future__ import annotations

import anyio
import pytest

from capa.core.backpressure import BackpressurePolicy, BoundedQueue
from capa.core.errors import BackpressureAbortError
from capa.experiment.engine import (
    DEFAULT_ADAPTER_EMISSION_RATE_HZ,
    PRODUCER_QUEUE_MAX_CAPACITY,
    PRODUCER_QUEUE_MIN_CAPACITY,
    ExperimentEngine,
)


class _RateAdapter:
    """Bare-minimum adapter stand-in exposing the rate hint."""

    def __init__(self, name: str, rate_hz: float | None) -> None:
        self.name = name
        # Mirror the protocol: a property on the class would be ideal, but
        # tests construct these one-off so a plain attribute is fine — the
        # engine reads via ``getattr`` and treats ``None`` as "unknown".
        self.expected_emission_rate_hz = rate_hz


def _engine_with_adapters(adapters: list[_RateAdapter]) -> ExperimentEngine:
    engine = ExperimentEngine()
    # The capacity helper only reads ``self._adapters`` and ``self._logger``;
    # bypass the full ``run()`` wiring.
    engine._adapters = list(adapters)
    import structlog

    engine._logger = structlog.get_logger("test")
    return engine


def test_capacity_scales_with_aggregate_rate_under_ceiling() -> None:
    # 8 adapters × 50 Hz × (1 + 3 channels) ≈ 1600 Hz aggregate.
    # × abort_after_s=5 × headroom 1.5 = 12000 → within [MIN, MAX].
    adapters = [_RateAdapter(f"d{i}", 200.0) for i in range(8)]
    engine = _engine_with_adapters(adapters)
    cap = engine._compute_producer_queue_capacity(abort_after_s=5.0)
    assert cap == 12_000
    assert PRODUCER_QUEUE_MIN_CAPACITY <= cap <= PRODUCER_QUEUE_MAX_CAPACITY


def test_capacity_clamped_to_max_on_high_rate_rigs() -> None:
    # 10 adapters × 100 Hz × 4 channels = 4000/s each, 40 000/s aggregate.
    # × 5 × 1.5 = 300 000 → clamped to MAX.
    adapters = [_RateAdapter(f"d{i}", 4_000.0) for i in range(10)]
    engine = _engine_with_adapters(adapters)
    cap = engine._compute_producer_queue_capacity(abort_after_s=5.0)
    assert cap == PRODUCER_QUEUE_MAX_CAPACITY


def test_capacity_clamped_to_min_on_idle_rigs() -> None:
    adapters = [_RateAdapter("idle", 1.0)]
    engine = _engine_with_adapters(adapters)
    cap = engine._compute_producer_queue_capacity(abort_after_s=5.0)
    assert cap == PRODUCER_QUEUE_MIN_CAPACITY


def test_missing_rate_hint_falls_back_to_default() -> None:
    # Three adapters, none expose a rate. Aggregate becomes
    # 3 × DEFAULT_ADAPTER_EMISSION_RATE_HZ = 720 Hz.
    # × 5 × 1.5 = 5400 → not clamped.
    adapters = [_RateAdapter(f"d{i}", None) for i in range(3)]
    engine = _engine_with_adapters(adapters)
    cap = engine._compute_producer_queue_capacity(abort_after_s=5.0)
    expected = int(3 * DEFAULT_ADAPTER_EMISSION_RATE_HZ * 5.0 * 1.5)
    assert cap == expected


@pytest.mark.anyio
async def test_abort_run_queue_raises_when_consumer_is_parked() -> None:
    """The engine now constructs the producer queue with ``ABORT_RUN`` so a
    stuck fan-out crashes the run instead of freezing acquisition. Pin the
    underlying queue behavior here — the engine path inherits it."""
    queue: BoundedQueue[int] = BoundedQueue(
        name="test",
        capacity=2,
        policy=BackpressurePolicy.ABORT_RUN,
        abort_after_s=0.05,
    )
    await queue.put(1)
    await queue.put(2)
    # Queue is full; consumer never drains; ABORT_RUN must surface.
    with pytest.raises(BackpressureAbortError):
        with anyio.fail_after(2.0):
            await queue.put(3)
